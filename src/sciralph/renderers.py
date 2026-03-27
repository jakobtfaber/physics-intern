"""Renderers: produce Markdown files and agent context from ResearchState.

Snapshot renderers produce full Markdown files (for git snapshots and agent
context).  Per-agent context renderers produce the user-message content that
each agent sees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .markdown import render_frontmatter
from .research_state import (
    BackgroundSurvey,
    CritiqueStatus,
    Hypothesis,
    HypothesisStatus,
    ResearchState,
    RQStatus,
    Severity,
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
    if survey.has_structured_sections:
        for label, field_name in [
            ("Background", "background"),
            ("Key Insights", "key_insights"),
            ("Known Methods and Techniques", "known_methods"),
            ("Known Pitfalls", "known_pitfalls"),
            ("Conventions and Definitions", "conventions_and_definitions"),
            ("Sanity Checks", "sanity_checks"),
        ]:
            content = getattr(survey, field_name, "")
            if content:
                parts.append(f"### {label}\n\n{content}\n")
    elif survey.raw_notes:
        parts.append(survey.raw_notes)
        parts.append("")

    return "\n".join(parts)


def render_survey_sections_text(survey: BackgroundSurvey) -> str:
    """Render survey as plain text sections for embedding in XML contexts.

    Uses structured fields when available, otherwise falls back to raw_notes.
    """
    if survey.has_structured_sections:
        parts: list[str] = []
        for label, field_name in [
            ("Background", "background"),
            ("Key Insights", "key_insights"),
            ("Known Methods and Techniques", "known_methods"),
            ("Known Pitfalls", "known_pitfalls"),
            ("Conventions and Definitions", "conventions_and_definitions"),
            ("Sanity Checks", "sanity_checks"),
        ]:
            content = getattr(survey, field_name, "")
            if content:
                parts.append(f"## {label}\n\n{content}")
        return "\n\n".join(parts)
    return survey.raw_notes or ""


def render_survey_sections_xml(survey: BackgroundSurvey) -> str:
    """Render survey as individual XML tags for orchestrator context.

    Skips conventions_and_definitions (rendered separately in <conventions>).
    """
    if not survey.has_structured_sections:
        return f"<survey-background>\n{survey.raw_notes}\n</survey-background>" if survey.raw_notes else ""
    tag_map = [
        ("survey-background", "background"),
        ("survey-key-insights", "key_insights"),
        ("survey-known-methods", "known_methods"),
        ("survey-known-pitfalls", "known_pitfalls"),
        ("survey-sanity-checks", "sanity_checks"),
    ]
    parts: list[str] = []
    for tag, field_name in tag_map:
        content = getattr(survey, field_name, "")
        if content:
            parts.append(f"<{tag}>\n{content}\n</{tag}>")
    return "\n\n".join(parts)


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
                for idx, ev in enumerate(rq.evidence):
                    prefix = f"  Evidence {idx + 1}/{len(rq.evidence)} " if len(rq.evidence) > 1 else "  Evidence "
                    parts.append(f"{prefix}({ev.type}): {ev.result[:500] if ev.result else 'pending'}")
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

    # Established Results
    parts.append("# Established Results (ER)\n")
    ers = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.ESTABLISHED],
        key=lambda h: h.id,
    )
    for h in ers:
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
        if h.evidence:
            for idx, ev in enumerate(h.evidence):
                prefix = f"**Evidence {idx + 1}/{len(h.evidence)} " if len(h.evidence) > 1 else "**Evidence "
                parts.append(f"{prefix}({ev.type}):** {ev.method or 'not specified'}")
                if ev.summary:
                    parts.append(f"  Summary: {ev.summary}")
                if ev.confidence:
                    parts.append(f"  Confidence: {ev.confidence}")
                if ev.result:
                    parts.append(f"  Result: {ev.result[:800]}")
                parts.append("")
        if h.review:
            v = h.review
            parts.append(f"**Review:** {v.verdict}")
            if v.summary:
                parts.append(f"  Summary: {v.summary}")
            if v.details:
                parts.append(f"  Details: {v.details}")
            parts.append("")

    # Working Hypotheses
    parts.append("# Working Hypotheses (WH)\n")
    whs = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.WORKING],
        key=lambda h: h.id,
    )
    for h in whs:
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
            for idx, ev in enumerate(h.evidence):
                prefix = f"**Evidence {idx + 1}/{len(h.evidence)} " if len(h.evidence) > 1 else "**Evidence "
                parts.append(f"{prefix}({ev.type}):** {ev.method or 'not specified'}")
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
                parts.append(f"  Summary: {v.summary}")
            if v.details:
                parts.append(f"  Details: {v.details}")
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
    for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
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
            for ev in h.evidence:
                entries.append(("evidence", ev.iteration or h.iteration_created, h, ev))
        if h.review:
            entries.append(("verification", h.review.iteration or h.iteration_modified, h, None))
    # Also check RQs for evidence — deduplicate when RQ was promoted to a WH
    for rq in state.research_questions.values():
        if rq.evidence:
            promoted = any(
                hid in state.hypotheses and len(state.hypotheses[hid].evidence) > 0
                for hid in rq.resolved_to
            )
            for ev in rq.evidence:
                if promoted:
                    entries.append(("rq_promoted", ev.iteration or rq.iteration_created, rq, ev))
                else:
                    entries.append(("rq_evidence", ev.iteration or rq.iteration_created, rq, ev))

    entries.sort(key=lambda e: e[1])

    for entry_type, iteration, entity, ev in entries:
        if entry_type == "rq_promoted":
            resolved_ids = ", ".join(entity.resolved_to) if entity.resolved_to else "?"
            ev_label = f" {ev.id}" if ev.id else ""
            parts.append(f"## {entity.id}:{ev_label} Evidence ({ev.type}) → promoted\n")
            parts.append(f"**Question:** {entity.question}")
            parts.append(f"**Result:** {ev.result}")
            parts.append(f"*(Full evidence under {resolved_ids}.)*")
            parts.append(f"**Iteration:** {iteration}\n")
        elif entry_type == "rq_evidence":
            ev_label = f" {ev.id}" if ev.id else ""
            parts.append(f"## {entity.id}:{ev_label} Evidence ({ev.type})\n")
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
            ev_label = f" {ev.id}" if ev.id else ""
            parts.append(f"## {entity.id}:{ev_label} Evidence ({ev.type})\n")
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
            if ev.derivation_file:
                parts.append(f"**Derivation file:** `derivations/{ev.derivation_file}`")
            if ev.reasoning:
                parts.append(f"**Reasoning:** {ev.reasoning[:2000]}")
            parts.append(f"**Iteration:** {iteration}\n")
        elif entry_type == "verification":
            v = entity.review
            parts.append(f"## {entity.id}: Review — {v.verdict}\n")
            parts.append(f"**Statement:** {entity.statement}")
            if v.summary:
                parts.append(f"**Summary:** {v.summary}")
            if v.details:
                parts.append(f"**Details:** {v.details}")
            parts.append(f"**Iteration:** {iteration}\n")

    if not entries:
        parts.append("(No evidence or verification recorded yet.)\n")

    return "\n".join(parts)


def render_evidence_log_md(state: ResearchState) -> str:
    """Render EVIDENCE_LOG.md from ResearchState — evidence and verification on hypotheses."""
    body = _evidence_log_body(state)
    # Count entries for frontmatter
    n_entries = sum(
        len(h.evidence) for h in state.hypotheses.values()
    ) + sum(
        1 for h in state.hypotheses.values() if h.review
    ) + sum(
        len(rq.evidence) for rq in state.research_questions.values()
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

def _render_hypothesis_parts(h: Hypothesis) -> list[str]:
    """Render the inner XML parts of a hypothesis/result entry."""
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
        for ev in h.evidence:
            ev_parts = [f"Method: {ev.method or 'not specified'}"]
            if ev.confidence:
                ev_parts.append(f"Confidence: {ev.confidence}")
            if ev.result:
                ev_parts.append(f"Result: {ev.result[:1500]}")
            ev_id_attr = f' id="{ev.id}"' if ev.id else ""
            h_parts.append(f'<evidence{ev_id_attr} type="{ev.type}">\n' + "\n".join(ev_parts) + "\n</evidence>")
    if h.review:
        v = h.review
        v_parts: list[str] = []
        if v.summary:
            v_parts.append(f"Summary: {v.summary[:1500]}")
        h_parts.append(f'<review verdict="{v.verdict}">\n' + "\n".join(v_parts) + "\n</review>")
    return h_parts


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
                for ev in rq.evidence:
                    ev_label = f"{ev.id} " if ev.id else ""
                    rq_content.append(f"Evidence {ev_label}({ev.type}): {ev.result[:1000] if ev.result else 'pending'}")
            rq_lines.append(f'<rq id="{rq.id}" status="OPEN">\n' + "\n".join(rq_content) + "\n</rq>")
        for rq in resolved_rqs:
            if rq.resolved_to:
                body = f"→ {', '.join(rq.resolved_to)}"
            elif rq.resolution_reason:
                body = f"Closed: {rq.resolution_reason[:80]}"
            else:
                body = "Closed"
            status_tag = rq.status.upper()
            rq_lines.append(f'<rq id="{rq.id}" status="{status_tag}">{body}</rq>')
        parts.append("<research-questions>\n" + "\n".join(rq_lines) + "\n</research-questions>")

    # Established Results
    ers = sorted(
        [h for h in state.hypotheses.values()
         if h.status == HypothesisStatus.ESTABLISHED and not h.status == HypothesisStatus.ABANDONED],
        key=lambda h: h.id,
    )
    if ers:
        er_lines: list[str] = []
        for h in ers:
            h_parts = _render_hypothesis_parts(h)
            er_lines.append(f'<result id="{h.id}">\n' + "\n".join(h_parts) + "\n</result>")
        parts.append("<established-results>\n" + "\n".join(er_lines) + "\n</established-results>")

    # Working Hypotheses
    whs = sorted(
        [h for h in state.hypotheses.values()
         if h.status == HypothesisStatus.WORKING],
        key=lambda h: h.id,
    )
    hyp_lines: list[str] = []
    for h in whs:
        h_parts = _render_hypothesis_parts(h)
        if not h.review and h.evidence:
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
        for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
            if h.status == HypothesisStatus.ABANDONED:
                desc = f"Abandoned {h.id} — {h.statement}"
                if desc not in fa_descriptions:
                    de_parts.append(f"- {desc}")
        parts.append("<dead-ends>\n" + "\n".join(de_parts) + "\n</dead-ends>")

    return "\n\n".join(parts)


def render_orchestrator_slim_state(state: ResearchState) -> str:
    """Render compact one-liner research state for the orchestrator.

    Target ~8K tokens.  Keeps conventions and strategy in full; renders
    entities as one-liners (no derivations, evidence details, or review text).
    """
    parts: list[str] = []

    # Conventions (full)
    conv = state.conventions or "(To be populated by the orchestrator as conventions become clear.)"
    parts.append(f"<conventions>\n{conv}\n</conventions>")

    # Strategy (full)
    strat = state.strategy or "(No strategy set.)"
    parts.append(f"<strategy>\n{strat}\n</strategy>")

    # Established Results — one-liner per ER
    ers = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.ESTABLISHED],
        key=lambda h: h.id,
    )
    if ers:
        er_lines = [f"- {h.id}: {h.statement}, VERIFIED" for h in ers]
        parts.append("<established-results>\n" + "\n".join(er_lines) + "\n</established-results>")

    # Working Hypotheses — one-liner per WH
    whs = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.WORKING],
        key=lambda h: h.id,
    )
    if whs:
        wh_lines: list[str] = []
        for h in whs:
            if h.review:
                verdict = h.review.verdict
                wh_lines.append(f"- {h.id}: {h.statement}, {verdict}")
            elif h.evidence:
                wh_lines.append(f"- {h.id}: {h.statement}, PENDING REVIEW")
            else:
                wh_lines.append(f"- {h.id}: {h.statement}, no evidence yet")
        parts.append("<hypotheses>\n" + "\n".join(wh_lines) + "\n</hypotheses>")

    # Research Questions — one-liner per RQ
    if state.research_questions:
        rq_lines: list[str] = []
        for rq in sorted(state.research_questions.values(), key=lambda r: r.id):
            if rq.status == RQStatus.OPEN:
                n_ev = len(rq.evidence)
                ev_note = f", {n_ev} evidence item{'s' if n_ev != 1 else ''}" if n_ev else ""
                rq_lines.append(f"- {rq.id}: {rq.question}, OPEN{ev_note}")
            elif rq.resolved_to:
                rq_lines.append(f"- {rq.id}: {rq.question}, RESOLVED → {', '.join(rq.resolved_to)}")
            else:
                rq_lines.append(f"- {rq.id}: {rq.question}, {rq.status.upper()}")
        parts.append("<research-questions>\n" + "\n".join(rq_lines) + "\n</research-questions>")

    # Dead Ends — one-liner per entry
    dead_lines: list[str] = []
    for fa in state.failed_approaches:
        reason = f" ({fa.reason})" if fa.reason else ""
        dead_lines.append(f"- {fa.description}{reason}")
    fa_descriptions = {fa.description for fa in state.failed_approaches}
    for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
        if h.status == HypothesisStatus.ABANDONED:
            desc = f"Abandoned {h.id} — {h.statement}"
            if desc not in fa_descriptions:
                dead_lines.append(f"- {desc}")
    if dead_lines:
        parts.append("<dead-ends>\n" + "\n".join(dead_lines) + "\n</dead-ends>")

    return "\n\n".join(parts)


def render_critic_context(state: ResearchState, iteration: int) -> str:
    """Render strategic context for the deep critic using XML tags.

    Provides: problem statement, strategy, conventions, research notes,
    RQ list, hypothesis summaries (evidence/review one-liners), dead ends,
    background survey, and previous critiques.  No derivations, scripts,
    reasoning, or approach text.
    """
    parts: list[str] = []

    parts.append(f"<iteration>{iteration}</iteration>")

    # Problem Statement
    parts.append(f"<problem-statement>\n{state.problem_statement or '(No problem statement.)'}\n</problem-statement>")

    # Strategy
    strat = state.strategy or "(No strategy set.)"
    parts.append(f"<strategy>\n{strat}\n</strategy>")

    # Conventions
    conv = state.conventions or "(No conventions set.)"
    parts.append(f"<conventions>\n{conv}\n</conventions>")

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

    # Compute last critic iteration from clean reviews and filed critiques
    last_critic_iter = 0
    if state.critic_clean_reviews:
        last_critic_iter = max(r.get("iteration", 0) for r in state.critic_clean_reviews)
    for c in state.critiques.values():
        if c.iteration_filed > last_critic_iter:
            last_critic_iter = c.iteration_filed

    # Collect hypothesis IDs targeted by critiques (used for survived-critic check)
    critic_targets_since: dict[str, int] = {}  # hid -> latest critique iteration targeting it
    for c in state.critiques.values():
        for t in c.targets:
            if c.iteration_filed > critic_targets_since.get(t, 0):
                critic_targets_since[t] = c.iteration_filed

    def _critic_hyp_parts(h: Hypothesis) -> list[str]:
        h_parts: list[str] = []
        if h.statement:
            h_parts.append(f"Statement: {h.statement}")
        if h.depends_on:
            h_parts.append(f"Depends on: {', '.join(h.depends_on)}")
        if h.promotion_justification:
            h_parts.append(f"Promotion justification: {h.promotion_justification}")
        if h.evidence:
            for ev in h.evidence:
                result_short = (ev.result[:300] + "...") if ev.result and len(ev.result) > 300 else (ev.result or "")
                h_parts.append(f"Evidence ({ev.type}): {ev.method or 'not specified'}, confidence={ev.confidence or '?'}, Result: {result_short}")
        if h.review:
            v = h.review
            h_parts.append(f"Review: {v.verdict} — {v.summary}")
            survived_critic = (
                h.iteration_modified <= last_critic_iter
                and critic_targets_since.get(h.id, 0) < h.iteration_modified
            )
            if not survived_critic and v.details:
                details_truncated = (v.details[:1000] + "...") if len(v.details) > 1000 else v.details
                h_parts.append(f"Review details: {details_truncated}")
        return h_parts

    # Established Results
    ers = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.ESTABLISHED],
        key=lambda h: h.id,
    )
    if ers:
        er_lines: list[str] = []
        for h in ers:
            er_lines.append(f'<result id="{h.id}">\n' + "\n".join(_critic_hyp_parts(h)) + "\n</result>")
        parts.append("<established-results>\n" + "\n".join(er_lines) + "\n</established-results>")

    # Working Hypotheses
    whs = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.WORKING],
        key=lambda h: h.id,
    )
    if whs:
        hyp_lines: list[str] = []
        for h in whs:
            hyp_lines.append(f'<hypothesis id="{h.id}">\n' + "\n".join(_critic_hyp_parts(h)) + "\n</hypothesis>")
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
        for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
            if h.status == HypothesisStatus.ABANDONED:
                desc = f"Abandoned {h.id} — {h.statement}"
                if desc not in fa_descriptions:
                    de_parts.append(f"- {desc}")
        parts.append("<dead-ends>\n" + "\n".join(de_parts) + "\n</dead-ends>")

    # Background Survey
    if state.background_survey:
        survey_text = render_survey_sections_text(state.background_survey)
        if survey_text:
            parts.append(f"<background-survey>\n{survey_text}\n</background-survey>")

    # Previous Critiques (reuse existing XML renderer)
    critique_xml = render_orchestrator_critique_log(state)
    parts.append(f"<previous-critiques>\n{critique_xml}\n</previous-critiques>")

    return "\n\n".join(parts)


def render_formatter_context(
    state: ResearchState,
    answer_ers: list[str] | None = None,
) -> str:
    """Render focused context for the formatter agent using XML tags.

    Includes only what the formatter needs: problem statement, conventions,
    established results (with evidence/review), and a brief warning for any
    remaining open RQs or WHs.

    If *answer_ers* is provided, an ``<answer-structure>`` section is emitted
    listing the ER IDs in the order chosen by the orchestrator.
    """
    parts: list[str] = []

    # Problem Statement
    parts.append(f"<problem-statement>\n{state.problem_statement or '(No problem statement.)'}\n</problem-statement>")

    # Conventions
    if state.conventions:
        parts.append(f"<conventions>\n{state.conventions}\n</conventions>")

    # Answer structure hint from orchestrator
    if answer_ers:
        er_list = "\n".join(f"- {er_id}" for er_id in answer_ers)
        parts.append(
            "<answer-structure>\n"
            "The orchestrator identified these established results as the "
            "key answers, in this order:\n"
            f"{er_list}\n"
            "</answer-structure>"
        )

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
                for ev in h.evidence:
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


def render_planner_revise_context(state: ResearchState, trigger_text: str) -> str:
    """Render context for the planner's strategy-revision mode.

    Provides: problem statement, background survey, current strategy,
    revision trigger, entity one-liners, dead ends, research notes,
    and conventions.
    """
    parts: list[str] = []

    # Problem Statement
    parts.append(f"<problem-statement>\n{state.problem_statement or '(No problem statement.)'}\n</problem-statement>")

    # Background Survey
    if state.background_survey:
        survey_text = render_survey_sections_text(state.background_survey)
        if survey_text:
            parts.append(f"<background-survey>\n{survey_text}\n</background-survey>")

    # Current Strategy
    strat = state.strategy or "(No strategy set.)"
    parts.append(f"<current-strategy>\n{strat}\n</current-strategy>")

    # Revision Trigger
    parts.append(f"<revision-trigger>\n{trigger_text}\n</revision-trigger>")

    # Entities — one-liner per active entity
    entity_lines: list[str] = []
    for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
        if h.status == HypothesisStatus.ESTABLISHED:
            entity_lines.append(f"{h.id}: {h.statement}, VERIFIED")
    for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
        if h.status == HypothesisStatus.WORKING:
            review_tag = h.review.verdict if h.review else "PENDING REVIEW"
            entity_lines.append(f"{h.id}: {h.statement}, {review_tag}")
    for rq in sorted(state.research_questions.values(), key=lambda r: r.id):
        if rq.status == RQStatus.OPEN:
            n_ev = len(rq.evidence)
            entity_lines.append(f"{rq.id}: {rq.question}, OPEN, {n_ev} evidence item{'s' if n_ev != 1 else ''}")
    if entity_lines:
        parts.append("<entities>\n" + "\n".join(entity_lines) + "\n</entities>")

    # Dead Ends
    de_lines: list[str] = []
    for fa in state.failed_approaches:
        line = f"- {fa.description}"
        if fa.reason:
            line += f" (Reason: {fa.reason})"
        de_lines.append(line)
    fa_descriptions = {fa.description for fa in state.failed_approaches}
    for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
        if h.status == HypothesisStatus.ABANDONED:
            desc = f"Abandoned {h.id} — {h.statement}"
            if desc not in fa_descriptions:
                de_lines.append(f"- {desc}")
    if de_lines:
        parts.append("<dead-ends>\n" + "\n".join(de_lines) + "\n</dead-ends>")

    # Research Notes
    if state.research_notes:
        note_lines: list[str] = []
        for n in state.research_notes:
            it = n.get("iteration", "?")
            text = n.get("text", "")
            note_lines.append(f"[iter {it}] {text}")
        parts.append("<research-notes>\n" + "\n".join(note_lines) + "\n</research-notes>")

    # Conventions
    if state.conventions:
        parts.append(f"<conventions>\n{state.conventions}\n</conventions>")

    return "\n\n".join(parts)


def render_orchestrator_critique_log(state: ResearchState) -> str:
    """Render critique log for orchestrator context using XML tags.

    Only shows ACTIVE (unresolved) critiques — resolved critiques are noise
    for the orchestrator (already handled; preserved in git snapshots).
    """
    active = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]

    parts: list[str] = []

    for c in sorted(active, key=lambda c: c.id):
        target_str = ", ".join(c.targets) if c.targets else "general"
        blocking = "true" if c.severity == Severity.HIGH else "false"
        content = c.argument or ""
        parts.append(f'<critique id="{c.id}" severity="{c.severity}" blocking="{blocking}" status="UNRESOLVED" target="{target_str}">\n{content}\n</critique>')

    if state.critic_clean_reviews:
        review_lines: list[str] = []
        for rev in sorted(state.critic_clean_reviews, key=lambda r: r.get("iteration", 0)):
            review_lines.append(f"Iteration {rev.get('iteration', '?')}: {rev.get('summary', '')}")
        parts.append("<clean-reviews>\n" + "\n".join(review_lines) + "\n</clean-reviews>")

    return "\n".join(parts)
