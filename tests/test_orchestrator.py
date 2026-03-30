"""Tests for orchestrator agent response parsing and integration."""

import pytest

from sciralph.agents.orchestrator import OrchestratorAgent
from sciralph.config import Config
from sciralph.metrics import MetricsTracker
from sciralph.research_state import ResearchState
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


TASK_TEXT = """---
task_id: TASK-002
task_type: review
assigned_to: reviewer
priority: high
iteration: 2
---

# Task Description

Verify result B.
"""


class TestParseTask:
    def test_bare_text(self, orchestrator):
        task = orchestrator.parse_task(TASK_TEXT)
        assert task.task_id == "TASK-002"
        assert task.task_type == "review"

    def test_parse_task_missing_id_uses_engine_iteration(self, orchestrator):
        text = "---\ntask_type: research\nassigned_to: researcher\npriority: high\n---\nDo something."
        task = orchestrator.parse_task(text, iteration=7)
        assert task.task_id == "TASK-007"
        assert task.iteration == 7

    def test_parse_task_present_id_preferred(self, orchestrator):
        text = "---\ntask_id: TASK-042\ntask_type: review\nassigned_to: reviewer\npriority: high\niteration: 42\n---\nVerify."
        task = orchestrator.parse_task(text, iteration=5)
        assert task.task_id == "TASK-042"
        assert task.iteration == 42



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


class TestProblemStatementInContext:
    """Test that problem statement appears in user message (build_context), not system prompt."""

    def test_problem_statement_in_context(self, orchestrator):
        orchestrator.research_state = ResearchState(
            problem_statement="Derive the Hawking temperature for a Schwarzschild black hole.",
        )
        context = orchestrator.build_context(_EMPTY_TASK, iteration=1)
        assert "<problem-statement>" in context
        assert "Derive the Hawking temperature" in context

    def test_problem_statement_not_in_system_prompt(self, orchestrator):
        orchestrator.research_state = ResearchState(
            problem_statement="Derive the Hawking temperature for a Schwarzschild black hole.",
        )
        prompt = orchestrator.system_prompt
        assert "<problem-statement>" not in prompt

    def test_no_problem_statement_without_research_state(self, orchestrator):
        context = orchestrator.build_context(_EMPTY_TASK, iteration=1)
        assert "<problem-statement>" not in context

    def test_problem_statement_present_with_survey(self, orchestrator):
        orchestrator.research_state = ResearchState(
            problem_statement="Test problem.",
            survey_background="Some survey notes.",
        )
        context = orchestrator.build_context(_EMPTY_TASK, iteration=1)
        assert "<problem-statement>" in context
        # Survey is now included so orchestrator can relay relevant parts to agents
        assert "<survey-background>" in context


class TestNoMetricsInContext:
    """Verify METRICS.md is no longer included in orchestrator context."""

    def test_no_metrics_in_context(self, orchestrator):
        orchestrator.research_state = ResearchState()
        context = orchestrator.build_context(_EMPTY_TASK, iteration=1)
        assert "METRICS" not in context


class TestSuffixPlacement:
    """Verify context_suffix appears after research state in build_context."""

    def test_suffix_appears_after_research_state(self, orchestrator):
        """context_suffix should be appended at the end, after research state and notes."""
        orchestrator.research_state = ResearchState()
        orchestrator.context_suffix = ">>> EVIDENCE RESULTS (previous iteration) <<<"
        context = orchestrator.build_context(_EMPTY_TASK, iteration=1)

        assert "EVIDENCE RESULTS" in context
        # Suffix must appear after the strategy section (slim state)
        strat_pos = context.index("</strategy>")
        suffix_pos = context.index("EVIDENCE RESULTS")
        assert suffix_pos > strat_pos

    def test_suffix_consumed_after_use(self, orchestrator):
        """context_suffix is cleared after build_context consumes it."""
        orchestrator.research_state = ResearchState()
        orchestrator.context_suffix = ">>> TEST BANNER <<<"
        orchestrator.build_context(_EMPTY_TASK, iteration=1)
        assert orchestrator.context_suffix == ""

    def test_suffix_after_research_notes(self, orchestrator):
        """context_suffix appears after research notes."""
        rs = ResearchState()
        rs.research_notes = [{"iteration": 1, "text": "Found a key relation."}]
        orchestrator.research_state = rs
        orchestrator.context_suffix = ">>> VERIFIED HYPOTHESES <<<"
        context = orchestrator.build_context(_EMPTY_TASK, iteration=1)

        notes_pos = context.index("research-notes")
        suffix_pos = context.index("VERIFIED HYPOTHESES")
        assert suffix_pos > notes_pos
