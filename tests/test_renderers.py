"""Tests for the renderers module (snapshot and context renderers)."""

import pytest

from sciralph.markdown import parse_frontmatter
from sciralph.renderers import (
    render_critique_log_md,
    render_evidence_log_md,
    render_orchestrator_critique_log,
    render_orchestrator_research_state,
    render_background_survey,
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
    BackgroundSurvey,
    Severity,
    Verdict,
    VerificationResult,
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
        evidence=Evidence(
            type="compute",
            method="Symbolic computation with sympy",
            result="T = 1/(8*pi*M)",
            confidence="exact",
            iteration=3,
        ),
        verification=VerificationResult(
            verdict=Verdict.VERIFIED,
            reasoning="Symbolic computation confirms the formula.",
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
        evidence=Evidence(
            type="compute",
            method="Numerical integration",
            result="S ~ 4*pi*M**2 to 1e-10",
            confidence="approximate",
            iteration=4,
        ),
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
        task_type=TaskType.VERIFY,
        assigned_to="verifier",
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

    def test_promotion_justification_rendered(self):
        state = ResearchState(problem_statement="Test")
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", statement="Established result",
            status=HypothesisStatus.ESTABLISHED,
            promotion_justification="Verified by verifier.",
        )
        md = render_research_state_md(state)
        assert "**Promotion justification:** Verified by verifier." in md

    def test_evidence_summary_rendered(self, populated_state):
        """Evidence summary appears in the research state output."""
        md = render_research_state_md(populated_state)
        assert "**Evidence (compute):**" in md

    def test_verification_rendered(self, populated_state):
        """Verification status appears in the research state output."""
        md = render_research_state_md(populated_state)
        assert "**Verification:** VERIFIED" in md


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
        assert "ER-001: Verification" in md
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
            evidence=Evidence(
                type="research", method="analysis",
                result="F = pi/4", iteration=2,
            ),
        )
        md = render_evidence_log_md(state)
        assert "RQ-001: Evidence (research)" in md
        assert "F = pi/4" in md


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
        assert meta["unresolved_high"] == 1
        assert meta["unresolved_medium"] == 0
        assert meta["unresolved_low"] == 0

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
        assert meta["unresolved_high"] == 0
        assert meta["unresolved_medium"] == 0
        assert meta["unresolved_low"] == 0
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
        assert meta["task_type"] == "verify"
        assert meta["assigned_to"] == "verifier"
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
        assert "# Problem Statement" not in text
        assert "Derive the Hawking temperature" not in text

    def test_conventions_included(self, populated_state):
        text = render_orchestrator_research_state(populated_state)
        assert "# Conventions" in text
        assert "Natural units" in text

    def test_hypotheses_included(self, populated_state):
        text = render_orchestrator_research_state(populated_state)
        assert "## ER-001" in text
        assert "## WH-002" in text

    def test_skips_empty_dead_ends(self, empty_state):
        text = render_orchestrator_research_state(empty_state)
        assert "# Dead Ends" not in text

    def test_includes_populated_dead_ends(self, populated_state):
        text = render_orchestrator_research_state(populated_state)
        assert "# Dead Ends" in text
        assert "Abandoned WH-003" in text

    def test_evidence_summary_in_context(self, populated_state):
        """Orchestrator context includes evidence summaries on hypotheses."""
        text = render_orchestrator_research_state(populated_state)
        assert "Evidence (compute)" in text

    def test_verification_in_context(self, populated_state):
        """Orchestrator context includes verification status on hypotheses."""
        text = render_orchestrator_research_state(populated_state)
        assert "Verification:" in text


# ===========================================================================
# render_orchestrator_critique_log
# ===========================================================================

class TestRenderOrchestratorCritiqueLog:

    def test_empty_returns_compact(self, empty_state):
        text = render_orchestrator_critique_log(empty_state)
        assert text == "No critiques filed."

    def test_no_frontmatter(self, populated_state):
        text = render_orchestrator_critique_log(populated_state)
        assert "---" not in text

    def test_body_without_frontmatter(self, populated_state):
        text = render_orchestrator_critique_log(populated_state)
        assert "# Active Critiques" in text
        assert "CRIT-001" in text
        assert "# Resolved Critiques" in text
        assert "CRIT-002" in text


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
        assert meta["unresolved_high"] == 1
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
        state.background_survey = BackgroundSurvey(
            survey_notes="Derive Hawking temperature via surface gravity.\n\nUse Killing vector method first.",
            iteration_created=0,
            iteration_updated=0,
        )
        return state

    def test_render_background_survey_with_notes(self):
        state = self._make_survey_state()
        text = render_background_survey(state)
        assert "# Background Survey" in text
        assert "Derive Hawking temperature via surface gravity." in text
        assert "Killing vector method" in text

    def test_background_survey_none_renders_no_survey(self):
        state = ResearchState(problem_statement="Test")
        text = render_background_survey(state)
        assert text == "(No background survey.)"

    def test_background_survey_rendered_in_orchestrator_context(self):
        state = self._make_survey_state()
        text = render_orchestrator_research_state(state)
        assert "# Background Survey" in text
        assert "Killing vector method" in text

    def test_background_survey_none_renders_nothing_in_orchestrator(self):
        """When survey is None, no background survey section in orchestrator context."""
        state = ResearchState(problem_statement="Test")
        text = render_orchestrator_research_state(state)
        assert "# Background Survey" not in text
