"""Tests for orchestrator agent response parsing and integration."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from sciralph.agents.orchestrator import OrchestratorAgent
from sciralph.config import Config
from sciralph.llm import LLMResponse
from sciralph.metrics import MetricsTracker
from sciralph.research_state import (
    Computation,
    Critique,
    CritiqueStatus,
    Hypothesis,
    HypothesisStatus,
    ResearchState,
    Severity,
    Verdict,
)
from sciralph.task import Task, TaskType
from sciralph.workspace import WorkspaceManager

_EMPTY_TASK = Task(task_id="", task_type=TaskType.RESEARCH_EXPLORE, assigned_to="orchestrator")


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


TASK_TEXT = """---
task_id: TASK-002
task_type: compute_verify
assigned_to: compute_verify
priority: high
iteration: 2
---

# Task Description

Verify result B numerically.
"""


class TestParseTask:
    def test_bare_text(self, orchestrator):
        task = orchestrator.parse_task(TASK_TEXT)
        assert task.task_id == "TASK-002"
        assert task.task_type == "compute_verify"

    def test_parse_task_missing_id_uses_engine_iteration(self, orchestrator):
        text = "---\ntask_type: research_explore\nassigned_to: research_explore\npriority: high\n---\nDo something."
        task = orchestrator.parse_task(text, iteration=7)
        assert task.task_id == "TASK-007"
        assert task.iteration == 7

    def test_parse_task_present_id_preferred(self, orchestrator):
        text = "---\ntask_id: TASK-042\ntask_type: compute_verify\nassigned_to: compute_verify\npriority: high\niteration: 42\n---\nVerify."
        task = orchestrator.parse_task(text, iteration=5)
        assert task.task_id == "TASK-042"
        assert task.iteration == 42


class TestCompletionAnalysis:
    def test_triggers(self, orchestrator, workspace):
        rs = ResearchState()
        for i in range(1, 6):
            rs.hypotheses[f"ER-{i:03d}"] = Hypothesis(
                id=f"ER-{i:03d}", status=HypothesisStatus.ESTABLISHED,
            )
        orchestrator.research_state = rs
        result = orchestrator._completion_analysis()
        assert result is not None
        assert "COMPLETION CHECK" in result
        assert "terminate" in result
        assert "synthesize" not in result

    def test_not_triggered_with_wh(self, orchestrator, workspace):
        rs = ResearchState()
        for i in range(1, 4):
            rs.hypotheses[f"ER-{i:03d}"] = Hypothesis(
                id=f"ER-{i:03d}", status=HypothesisStatus.ESTABLISHED,
            )
        rs.hypotheses["WH-001"] = Hypothesis(id="WH-001", status=HypothesisStatus.WORKING)
        orchestrator.research_state = rs
        assert orchestrator._completion_analysis() is None

    def test_blocked_by_critiques(self, orchestrator, workspace):
        rs = ResearchState()
        for i in range(1, 4):
            rs.hypotheses[f"ER-{i:03d}"] = Hypothesis(
                id=f"ER-{i:03d}", status=HypothesisStatus.ESTABLISHED,
            )
        rs.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.HIGH, status=CritiqueStatus.ACTIVE,
        )
        orchestrator.research_state = rs
        assert orchestrator._completion_analysis() is None


class TestBudgetAwareTermination:
    """Test budget-aware synthesis triggers when iterations are running low."""

    def _make_state_with_wh(self):
        """State with 3 ERs and 1 WH (blocks normal completion)."""
        rs = ResearchState()
        for i in range(1, 4):
            rs.hypotheses[f"ER-{i:03d}"] = Hypothesis(
                id=f"ER-{i:03d}", status=HypothesisStatus.ESTABLISHED,
            )
        rs.hypotheses["WH-001"] = Hypothesis(id="WH-001", status=HypothesisStatus.WORKING)
        return rs

    def test_budget_banner_when_low(self, workspace):
        """Budget synthesis banner fires when <=3 iterations remain, even with WHs."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        orch.research_state = self._make_state_with_wh()

        # iteration 18 of 20 -> 2 remaining -> should fire
        result = orch._completion_analysis(iteration=18)
        assert result is not None
        assert "BUDGET SYNTHESIS REQUIRED" in result
        assert "2 iteration(s) remaining" in result

    def test_no_budget_banner_when_plenty_remaining(self, workspace):
        """No budget banner when >3 iterations remain."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        orch.research_state = self._make_state_with_wh()

        # iteration 10 of 20 -> 10 remaining -> should NOT fire
        assert orch._completion_analysis(iteration=10) is None

    def test_budget_banner_with_unresolved_critiques(self, workspace):
        """Budget synthesis fires even with unresolved HIGH critiques."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        rs = self._make_state_with_wh()
        rs.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.HIGH, status=CritiqueStatus.ACTIVE,
        )
        orch.research_state = rs

        result = orch._completion_analysis(iteration=19)
        assert result is not None
        assert "BUDGET SYNTHESIS REQUIRED" in result
        assert "1 HIGH" in result

    def test_completion_check_takes_priority_over_budget(self, workspace):
        """Normal completion check fires when all conditions met, not budget banner."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        rs = ResearchState()
        for i in range(1, 4):
            rs.hypotheses[f"ER-{i:03d}"] = Hypothesis(
                id=f"ER-{i:03d}", status=HypothesisStatus.ESTABLISHED,
            )
        orch.research_state = rs

        # No WHs, no critiques, <=3 remaining -> normal completion check wins
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
        rs = ResearchState()
        rs.hypotheses["WH-001"] = Hypothesis(id="WH-001", status=HypothesisStatus.WORKING)
        orch.research_state = rs

        assert orch._completion_analysis(iteration=19) is None

    def test_context_includes_iteration_budget(self, workspace):
        """build_context always shows iteration X of Y (Z remaining)."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        orch.research_state = ResearchState()

        context = orch.build_context(_EMPTY_TASK, iteration=5)
        assert "5 of 20" in context
        assert "15 remaining" in context


class TestConventionReminder:
    """Test the gentle nudge when the Conventions section is still placeholder."""

    def test_convention_reminder_at_iteration_3(self, orchestrator, workspace):
        orchestrator.research_state = ResearchState()  # conventions is empty by default
        context = orchestrator.build_context(_EMPTY_TASK, iteration=3)
        assert "REMINDER" in context
        assert "Conventions" in context

    def test_no_reminder_when_conventions_populated(self, orchestrator, workspace):
        rs = ResearchState()
        rs.conventions = "- Natural units: h = c = k_B = 1\n- Metric signature: (-, +, +, +)"
        orchestrator.research_state = rs
        context = orchestrator.build_context(_EMPTY_TASK, iteration=5)
        assert "REMINDER" not in context

    def test_no_reminder_at_iteration_1(self, orchestrator, workspace):
        orchestrator.research_state = ResearchState()  # conventions is empty by default
        context = orchestrator.build_context(_EMPTY_TASK, iteration=1)
        assert "REMINDER" not in context


class TestStallBannerInContext:
    """Test that computation stall banners appear in orchestrator context."""

    def test_stall_banner_in_context(self, workspace):
        """3 consecutive INCONCLUSIVE (>= stall_threshold=2) -> banner in context."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        # Set up research state with stalled computations
        rs = ResearchState()
        for i in range(1, 4):
            rs.computations[f"COMP-{i:03d}"] = Computation(
                id=f"COMP-{i:03d}", target_hypothesis="WH-002",
                verdict=Verdict.INCONCLUSIVE, kind="verify",
                claim="Verify WH-002 partition function", iteration=i,
            )
        orch.research_state = rs

        context = orch.build_context(_EMPTY_TASK, iteration=5)
        assert "COMPUTATION STALL" in context
        assert "WH-002" in context
        assert "3 consecutive failures" in context

    def test_no_stall_banner_below_threshold(self, workspace):
        """1 failure (< stall_threshold=2) -> no banner."""
        config = Config(workspace_dir=str(workspace.root), max_iterations=20)
        metrics = MetricsTracker()
        orch = OrchestratorAgent(config, workspace, metrics)
        rs = ResearchState()
        rs.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-002",
            verdict=Verdict.INCONCLUSIVE, kind="verify", iteration=1,
        )
        orch.research_state = rs

        context = orch.build_context(_EMPTY_TASK, iteration=5)
        assert "COMPUTATION STALL" not in context


class TestSystemPrompt:
    """Test that system_prompt includes the problem statement."""

    def test_system_prompt_includes_problem_statement(self, orchestrator):
        orchestrator.research_state = ResearchState(
            problem_statement="Derive the Hawking temperature for a Schwarzschild black hole.",
        )
        prompt = orchestrator.system_prompt
        assert "## Problem Statement" in prompt
        assert "Derive the Hawking temperature" in prompt

    def test_system_prompt_without_research_state(self, orchestrator):
        # No research_state set — should still return the base prompt
        prompt = orchestrator.system_prompt
        assert "## Problem Statement" not in prompt

    def test_system_prompt_cached(self, orchestrator):
        orchestrator.research_state = ResearchState(
            problem_statement="Test problem.",
        )
        first = orchestrator.system_prompt
        second = orchestrator.system_prompt
        assert first is second  # same object, not recomputed


class TestNoMetricsInContext:
    """Verify METRICS.md is no longer included in orchestrator context."""

    def test_no_metrics_in_context(self, orchestrator):
        orchestrator.research_state = ResearchState()
        context = orchestrator.build_context(_EMPTY_TASK, iteration=1)
        assert "METRICS" not in context
