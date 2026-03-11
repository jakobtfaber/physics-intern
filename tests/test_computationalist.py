"""Tests for computationalist agent response parsing."""

import tempfile
from unittest.mock import MagicMock

from sciralph.agents.computationalist import ComputationalistAgent
from sciralph.llm import AgentResult
from sciralph.sandbox import execute_python
from sciralph.task import Task, TaskType
from sciralph.tools import ToolCall, ToolExecutor


class TestSoftCheckPattern:
    """Test that the soft-check pattern exits 0 and genuine crashes exit nonzero."""

    def test_soft_check_pattern_exits_zero(self):
        """Script with the new soft-check pattern (some checks fail) exits 0."""
        script = """\
import numpy as np

results = []
test_points = [("a", 1.0, 1.0), ("b", 1.0, 2.0), ("c", 3.0, 3.0)]
for name, lhs, rhs in test_points:
    try:
        ok = np.isclose(lhs, rhs, rtol=1e-6)
        results.append(ok)
        status = "PASS" if ok else "FAIL"
        print(f"{status}: {name} -> lhs={lhs}, rhs={rhs}")
    except Exception as e:
        results.append(False)
        print(f"ERROR: {name} -> {e}")
n_passed = sum(results)
n_total = len(results)
print(f"\\nCHECKS: {n_passed}/{n_total} PASSED")
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(script)
            f.flush()
            result = execute_python(f.name)
        assert result.returncode == 0
        assert "CHECKS: 2/3 PASSED" in result.stdout
        assert "FAIL:" in result.stdout

    def test_genuine_crash_exits_nonzero(self):
        """Script with an ImportError exits with nonzero returncode."""
        script = "import nonexistent_module_xyz\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(script)
            f.flush()
            result = execute_python(f.name)
        assert result.returncode != 0


class TestToolsAttribute:
    def test_computationalist_has_tools(self):
        assert ComputationalistAgent.tools
        assert len(ComputationalistAgent.tools) == 1
        assert ComputationalistAgent.tools[0]["name"] == "execute_python"

    def test_other_agents_no_tools(self):
        from sciralph.agents.orchestrator import OrchestratorAgent
        from sciralph.agents.researcher import ResearcherAgent
        from sciralph.agents.critic import CriticAgent
        assert OrchestratorAgent.tools == []
        assert ResearcherAgent.tools == []
        assert CriticAgent.tools == []


def _make_agent():
    """Create a ComputationalistAgent with mocked dependencies."""
    config = MagicMock()
    config.sympy_timeout_seconds = 10
    config.tool_output_limit = 10_000
    workspace = MagicMock()
    workspace.root = MagicMock()
    workspace.computations_dir = "/tmp"
    metrics = MagicMock()
    return ComputationalistAgent(config=config, workspace=workspace, metrics=metrics)


class TestAgenticResponse:
    def test_process_agentic_response_appends_to_log(self):
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        result = AgentResult(
            text=(
                "## COMP-020: Test\n\n"
                "**CLAIM:** x = 1\n"
                "**VERDICT:** VERIFIED\n"
                "**NOTES:** All checks passed."
            ),
            tool_calls=[
                ToolCall("execute_python", {"code": "print(1)"}, "1\n", False, 0.5),
            ],
            total_input_tokens=500,
            total_output_tokens=200,
            rounds=2,
        )

        agent.process_response(result, Task(task_id="COMP-020", task_type=TaskType.COMPUTE, assigned_to="computationalist"), iteration=7)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "**VERDICT:** VERIFIED" in appended_text
        assert "**Iteration:** 7" in appended_text
        assert "**Tool calls:** 1" in appended_text

    def test_process_agentic_response_adds_header(self):
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        result = AgentResult(
            text="**CLAIM:** x = 1\n**VERDICT:** VERIFIED\n**NOTES:** OK.",
            tool_calls=[],
            total_input_tokens=300,
            total_output_tokens=100,
            rounds=1,
        )

        agent.process_response(result, Task(task_id="TASK-005", task_type=TaskType.COMPUTE, assigned_to="computationalist"), iteration=5)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "## TASK-005" in appended_text
        # Header should have been added since text didn't start with ##
        assert "## TASK-005: Computation" in appended_text

    def test_process_agentic_response_empty_text(self):
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        result = AgentResult(
            text="",
            tool_calls=[],
            total_input_tokens=100,
            total_output_tokens=10,
            rounds=1,
        )

        agent.process_response(result, Task(task_id="TASK-010", task_type=TaskType.COMPUTE, assigned_to="computationalist"), iteration=10)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "INCONCLUSIVE" in appended_text
        assert "**CLAIM:** unknown" in appended_text

    def test_process_empty_text_extracts_claim_from_task_body(self):
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        result = AgentResult(
            text="",
            tool_calls=[],
            total_input_tokens=100,
            total_output_tokens=10,
            rounds=1,
        )

        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE,
            assigned_to="computationalist",
            body="Verify WH-005 Maslov phase for ωT > π",
        )
        agent.process_response(result, task, iteration=5)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "**CLAIM:** WH-005" in appended_text
        assert "INCONCLUSIVE" in appended_text
