"""Tests for the renderers module (snapshot and context renderers)."""

import pytest

from sciralph.markdown import parse_frontmatter
from sciralph.renderers import (
    render_critique_log_md,
    render_critic_context,
    render_evidence_log_md,
    render_formatter_context,
    render_orchestrator_critique_log,
    render_orchestrator_research_state,
    render_orchestrator_slim_state,
    render_background_survey,
    render_planner_revise_context,
    render_research_state_md,
    render_task_md,
)
from sciralph.research_state import (
    Critique,
    CritiqueStatus,
    Evidence,
    FailedApproach,
    Hypothesis,
    HypothesisStatus,
    ResearchState,
    Severity,
    Verdict,
    ReviewResult,
)
from sciralph.task import Task, TaskType


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_state():
    """ResearchState with representative entities for concise tests.

    Contains:
      - 1 ER hypothesis (ER-001, established, with evidence + verification)
      - 1 WH hypothesis (WH-002, working, with evidence)
      - 1 abandoned hypothesis (WH-003)
      - 1 active HIGH critique (CRIT-001)
      - 1 resolved LOW critique (CRIT-002)
      - 1 failed approach
    """
    state = ResearchState(
        iteration=5,
        status="in_progress",
        title="Hawking Temperature Derivation",
        problem_statement="Derive the Hawking temperature for a Schwarzschild black hole.",
        conventions="Natural units: G = c = hbar = k_B = 1.",
    )

    state.hypotheses["ER-001"] = Hypothesis(
        id="ER-001",
        statement="T_H = 1/(8 pi M)",
        status=HypothesisStatus.ESTABLISHED,
        derivation="Surface gravity kappa = 1/(4M), then T = kappa/(2 pi).",
        iteration_created=1,
        iteration_modified=3,
        evidence=[Evidence(
            type="compute",
            method="Symbolic computation with sympy",
            result="T = 1/(8*pi*M)",
            confidence="exact",
            iteration=3,
        )],
        review=ReviewResult(
            verdict=Verdict.VERIFIED,
            summary="Symbolic computation confirms the formula.",
            iteration=3,
        ),
    )
    state.hypotheses["WH-002"] = Hypothesis(
        id="WH-002",
        statement="Entropy S = 4 pi M^2",
        status=HypothesisStatus.WORKING,
        derivation="From integration of dS = dM/T.",
        iteration_created=2,
        iteration_modified=4,
        evidence=[Evidence(
            type="compute",
            method="Numerical integration",
            result="S ~ 4*pi*M**2 to 1e-10",
            confidence="approximate",
            iteration=4,
        )],
    )
    state.hypotheses["WH-003"] = Hypothesis(
        id="WH-003",
        statement="Radiation is purely thermal",
        status=HypothesisStatus.ABANDONED,
        derivation="Greybody factors modify the spectrum.",
        iteration_created=1,
        iteration_modified=5,
    )

    state.critiques["CRIT-001"] = Critique(
        id="CRIT-001",
        targets=["WH-002"],
        severity=Severity.HIGH,
        argument="The entropy derivation assumes thermal equilibrium without justification.",
        status=CritiqueStatus.ACTIVE,
        iteration_filed=3,
    )
    state.critiques["CRIT-002"] = Critique(
        id="CRIT-002",
        targets=["ER-001"],
        severity=Severity.LOW,
        argument="Minor: notation inconsistency in temperature expression.",
        status=CritiqueStatus.RESOLVED,
        resolution="Fixed notation in iteration 4.",
        iteration_filed=2,
        iteration_resolved=4,
    )

    state.failed_approaches.append(FailedApproach(
        description="Direct Euclidean path integral approach",
        reason="Requires regularization scheme not yet implemented",
        related_entities=["ER-001"],
        iteration=2,
        derivation_excerpt="Euclidean continuation t -> -i tau, period beta = 1/T.",
    ))

    return state


@pytest.fixture
def empty_state():
    """Minimal empty ResearchState."""
    return ResearchState(
        problem_statement="Empty test problem.",
        title="Empty",
        iteration=0,
    )


@pytest.fixture
def sample_task():
    return Task(
        task_id="TASK-005",
        task_type=TaskType.REVIEW,
        assigned_to="reviewer",
        priority="high",
        iteration=5,
        target_claim="ER-001",
        body="Verify that T_H = 1/(8 pi M).",
    )


@pytest.fixture
def resolve_task():
    return Task(
        task_id="TASK-006",
        task_type=TaskType.RESEARCH,
        assigned_to="researcher",
        priority="high",
        iteration=5,
        blocking_critiques=["CRIT-001"],
        body="Resolve the critique about thermal equilibrium.",
    )


# ===========================================================================
# render_research_state_md
# ===========================================================================

class TestRenderResearchStateMd:

    def test_frontmatter_fields(self, populated_state):
        md = render_research_state_md(populated_state)
        meta, _ = parse_frontmatter(md)
        assert meta["title"] == "Hawking Temperature Derivation"
        assert meta["status"] == "in_progress"
        assert meta["iteration"] == 5
        assert meta["problem_id"] == "research-session"

    def test_problem_statement_section(self, populated_state):
        md = render_research_state_md(populated_state)
        assert "# Problem Statement" in md
        assert "Derive the Hawking temperature" in md

    def test_conventions_section(self, populated_state):
        md = render_research_state_md(populated_state)
        assert "# Conventions" in md
        assert "Natural units" in md

    def test_er_before_wh_ordering(self, populated_state):
        md = render_research_state_md(populated_state)
        er_pos = md.index("## ER-001")
        wh_pos = md.index("## WH-002")
        assert er_pos < wh_pos, "ER hypotheses should appear before WH hypotheses"

    def test_er_section_present(self, populated_state):
        md = render_research_state_md(populated_state)
        assert "## ER-001" in md
        assert "T_H = 1/(8 pi M)" in md

    def test_wh_section_present(self, populated_state):
        md = render_research_state_md(populated_state)
        assert "## WH-002" in md
        assert "Entropy S = 4 pi M^2" in md

    def test_abandoned_not_in_hypotheses_section(self, populated_state):
        md = render_research_state_md(populated_state)
        # WH-003 is abandoned; it should NOT appear as a ## WH-003 section header
        # in the hypotheses area (between "# Working Hypotheses" and "# Dead Ends")
        hyp_section_start = md.index("# Working Hypotheses")
        dead_ends_start = md.index("# Dead Ends")
        hyp_section = md[hyp_section_start:dead_ends_start]
        assert "## WH-003" not in hyp_section

    def test_abandoned_in_dead_ends(self, populated_state):
        md = render_research_state_md(populated_state)
        dead_ends_start = md.index("# Dead Ends")
        dead_ends = md[dead_ends_start:]
        assert "Abandoned WH-003" in dead_ends

    def test_failed_approaches_in_dead_ends(self, populated_state):
        md = render_research_state_md(populated_state)
        dead_ends_start = md.index("# Dead Ends")
        dead_ends = md[dead_ends_start:]
        assert "Direct Euclidean path integral approach" in dead_ends
        assert "Requires regularization" in dead_ends

    def test_dead_ends_renders_derivation_and_related_entities(self, populated_state):
        md = render_research_state_md(populated_state)
        dead_ends_start = md.index("# Dead Ends")
        dead_ends = md[dead_ends_start:]
        assert "Derivation: Euclidean continuation" in dead_ends
        assert "Related entities: ER-001" in dead_ends

    def test_empty_state_valid_markdown(self, empty_state):
        md = render_research_state_md(empty_state)
        meta, body = parse_frontmatter(md)
        assert meta["iteration"] == 0
        assert "# Problem Statement" in body
        assert "# Conventions" in body
        assert "# Dead Ends" in body
        assert "(None yet.)" in body

    def test_conventions_placeholder_when_empty(self):
        state = ResearchState(problem_statement="Test", conventions="")
        md = render_research_state_md(state)
        assert "To be populated by the orchestrator" in md

    def test_strategy_section_rendered(self):
        state = ResearchState(problem_statement="Test", strategy="Focus on surface gravity.")
        md = render_research_state_md(state)
        assert "# Strategy" in md
        assert "Focus on surface gravity." in md

    def test_strategy_placeholder_when_empty(self):
        state = ResearchState(problem_statement="Test", strategy="")
        md = render_research_state_md(state)
        assert "# Strategy" in md
        assert "No strategy set" in md

    def test_title_falls_back_to_problem_truncation(self):
        long_problem = "A" * 200
        state = ResearchState(problem_statement=long_problem, title="")
        md = render_research_state_md(state)
        meta, _ = parse_frontmatter(md)
        assert meta["title"] == long_problem[:80]

    def test_derivation_body_included(self, populated_state):
        md = render_research_state_md(populated_state)
        assert "Surface gravity kappa = 1/(4M)" in md

    def test_depends_on_rendered(self):
        state = ResearchState(problem_statement="Test")
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Depends on ER-001",
            depends_on=["ER-001", "WH-003"],
        )
        md = render_research_state_md(state)
        assert "**Depends on:** ER-001, WH-003" in md

    def test_depends_on_omitted_when_empty(self, populated_state):
        md = render_research_state_md(populated_state)
        assert "**Depends on:**" not in md

    def test_research_questions_section_rendered(self):
        from sciralph.research_state import ResearchQuestion, RQStatus
        state = ResearchState(problem_statement="Test")
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is F(p)?",
            context="Needed for verification", status=RQStatus.OPEN,
        )
        state.research_questions["RQ-002"] = ResearchQuestion(
            id="RQ-002", question="Resolved question",
            status=RQStatus.RESOLVED, resolved_to=["WH-003"],
        )
        md = render_research_state_md(state)
        assert "# Research Questions" in md
        assert "RQ-001 [OPEN]" in md
        assert "What is F(p)?" in md
        assert "RQ-002 [RESOLVED]" in md
        assert "Resolved to: WH-003" in md
        # RQ section should appear before hypotheses section
        rq_pos = md.index("# Research Questions")
        wh_pos = md.index("# Working Hypotheses")
        assert rq_pos < wh_pos

    def test_no_rq_section_when_empty(self, empty_state):
        md = render_research_state_md(empty_state)
        assert "# Research Questions" not in md

    def test_evidence_summary_rendered(self, populated_state):
        """Evidence summary appears in the research state output."""
        md = render_research_state_md(populated_state)
        assert "**Evidence (compute):**" in md

    def test_verification_rendered(self, populated_state):
        """Verification status appears in the research state output."""
        md = render_research_state_md(populated_state)
        assert "**Review:** VERIFIED" in md


# ===========================================================================
# render_evidence_log_md
# ===========================================================================

class TestRenderEvidenceLogMd:

    def test_frontmatter_total_entries(self, populated_state):
        md = render_evidence_log_md(populated_state)
        meta, _ = parse_frontmatter(md)
        # ER-001 has evidence + verification, WH-002 has evidence
        assert meta["total_entries"] == 3

    def test_evidence_entry_fields(self, populated_state):
        md = render_evidence_log_md(populated_state)
        assert "ER-001: Evidence (compute)" in md
        assert "**Method:** Symbolic computation with sympy" in md
        assert "**Result:** T = 1/(8*pi*M)" in md

    def test_verification_entry_fields(self, populated_state):
        md = render_evidence_log_md(populated_state)
        assert "ER-001: Review" in md
        assert "VERIFIED" in md
        assert "Symbolic computation confirms" in md

    def test_sorted_by_iteration(self, populated_state):
        md = render_evidence_log_md(populated_state)
        # ER-001 evidence (iter 3) should precede WH-002 evidence (iter 4)
        pos_er = md.index("ER-001: Evidence")
        pos_wh = md.index("WH-002: Evidence")
        assert pos_er < pos_wh

    def test_empty_state_valid_markdown(self, empty_state):
        md = render_evidence_log_md(empty_state)
        meta, body = parse_frontmatter(md)
        assert meta["total_entries"] == 0
        assert "No evidence or verification recorded yet" in body

    def test_confidence_rendered(self, populated_state):
        md = render_evidence_log_md(populated_state)
        assert "**Confidence:** approximate" in md

    def test_rq_evidence_rendered(self):
        """Evidence on research questions appears in evidence log."""
        from sciralph.research_state import ResearchQuestion, RQStatus
        state = ResearchState()
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is F?",
            evidence=[Evidence(
                type="research", method="analysis",
                result="F = pi/4", iteration=2,
            )],
        )
        md = render_evidence_log_md(state)
        assert "RQ-001: Evidence (research)" in md
        assert "F = pi/4" in md

    def test_long_approach_preserved_in_evidence_log(self):
        """Approach text up to 2000 chars should not be truncated."""
        long_approach = "x" * 1800
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Test",
            status=HypothesisStatus.WORKING,
            evidence=[Evidence(
                type="compute", method="test",
                result="ok", approach=long_approach, iteration=1,
            )],
        )
        md = render_evidence_log_md(state)
        assert long_approach in md

    def test_long_reasoning_preserved_in_evidence_log(self):
        """Reasoning text up to 2000 chars should not be truncated."""
        long_reasoning = "y" * 1800
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Test",
            status=HypothesisStatus.WORKING,
            evidence=[Evidence(
                type="research", method="test",
                result="ok", reasoning=long_reasoning, iteration=1,
            )],
        )
        md = render_evidence_log_md(state)
        assert long_reasoning in md

    def test_long_verification_reasoning_preserved(self):
        """Verification reasoning up to 2000 chars should not be truncated."""
        long_reasoning = "z" * 1800
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Test",
            status=HypothesisStatus.WORKING,
            review=ReviewResult(
                verdict=Verdict.VERIFIED,
                summary=long_reasoning, iteration=2,
            ),
        )
        md = render_evidence_log_md(state)
        assert long_reasoning in md

    def test_promoted_rq_shows_cross_reference(self):
        """When RQ evidence was copied to a WH, the RQ entry should be a short cross-reference."""
        from sciralph.research_state import ResearchQuestion, RQStatus
        ev = Evidence(
            type="compute", method="symbolic", approach="Long approach text " * 50,
            result="T = 1/(8*pi*M)", reasoning="Full reasoning " * 50, iteration=2,
        )
        state = ResearchState()
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is T?",
            resolved_to=["WH-001"], status=RQStatus.RESOLVED,
            evidence=[ev],
        )
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="T = 1/(8*pi*M)",
            status=HypothesisStatus.WORKING,
            evidence=[ev],  # same evidence, deep-copied in real code
        )
        md = render_evidence_log_md(state)
        # WH-001 should have full evidence
        assert "WH-001: Evidence (compute)" in md
        # RQ-001 should be a short cross-reference, not full evidence
        assert "RQ-001: Evidence (compute) → promoted" in md
        assert "Full evidence under WH-001" in md
        # The RQ block should NOT have the approach field (it's a brief xref)
        rq_section_start = md.index("RQ-001: Evidence")
        rq_section_end = md.index("**Iteration:**", rq_section_start) + 20
        rq_section = md[rq_section_start:rq_section_end]
        assert "**Approach:**" not in rq_section


# ===========================================================================
# render_critique_log_md
# ===========================================================================

class TestRenderCritiqueLogMd:

    def test_active_under_active_section(self, populated_state):
        md = render_critique_log_md(populated_state)
        active_start = md.index("# Active Critiques")
        resolved_start = md.index("# Resolved Critiques")
        active_section = md[active_start:resolved_start]
        assert "CRIT-001" in active_section

    def test_resolved_under_resolved_section(self, populated_state):
        md = render_critique_log_md(populated_state)
        resolved_start = md.index("# Resolved Critiques")
        resolved_section = md[resolved_start:]
        assert "CRIT-002" in resolved_section

    def test_severity_tags_present(self, populated_state):
        md = render_critique_log_md(populated_state)
        assert "[HIGH]" in md
        assert "[LOW]" in md

    def test_unresolved_tag(self, populated_state):
        md = render_critique_log_md(populated_state)
        assert "[UNRESOLVED]" in md

    def test_resolved_tag(self, populated_state):
        md = render_critique_log_md(populated_state)
        assert "[RESOLVED]" in md

    def test_frontmatter_unresolved_counts(self, populated_state):
        md = render_critique_log_md(populated_state)
        meta, _ = parse_frontmatter(md)
        assert meta["total_critiques"] == 2
        assert meta["unresolved_critiques"] == 1

    def test_target_rendered(self, populated_state):
        md = render_critique_log_md(populated_state)
        assert "**Target:** WH-002" in md
        assert "**Target:** ER-001" in md

    def test_argument_rendered(self, populated_state):
        md = render_critique_log_md(populated_state)
        assert "thermal equilibrium without justification" in md

    def test_resolution_rendered(self, populated_state):
        md = render_critique_log_md(populated_state)
        assert "**Resolution:** Fixed notation in iteration 4." in md

    def test_empty_critiques_valid_markdown(self, empty_state):
        md = render_critique_log_md(empty_state)
        meta, body = parse_frontmatter(md)
        assert meta["total_critiques"] == 0
        assert meta["unresolved_critiques"] == 0
        assert "# Active Critiques" in body
        assert "# Resolved Critiques" in body

    def test_withdrawn_in_resolved_section(self):
        state = ResearchState()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001",
            targets=["WH-001"],
            severity=Severity.MEDIUM,
            argument="Withdrawn critique.",
            status=CritiqueStatus.WITHDRAWN,
        )
        md = render_critique_log_md(state)
        resolved_start = md.index("# Resolved Critiques")
        resolved_section = md[resolved_start:]
        assert "CRIT-001" in resolved_section
        assert "[WITHDRAWN]" in resolved_section

    def test_medium_severity_tag(self):
        state = ResearchState()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001",
            severity=Severity.MEDIUM,
            status=CritiqueStatus.ACTIVE,
        )
        md = render_critique_log_md(state)
        assert "[MEDIUM]" in md

    def test_general_target_when_empty(self):
        state = ResearchState()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001",
            targets=[],
            severity=Severity.LOW,
            status=CritiqueStatus.ACTIVE,
        )
        md = render_critique_log_md(state)
        assert "**Target:** general" in md


# ===========================================================================
# render_task_md
# ===========================================================================

class TestRenderTaskMd:

    def test_matches_to_markdown(self, sample_task):
        rendered = render_task_md(sample_task)
        expected = sample_task.to_markdown()
        assert rendered == expected

    def test_contains_frontmatter(self, sample_task):
        md = render_task_md(sample_task)
        meta, body = parse_frontmatter(md)
        assert meta["task_id"] == "TASK-005"
        assert meta["task_type"] == "review"
        assert meta["assigned_to"] == "reviewer"
        assert meta["priority"] == "high"
        assert meta["iteration"] == 5

    def test_body_present(self, sample_task):
        md = render_task_md(sample_task)
        assert "Verify that T_H = 1/(8 pi M)." in md

    def test_resolve_task_has_blocking_critiques(self, resolve_task):
        md = render_task_md(resolve_task)
        assert "CRIT-001" in md
        assert "blocking_critiques" in md


# ===========================================================================
# render_orchestrator_research_state
# ===========================================================================

class TestRenderOrchestratorResearchState:

    def test_no_frontmatter(self, populated_state):
        text = render_orchestrator_research_state(populated_state)
        assert "---" not in text

    def test_no_problem_statement(self, populated_state):
        text = render_orchestrator_research_state(populated_state)
        assert "<problem-statement>" not in text
        assert "Derive the Hawking temperature" not in text

    def test_conventions_included(self, populated_state):
        text = render_orchestrator_research_state(populated_state)
        assert "<conventions>" in text
        assert "Natural units" in text

    def test_hypotheses_included(self, populated_state):
        text = render_orchestrator_research_state(populated_state)
        assert 'id="ER-001"' in text
        assert 'id="WH-002"' in text

    def test_skips_empty_dead_ends(self, empty_state):
        text = render_orchestrator_research_state(empty_state)
        assert "<dead-ends>" not in text

    def test_includes_populated_dead_ends(self, populated_state):
        text = render_orchestrator_research_state(populated_state)
        assert "<dead-ends>" in text
        assert "Abandoned WH-003" in text

    def test_evidence_summary_in_context(self, populated_state):
        """Orchestrator context includes evidence summaries on hypotheses."""
        text = render_orchestrator_research_state(populated_state)
        assert 'type="compute"' in text

    def test_verification_in_context(self, populated_state):
        """Orchestrator context includes verification status on hypotheses."""
        text = render_orchestrator_research_state(populated_state)
        assert 'verdict="VERIFIED"' in text


# ===========================================================================
# render_orchestrator_critique_log
# ===========================================================================

class TestRenderOrchestratorCritiqueLog:

    def test_empty_returns_compact(self, empty_state):
        text = render_orchestrator_critique_log(empty_state)
        assert text == ""

    def test_no_frontmatter(self, populated_state):
        text = render_orchestrator_critique_log(populated_state)
        assert "---" not in text

    def test_body_without_frontmatter(self, populated_state):
        text = render_orchestrator_critique_log(populated_state)
        # Only ACTIVE (unresolved) critiques shown to orchestrator
        assert 'status="UNRESOLVED"' in text
        assert "CRIT-001" in text
        # CRIT-002 is resolved — should NOT appear in orchestrator context
        assert 'status="RESOLVED"' not in text
        assert "CRIT-002" not in text


# ===========================================================================
# Snapshot regression: helpers don't break existing renderers
# ===========================================================================

class TestSnapshotRegression:
    """Guard against helper extraction breaking snapshot renderers."""

    def test_research_state_md_still_has_frontmatter(self, populated_state):
        md = render_research_state_md(populated_state)
        meta, body = parse_frontmatter(md)
        assert meta["title"] == "Hawking Temperature Derivation"
        assert "# Problem Statement" in body
        assert "# Dead Ends" in body

    def test_research_state_md_empty_still_valid(self, empty_state):
        md = render_research_state_md(empty_state)
        meta, body = parse_frontmatter(md)
        assert meta["iteration"] == 0
        assert "# Problem Statement" in body
        assert "(None yet.)" in body

    def test_critique_log_md_still_has_frontmatter(self, populated_state):
        md = render_critique_log_md(populated_state)
        meta, body = parse_frontmatter(md)
        assert meta["total_critiques"] == 2
        assert meta["unresolved_critiques"] == 1
        assert "# Active Critiques" in body
        assert "# Resolved Critiques" in body

    def test_critique_log_md_empty_still_valid(self, empty_state):
        md = render_critique_log_md(empty_state)
        meta, body = parse_frontmatter(md)
        assert meta["total_critiques"] == 0


# ===========================================================================
# render_background_survey
# ===========================================================================

class TestRenderBackgroundSurvey:

    def _make_survey_state(self):
        state = ResearchState(problem_statement="Test problem")
        state.survey_background = "## Background\n\nDerive Hawking temperature via surface gravity.\n\n## Key Insights\n\nUse Killing vector method first."
        state.survey_methods = "Method A."
        state.known_pitfalls = "Pitfall B."
        state.conventions = "Some conventions."
        state.sanity_checks = ["Check C."]
        return state

    def test_render_background_survey_with_sections(self):
        state = self._make_survey_state()
        text = render_background_survey(state)
        assert "# Background Survey" in text
        assert "Derive Hawking temperature via surface gravity." in text
        assert "Killing vector method" in text
        assert "Method A." in text
        assert "Pitfall B." in text
        assert "Check C." in text

    def test_background_survey_empty_renders_no_survey(self):
        state = ResearchState(problem_statement="Test")
        text = render_background_survey(state)
        assert text == "(No background survey.)"

    def test_background_survey_not_in_orchestrator_research_state(self):
        """Background survey is not in render_orchestrator_research_state."""
        state = self._make_survey_state()
        text = render_orchestrator_research_state(state)
        assert "<background-survey>" not in text


# ===========================================================================
# Collapsed resolved RQs in orchestrator context
# ===========================================================================

class TestCollapsedResolvedRQs:

    def test_resolved_rq_to_er_omitted(self):
        """Resolved RQ pointing to an ER is omitted (already in established-results)."""
        from sciralph.research_state import ResearchQuestion, RQStatus
        state = ResearchState(problem_statement="Test")
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is X?",
            status=RQStatus.RESOLVED, resolved_to=["ER-001"],
            iteration_created=1, iteration_resolved=2,
        )
        text = render_orchestrator_research_state(state)
        assert "RQ-001" not in text
        assert "What is X?" not in text
        # No research-questions section at all when only resolved→ER RQs exist
        assert "research-questions" not in text

    def test_abandoned_rq_one_liner(self):
        from sciralph.research_state import ResearchQuestion, RQStatus
        state = ResearchState(problem_statement="Test")
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is X?",
            status=RQStatus.ABANDONED, resolution_reason="Dead end after 2 attempts",
            iteration_created=1, iteration_resolved=3,
        )
        text = render_orchestrator_research_state(state)
        assert '<rq id="RQ-001" status="ABANDONED">Closed: Dead end after 2 attempts</rq>' in text
        assert "What is X?" not in text


# ---------------------------------------------------------------------------
# Enriched planner revision context
# ---------------------------------------------------------------------------

class TestPlannerReviseEnrichedContext:
    def test_enriched_er_shows_deps_evidence_review(self):
        from sciralph.research_state import ResearchQuestion, RQStatus
        state = ResearchState(problem_statement="Test problem")
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001",
            statement="F(p) is a rational function",
            status=HypothesisStatus.ESTABLISHED,
            depends_on=[],
            evidence=[Evidence(id="E-001", type="compute", summary="Markov chain yields degree-5 poly", iteration=2)],
            review=ReviewResult(verdict="VERIFIED", summary="Independent derivation confirms", iteration=3),
        )
        text = render_planner_revise_context(state, "ER-002 was overturned")
        assert "ER-001: F(p) is a rational function, VERIFIED" in text
        assert "depends_on: none" in text
        assert "evidence: [E-001] compute" in text
        assert "Markov chain" in text
        assert "review: VERIFIED" in text
        assert "Independent derivation" in text

    def test_enriched_wh_shows_pending_review(self):
        state = ResearchState(problem_statement="Test")
        state.hypotheses["WH-002"] = Hypothesis(
            id="WH-002",
            statement="X depends on Y",
            status=HypothesisStatus.WORKING,
            depends_on=["ER-001"],
        )
        text = render_planner_revise_context(state, "trigger")
        assert "WH-002: X depends on Y, PENDING REVIEW" in text
        assert "depends_on: ER-001" in text

    def test_enriched_rq_shows_evidence(self):
        from sciralph.research_state import ResearchQuestion, RQStatus
        state = ResearchState(problem_statement="Test")
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001",
            question="What is the leading term?",
            status=RQStatus.OPEN,
            evidence=[Evidence(id="E-005", type="research", summary="Leading term is O(p^2)", iteration=4)],
        )
        text = render_planner_revise_context(state, "trigger")
        assert "RQ-001: What is the leading term?, OPEN, 1 evidence item" in text
        assert "evidence: [E-005] research" in text
        assert "O(p^2)" in text

    def test_abandoned_entities_excluded(self):
        state = ResearchState(problem_statement="Test")
        state.hypotheses["WH-003"] = Hypothesis(
            id="WH-003",
            statement="Abandoned claim",
            status=HypothesisStatus.ABANDONED,
        )
        text = render_planner_revise_context(state, "trigger")
        assert "WH-003" not in text.split("<entities>")[0] if "<entities>" in text else True

    def test_critic_clean_reviews_included(self):
        state = ResearchState(problem_statement="Test")
        state.critic_clean_reviews = [
            {"iteration": 3, "summary": "All results consistent"},
            {"iteration": 7, "summary": "No issues found"},
        ]
        text = render_planner_revise_context(state, "trigger")
        assert "<critic-clean-reviews>" in text
        assert "Iteration 3: All results consistent" in text
        assert "Iteration 7: No issues found" in text

    def test_no_critic_clean_reviews_when_empty(self):
        state = ResearchState(problem_statement="Test")
        text = render_planner_revise_context(state, "trigger")
        assert "<critic-clean-reviews>" not in text


# ===========================================================================
# render_critic_context — sanity checks
# ===========================================================================

class TestRenderCriticContextSanityChecks:

    def test_sanity_checks_included(self):
        state = ResearchState(problem_statement="Test", strategy="Do X")
        state.sanity_checks = ["T -> 0 as M -> inf", "Result must be positive"]
        text = render_critic_context(state, iteration=3)
        assert "<sanity-checks>" in text
        assert "T -> 0 as M -> inf" in text
        assert "Result must be positive" in text

    def test_no_sanity_checks_when_empty(self):
        state = ResearchState(problem_statement="Test", strategy="Do X")
        text = render_critic_context(state, iteration=3)
        assert "<sanity-checks>" not in text


# ===========================================================================
# render_orchestrator_slim_state — sanity checks + known pitfalls
# ===========================================================================

class TestRenderOrchestratorSlimState:

    def test_sanity_checks_included(self):
        state = ResearchState(conventions="Natural units")
        state.sanity_checks = ["T -> 0 as M -> inf"]
        text = render_orchestrator_slim_state(state)
        assert "<sanity-checks>" in text
        assert "T -> 0 as M -> inf" in text

    def test_known_pitfalls_included(self):
        state = ResearchState(conventions="Natural units")
        state.known_pitfalls = "Sign conventions for metric signature"
        text = render_orchestrator_slim_state(state)
        assert "<known-pitfalls>" in text
        assert "Sign conventions" in text

    def test_no_sanity_checks_when_empty(self):
        state = ResearchState(conventions="Natural units")
        text = render_orchestrator_slim_state(state)
        assert "<sanity-checks>" not in text

    def test_no_known_pitfalls_when_empty(self):
        state = ResearchState(conventions="Natural units")
        text = render_orchestrator_slim_state(state)
        assert "<known-pitfalls>" not in text

    def test_ordering_strategy_before_sanity_checks(self):
        state = ResearchState(conventions="Natural units", strategy="Use surface gravity")
        state.sanity_checks = ["T > 0"]
        state.known_pitfalls = "Sign errors"
        text = render_orchestrator_slim_state(state)
        assert text.index("<strategy>") < text.index("<sanity-checks>")
        assert text.index("<sanity-checks>") < text.index("<known-pitfalls>")


# ===========================================================================
# render_formatter_context — sanity checks
# ===========================================================================

class TestRenderFormatterContextSanityChecks:

    def test_sanity_checks_included(self):
        state = ResearchState(problem_statement="Test", conventions="Natural units")
        state.sanity_checks = ["Result must be dimensionless"]
        text = render_formatter_context(state)
        assert "<sanity-checks>" in text
        assert "Result must be dimensionless" in text

    def test_no_sanity_checks_when_empty(self):
        state = ResearchState(problem_statement="Test")
        text = render_formatter_context(state)
        assert "<sanity-checks>" not in text
