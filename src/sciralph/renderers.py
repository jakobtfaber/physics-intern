"""Renderers: produce Markdown files and agent context from ResearchState.

Snapshot renderers produce full Markdown files (for git snapshots and agent
context).  Per-agent context renderers produce the user-message content that
each agent sees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .markdown import render_frontmatter
from .research_state import (
    CritiqueStatus,
    HypothesisStatus,
    ResearchState,
    RQStatus,
)

if TYPE_CHECKING:
    from .task import Task


# ---------------------------------------------------------------------------
# Snapshot renderers (full Markdown files from state)
# ---------------------------------------------------------------------------

def render_background_survey(state: ResearchState) -> str:
    """Render the background survey section from ResearchState."""
    survey = state.background_survey
    if survey is None:
        return "(No background survey.)"

    parts: list[str] = ["# Background Survey\n"]
    if survey.survey_notes:
        parts.append(survey.survey_notes)
        parts.append("")

    return "\n".join(parts)


def _research_state_body(state: ResearchState) -> str:
    """Build the body text for a research state snapshot rendering."""
    parts: list[str] = []

    # Problem Statement
    parts.append("# Problem Statement\n")
    parts.append(state.problem_statement or "(No problem statement.)")
    parts.append("")

    # Conventions
    parts.append("# Conventions\n")
    parts.append(state.conventions or "(To be populated by the orchestrator as conventions become clear.)")
    parts.append("")

    # Strategy
    parts.append("# Strategy\n")
    parts.append(state.strategy or "(No strategy set.)")
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
            if rq.evidence:
                ev = rq.evidence
                parts.append(f"  Evidence ({ev.type}): {ev.result[:500] if ev.result else 'pending'}")
            parts.append("")
        for rq in sorted(resolved_rqs, key=lambda r: r.id):
            status_tag = f"[{rq.status.upper()}]"
            parts.append(f"## {rq.id} {status_tag} — {rq.question}")
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
        # Evidence summary
        if h.evidence:
            ev = h.evidence
            parts.append(f"**Evidence ({ev.type}):** {ev.method or 'not specified'}")
            if ev.summary:
                parts.append(f"  Summary: {ev.summary}")
            if ev.confidence:
                parts.append(f"  Confidence: {ev.confidence}")
            if ev.result:
                parts.append(f"  Result: {ev.result[:800]}")
            parts.append("")
        # Review status
        if h.review:
            v = h.review
            parts.append(f"**Review:** {v.verdict}")
            if v.summary:
                parts.append(f"  Summary: {v.summary[:1500]}")
            parts.append("")

    # Dead Ends
    has_dead_ends = bool(state.failed_approaches) or any(
        h.status == HypothesisStatus.ABANDONED for h in state.hypotheses.values()
    )
    parts.append("# Dead Ends\n")
    for fa in state.failed_approaches:
        parts.append(f"- {fa.description}")
        if fa.reason:
            parts.append(f"  Reason: {fa.reason}")
        if fa.derivation_excerpt:
            parts.append(f"  Derivation: {fa.derivation_excerpt}")
        if fa.related_entities:
            parts.append(f"  Related entities: {', '.join(fa.related_entities)}")
    # Only render abandoned hypotheses not already covered by failed_approaches
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


def _evidence_log_body(state: ResearchState) -> str:
    """Build the body text for an evidence log rendering."""
    parts: list[str] = ["# Evidence Log\n"]

    # Collect all hypotheses with evidence or verification, sorted by iteration
    entries = []
    for h in state.hypotheses.values():
        if h.evidence:
            entries.append(("evidence", h.evidence.iteration or h.iteration_created, h))
        if h.review:
            entries.append(("verification", h.review.iteration or h.iteration_modified, h))
    # Also check RQs for evidence — deduplicate when RQ was promoted to a WH
    for rq in state.research_questions.values():
        if rq.evidence:
            promoted = any(
                hid in state.hypotheses and state.hypotheses[hid].evidence is not None
                for hid in rq.resolved_to
            )
            if promoted:
                entries.append(("rq_promoted", rq.evidence.iteration or rq.iteration_created, rq))
            else:
                entries.append(("rq_evidence", rq.evidence.iteration or rq.iteration_created, rq))

    entries.sort(key=lambda e: e[1])

    for entry_type, iteration, entity in entries:
        if entry_type == "rq_promoted":
            ev = entity.evidence
            resolved_ids = ", ".join(entity.resolved_to) if entity.resolved_to else "?"
            parts.append(f"## {entity.id}: Evidence ({ev.type}) → promoted\n")
            parts.append(f"**Question:** {entity.question}")
            parts.append(f"**Result:** {ev.result}")
            parts.append(f"*(Full evidence under {resolved_ids}.)*")
            parts.append(f"**Iteration:** {iteration}\n")
        elif entry_type == "rq_evidence":
            ev = entity.evidence
            parts.append(f"## {entity.id}: Evidence ({ev.type})\n")
            parts.append(f"**Question:** {entity.question}")
            parts.append(f"**Method:** {ev.method}")
            if ev.summary:
                parts.append(f"**Summary:** {ev.summary}")
            if ev.approach:
                parts.append(f"**Approach:** {ev.approach[:2000]}")
            parts.append(f"**Result:** {ev.result}")
            if ev.confidence:
                parts.append(f"**Confidence:** {ev.confidence}")
            if ev.scripts:
                parts.append(f"**Scripts:** {', '.join(ev.scripts)}")
            parts.append(f"**Iteration:** {iteration}\n")
        elif entry_type == "evidence":
            ev = entity.evidence
            parts.append(f"## {entity.id}: Evidence ({ev.type})\n")
            parts.append(f"**Statement:** {entity.statement}")
            parts.append(f"**Method:** {ev.method}")
            if ev.summary:
                parts.append(f"**Summary:** {ev.summary}")
            if ev.approach:
                parts.append(f"**Approach:** {ev.approach[:2000]}")
            parts.append(f"**Result:** {ev.result}")
            if ev.confidence:
                parts.append(f"**Confidence:** {ev.confidence}")
            if ev.scripts:
                parts.append(f"**Scripts:** {', '.join(ev.scripts)}")
            if ev.reasoning:
                parts.append(f"**Reasoning:** {ev.reasoning[:2000]}")
            parts.append(f"**Iteration:** {iteration}\n")
        elif entry_type == "verification":
            v = entity.review
            parts.append(f"## {entity.id}: Review — {v.verdict}\n")
            parts.append(f"**Statement:** {entity.statement}")
            if v.summary:
                parts.append(f"**Summary:** {v.summary[:2000]}")
            parts.append(f"**Iteration:** {iteration}\n")

    if not entries:
        parts.append("(No evidence or verification recorded yet.)\n")

    return "\n".join(parts)


def render_evidence_log_md(state: ResearchState) -> str:
    """Render EVIDENCE_LOG.md from ResearchState — evidence and verification on hypotheses."""
    body = _evidence_log_body(state)
    # Count entries for frontmatter
    n_entries = sum(
        1 for h in state.hypotheses.values() if h.evidence
    ) + sum(
        1 for h in state.hypotheses.values() if h.review
    ) + sum(
        1 for rq in state.research_questions.values() if rq.evidence
    )
    meta = {"total_entries": n_entries}
    return render_frontmatter(meta, body)


def _critique_log_body(state: ResearchState) -> str:
    """Build the body text for a critique log rendering."""
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

    meta = {
        "total_critiques": len(state.critiques),
        "unresolved_critiques": len(active),
    }

    body = _critique_log_body(state)
    return render_frontmatter(meta, body)


def render_task_md(task: Task) -> str:
    """Render CURRENT_TASK.md from a Task object."""
    return task.to_markdown()


# ---------------------------------------------------------------------------
# Per-agent context renderers (XML-delimited for dynamic content)
# ---------------------------------------------------------------------------

def render_orchestrator_research_state(state: ResearchState) -> str:
    """Render research state for orchestrator context using XML tags."""
    parts: list[str] = []

    # Conventions
    conv = state.conventions or "(To be populated by the orchestrator as conventions become clear.)"
    parts.append(f"<conventions>\n{conv}\n</conventions>")

    # Strategy
    strat = state.strategy or "(No strategy set. The orchestrator should formulate an initial research strategy based on the background survey.)"
    parts.append(f"<strategy>\n{strat}\n</strategy>")

    # Research Questions
    if state.research_questions:
        open_rqs = sorted(
            [rq for rq in state.research_questions.values() if rq.status == RQStatus.OPEN],
            key=lambda r: r.id,
        )
        resolved_rqs = sorted(
            [rq for rq in state.research_questions.values() if rq.status != RQStatus.OPEN],
            key=lambda r: r.id,
        )
        rq_lines: list[str] = []
        for rq in open_rqs:
            rq_content = [rq.question]
            if rq.context:
                rq_content.append(f"Context: {rq.context}")
            if rq.evidence:
                ev = rq.evidence
                rq_content.append(f"Evidence ({ev.type}): {ev.result[:1000] if ev.result else 'pending'}")
            rq_lines.append(f'<rq id="{rq.id}" status="OPEN">\n' + "\n".join(rq_content) + "\n</rq>")
        for rq in resolved_rqs:
            rq_content = [rq.question]
            if rq.resolved_to:
                rq_content.append(f"Resolved to: {', '.join(rq.resolved_to)}")
            resolution_parts: list[str] = []
            if rq.iteration_resolved is not None:
                resolution_parts.append(f"iteration {rq.iteration_resolved}")
            if rq.resolution_reason:
                resolution_parts.append(rq.resolution_reason)
            if resolution_parts:
                rq_content.append(f"Closed: {' — '.join(resolution_parts)}")
            rq_content.append("This RQ is closed. Do not resolve it again or create a WH from it.")
            status_tag = rq.status.upper()
            rq_lines.append(f'<rq id="{rq.id}" status="{status_tag}">\n' + "\n".join(rq_content) + "\n</rq>")
        parts.append("<research-questions>\n" + "\n".join(rq_lines) + "\n</research-questions>")

    # Hypotheses
    sorted_hyps = sorted(
        state.hypotheses.values(),
        key=lambda h: (0 if h.id.startswith("ER-") else 1, h.id),
    )
    hyp_lines: list[str] = []
    for h in sorted_hyps:
        if h.status == HypothesisStatus.ABANDONED:
            continue
        h_parts: list[str] = []
        if h.statement:
            h_parts.append(f"Statement: {h.statement}")
        if h.depends_on:
            h_parts.append(f"Depends on: {', '.join(h.depends_on)}")
        if h.promotion_justification:
            h_parts.append(f"Promotion justification: {h.promotion_justification}")
        if h.derivation:
            h_parts.append(f"<derivation>\n{h.derivation}\n</derivation>")
        if h.evidence:
            ev = h.evidence
            ev_parts = [f"Method: {ev.method or 'not specified'}"]
            if ev.confidence:
                ev_parts.append(f"Confidence: {ev.confidence}")
            if ev.result:
                ev_parts.append(f"Result: {ev.result[:1500]}")
            h_parts.append(f'<evidence type="{ev.type}">\n' + "\n".join(ev_parts) + "\n</evidence>")
        if h.review:
            v = h.review
            v_parts: list[str] = []
            if v.summary:
                v_parts.append(f"Summary: {v.summary[:1500]}")
            h_parts.append(f'<review verdict="{v.verdict}">\n' + "\n".join(v_parts) + "\n</review>")
        elif h.status == HypothesisStatus.WORKING and h.evidence:
            h_parts.append('<review verdict="PENDING">Not yet reviewed.</review>')
        hyp_lines.append(f'<hypothesis id="{h.id}">\n' + "\n".join(h_parts) + "\n</hypothesis>")
    parts.append("<hypotheses>\n" + "\n".join(hyp_lines) + "\n</hypotheses>")

    # Dead Ends
    has_dead_ends = bool(state.failed_approaches) or any(
        h.status == HypothesisStatus.ABANDONED for h in state.hypotheses.values()
    )
    if has_dead_ends:
        de_parts: list[str] = []
        for fa in state.failed_approaches:
            de_parts.append(f"- {fa.description}")
            if fa.reason:
                de_parts.append(f"  Reason: {fa.reason}")
            if fa.derivation_excerpt:
                de_parts.append(f"  Derivation: {fa.derivation_excerpt}")
            if fa.related_entities:
                de_parts.append(f"  Related entities: {', '.join(fa.related_entities)}")
        fa_descriptions = {fa.description for fa in state.failed_approaches}
        for h in sorted_hyps:
            if h.status == HypothesisStatus.ABANDONED:
                desc = f"Abandoned {h.id} — {h.statement}"
                if desc not in fa_descriptions:
                    de_parts.append(f"- {desc}")
        parts.append("<dead-ends>\n" + "\n".join(de_parts) + "\n</dead-ends>")

    return "\n\n".join(parts)


def render_critic_context(state: ResearchState, iteration: int) -> str:
    """Render strategic context for the deep critic using XML tags.

    Provides a high-level view: strategy, conventions, situation assessment,
    research notes, RQ list, hypothesis summaries (evidence/review one-liners),
    dead ends, background survey, and previous critiques.  No derivations,
    scripts, reasoning, or approach text.
    """
    parts: list[str] = []

    parts.append(f"<iteration>{iteration}</iteration>")

    # Strategy
    strat = state.strategy or "(No strategy set.)"
    parts.append(f"<strategy>\n{strat}\n</strategy>")

    # Conventions
    conv = state.conventions or "(No conventions set.)"
    parts.append(f"<conventions>\n{conv}\n</conventions>")

    # Situation Assessment
    if state.situation_assessment:
        parts.append(f"<situation-assessment>\n{state.situation_assessment}\n</situation-assessment>")

    # Research Notes (last 10)
    if state.research_notes:
        notes = state.research_notes[-10:]
        note_lines = []
        for n in notes:
            it = n.get("iteration", "?")
            text = n.get("text", "")
            note_lines.append(f'<note iteration="{it}">{text}</note>')
        parts.append("<research-notes>\n" + "\n".join(note_lines) + "\n</research-notes>")

    # Research Questions
    if state.research_questions:
        rq_lines: list[str] = []
        for rq in sorted(state.research_questions.values(), key=lambda r: r.id):
            rq_lines.append(f'<rq id="{rq.id}" status="{rq.status.upper()}">{rq.question}</rq>')
        parts.append("<research-questions>\n" + "\n".join(rq_lines) + "\n</research-questions>")

    # Hypotheses (one-liner summaries)
    sorted_hyps = sorted(
        state.hypotheses.values(),
        key=lambda h: (0 if h.id.startswith("ER-") else 1, h.id),
    )
    hyp_lines: list[str] = []
    for h in sorted_hyps:
        if h.status == HypothesisStatus.ABANDONED:
            continue
        h_parts: list[str] = []
        if h.statement:
            h_parts.append(f"Statement: {h.statement}")
        if h.depends_on:
            h_parts.append(f"Depends on: {', '.join(h.depends_on)}")
        if h.promotion_justification:
            h_parts.append(f"Promotion justification: {h.promotion_justification}")
        if h.evidence:
            ev = h.evidence
            result_short = (ev.result[:300] + "...") if ev.result and len(ev.result) > 300 else (ev.result or "")
            h_parts.append(f"Evidence ({ev.type}): {ev.method or 'not specified'}, confidence={ev.confidence or '?'}, Result: {result_short}")
        if h.review:
            v = h.review
            summary_short = (v.summary[:300] + "...") if v.summary and len(v.summary) > 300 else (v.summary or "")
            h_parts.append(f"Review: {v.verdict} — {summary_short}")
        hyp_lines.append(f'<hypothesis id="{h.id}">\n' + "\n".join(h_parts) + "\n</hypothesis>")
    if hyp_lines:
        parts.append("<hypotheses>\n" + "\n".join(hyp_lines) + "\n</hypotheses>")

    # Dead Ends
    has_dead_ends = bool(state.failed_approaches) or any(
        h.status == HypothesisStatus.ABANDONED for h in state.hypotheses.values()
    )
    if has_dead_ends:
        de_parts: list[str] = []
        for fa in state.failed_approaches:
            line = f"- {fa.description}"
            if fa.reason:
                line += f" (Reason: {fa.reason})"
            de_parts.append(line)
        fa_descriptions = {fa.description for fa in state.failed_approaches}
        for h in sorted_hyps:
            if h.status == HypothesisStatus.ABANDONED:
                desc = f"Abandoned {h.id} — {h.statement}"
                if desc not in fa_descriptions:
                    de_parts.append(f"- {desc}")
        parts.append("<dead-ends>\n" + "\n".join(de_parts) + "\n</dead-ends>")

    # Background Survey
    if state.background_survey and state.background_survey.survey_notes:
        parts.append(f"<background-survey>\n{state.background_survey.survey_notes}\n</background-survey>")

    # Previous Critiques (reuse existing XML renderer)
    critique_xml = render_orchestrator_critique_log(state)
    parts.append(f"<previous-critiques>\n{critique_xml}\n</previous-critiques>")

    return "\n\n".join(parts)


def render_formatter_context(state: ResearchState) -> str:
    """Render focused context for the formatter agent using XML tags.

    Includes only what the formatter needs: problem statement, conventions,
    established results (with evidence/review), and a brief warning for any
    remaining open RQs or WHs.
    """
    parts: list[str] = []

    # Problem Statement
    parts.append(f"<problem-statement>\n{state.problem_statement or '(No problem statement.)'}\n</problem-statement>")

    # Conventions
    if state.conventions:
        parts.append(f"<conventions>\n{state.conventions}\n</conventions>")

    # Established Results
    ers = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.ESTABLISHED],
        key=lambda h: h.id,
    )
    if ers:
        er_lines: list[str] = []
        for h in ers:
            h_parts: list[str] = []
            if h.statement:
                h_parts.append(f"Statement: {h.statement}")
            if h.derivation:
                h_parts.append(f"Derivation: {h.derivation}")
            if h.evidence:
                ev = h.evidence
                ev_parts: list[str] = []
                if ev.method:
                    ev_parts.append(f"Method: {ev.method}")
                if ev.result:
                    ev_parts.append(f"Result: {ev.result}")
                if ev.confidence:
                    ev_parts.append(f"Confidence: {ev.confidence}")
                h_parts.append("<evidence>\n" + "\n".join(ev_parts) + "\n</evidence>")
            if h.review:
                h_parts.append(f"Review verdict: {h.review.verdict}")
            er_lines.append(f'<result id="{h.id}">\n' + "\n".join(h_parts) + "\n</result>")
        parts.append("<established-results>\n" + "\n".join(er_lines) + "\n</established-results>")
    else:
        parts.append("<established-results>\n(No established results.)\n</established-results>")

    # Unresolved items warning
    open_rqs = [rq for rq in state.research_questions.values() if rq.status == RQStatus.OPEN]
    working_whs = [h for h in state.hypotheses.values() if h.status == HypothesisStatus.WORKING]
    if open_rqs or working_whs:
        warning_lines: list[str] = []
        for rq in sorted(open_rqs, key=lambda r: r.id):
            warning_lines.append(f"- {rq.id} [OPEN]: {rq.question}")
        for h in sorted(working_whs, key=lambda h: h.id):
            warning_lines.append(f"- {h.id} [WORKING]: {h.statement}")
        parts.append("<unresolved-items>\n" + "\n".join(warning_lines) + "\n</unresolved-items>")

    return "\n\n".join(parts)


def render_orchestrator_critique_log(state: ResearchState) -> str:
    """Render critique log for orchestrator context using XML tags."""
    if not state.critiques:
        return "No critiques filed."

    active = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]
    resolved = [c for c in state.critiques.values() if c.status in (CritiqueStatus.RESOLVED, CritiqueStatus.WITHDRAWN)]

    parts: list[str] = []

    for c in sorted(active, key=lambda c: c.id):
        target_str = ", ".join(c.targets) if c.targets else "general"
        content = c.argument or ""
        parts.append(f'<critique id="{c.id}" severity="{c.severity}" status="UNRESOLVED" target="{target_str}">\n{content}\n</critique>')

    for c in sorted(resolved, key=lambda c: c.id):
        target_str = ", ".join(c.targets) if c.targets else "general"
        content = c.argument or ""
        if c.resolution:
            content += f"\n<resolution>{c.resolution}</resolution>"
        status_tag = c.status.upper()
        parts.append(f'<critique id="{c.id}" severity="{c.severity}" status="{status_tag}" target="{target_str}">\n{content}\n</critique>')

    if state.critic_clean_reviews:
        review_lines: list[str] = []
        for rev in sorted(state.critic_clean_reviews, key=lambda r: r.get("iteration", 0)):
            review_lines.append(f"Iteration {rev.get('iteration', '?')}: {rev.get('summary', '')}")
        parts.append("<clean-reviews>\n" + "\n".join(review_lines) + "\n</clean-reviews>")

    return "\n".join(parts)
