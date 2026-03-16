"""Tests for the renderers module (snapshot and context renderers)."""

import pytest

from sciralph.config import Config
from sciralph.markdown import parse_frontmatter
from sciralph.renderers import (
    render_computation_log_md,
    render_computationalist_context,
    render_critic_context,
    render_critique_log_md,
    render_orchestrator_context,
    render_research_state_md,
    render_researcher_context,
    render_task_md,
)
from sciralph.research_state import (
    Computation,
    Critique,
    CritiqueStatus,
    FailedApproach,
    Hypothesis,
    HypothesisStatus,
    ResearchState,
    Severity,
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
        open_questions="Is the greybody factor significant at leading order?",
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
        assigned_to="computationalist",
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


@pytest.fixture
def config():
    """Config with deterministic defaults for testing."""
    cfg = Config.__new__(Config)
    cfg.max_iterations = 20
    cfg.orchestrator_comp_log_tail = 5
    return cfg


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

    def test_open_questions_section(self, populated_state):
        md = render_research_state_md(populated_state)
        assert "# Open Questions" in md
        assert "greybody factor" in md

    def test_empty_state_valid_markdown(self, empty_state):
        md = render_research_state_md(empty_state)
        meta, body = parse_frontmatter(md)
        assert meta["iteration"] == 0
        assert "# Problem Statement" in body
        assert "# Conventions" in body
        assert "# Dead Ends" in body
        assert "(None yet.)" in body
        assert "# Open Questions" in body

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
        assert meta["assigned_to"] == "computationalist"
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
# render_orchestrator_context
# ===========================================================================

class TestRenderOrchestratorContext:

    def test_includes_context_prefix(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            context_prefix="ALERT: violations detected",
            config=config,
            iteration=5,
        )
        assert "ALERT: violations detected" in ctx

    def test_no_prefix_when_empty(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            context_prefix="",
            config=config,
            iteration=5,
        )
        # Should start with iteration info, not an empty prefix artifact
        lines = ctx.strip().splitlines()
        assert lines[0].startswith("# Current Iteration")

    def test_iteration_budget_info(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
        )
        assert "5 of 20" in ctx
        assert "15 remaining" in ctx

    def test_contains_research_state(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
        )
        assert "## RESEARCH_STATE.md" in ctx
        assert "# Problem Statement" in ctx

    def test_contains_critique_log(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
        )
        assert "## CRITIQUE_LOG.md" in ctx

    def test_contains_computation_log_tail(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
        )
        assert "## COMPUTATION_LOG.md" in ctx
        assert f"last {config.orchestrator_comp_log_tail} entries" in ctx

    def test_comp_log_tail_override(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
            comp_log_tail="## COMP-099: Custom tail",
        )
        assert "COMP-099: Custom tail" in ctx

    def test_comp_log_tail_auto_render(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
        )
        # Both computations should appear in the tail (only 2 entries, tail=5)
        assert "COMP-001" in ctx
        assert "COMP-002" in ctx

    def test_metrics_text_included(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
            metrics_text="Total tokens: 50000",
        )
        assert "## METRICS.md (summary)" in ctx
        assert "Total tokens: 50000" in ctx

    def test_proposed_changes_included(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
            proposed_changes="## Proposed\n\nAdd new hypothesis WH-004.",
        )
        assert "## PROPOSED_CHANGES.md (pending review)" in ctx
        assert "Add new hypothesis WH-004" in ctx

    def test_proposed_changes_omitted_when_empty(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=5,
            proposed_changes="",
        )
        assert "PROPOSED_CHANGES.md" not in ctx

    def test_budget_at_last_iteration(self, populated_state, config):
        ctx = render_orchestrator_context(
            populated_state,
            config=config,
            iteration=20,
        )
        assert "20 of 20" in ctx
        assert "0 remaining" in ctx


# ===========================================================================
# render_researcher_context
# ===========================================================================

class TestRenderResearcherContext:

    def test_includes_task(self, populated_state, sample_task):
        ctx = render_researcher_context(populated_state, sample_task)
        assert "## CURRENT_TASK.md" in ctx
        assert "TASK-005" in ctx

    def test_includes_research_state(self, populated_state, sample_task):
        ctx = render_researcher_context(populated_state, sample_task)
        assert "## RESEARCH_STATE.md" in ctx
        assert "# Problem Statement" in ctx
        assert "Hawking temperature" in ctx

    def test_resolve_task_includes_relevant_critiques(self, populated_state, resolve_task):
        ctx = render_researcher_context(populated_state, resolve_task)
        assert "## Relevant Critiques" in ctx
        assert "CRIT-001" in ctx
        assert "thermal equilibrium" in ctx

    def test_non_resolve_task_no_critique_section(self, populated_state, sample_task):
        ctx = render_researcher_context(populated_state, sample_task)
        assert "## Relevant Critiques" not in ctx

    def test_resolve_with_missing_critique_id(self, populated_state):
        task = Task(
            task_id="TASK-007",
            task_type=TaskType.RESEARCH_EXPLORE,
            assigned_to="research_explore",
            iteration=5,
            blocking_critiques=["CRIT-999"],  # does not exist
            body="Resolve non-existent critique.",
        )
        ctx = render_researcher_context(populated_state, task)
        assert "## Relevant Critiques" in ctx
        # CRIT-999 is not in state, so it should not appear in the critiques section
        crit_section_start = ctx.index("## Relevant Critiques")
        crit_section = ctx[crit_section_start:]
        assert "CRIT-999" not in crit_section

    def test_resolve_shows_severity_and_target(self, populated_state, resolve_task):
        ctx = render_researcher_context(populated_state, resolve_task)
        assert "[HIGH]" in ctx
        assert "**Target:** WH-002" in ctx


# ===========================================================================
# render_computationalist_context
# ===========================================================================

class TestRenderComputationalistContext:

    def test_includes_task(self, populated_state, sample_task):
        ctx = render_computationalist_context(populated_state, sample_task)
        assert "## CURRENT_TASK.md" in ctx
        assert "TASK-005" in ctx

    def test_includes_research_state(self, populated_state, sample_task):
        ctx = render_computationalist_context(populated_state, sample_task)
        assert "## Relevant Research State (excerpts)" in ctx
        assert "# Problem Statement" in ctx

    def test_includes_hypotheses(self, populated_state, sample_task):
        ctx = render_computationalist_context(populated_state, sample_task)
        assert "ER-001" in ctx
        assert "WH-002" in ctx


# ===========================================================================
# render_critic_context
# ===========================================================================

class TestRenderCriticContext:

    def test_includes_research_state(self, populated_state):
        ctx = render_critic_context(populated_state)
        assert "## RESEARCH_STATE.md" in ctx
        assert "# Problem Statement" in ctx

    def test_includes_computation_log(self, populated_state):
        ctx = render_critic_context(populated_state)
        assert "## COMPUTATION_LOG.md" in ctx
        assert "COMP-001" in ctx

    def test_includes_previous_critiques(self, populated_state):
        ctx = render_critic_context(populated_state)
        assert "## Your Previous Critiques (do not repeat)" in ctx
        assert "CRIT-001" in ctx
        assert "CRIT-002" in ctx

    def test_all_three_sections_present(self, populated_state):
        ctx = render_critic_context(populated_state)
        assert "## RESEARCH_STATE.md" in ctx
        assert "## COMPUTATION_LOG.md" in ctx
        assert "## Your Previous Critiques (do not repeat)" in ctx

    def test_section_ordering(self, populated_state):
        ctx = render_critic_context(populated_state)
        rs_pos = ctx.index("## RESEARCH_STATE.md")
        cl_pos = ctx.index("## COMPUTATION_LOG.md")
        crit_pos = ctx.index("## Your Previous Critiques")
        assert rs_pos < cl_pos < crit_pos
