"""Tests for the renderers module (snapshot and context renderers)."""

import pytest

from sciralph.markdown import parse_frontmatter
from sciralph.renderers import (
    render_computation_log_md,
    render_computation_log_tail,
    render_compute_research_state,
    render_critique_log_md,
    render_orchestrator_critique_log,
    render_orchestrator_research_state,
    render_research_plan,
    render_research_state_md,
    render_task_md,
)
from sciralph.research_state import (
    Computation,
    Critique,
    CritiqueStatus,
    FailedApproach,
    Hypothesis,
    HypothesisStatus,
    ResearchPlan,
    ResearchState,
    Severity,
    SubProblem,
    Verdict,
)
from sciralph.task import Task, TaskType


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_state():
    """ResearchState with representative entities for concise tests.

    Contains:
      - 1 ER hypothesis (ER-001, established)
      - 1 WH hypothesis (WH-002, working)
      - 1 abandoned hypothesis (WH-003)
      - 1 VERIFIED verify computation (COMP-001, targeting ER-001)
      - 1 explore computation (COMP-002, targeting WH-002)
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
    )
    state.hypotheses["WH-002"] = Hypothesis(
        id="WH-002",
        statement="Entropy S = 4 pi M^2",
        status=HypothesisStatus.WORKING,
        derivation="From integration of dS = dM/T.",
        iteration_created=2,
        iteration_modified=4,
    )
    state.hypotheses["WH-003"] = Hypothesis(
        id="WH-003",
        statement="Radiation is purely thermal",
        status=HypothesisStatus.ABANDONED,
        derivation="Greybody factors modify the spectrum.",
        iteration_created=1,
        iteration_modified=5,
    )

    state.computations["COMP-001"] = Computation(
        id="COMP-001",
        target_hypothesis="ER-001",
        verdict=Verdict.VERIFIED,
        claim="T_H = 1/(8 pi M) from surface gravity",
        method="Symbolic computation with sympy",
        result="T = 1/(8*pi*M)",
        iteration=3,
        kind="verify",
    )
    state.computations["COMP-002"] = Computation(
        id="COMP-002",
        target_hypothesis="WH-002",
        claim="Explore entropy integral",
        method="Numerical integration",
        result="S ~ 4*pi*M**2 to 1e-10",
        confidence="approximate",
        iteration=4,
        kind="explore",
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
        related_comps=["COMP-001"],
        iteration=2,
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
        task_type=TaskType.COMPUTE_VERIFY,
        assigned_to="compute_verify",
        priority="high",
        iteration=5,
        target_claim="ER-001",
        body="Verify that T_H = 1/(8 pi M).",
    )


@pytest.fixture
def resolve_task():
    return Task(
        task_id="TASK-006",
        task_type=TaskType.RESEARCH_EXPLORE,
        assigned_to="research_explore",
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
            promotion_justification="Verified by COMP-001.",
        )
        md = render_research_state_md(state)
        assert "**Promotion justification:** Verified by COMP-001." in md


# ===========================================================================
# render_computation_log_md
# ===========================================================================

class TestRenderComputationLogMd:

    def test_frontmatter_total_computations(self, populated_state):
        md = render_computation_log_md(populated_state)
        meta, _ = parse_frontmatter(md)
        assert meta["total_computations"] == 2

    def test_verify_entry_fields(self, populated_state):
        md = render_computation_log_md(populated_state)
        assert "COMP-001: Computation" in md
        assert "**CLAIM:**" in md
        assert "**VERDICT:** VERIFIED" in md
        assert "**METHOD:** Symbolic computation with sympy" in md

    def test_explore_entry_fields(self, populated_state):
        md = render_computation_log_md(populated_state)
        assert "COMP-002: Exploration" in md
        assert "**TARGET:** WH-002" in md
        assert "**DESCRIPTION:** Explore entropy integral" in md
        assert "**CONFIDENCE:** approximate" in md

    def test_sorted_by_iteration(self, populated_state):
        md = render_computation_log_md(populated_state)
        pos_comp1 = md.index("COMP-001")
        pos_comp2 = md.index("COMP-002")
        assert pos_comp1 < pos_comp2, "COMP-001 (iter 3) should precede COMP-002 (iter 4)"

    def test_iteration_label_present(self, populated_state):
        md = render_computation_log_md(populated_state)
        assert "**Iteration:** 3" in md
        assert "**Iteration:** 4" in md

    def test_empty_computations_valid_markdown(self, empty_state):
        md = render_computation_log_md(empty_state)
        meta, body = parse_frontmatter(md)
        assert meta["total_computations"] == 0
        assert "# Computations" in body

    def test_verify_entry_with_notes(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(
            id="COMP-001",
            claim="Test claim",
            method="Test method",
            result="Result",
            verdict=Verdict.INCONCLUSIVE,
            notes="Convergence issues at large M",
            iteration=1,
            kind="verify",
        )
        md = render_computation_log_md(state)
        assert "**NOTES:** Convergence issues at large M" in md

    def test_verify_entry_with_failure_detail_no_notes(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(
            id="COMP-001",
            claim="Test claim",
            method="Test method",
            result="Result",
            verdict=Verdict.REFUTED,
            failure_detail="Division by zero at r=0",
            iteration=1,
            kind="verify",
        )
        md = render_computation_log_md(state)
        assert "**NOTES:** Division by zero at r=0" in md

    def test_explore_entry_with_notes(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(
            id="COMP-001",
            target_hypothesis="WH-001",
            claim="Explore something",
            method="Numerical",
            result="42",
            confidence="exact",
            notes="Interesting behaviour near horizon",
            iteration=1,
            kind="explore",
        )
        md = render_computation_log_md(state)
        assert "**NOTES:** Interesting behaviour near horizon" in md

    def test_zero_output_collapsed_to_single_line(self):
        """zero_output computations render as a single FAILED line (C4)."""
        state = ResearchState()
        state.computations["TASK-003"] = Computation(
            id="TASK-003",
            target_hypothesis="WH-001",
            claim="Test claim",
            method="Test method",
            result="",
            verdict=Verdict.INCONCLUSIVE,
            zero_output=True,
            iteration=3,
            kind="verify",
        )
        md = render_computation_log_md(state)
        assert "TASK-003: FAILED (no result produced, iteration 3)" in md
        # Should NOT include the full entry fields
        assert "**CLAIM:**" not in md
        assert "**VERDICT:**" not in md

    def test_task_prefixed_id_counted_in_total(self):
        """TASK-prefixed computation IDs are counted in total_computations (A2)."""
        state = ResearchState()
        state.computations["TASK-003"] = Computation(
            id="TASK-003", claim="Test", iteration=3, kind="verify",
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", claim="Test2", iteration=2, kind="verify",
        )
        md = render_computation_log_md(state)
        meta, _ = parse_frontmatter(md)
        assert meta["total_computations"] == 2

    def test_target_hypothesis_prefix_in_verify(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(
            id="COMP-001",
            target_hypothesis="ER-001",
            claim="Energy conservation",
            method="Analytical",
            result="Confirmed",
            verdict=Verdict.VERIFIED,
            iteration=1,
            kind="verify",
        )
        md = render_computation_log_md(state)
        assert "**CLAIM:** ER-001 — Energy conservation" in md


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
        assert meta["task_type"] == "compute_verify"
        assert meta["assigned_to"] == "compute_verify"
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
# render_computation_log_tail
# ===========================================================================

class TestRenderComputationLogTail:

    def test_returns_last_n_entries(self, populated_state):
        text = render_computation_log_tail(populated_state, 1)
        assert "COMP-002" in text
        assert "COMP-001" not in text

    def test_returns_all_when_n_exceeds_count(self, populated_state):
        text = render_computation_log_tail(populated_state, 10)
        assert "COMP-001" in text
        assert "COMP-002" in text

    def test_empty_state_returns_empty(self, empty_state):
        text = render_computation_log_tail(empty_state, 5)
        assert text == ""

    def test_zero_output_collapsed(self):
        from sciralph.research_state import ResearchState
        state = ResearchState()
        state.computations["TASK-003"] = Computation(
            id="TASK-003", target_hypothesis="WH-001",
            claim="Test", zero_output=True, iteration=3, kind="verify",
        )
        text = render_computation_log_tail(state, 5)
        assert "TASK-003: FAILED" in text
        assert "**CLAIM:**" not in text


# ===========================================================================
# render_orchestrator_research_state
# ===========================================================================

class TestRenderComputeResearchState:

    def test_no_frontmatter(self, populated_state):
        text = render_compute_research_state(populated_state)
        assert "---" not in text

    def test_includes_problem_statement(self, populated_state):
        text = render_compute_research_state(populated_state)
        assert "# Problem Statement" in text
        assert "Derive the Hawking temperature" in text

    def test_conventions_included(self, populated_state):
        text = render_compute_research_state(populated_state)
        assert "# Conventions" in text
        assert "Natural units" in text

    def test_hypotheses_included(self, populated_state):
        text = render_compute_research_state(populated_state)
        assert "## ER-001" in text
        assert "## WH-002" in text

    def test_skips_empty_dead_ends(self, empty_state):
        text = render_compute_research_state(empty_state)
        assert "# Dead Ends" not in text

    def test_includes_populated_dead_ends(self, populated_state):
        text = render_compute_research_state(populated_state)
        assert "# Dead Ends" in text
        assert "Abandoned WH-003" in text

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
# render_research_plan
# ===========================================================================

class TestRenderResearchPlan:

    def _make_plan_state(self):
        state = ResearchState(problem_statement="Test problem")
        state.research_plan = ResearchPlan(
            sub_problems={
                "SP-001": SubProblem(
                    id="SP-001",
                    description="Derive surface gravity",
                    approach="Killing vector method",
                    alternatives=["Euclidean method"],
                    depends_on=[],
                    status="open",
                    initial_rqs=["RQ-001"],
                    notes="Standard first step",
                ),
                "SP-002": SubProblem(
                    id="SP-002",
                    description="Apply first law",
                    approach="T = kappa / (2 pi)",
                    depends_on=["SP-001"],
                    status="in_progress",
                ),
            },
            strategy_summary="Derive Hawking temperature via surface gravity.",
            known_pitfalls=["Don't confuse coordinate and invariant quantities."],
            iteration_created=0,
            iteration_updated=0,
        )
        return state

    def test_render_research_plan_with_sub_problems(self):
        state = self._make_plan_state()
        text = render_research_plan(state)
        assert "# Research Plan" in text
        assert "Derive Hawking temperature via surface gravity." in text
        assert "SP-001" in text
        assert "[OPEN]" in text
        assert "Derive surface gravity" in text
        assert "**Approach:** Killing vector method" in text
        assert "**Alternatives:** Euclidean method" in text
        assert "SP-002" in text
        assert "[IN_PROGRESS]" in text
        assert "**Depends on:** SP-001" in text

    def test_research_plan_none_renders_no_plan(self):
        state = ResearchState(problem_statement="Test")
        text = render_research_plan(state)
        assert text == "(No research plan.)"

    def test_research_plan_rendered_in_orchestrator_context(self):
        state = self._make_plan_state()
        text = render_orchestrator_research_state(state)
        assert "# Research Plan" in text
        assert "SP-001" in text
        assert "SP-002" in text

    def test_research_plan_not_in_compute_context(self):
        state = self._make_plan_state()
        text = render_compute_research_state(state)
        assert "# Research Plan" not in text

    def test_research_plan_none_renders_nothing_in_orchestrator(self):
        """When plan is None, no research plan section in orchestrator context."""
        state = ResearchState(problem_statement="Test")
        text = render_orchestrator_research_state(state)
        assert "# Research Plan" not in text
