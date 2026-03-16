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
    from .config import Config
    from .task import Task


# ---------------------------------------------------------------------------
# Snapshot renderers (full Markdown files from state)
# ---------------------------------------------------------------------------

def render_research_state_md(state: ResearchState) -> str:
    """Render RESEARCH_STATE.md from ResearchState."""
    meta = {
        "problem_id": "research-session",
        "title": state.title or state.problem_statement[:80],
        "status": state.status,
        "iteration": state.iteration,
    }

    parts: list[str] = []

    # Problem Statement
    parts.append("# Problem Statement\n")
    parts.append(state.problem_statement or "(No problem statement.)")
    parts.append("")

    # Conventions
    parts.append("# Conventions\n")
    parts.append(state.conventions or "(To be populated by the orchestrator as conventions become clear.)")
    parts.append("")

    # Research Questions
    open_rqs = [rq for rq in state.research_questions.values() if rq.status == RQStatus.OPEN]
    resolved_rqs = [rq for rq in state.research_questions.values() if rq.status != RQStatus.OPEN]
    if state.research_questions:
        parts.append("# Research Questions\n")
        for rq in sorted(open_rqs, key=lambda r: r.id):
            parts.append(f"## {rq.id} [OPEN] — {rq.question}")
            if rq.context:
                parts.append(f"  Context: {rq.context}")
            parts.append("")
        for rq in sorted(resolved_rqs, key=lambda r: r.id):
            status_tag = f"[{rq.status.upper()}]"
            parts.append(f"## {rq.id} {status_tag} — {rq.question}")
            if rq.resolved_to:
                parts.append(f"  Resolved to: {', '.join(rq.resolved_to)}")
            parts.append("")

    # Working Hypotheses and Established Results
    parts.append("# Working Hypotheses (WH) and Established Results (ER)\n")
    parts.append("Claims use ## ER-NNN (established, verified) or ## WH-NNN (working hypothesis, pending).")
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
        parts.append(f"## {h.id}{statement_part}\n")
        if h.depends_on:
            parts.append(f"**Depends on:** {', '.join(h.depends_on)}\n")
        if h.promotion_justification:
            parts.append(f"**Promotion justification:** {h.promotion_justification}\n")
        if h.derivation:
            parts.append(h.derivation)
            parts.append("")

    # Dead Ends
    parts.append("# Dead Ends\n")
    for fa in state.failed_approaches:
        parts.append(f"- {fa.description}")
        if fa.reason:
            parts.append(f"  Reason: {fa.reason}")
    # Also include abandoned hypotheses
    for h in sorted_hyps:
        if h.status == HypothesisStatus.ABANDONED:
            parts.append(f"- Abandoned {h.id} — {h.statement}")
    if not state.failed_approaches and not any(
        h.status == HypothesisStatus.ABANDONED for h in state.hypotheses.values()
    ):
        parts.append("(None yet.)")
    parts.append("")

    # Open Questions
    parts.append("# Open Questions\n")
    parts.append(state.open_questions or "(None.)")
    parts.append("")

    body = "\n".join(parts)
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


def render_critique_log_md(state: ResearchState) -> str:
    """Render CRITIQUE_LOG.md from ResearchState."""
    active = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]
    resolved = [c for c in state.critiques.values() if c.status in (CritiqueStatus.RESOLVED, CritiqueStatus.WITHDRAWN)]

    # Compute unresolved counts
    unresolved_high = sum(1 for c in active if c.severity == Severity.HIGH)
    unresolved_medium = sum(1 for c in active if c.severity == Severity.MEDIUM)
    unresolved_low = sum(1 for c in active if c.severity == Severity.LOW)

    meta = {
        "total_critiques": len(state.critiques),
        "unresolved_high": unresolved_high,
        "unresolved_medium": unresolved_medium,
        "unresolved_low": unresolved_low,
    }

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

    body = "\n".join(parts)
    return render_frontmatter(meta, body)


def render_task_md(task: Task) -> str:
    """Render CURRENT_TASK.md from a Task object."""
    return task.to_markdown()


# ---------------------------------------------------------------------------
# Per-agent context renderers
# ---------------------------------------------------------------------------

def render_orchestrator_context(
    state: ResearchState,
    *,
    context_prefix: str = "",
    metrics_text: str = "",
    config: Config,
    iteration: int,
    proposed_changes: str = "",
    comp_log_tail: str = "",
) -> str:
    """Render orchestrator user-message context from ResearchState.

    Structurally equivalent to OrchestratorAgent.build_context().
    """
    parts: list[str] = []

    if context_prefix:
        parts.append(context_prefix)

    budget_remaining = config.max_iterations - iteration
    parts.append(
        f"# Current Iteration: {iteration} of {config.max_iterations} "
        f"({budget_remaining} remaining)\n"
    )

    parts.append("## RESEARCH_STATE.md\n")
    parts.append(render_research_state_md(state))

    parts.append("\n## CRITIQUE_LOG.md\n")
    parts.append(render_critique_log_md(state))

    parts.append(f"\n## COMPUTATION_LOG.md (last {config.orchestrator_comp_log_tail} entries)\n")
    if comp_log_tail:
        parts.append(comp_log_tail)
    else:
        # Render last N computation entries
        comps = sorted(state.computations.values(), key=lambda c: (c.iteration, c.id))
        tail = comps[-config.orchestrator_comp_log_tail:] if comps else []
        for c in tail:
            if c.kind == "explore":
                parts.append(f"## {c.id}: Exploration\n")
                parts.append(f"**TARGET:** {c.target_hypothesis}")
                parts.append(f"**DESCRIPTION:** {c.claim}")
                parts.append(f"**RESULT:** {c.result}\n")
                parts.append(f"**CONFIDENCE:** {c.confidence}")
            else:
                claim_prefix = f"{c.target_hypothesis} — " if c.target_hypothesis else ""
                parts.append(f"## {c.id}: Computation\n")
                parts.append(f"**CLAIM:** {claim_prefix}{c.claim}")
                parts.append(f"**VERDICT:** {c.verdict}")
            parts.append(f"- **Iteration:** {c.iteration}\n")

    parts.append("\n## METRICS.md (summary)\n")
    parts.append(metrics_text)

    if proposed_changes:
        parts.append("\n## PROPOSED_CHANGES.md (pending review)\n")
        parts.append(proposed_changes)

    return "\n".join(parts)


def render_researcher_context(state: ResearchState, task: Task) -> str:
    """Render researcher user-message context from ResearchState.

    Structurally equivalent to ResearcherAgent.build_context().
    """
    from .task import TaskType

    parts = [
        "## CURRENT_TASK.md\n",
        task.to_markdown(),
        "\n## RESEARCH_STATE.md\n",
        render_research_state_md(state),
    ]

    if task.blocking_critiques:
        parts.append("\n## Relevant Critiques\n")
        for crit_id in task.blocking_critiques:
            if crit_id in state.critiques:
                c = state.critiques[crit_id]
                sev_tag = f"[{c.severity}]"
                parts.append(f"## {c.id} {sev_tag}\n")
                targets_str = ", ".join(c.targets) if c.targets else "general"
                parts.append(f"**Target:** {targets_str}\n")
                if c.argument:
                    parts.append(c.argument)
                parts.append("")

    return "\n".join(parts)


def render_computationalist_context(state: ResearchState, task: Task) -> str:
    """Render computationalist user-message context from ResearchState.

    Structurally equivalent to ComputationalistAgent.build_context().
    """
    parts = [
        "## CURRENT_TASK.md\n",
        task.to_markdown(),
        "\n## Relevant Research State (excerpts)\n",
        render_research_state_md(state),
    ]
    return "\n".join(parts)


def render_critic_context(state: ResearchState) -> str:
    """Render critic user-message context from ResearchState.

    Structurally equivalent to CriticAgent.build_context().
    """
    parts = [
        "## RESEARCH_STATE.md\n",
        render_research_state_md(state),
        "\n## COMPUTATION_LOG.md\n",
        render_computation_log_md(state),
        "\n## Your Previous Critiques (do not repeat)\n",
        render_critique_log_md(state),
    ]
    return "\n".join(parts)
