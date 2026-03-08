"""Tests for orchestrator agent response parsing and integration."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from sciralph.agents.orchestrator import OrchestratorAgent, _split_response
from sciralph.config import Config
from sciralph.llm import LLMResponse
from sciralph.metrics import MetricsTracker
from sciralph.workspace import WorkspaceManager


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
        orchestrator.process_response(response, {}, 2)

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
        orchestrator.process_response(response, {}, 1)

        assert workspace.read_file("RESEARCH_STATE.md") == "original state"
        assert "TASK-002" in workspace.read_file("CURRENT_TASK.md")

    def test_no_delimiters_backward_compat(self, orchestrator, workspace):
        response = LLMResponse(
            text=TASK_TEXT,
            input_tokens=0, output_tokens=0, stop_reason="end_turn", duration=0.0,
        )
        orchestrator.process_response(response, {}, 1)

        assert "TASK-002" in workspace.read_file("CURRENT_TASK.md")


class TestParseTask:
    def test_with_delimiters(self, orchestrator):
        text = f"=== RESEARCH_STATE.md ===\n{RESEARCH_STATE}\n=== CURRENT_TASK.md ===\n{TASK_TEXT}"
        task = orchestrator.parse_task(text)
        assert task["task_id"] == "TASK-002"
        assert task["task_type"] == "compute"
        assert task["assigned_to"] == "computationalist"

    def test_bare_text(self, orchestrator):
        task = orchestrator.parse_task(TASK_TEXT)
        assert task["task_id"] == "TASK-002"
        assert task["task_type"] == "compute"

    def test_parse_task_missing_id_uses_engine_iteration(self, orchestrator):
        text = "---\ntask_type: research\nassigned_to: researcher\npriority: high\n---\nDo something."
        task = orchestrator.parse_task(text, iteration=7)
        assert task["task_id"] == "TASK-007"
        assert task["iteration"] == 7

    def test_parse_task_present_id_preferred(self, orchestrator):
        text = "---\ntask_id: TASK-042\ntask_type: compute\nassigned_to: computationalist\npriority: high\niteration: 42\n---\nVerify."
        task = orchestrator.parse_task(text, iteration=5)
        assert task["task_id"] == "TASK-042"
        assert task["iteration"] == 42


class TestCompletionAnalysis:
    def test_triggers(self, orchestrator, workspace):
        state = "---\nstatus: in_progress\n---\n\n## ER-001\nA\n## ER-002\nB\n## ER-003\nC\n## ER-004\nD\n## ER-005\nE\n"
        critique = "---\nunresolved_high: 0\nunresolved_medium: 0\n---\nNo issues.\n"
        workspace.write_file("RESEARCH_STATE.md", state)
        workspace.write_file("CRITIQUE_LOG.md", critique)
        result = orchestrator._completion_analysis()
        assert result is not None
        assert "COMPLETION CHECK" in result

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
