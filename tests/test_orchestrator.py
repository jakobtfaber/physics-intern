"""Tests for orchestrator agent response parsing and integration."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from sciralph.agents.orchestrator import OrchestratorAgent, _split_response
from sciralph.config import Config
from sciralph.llm import LLMResponse
from sciralph.metrics import MetricsTracker
from sciralph.task import Task, TaskType
from sciralph.workspace import WorkspaceManager

_EMPTY_TASK = Task(task_id="", task_type=TaskType.RESEARCH, assigned_to="orchestrator")


@pytest.fixture
def workspace(tmp_path):
    """Create a WorkspaceManager with a real temp directory (no git)."""
    config = Config(workspace_dir=str(tmp_path))
    ws = WorkspaceManager(config)
    ws.root.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def orchestrator(workspace):
    config = Config(workspace_dir=str(workspace.root))
    metrics = MetricsTracker()
    return OrchestratorAgent(config, workspace, metrics)


RESEARCH_STATE = """---
problem_id: test
status: in_progress
---

# Established Results

Result A is proven.
"""

TASK_TEXT = """---
task_id: TASK-002
task_type: compute
assigned_to: computationalist
priority: high
iteration: 2
---

# Task Description

Verify result B numerically.
"""


class TestSplitResponse:
    def test_both_sections(self):
        text = f"=== RESEARCH_STATE.md ===\n{RESEARCH_STATE}\n=== CURRENT_TASK.md ===\n{TASK_TEXT}"
        rs, task = _split_response(text)
        assert rs is not None
        assert "Result A is proven" in rs
        assert "TASK-002" in task

    def test_task_only(self):
        text = f"=== CURRENT_TASK.md ===\n{TASK_TEXT}"
        rs, task = _split_response(text)
        assert rs is None
        assert "TASK-002" in task

    def test_no_delimiters(self):
        rs, task = _split_response(TASK_TEXT)
        assert rs is None
        assert "TASK-002" in task


class TestProcessResponse:
    def test_with_integration(self, orchestrator, workspace):
        # Set up a PROPOSED_CHANGES.md that should be deleted after integration
        workspace.write_file("PROPOSED_CHANGES.md", "some proposed changes")

        response = LLMResponse(
            text=f"=== RESEARCH_STATE.md ===\n{RESEARCH_STATE}\n=== CURRENT_TASK.md ===\n{TASK_TEXT}",
            input_tokens=0, output_tokens=0, stop_reason="end_turn", duration=0.0,
        )
        orchestrator.process_response(response, _EMPTY_TASK,2)

        assert "Result A is proven" in workspace.read_file("RESEARCH_STATE.md")
        assert "TASK-002" in workspace.read_file("CURRENT_TASK.md")
        assert not workspace.file_exists("PROPOSED_CHANGES.md")

    def test_task_only(self, orchestrator, workspace):
        # No PROPOSED_CHANGES.md exists
        workspace.write_file("RESEARCH_STATE.md", "original state")

        response = LLMResponse(
            text=f"=== CURRENT_TASK.md ===\n{TASK_TEXT}",
            input_tokens=0, output_tokens=0, stop_reason="end_turn", duration=0.0,
        )
        orchestrator.process_response(response, _EMPTY_TASK,1)

        assert workspace.read_file("RESEARCH_STATE.md") == "original state"
        assert "TASK-002" in workspace.read_file("CURRENT_TASK.md")

    def test_no_delimiters_backward_compat(self, orchestrator, workspace):
        response = LLMResponse(
            text=TASK_TEXT,
            input_tokens=0, output_tokens=0, stop_reason="end_turn", duration=0.0,
        )
        orchestrator.process_response(response, _EMPTY_TASK,1)

        assert "TASK-002" in workspace.read_file("CURRENT_TASK.md")


class TestParseTask:
    def test_with_delimiters(self, orchestrator):
        text = f"=== RESEARCH_STATE.md ===\n{RESEARCH_STATE}\n=== CURRENT_TASK.md ===\n{TASK_TEXT}"
        task = orchestrator.parse_task(text)
        assert task.task_id == "TASK-002"
        assert task.task_type == "compute"
        assert task.assigned_to == "computationalist"

    def test_bare_text(self, orchestrator):
        task = orchestrator.parse_task(TASK_TEXT)
        assert task.task_id == "TASK-002"
        assert task.task_type == "compute"

    def test_parse_task_missing_id_uses_engine_iteration(self, orchestrator):
        text = "---\ntask_type: research\nassigned_to: researcher\npriority: high\n---\nDo something."
        task = orchestrator.parse_task(text, iteration=7)
        assert task.task_id == "TASK-007"
        assert task.iteration == 7

    def test_parse_task_present_id_preferred(self, orchestrator):
        text = "---\ntask_id: TASK-042\ntask_type: compute\nassigned_to: computationalist\npriority: high\niteration: 42\n---\nVerify."
        task = orchestrator.parse_task(text, iteration=5)
        assert task.task_id == "TASK-042"
        assert task.iteration == 42


class TestCompletionAnalysis:
    def test_triggers(self, orchestrator, workspace):
        state = "---\nstatus: in_progress\n---\n\n## ER-001\nA\n## ER-002\nB\n## ER-003\nC\n## ER-004\nD\n## ER-005\nE\n"
        critique = "---\nunresolved_high: 0\nunresolved_medium: 0\n---\nNo issues.\n"
        workspace.write_file("RESEARCH_STATE.md", state)
        workspace.write_file("CRITIQUE_LOG.md", critique)
        result = orchestrator._completion_analysis()
        assert result is not None
        assert "COMPLETION CHECK" in result
        assert "terminate" in result
        assert "synthesize" not in result

    def test_not_triggered_with_wh(self, orchestrator, workspace):
        state = "---\nstatus: in_progress\n---\n\n## ER-001\nA\n## ER-002\nB\n## ER-003\nC\n## WH-001\nPending\n"
        critique = "---\nunresolved_high: 0\nunresolved_medium: 0\n---\n"
        workspace.write_file("RESEARCH_STATE.md", state)
        workspace.write_file("CRITIQUE_LOG.md", critique)
        assert orchestrator._completion_analysis() is None

    def test_blocked_by_critiques(self, orchestrator, workspace):
        state = "---\nstatus: in_progress\n---\n\n## ER-001\nA\n## ER-002\nB\n## ER-003\nC\n"
        critique = "---\nunresolved_high: 1\nunresolved_medium: 0\n---\n"
        workspace.write_file("RESEARCH_STATE.md", state)
        workspace.write_file("CRITIQUE_LOG.md", critique)
        assert orchestrator._completion_analysis() is None


class TestBudgetAwareTermination:
    """Test budget-aware synthesis triggers when iterations are running low."""

    STATE_WITH_WH = (
        "---\nstatus: in_progress\n---\n\n"
        "## ER-001\nA\n## ER-002\nB\n## ER-003\nC\n## WH-001\nPending\n"
    )
    CRITIQUE_WITH_UNRESOLVED = (
        "---\nunresolved_high: 1\nunresolved_medium: 0\n---\n"
    )
    CRITIQUE_CLEAN = "---\nunresolved_high: 0\nunresolved_medium: 0\n---\n"

    def test_budget_banner_when_low(self, workspace):
        """Budget synthesis banner fires when ≤3 iterations remain, even with WHs."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        workspace.write_file("RESEARCH_STATE.md", self.STATE_WITH_WH)
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_CLEAN)

        # iteration 18 of 20 → 2 remaining → should fire
        result = orch._completion_analysis(iteration=18)
        assert result is not None
        assert "BUDGET SYNTHESIS REQUIRED" in result
        assert "2 iteration(s) remaining" in result

    def test_no_budget_banner_when_plenty_remaining(self, workspace):
        """No budget banner when >3 iterations remain."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        workspace.write_file("RESEARCH_STATE.md", self.STATE_WITH_WH)
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_CLEAN)

        # iteration 10 of 20 → 10 remaining → should NOT fire
        assert orch._completion_analysis(iteration=10) is None

    def test_budget_banner_with_unresolved_critiques(self, workspace):
        """Budget synthesis fires even with unresolved HIGH critiques."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        workspace.write_file("RESEARCH_STATE.md", self.STATE_WITH_WH)
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_WITH_UNRESOLVED)

        result = orch._completion_analysis(iteration=19)
        assert result is not None
        assert "BUDGET SYNTHESIS REQUIRED" in result
        assert "1 HIGH" in result

    def test_completion_check_takes_priority_over_budget(self, workspace):
        """Normal completion check fires when all conditions met, not budget banner."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        state = "---\nstatus: in_progress\n---\n\n## ER-001\nA\n## ER-002\nB\n## ER-003\nC\n"
        workspace.write_file("RESEARCH_STATE.md", state)
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_CLEAN)

        # No WHs, no critiques, ≤3 remaining → normal completion check wins
        result = orch._completion_analysis(iteration=18)
        assert result is not None
        assert "COMPLETION CHECK" in result
        assert "BUDGET" not in result
        assert "terminate" in result
        assert "synthesize" not in result

    def test_no_budget_banner_without_established_results(self, workspace):
        """Budget banner requires at least 1 ER (nothing to synthesize otherwise)."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        state = "---\nstatus: in_progress\n---\n\n## WH-001\nPending\n"
        workspace.write_file("RESEARCH_STATE.md", state)
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_CLEAN)

        assert orch._completion_analysis(iteration=19) is None

    def test_context_includes_iteration_budget(self, workspace):
        """build_context always shows iteration X of Y (Z remaining)."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        workspace.write_file("RESEARCH_STATE.md", "---\nstatus: in_progress\n---\n\nNothing yet.\n")
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_CLEAN)
        workspace.write_file("COMPUTATION_LOG.md", "---\n---\n")
        workspace.write_file("METRICS.md", "---\n---\n")

        context = orch.build_context(_EMPTY_TASK,iteration=5)
        assert "5 of 20" in context
        assert "15 remaining" in context


class TestConventionReminder:
    """Test the gentle nudge when the Conventions section is still placeholder."""

    RESEARCH_STATE_PLACEHOLDER = (
        "---\nproblem_id: test\nstatus: in_progress\n---\n\n"
        "# Problem Statement\n\nDerive something.\n\n"
        "# Conventions\n\n(To be populated by the orchestrator as conventions become clear.)\n\n"
        "# Established Results\n\nNone yet.\n"
    )

    RESEARCH_STATE_POPULATED = (
        "---\nproblem_id: test\nstatus: in_progress\n---\n\n"
        "# Problem Statement\n\nDerive something.\n\n"
        "# Conventions\n\n- Natural units: ħ = c = k_B = 1\n- Metric signature: (−, +, +, +)\n\n"
        "# Established Results\n\nNone yet.\n"
    )

    def test_convention_reminder_at_iteration_3(self, orchestrator, workspace):
        workspace.write_file("RESEARCH_STATE.md", self.RESEARCH_STATE_PLACEHOLDER)
        workspace.write_file("CRITIQUE_LOG.md", "---\nunresolved_high: 0\nunresolved_medium: 0\n---\n")
        workspace.write_file("METRICS.md", "---\n---\n")
        context = orchestrator.build_context(_EMPTY_TASK,iteration=3)
        assert "REMINDER" in context
        assert "Conventions" in context

    def test_no_reminder_when_conventions_populated(self, orchestrator, workspace):
        workspace.write_file("RESEARCH_STATE.md", self.RESEARCH_STATE_POPULATED)
        workspace.write_file("CRITIQUE_LOG.md", "---\nunresolved_high: 0\nunresolved_medium: 0\n---\n")
        workspace.write_file("METRICS.md", "---\n---\n")
        context = orchestrator.build_context(_EMPTY_TASK,iteration=5)
        assert "REMINDER" not in context

    def test_no_reminder_at_iteration_1(self, orchestrator, workspace):
        workspace.write_file("RESEARCH_STATE.md", self.RESEARCH_STATE_PLACEHOLDER)
        workspace.write_file("CRITIQUE_LOG.md", "---\nunresolved_high: 0\nunresolved_medium: 0\n---\n")
        workspace.write_file("METRICS.md", "---\n---\n")
        context = orchestrator.build_context(_EMPTY_TASK,iteration=1)
        assert "REMINDER" not in context


class TestCritiqueResolution:
    """Test that the orchestrator resolves critiques in CRITIQUE_LOG.md."""

    CRITIQUE_LOG = """---
total_critiques: 2
unresolved_high: 1
unresolved_medium: 1
unresolved_low: 0
last_critic_pass: "2026-03-07T14:20:00Z"
---

# Active Critiques

## CRIT-001 [HIGH] [UNRESOLVED]
- **Target:** WH-1
- **Filed:** iteration 2
- **Critique:** Needs verification.

## CRIT-002 [MEDIUM] [UNRESOLVED]
- **Target:** WH-2
- **Filed:** iteration 2
- **Critique:** Missing justification.

# Resolved Critiques
"""

    def test_resolves_via_list(self, orchestrator, workspace):
        """Orchestrator output with resolved_critiques list updates CRITIQUE_LOG."""
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_LOG)
        workspace.write_file("RESEARCH_STATE.md",
            "---\nproblem_id: test\nstatus: in_progress\niteration: 3\n---\n\n# Results\n")

        response_text = (
            "=== RESEARCH_STATE.md ===\n"
            "---\nproblem_id: test\nstatus: in_progress\niteration: 3\n"
            "resolved_critiques: [CRIT-001]\n---\n\n# Established Results\n## ER-001\nDone.\n"
            "\n=== CURRENT_TASK.md ===\n"
            "---\ntask_id: TASK-003\ntask_type: compute\nassigned_to: computationalist\npriority: high\niteration: 3\n---\nVerify.\n"
        )
        response = LLMResponse(
            text=response_text,
            input_tokens=0, output_tokens=0, stop_reason="end_turn", duration=0.0,
        )
        orchestrator.process_response(response, _EMPTY_TASK,3)

        critique_log = workspace.read_file("CRITIQUE_LOG.md")
        from sciralph.markdown import count_unresolved_critiques
        counts = count_unresolved_critiques(critique_log)
        assert counts["HIGH"] == 0, "CRIT-001 should be resolved"
        assert counts["MEDIUM"] == 1, "CRIT-002 should still be unresolved"

    def test_resolves_via_prose(self, orchestrator, workspace):
        """Orchestrator prose mentioning 'CRIT-002 addressed' triggers resolution."""
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_LOG)
        workspace.write_file("RESEARCH_STATE.md",
            "---\nproblem_id: test\nstatus: in_progress\niteration: 3\n---\n\n# Results\n")

        response_text = (
            "=== RESEARCH_STATE.md ===\n"
            "---\nproblem_id: test\nstatus: in_progress\niteration: 3\n---\n"
            "\n# Established Results\nCRIT-002 addressed by new derivation.\n"
            "\n=== CURRENT_TASK.md ===\n"
            "---\ntask_id: TASK-003\ntask_type: compute\nassigned_to: computationalist\npriority: high\niteration: 3\n---\nVerify.\n"
        )
        response = LLMResponse(
            text=response_text,
            input_tokens=0, output_tokens=0, stop_reason="end_turn", duration=0.0,
        )
        orchestrator.process_response(response, _EMPTY_TASK,3)

        critique_log = workspace.read_file("CRITIQUE_LOG.md")
        from sciralph.markdown import count_unresolved_critiques
        counts = count_unresolved_critiques(critique_log)
        assert counts["MEDIUM"] == 0, "CRIT-002 should be resolved via prose detection"
        assert counts["HIGH"] == 1, "CRIT-001 should still be unresolved"

    def test_no_resolution_when_no_research_state(self, orchestrator, workspace):
        """When orchestrator only emits CURRENT_TASK, no critique resolution happens."""
        workspace.write_file("CRITIQUE_LOG.md", self.CRITIQUE_LOG)
        workspace.write_file("RESEARCH_STATE.md", "original state")

        response = LLMResponse(
            text="=== CURRENT_TASK.md ===\n---\ntask_id: TASK-003\ntask_type: critique\nassigned_to: deep_critic\npriority: high\niteration: 3\n---\nReview.\n",
            input_tokens=0, output_tokens=0, stop_reason="end_turn", duration=0.0,
        )
        orchestrator.process_response(response, _EMPTY_TASK,3)

        critique_log = workspace.read_file("CRITIQUE_LOG.md")
        from sciralph.markdown import count_unresolved_critiques
        counts = count_unresolved_critiques(critique_log)
        assert counts["HIGH"] == 1
        assert counts["MEDIUM"] == 1


class TestStallBannerInContext:
    """Test that computation stall banners appear in orchestrator context."""

    COMP_LOG_WITH_STALL = """\
---
total_computations: 3
---

## COMP-001: Check WH-002
- **CLAIM**: Verify WH-002 partition function
- **VERDICT**: INCONCLUSIVE
- **RESULT**:
  Failed attempt 1.

## COMP-002: Retry WH-002
- **CLAIM**: Verify WH-002 partition function
- **VERDICT**: INCONCLUSIVE
- **RESULT**:
  Failed attempt 2.

## COMP-003: Retry WH-002 again
- **CLAIM**: Verify WH-002 partition function
- **VERDICT**: INCONCLUSIVE
- **RESULT**:
  Failed attempt 3.
"""

    COMP_LOG_BELOW_THRESHOLD = """\
---
total_computations: 1
---

## COMP-001: Check WH-002
- **CLAIM**: Verify WH-002 partition function
- **VERDICT**: INCONCLUSIVE
- **RESULT**:
  Failed attempt 1.
"""

    def test_stall_banner_in_context(self, workspace):
        """COMPUTATION_LOG with 3 failures (>= stall_threshold=2) -> banner in context."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        workspace.write_file("RESEARCH_STATE.md", "---\nstatus: in_progress\n---\n\nNothing yet.\n")
        workspace.write_file("CRITIQUE_LOG.md", "---\nunresolved_high: 0\nunresolved_medium: 0\n---\n")
        workspace.write_file("COMPUTATION_LOG.md", self.COMP_LOG_WITH_STALL)
        workspace.write_file("METRICS.md", "---\n---\n")

        context = orch.build_context(_EMPTY_TASK,iteration=5)
        assert "COMPUTATION STALL" in context
        assert "WH-002" in context
        assert "3 consecutive failures" in context

    def test_no_stall_banner_below_threshold(self, workspace):
        """1 failure (< stall_threshold=2) -> no banner."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        workspace.write_file("RESEARCH_STATE.md", "---\nstatus: in_progress\n---\n\nNothing yet.\n")
        workspace.write_file("CRITIQUE_LOG.md", "---\nunresolved_high: 0\nunresolved_medium: 0\n---\n")
        workspace.write_file("COMPUTATION_LOG.md", self.COMP_LOG_BELOW_THRESHOLD)
        workspace.write_file("METRICS.md", "---\n---\n")

        context = orch.build_context(_EMPTY_TASK,iteration=5)
        assert "COMPUTATION STALL" not in context


class TestResolveNoteValidation:
    """Tests for critique resolution text quality (Improvement 6D)."""

    def test_resolve_short_note_replaced(self):
        """Short notes (<20 chars) are replaced with structured fallback."""
        note = OrchestratorAgent._validate_resolution_note("OK.", "CRIT-001", 5)
        assert "iteration 5" in note
        assert len(note) >= 20

    def test_resolve_system_marker_replaced(self):
        """Notes containing system markers are replaced."""
        cases = [
            "[COMP-003:unverified] was checked",
            ">>> VIOLATION found <<<",
            "phantom reference detected",
            "[error] in processing",
        ]
        for case in cases:
            note = OrchestratorAgent._validate_resolution_note(case, "CRIT-001", 5)
            assert "iteration 5" in note, f"Marker not caught: {case}"

    def test_resolve_clean_note_preserved(self):
        """Clean resolution notes are preserved as-is."""
        clean = "Derivation corrected to include missing factor of 2 in normalization."
        note = OrchestratorAgent._validate_resolution_note(clean, "CRIT-001", 5)
        assert note == clean
