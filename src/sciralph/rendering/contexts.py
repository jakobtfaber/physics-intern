"""Per-agent context renderers: build user-message content from ResearchState.

Each agent gets a tailored view of the research state — these functions
produce that view as XML-delimited strings.
"""

from __future__ import annotations

from ..research_state import (
    CritiqueStatus,
    Hypothesis,
    HypothesisStatus,
    ResearchState,
    RQStatus,
    SanityCheck,
    Severity,
)


def _render_sanity_checks(checks: list[SanityCheck], tag: str = "sanity-checks") -> str:
    """Render structured sanity checks as an XML-tagged block."""
    lines: list[str] = []
    for sc in checks:
        line = f"- [{sc.id}] {sc.predicate}"
        if sc.rationale:
            line += f"\n  Rationale: {sc.rationale}"
        lines.append(line)
    return f"<{tag}>\n" + "\n".join(lines) + f"\n</{tag}>"


def render_background_survey_xml(state: ResearchState) -> str:
    """Render survey data as XML sub-tags for agent context.

    Returns inner content (background, key-insights, known-methods, known-pitfalls)
    without an outer wrapper — callers wrap in <background-survey> or
    <current-background-survey> as appropriate.
    """
    parts: list[str] = []
    if state.survey_background:
        parts.append(f"<background>\n{state.survey_background}\n</background>")
    if state.key_insights:
        parts.append(f"<key-insights>\n{state.key_insights}\n</key-insights>")
    if state.survey_methods:
        parts.append(f"<known-methods>\n{state.survey_methods}\n</known-methods>")
    if state.known_pitfalls:
        parts.append(f"<known-pitfalls>\n{state.known_pitfalls}\n</known-pitfalls>")
    if state.expected_answer_structure:
        parts.append(f"<expected-answer-structure>\n{state.expected_answer_structure}\n</expected-answer-structure>")
    return "\n".join(parts)


def _problem_guidelines() -> str:
    """Return the <problem-guidelines> block shared by all agent contexts."""
    return (
        "<problem-guidelines>\n"
        "- The problem statement is correct and well-posed. Do not question "
        "whether the problem contains errors or is ill-defined.\n"
        "- The answer template suggests a format, but do not infer that the "
        "final answer must depend on every parameter appearing in the template. "
        "A parameter's presence in the template does not guarantee it survives "
        "in the final expression.\n"
        "</problem-guidelines>"
    )


def render_research_context_xml(state: ResearchState) -> str:
    """Render <research-context> wrapper: problem-statement + answer-template."""
    parts = [f"<problem-statement>\n{state.problem_statement}\n</problem-statement>"]
    if state.answer_template:
        parts.append(f"<answer-template>\n{state.answer_template}\n</answer-template>")
    parts.append(_problem_guidelines())
    return "<research-context>\n" + "\n".join(parts) + "\n</research-context>"


def _render_hypothesis_parts(h: Hypothesis) -> list[str]:
    """Render the inner XML parts of a hypothesis/result entry."""
    h_parts: list[str] = []
    if h.statement:
        h_parts.append(f"Statement: {h.statement}")
    if h.depends_on:
        h_parts.append(f"Depends on: {', '.join(h.depends_on)}")
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
        # Omit resolved RQs that resolved entirely to ERs (already in established-results)
        resolved_rqs = sorted(
            [rq for rq in state.research_questions.values()
             if rq.status != RQStatus.OPEN
             and not (rq.resolved_to and all(t.startswith("ER-") for t in rq.resolved_to))],
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
        if rq_lines:
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


def render_orchestrator_slim_state(
    state: ResearchState,
    *,
    max_open_rqs: int | None = None,
) -> str:
    """Render compact one-liner research state for the orchestrator.

    Target ~8K tokens.  Keeps conventions and strategy in full; renders
    entities as one-liners (no derivations, evidence details, or review text).

    If *max_open_rqs* is given, a cap annotation is added to the
    ``<research-questions>`` section so the orchestrator knows the limit.
    """
    parts: list[str] = []

    # Conventions (full)
    conv = state.conventions or "(To be populated by the orchestrator as conventions become clear.)"
    parts.append(f"<conventions>\n{conv}\n</conventions>")

    # Strategy (full)
    strat = state.strategy or "(No strategy set.)"
    parts.append(f"<strategy>\n{strat}\n</strategy>")

    # Sanity checks
    if state.sanity_checks:
        parts.append(_render_sanity_checks(state.sanity_checks))

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

    # Research Questions — one-liner per RQ (omit resolved→ER, already in established-results)
    rq_lines: list[str] = []
    n_open_rqs = 0
    if state.research_questions:
        for rq in sorted(state.research_questions.values(), key=lambda r: r.id):
            if rq.status == RQStatus.OPEN:
                n_open_rqs += 1
                n_ev = len(rq.evidence)
                ev_note = f", {n_ev} evidence item{'s' if n_ev != 1 else ''}" if n_ev else ""
                rq_lines.append(f"- {rq.id}: {rq.question}, OPEN{ev_note}")
            elif rq.resolved_to and all(t.startswith("ER-") for t in rq.resolved_to):
                continue  # already visible in established-results
            elif rq.resolved_to:
                rq_lines.append(f"- {rq.id}: {rq.question}, RESOLVED → {', '.join(rq.resolved_to)}")
            else:
                rq_lines.append(f"- {rq.id}: {rq.question}, {rq.status.upper()}")
    if max_open_rqs is not None:
        cap_note = f"Open RQ cap: {max_open_rqs} (currently {n_open_rqs} open"
        if n_open_rqs >= max_open_rqs:
            cap_note += " — limit reached, resolve existing RQs before creating new ones"
        cap_note += ")"
        rq_lines.insert(0, cap_note)
    if rq_lines:
        parts.append("<research-questions>\n" + "\n".join(rq_lines) + "\n</research-questions>")

    # Dead Ends — one-liner per entry (truncated for context budget)
    def _trunc(s: str, cap: int) -> str:
        return s if len(s) <= cap else s[: cap - 1] + "\u2026"

    dead_lines: list[str] = []
    for fa in state.failed_approaches:
        desc = _trunc(fa.description, 150)
        reason = f" ({_trunc(fa.reason, 120)})" if fa.reason else ""
        dead_lines.append(f"- {desc}{reason}")
    fa_descriptions = {fa.description for fa in state.failed_approaches}
    for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
        if h.status == HypothesisStatus.ABANDONED:
            desc = f"Abandoned {h.id} — {h.statement}"
            if desc not in fa_descriptions:
                dead_lines.append(f"- {_trunc(desc, 150)}")
    if dead_lines:
        parts.append("<dead-ends>\n" + "\n".join(dead_lines) + "\n</dead-ends>")

    return "\n\n".join(parts)


def render_critic_context(state: ResearchState, iteration: int) -> str:
    """Render strategic context for the deep critic using XML tags.

    Provides: problem statement, strategy, conventions, RQ list,
    hypothesis summaries (evidence/review one-liners), background survey,
    and previous critiques.  No derivations, scripts, reasoning, or approach text.
    """
    parts: list[str] = []

    # 1. Research context — problem statement + answer template
    parts.append(render_research_context_xml(state))

    # 2. Background survey
    survey_ctx = render_background_survey_xml(state)
    if survey_ctx:
        parts.append(f"<background-survey>\n{survey_ctx}\n</background-survey>")

    # 3. Research state — strategy, conventions, sanity checks, entities
    rs_parts: list[str] = []

    conv = state.conventions or "(No conventions set.)"
    rs_parts.append(f"<conventions>\n{conv}\n</conventions>")

    strat = state.strategy or "(No strategy set.)"
    rs_parts.append(f"<strategy>\n{strat}\n</strategy>")

    if state.sanity_checks:
        rs_parts.append(_render_sanity_checks(state.sanity_checks))

    # Research Questions
    if state.research_questions:
        rq_lines: list[str] = []
        for rq in sorted(state.research_questions.values(), key=lambda r: r.id):
            rq_lines.append(f'<rq id="{rq.id}" status="{rq.status.upper()}">{rq.question}</rq>')
        rs_parts.append("<research-questions>\n" + "\n".join(rq_lines) + "\n</research-questions>")

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

    # Hypotheses (working)
    whs = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.WORKING],
        key=lambda h: h.id,
    )
    if whs:
        hyp_lines: list[str] = []
        for h in whs:
            hyp_lines.append(f'<hypothesis id="{h.id}">\n' + "\n".join(_critic_hyp_parts(h)) + "\n</hypothesis>")
        rs_parts.append("<hypotheses>\n" + "\n".join(hyp_lines) + "\n</hypotheses>")

    # Established Results
    ers = sorted(
        [h for h in state.hypotheses.values() if h.status == HypothesisStatus.ESTABLISHED],
        key=lambda h: h.id,
    )
    if ers:
        er_lines: list[str] = []
        for h in ers:
            er_lines.append(f'<result id="{h.id}">\n' + "\n".join(_critic_hyp_parts(h)) + "\n</result>")
        rs_parts.append("<established-results>\n" + "\n".join(er_lines) + "\n</established-results>")

    parts.append("<research-state>\n" + "\n\n".join(rs_parts) + "\n</research-state>")

    # 4. Previous Critiques (critic needs both active AND resolved to avoid re-filing)
    critique_xml = render_critic_previous_critiques(state)
    parts.append(f"<previous-critiques>\n{critique_xml}\n</previous-critiques>")

    return "\n\n".join(parts)


def render_formatter_context(
    state: ResearchState,
    answer_ers: list[str] | None = None,
    *,
    best_effort: bool = False,
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

    if state.answer_template:
        parts.append(f"<answer-template>\n{state.answer_template}\n</answer-template>")
    parts.append(_problem_guidelines())

    # Research state — conventions + sanity checks
    rs_parts: list[str] = []
    if state.conventions:
        rs_parts.append(f"<conventions>\n{state.conventions}\n</conventions>")
    if state.sanity_checks:
        rs_parts.append(_render_sanity_checks(state.sanity_checks))
    if rs_parts:
        parts.append("<research-state>\n" + "\n".join(rs_parts) + "\n</research-state>")

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

    # Best-effort: include working hypotheses with full evidence so the
    # formatter can attempt an answer even without full ER coverage.
    if best_effort:
        working_whs_full = sorted(
            [h for h in state.hypotheses.values() if h.status == HypothesisStatus.WORKING],
            key=lambda h: h.id,
        )
        if working_whs_full:
            wh_lines: list[str] = []
            for h in working_whs_full:
                h_parts_w: list[str] = []
                if h.statement:
                    h_parts_w.append(f"Statement: {h.statement}")
                if h.derivation:
                    h_parts_w.append(f"Derivation: {h.derivation}")
                if h.evidence:
                    for ev in h.evidence:
                        ev_parts_w: list[str] = []
                        if ev.method:
                            ev_parts_w.append(f"Method: {ev.method}")
                        if ev.result:
                            ev_parts_w.append(f"Result: {ev.result}")
                        if ev.confidence:
                            ev_parts_w.append(f"Confidence: {ev.confidence}")
                        h_parts_w.append("<evidence>\n" + "\n".join(ev_parts_w) + "\n</evidence>")
                if h.review:
                    h_parts_w.append(f"Review verdict: {h.review.verdict}")
                wh_lines.append(f'<working-hypothesis id="{h.id}">\n' + "\n".join(h_parts_w) + "\n</working-hypothesis>")
            parts.append(
                "<unverified-results>\n"
                "The following working hypotheses have NOT been fully verified but may "
                "contain useful partial results:\n"
                + "\n".join(wh_lines)
                + "\n</unverified-results>"
            )

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


def _render_entity_detail(h: Hypothesis, is_er: bool = False) -> str:
    """Render enriched entity description for planner revision context."""
    summary_limit = 300 if is_er else 150
    status_tag = "VERIFIED" if h.status == HypothesisStatus.ESTABLISHED else (
        h.review.verdict if h.review else "PENDING REVIEW"
    )
    lines = [f"{h.id}: {h.statement}, {status_tag}"]
    deps = ", ".join(h.depends_on) if h.depends_on else "none"
    lines.append(f"  depends_on: {deps}")
    if is_er and h.derivation:
        lines.append(f"  derivation (excerpt): {h.derivation[:300]}")
    for ev in h.evidence:
        summary = (ev.summary or "")[:summary_limit]
        lines.append(f"  evidence: [{ev.id}] {ev.type} — {summary}")
    if h.review:
        review_summary = (h.review.summary or "")[:summary_limit]
        lines.append(f"  review: {h.review.verdict} — {review_summary}")
    return "\n".join(lines)


def _render_rq_detail(rq) -> str:
    """Render enriched RQ description for planner revision context."""
    n_ev = len(rq.evidence)
    lines = [f"{rq.id}: {rq.question}, {rq.status.value.upper()}, {n_ev} evidence item{'s' if n_ev != 1 else ''}"]
    for ev in rq.evidence:
        summary = (ev.summary or "")[:150]
        lines.append(f"  evidence: [{ev.id}] {ev.type} — {summary}")
    return "\n".join(lines)


def render_planner_revise_context(state: ResearchState, trigger_text: str) -> str:
    """Render context for the planner's strategy-revision mode.

    Provides: problem statement, background survey, current strategy,
    revision trigger, enriched entity descriptions, dead ends, research notes,
    and conventions.
    """
    parts: list[str] = []

    # Problem Statement
    parts.append(f"<problem-statement>\n{state.problem_statement or '(No problem statement.)'}\n</problem-statement>")

    if state.answer_template:
        parts.append(f"<answer-template>\n{state.answer_template}\n</answer-template>")
    parts.append(_problem_guidelines())

    # Background Survey (excludes conventions and sanity checks — rendered separately)
    survey_ctx = render_background_survey_xml(state)
    if survey_ctx:
        parts.append(f"<background-survey>\n{survey_ctx}\n</background-survey>")

    # Research state — conventions, established results (enriched), dead ends
    rs_parts: list[str] = []
    if state.conventions:
        rs_parts.append(f"<conventions>\n{state.conventions}\n</conventions>")

    # Established Results only (enriched detail, not one-liner)
    er_lines: list[str] = []
    for h in sorted(state.hypotheses.values(), key=lambda h: h.id):
        if h.status == HypothesisStatus.ESTABLISHED:
            er_lines.append(_render_entity_detail(h, is_er=True))
    if er_lines:
        rs_parts.append("<established-results>\n" + "\n\n".join(er_lines) + "\n</established-results>")

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
        rs_parts.append("<dead-ends>\n" + "\n".join(de_lines) + "\n</dead-ends>")

    if rs_parts:
        parts.append("<research-state>\n" + "\n\n".join(rs_parts) + "\n</research-state>")

    # Current strategy (top-level, planner's own output being revised)
    strat = state.strategy or "(No strategy set.)"
    parts.append(f"<current-strategy>\n{strat}\n</current-strategy>")

    # Current sanity checks (editable by the planner)
    if state.sanity_checks:
        parts.append(_render_sanity_checks(state.sanity_checks, tag="current-sanity-checks"))

    # Revision Trigger
    parts.append(f"<revision-trigger>\n{trigger_text}\n</revision-trigger>")

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


def render_critic_previous_critiques(state: ResearchState) -> str:
    """Render previous critiques for the deep critic context.

    Unlike the orchestrator version, includes RESOLVED critiques so the
    critic can see what was already filed and avoid re-filing.
    """
    parts: list[str] = []

    # Active critiques first
    active = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]
    for c in sorted(active, key=lambda c: c.id):
        target_str = ", ".join(c.targets) if c.targets else "general"
        blocking = "true" if c.severity == Severity.HIGH else "false"
        content = c.argument or ""
        parts.append(
            f'<critique id="{c.id}" severity="{c.severity}" blocking="{blocking}"'
            f' status="UNRESOLVED" target="{target_str}">\n{content}\n</critique>'
        )

    # Resolved/withdrawn critiques — concise, with resolution
    resolved = [c for c in state.critiques.values() if c.status != CritiqueStatus.ACTIVE]
    for c in sorted(resolved, key=lambda c: c.id):
        target_str = ", ".join(c.targets) if c.targets else "general"
        res_type = c.resolution_type or "unknown"
        resolution = c.resolution or ""
        content = c.argument or ""
        parts.append(
            f'<critique id="{c.id}" severity="{c.severity}"'
            f' status="RESOLVED" resolution-type="{res_type}" target="{target_str}">'
            f"\n{content}"
            + (f"\nResolution: {resolution}" if resolution else "")
            + "\n</critique>"
        )

    # Clean reviews
    if state.critic_clean_reviews:
        review_lines: list[str] = []
        for rev in sorted(state.critic_clean_reviews, key=lambda r: r.get("iteration", 0)):
            review_lines.append(f"Iteration {rev.get('iteration', '?')}: {rev.get('summary', '')}")
        parts.append("<clean-reviews>\n" + "\n".join(review_lines) + "\n</clean-reviews>")

    return "\n".join(parts)
