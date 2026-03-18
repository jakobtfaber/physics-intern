"""Renderers: produce Markdown files and agent context from ResearchState.

Snapshot renderers produce full Markdown files (for git snapshots and agent
context).  Per-agent context renderers produce the user-message content that
each agent sees, structurally equivalent to what the old file-reading
build_context() methods produced.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .markdown import render_frontmatter
from .research_state import (
    CritiqueStatus,
    HypothesisStatus,
    ResearchState,
    RQStatus,
    Severity,
    Verdict,
)

if TYPE_CHECKING:
    from .task import Task


# ---------------------------------------------------------------------------
# Snapshot renderers (full Markdown files from state)
# ---------------------------------------------------------------------------

def _h(level: int, offset: int) -> str:
    """Return a Markdown heading prefix at the given level + offset."""
    return "#" * (level + offset)


def render_background_survey(state: ResearchState, *, heading_offset: int = 0) -> str:
    """Render the background survey section from ResearchState."""
    survey = state.background_survey
    if survey is None:
        return "(No background survey.)"

    h1 = _h(1, heading_offset)

    parts: list[str] = [f"{h1} Background Survey\n"]
    if survey.survey_notes:
        parts.append(survey.survey_notes)
        parts.append("")

    return "\n".join(parts)


def _research_state_body(
    state: ResearchState,
    *,
    include_problem_statement: bool = True,
    skip_empty_dead_ends: bool = False,
    include_background_survey: bool = False,
    include_computation_history: bool = False,
    heading_offset: int = 0,
) -> str:
    """Build the body text for a research state rendering.

    Shared by the snapshot renderer and orchestrator context renderer.
    *heading_offset* shifts all heading levels (0 = H1/H2, 2 = H3/H4).
    """
    h1 = _h(1, heading_offset)
    h2 = _h(2, heading_offset)
    parts: list[str] = []

    # Problem Statement
    if include_problem_statement:
        parts.append(f"{h1} Problem Statement\n")
        parts.append(state.problem_statement or "(No problem statement.)")
        parts.append("")

    # Background Survey (when requested and present)
    if include_background_survey and state.background_survey is not None:
        parts.append(render_background_survey(state, heading_offset=heading_offset))
        parts.append("")

    # Conventions
    parts.append(f"{h1} Conventions\n")
    parts.append(state.conventions or "(To be populated by the orchestrator as conventions become clear.)")
    parts.append("")

    # Strategy
    parts.append(f"{h1} Strategy\n")
    parts.append(state.strategy or "(No strategy set. The orchestrator should formulate an initial research strategy based on the background survey.)")
    parts.append("")

    # Research Questions
    open_rqs = [rq for rq in state.research_questions.values() if rq.status == RQStatus.OPEN]
    resolved_rqs = [rq for rq in state.research_questions.values() if rq.status != RQStatus.OPEN]
    if state.research_questions:
        parts.append(f"{h1} Research Questions\n")
        for rq in sorted(open_rqs, key=lambda r: r.id):
            parts.append(f"{h2} {rq.id} [OPEN] — {rq.question}")
            if rq.context:
                parts.append(f"  Context: {rq.context}")
            parts.append("")
        for rq in sorted(resolved_rqs, key=lambda r: r.id):
            status_tag = f"[{rq.status.upper()}]"
            parts.append(f"{h2} {rq.id} {status_tag} — {rq.question}")
            if rq.resolved_to:
                parts.append(f"  Resolved to: {', '.join(rq.resolved_to)}")
            resolution_parts: list[str] = []
            if rq.iteration_resolved is not None:
                resolution_parts.append(f"iteration {rq.iteration_resolved}")
            if rq.resolution_reason:
                resolution_parts.append(rq.resolution_reason)
            if resolution_parts:
                parts.append(f"  Closed: {' — '.join(resolution_parts)}")
            parts.append("  **This RQ is closed. Do not resolve it again or create a WH from it.**")
            parts.append("")

    # Working Hypotheses and Established Results
    parts.append(f"{h1} Working Hypotheses (WH) and Established Results (ER)\n")
    parts.append(f"Claims use {h2} ER-NNN (established, verified) or {h2} WH-NNN (working hypothesis, pending).")
    parts.append("")

    # Sort hypotheses: ER first (by number), then WH (by number)
    sorted_hyps = sorted(
        state.hypotheses.values(),
        key=lambda h: (0 if h.id.startswith("ER-") else 1, h.id),
    )
    for h in sorted_hyps:
        if h.status == HypothesisStatus.ABANDONED:
            continue  # abandoned go in Dead Ends
        statement_part = f" — {h.statement}" if h.statement else ""
        parts.append(f"{h2} {h.id}{statement_part}\n")
        if h.depends_on:
            parts.append(f"**Depends on:** {', '.join(h.depends_on)}\n")
        if h.promotion_justification:
            parts.append(f"**Promotion justification:** {h.promotion_justification}\n")
        if h.derivation:
            parts.append(h.derivation)
            parts.append("")
        if include_computation_history:
            h_comps = sorted(
                [c for c in state.computations.values()
                 if c.target_hypothesis == h.id and not c.zero_output],
                key=lambda c: (c.iteration, c.id),
            )
            if h_comps:
                summary = " → ".join(
                    f"{c.id} ({c.kind}, {c.verdict.value})"
                    for c in h_comps
                )
                parts.append(f"**Computation history:** {summary}\n")

    # Dead Ends
    has_dead_ends = bool(state.failed_approaches) or any(
        h.status == HypothesisStatus.ABANDONED for h in state.hypotheses.values()
    )
    if not (skip_empty_dead_ends and not has_dead_ends):
        parts.append(f"{h1} Dead Ends\n")
        for fa in state.failed_approaches:
            parts.append(f"- {fa.description}")
            if fa.reason:
                parts.append(f"  Reason: {fa.reason}")
            if fa.derivation_excerpt:
                parts.append(f"  Derivation: {fa.derivation_excerpt}")
            if fa.related_comps:
                parts.append(f"  Related computations: {', '.join(fa.related_comps)}")
        # Only render abandoned hypotheses not already covered by failed_approaches
        # (abandon_hypothesis tool adds to both, so skip those to avoid duplicates)
        fa_descriptions = {fa.description for fa in state.failed_approaches}
        for h in sorted_hyps:
            if h.status == HypothesisStatus.ABANDONED:
                desc = f"Abandoned {h.id} — {h.statement}"
                if desc not in fa_descriptions:
                    parts.append(f"- {desc}")
        if not has_dead_ends:
            parts.append("(None yet.)")
        parts.append("")

    return "\n".join(parts)


def render_research_state_md(state: ResearchState) -> str:
    """Render RESEARCH_STATE.md from ResearchState."""
    er_ids = sorted(
        h.id for h in state.hypotheses.values()
        if h.id.startswith("ER-")
    )
    meta: dict = {
        "problem_id": "research-session",
        "title": state.title or state.problem_statement[:80],
        "status": state.status,
        "iteration": state.iteration,
    }
    if er_ids:
        meta["verified_results"] = er_ids

    body = _research_state_body(state)
    return render_frontmatter(meta, body)


def render_computation_log_md(state: ResearchState) -> str:
    """Render COMPUTATION_LOG.md from ResearchState."""
    comps = sorted(state.computations.values(), key=lambda c: (c.iteration, c.id))
    meta = {
        "total_computations": len(comps),
    }

    parts: list[str] = ["# Computations\n"]

    for c in comps:
        if c.zero_output:
            parts.append(f"## {c.id}: FAILED (no result produced, iteration {c.iteration})\n")
            continue
        if c.kind == "explore":
            parts.append(f"## {c.id}: Exploration\n")
            parts.append(f"**TARGET:** {c.target_hypothesis}")
            parts.append(f"**DESCRIPTION:** {c.claim}")
            parts.append(f"**METHOD:** {c.method}")
            parts.append(f"**RESULT:** {c.result}\n")
            parts.append(f"**CONFIDENCE:** {c.confidence}")
            if c.notes:
                parts.append(f"**NOTES:** {c.notes}")
        else:
            parts.append(f"## {c.id}: Computation\n")
            claim_prefix = f"{c.target_hypothesis} — " if c.target_hypothesis else ""
            parts.append(f"**CLAIM:** {claim_prefix}{c.claim}")
            parts.append(f"**METHOD:** {c.method}")
            parts.append(f"**RESULT:** {c.result}\n")
            parts.append(f"**VERDICT:** {c.verdict}")
            if c.notes:
                parts.append(f"**NOTES:** {c.notes}")
            elif c.failure_detail:
                parts.append(f"**NOTES:** {c.failure_detail}")

        parts.append(f"\n- **Iteration:** {c.iteration}")
        parts.append("")

    body = "\n".join(parts)
    return render_frontmatter(meta, body)


def _critique_log_body(state: ResearchState) -> str:
    """Build the body text for a critique log rendering.

    Shared by the snapshot renderer and orchestrator context renderer.
    """
    active = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]
    resolved = [c for c in state.critiques.values() if c.status in (CritiqueStatus.RESOLVED, CritiqueStatus.WITHDRAWN)]

    parts: list[str] = ["# Active Critiques\n"]

    for c in sorted(active, key=lambda c: c.id):
        sev_tag = f"[{c.severity}]"
        parts.append(f"## {c.id} {sev_tag} [UNRESOLVED]\n")
        targets_str = ", ".join(c.targets) if c.targets else "general"
        parts.append(f"**Target:** {targets_str}\n")
        if c.argument:
            parts.append(c.argument)
        parts.append("")

    parts.append("# Resolved Critiques\n")

    for c in sorted(resolved, key=lambda c: c.id):
        sev_tag = f"[{c.severity}]"
        status_tag = f"[{c.status.upper()}]"
        parts.append(f"## {c.id} {sev_tag} {status_tag}\n")
        targets_str = ", ".join(c.targets) if c.targets else "general"
        parts.append(f"**Target:** {targets_str}\n")
        if c.argument:
            parts.append(c.argument)
        if c.resolution:
            parts.append(f"- **Resolution:** {c.resolution}")
        parts.append("")

    if state.critic_clean_reviews:
        parts.append("# Clean Reviews\n")
        for rev in sorted(state.critic_clean_reviews, key=lambda r: r.get("iteration", 0)):
            parts.append(f"**Iteration {rev.get('iteration', '?')}:** {rev.get('summary', '')}\n")

    return "\n".join(parts)


def render_critique_log_md(state: ResearchState) -> str:
    """Render CRITIQUE_LOG.md from ResearchState."""
    active = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]

    unresolved_high = sum(1 for c in active if c.severity == Severity.HIGH)
    unresolved_medium = sum(1 for c in active if c.severity == Severity.MEDIUM)
    unresolved_low = sum(1 for c in active if c.severity == Severity.LOW)

    meta = {
        "total_critiques": len(state.critiques),
        "unresolved_high": unresolved_high,
        "unresolved_medium": unresolved_medium,
        "unresolved_low": unresolved_low,
    }

    body = _critique_log_body(state)
    return render_frontmatter(meta, body)


def render_computation_log_tail(state: ResearchState, n: int) -> str:
    """Render the last *n* computation entries from ResearchState."""
    comps = sorted(state.computations.values(), key=lambda c: (c.iteration, c.id))
    tail = comps[-n:] if comps else []
    parts: list[str] = []
    for c in tail:
        if c.zero_output:
            parts.append(f"## {c.id}: FAILED (no result produced, iteration {c.iteration})\n")
            continue
        if c.kind == "explore":
            parts.append(f"## {c.id}: Exploration\n")
            parts.append(f"**TARGET:** {c.target_hypothesis}")
            parts.append(f"**DESCRIPTION:** {c.claim}")
            parts.append(f"**METHOD:** {c.method}")
            parts.append(f"**RESULT:** {c.result}\n")
            parts.append(f"**CONFIDENCE:** {c.confidence}")
            if c.notes:
                parts.append(f"**NOTES:** {c.notes}")
        else:
            parts.append(f"## {c.id}: Computation\n")
            claim_prefix = f"{c.target_hypothesis} — " if c.target_hypothesis else ""
            parts.append(f"**CLAIM:** {claim_prefix}{c.claim}")
            parts.append(f"**METHOD:** {c.method}")
            parts.append(f"**RESULT:** {c.result}\n")
            parts.append(f"**VERDICT:** {c.verdict}")
            if c.notes:
                parts.append(f"**NOTES:** {c.notes}")
            elif c.failure_detail:
                parts.append(f"**NOTES:** {c.failure_detail}")
        parts.append(f"\n- **Iteration:** {c.iteration}")
        parts.append("")
    return "\n".join(parts)


def render_task_md(task: Task) -> str:
    """Render CURRENT_TASK.md from a Task object."""
    return task.to_markdown()


# ---------------------------------------------------------------------------
# Per-agent context renderers
# ---------------------------------------------------------------------------

def render_orchestrator_research_state(state: ResearchState) -> str:
    """Render research state for orchestrator context (no frontmatter, no problem statement)."""
    return _research_state_body(
        state,
        include_problem_statement=False,
        skip_empty_dead_ends=True,
        include_background_survey=True,
        include_computation_history=True,
        heading_offset=2,
    )


def render_compute_research_state(state: ResearchState) -> str:
    """Render research state for compute agent context (no frontmatter, skips empties)."""
    return _research_state_body(
        state,
        skip_empty_dead_ends=True,
        include_background_survey=True,
        heading_offset=2,
    )


def render_orchestrator_critique_log(state: ResearchState) -> str:
    """Render critique log for orchestrator context (no frontmatter, compact when empty)."""
    if not state.critiques:
        return "No critiques filed."
    return _critique_log_body(state)

