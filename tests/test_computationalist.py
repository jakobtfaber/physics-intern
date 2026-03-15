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
        assert len(ComputationalistAgent.tools) == 3
        names = {t["function"]["name"] for t in ComputationalistAgent.tools}
        assert names == {"execute_python", "submit_verdict", "report_progress"}

    def test_other_agents_no_tools(self):
        from sciralph.agents.researcher import ResearcherAgent
        from sciralph.agents.critic import CriticAgent
        assert ResearcherAgent.tools == []
        assert CriticAgent.tools == []

    def test_orchestrator_has_state_mutation_tools(self):
        from sciralph.agents.orchestrator import OrchestratorAgent
        assert len(OrchestratorAgent.tools) > 0
        tool_names = {t["function"]["name"] for t in OrchestratorAgent.tools}
        assert "set_next_task" in tool_names
        assert "add_hypothesis" in tool_names


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


class TestSubmitVerdictProcessing:
    """Test that process_response extracts data from submit_verdict tool calls."""

    def test_process_response_uses_submit_verdict_data(self):
        """Empty text + submit_verdict tool call → formatted COMP entry."""
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        verdict_params = {
            "claim": "WH-003 — entropy scales as area",
            "method": "Numerical spot-checks at 5 test points",
            "result": "All 5 checks pass within rtol=1e-6",
            "verdict": "VERIFIED",
            "notes": "Entropy-area proportionality confirmed.",
        }
        result = AgentResult(
            text="",  # no free text — verdict is in tool call
            tool_calls=[
                ToolCall("execute_python", {"code": "print(1)"}, "1\n", False, 0.5),
                ToolCall("submit_verdict", verdict_params, "Verdict recorded: VERIFIED", False, 0.01),
            ],
            total_input_tokens=500,
            total_output_tokens=200,
            rounds=2,
        )

        task = Task(task_id="COMP-030", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify WH-003 entropy")
        agent.process_response(result, task, iteration=8)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "## COMP-030: Computation" in appended_text
        assert "**CLAIM:** WH-003" in appended_text
        assert "**VERDICT:** VERIFIED" in appended_text
        assert "**METHOD:** Numerical spot-checks" in appended_text
        assert "**NOTES:** Entropy-area proportionality confirmed." in appended_text

    def test_process_response_prefers_submit_verdict_over_text(self):
        """When both free text and submit_verdict exist, tool data wins."""
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        verdict_params = {
            "claim": "WH-001 — temperature is correct",
            "method": "numerical",
            "result": "5/5 pass",
            "verdict": "VERIFIED",
            "notes": "Confirmed.",
        }
        result = AgentResult(
            text="## COMP-099\n**CLAIM:** wrong\n**VERDICT:** REFUTED",  # free text
            tool_calls=[
                ToolCall("submit_verdict", verdict_params, "Verdict recorded: VERIFIED", False, 0.01),
            ],
            total_input_tokens=300,
            total_output_tokens=100,
            rounds=1,
        )

        task = Task(task_id="COMP-050", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist")
        agent.process_response(result, task, iteration=3)

        appended_text = agent.workspace.append_file.call_args[0][1]
        # Should use submit_verdict data, not the free text
        assert "**VERDICT:** VERIFIED" in appended_text
        assert "**CLAIM:** WH-001" in appended_text
        assert "REFUTED" not in appended_text

    def test_process_response_falls_back_without_submit_verdict(self):
        """No submit_verdict in tool_calls → uses free text (existing behavior)."""
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        result = AgentResult(
            text="## COMP-010: Test\n**CLAIM:** x = 1\n**VERDICT:** VERIFIED\n**NOTES:** OK.",
            tool_calls=[
                ToolCall("execute_python", {"code": "print(1)"}, "1\n", False, 0.5),
            ],
            total_input_tokens=300,
            total_output_tokens=100,
            rounds=2,
        )

        task = Task(task_id="COMP-010", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist")
        agent.process_response(result, task, iteration=5)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "## COMP-010: Test" in appended_text
        assert "**VERDICT:** VERIFIED" in appended_text


class TestExtractVerdictFromText:
    """Test _extract_verdict_from_text static method."""

    def test_extracts_call_syntax(self):
        text = 'call submit_verdict(claim="WH-001", method="numerical", result="ok", verdict="VERIFIED", notes="done")'
        result = ComputationalistAgent._extract_verdict_from_text(text)
        assert result is not None
        assert result["verdict"] == "VERIFIED"
        assert result["claim"] == "WH-001"
        assert result["method"] == "numerical"

    def test_extracts_without_call_prefix(self):
        text = 'submit_verdict(claim="X", method="m", result="r", verdict="REFUTED", notes="n")'
        result = ComputationalistAgent._extract_verdict_from_text(text)
        assert result["verdict"] == "REFUTED"

    def test_returns_none_when_no_match(self):
        text = "This is just regular text with no tool call."
        assert ComputationalistAgent._extract_verdict_from_text(text) is None

    def test_returns_none_without_verdict_field(self):
        text = 'call submit_verdict(claim="WH-001")'
        assert ComputationalistAgent._extract_verdict_from_text(text) is None

    def test_embedded_in_longer_text(self):
        text = (
            "The computation confirms the result.\n\n"
            'call submit_verdict(claim="WH-001 — temperature", '
            'method="spot checks", result="5/5 pass", '
            'verdict="VERIFIED", notes="Confirmed.")\n'
        )
        result = ComputationalistAgent._extract_verdict_from_text(text)
        assert result["verdict"] == "VERIFIED"
        assert "temperature" in result["claim"]

    def test_single_quoted_values(self):
        text = "submit_verdict(claim='WH-002', verdict='INCONCLUSIVE', notes='unclear')"
        result = ComputationalistAgent._extract_verdict_from_text(text)
        assert result["verdict"] == "INCONCLUSIVE"
        assert result["claim"] == "WH-002"


class TestVerdictTextExtraction:
    """Test that process_response uses text extraction as fallback."""

    def test_process_response_extracts_verdict_from_text(self):
        """Text containing submit_verdict syntax is extracted and formatted."""
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        result = AgentResult(
            text=(
                "The fidelity is confirmed.\n\n"
                'call submit_verdict(claim="WH-001 — fidelity", '
                'method="numerical", result="all pass", '
                'verdict="VERIFIED", notes="Confirmed.")'
            ),
            tool_calls=[
                ToolCall("execute_python", {"code": "print(1)"}, "1\n", False, 0.5),
            ],
            total_input_tokens=500,
            total_output_tokens=200,
            rounds=3,
        )

        task = Task(task_id="TASK-001", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify WH-001")
        agent.process_response(result, task, iteration=1)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "**VERDICT:** VERIFIED" in appended_text
        assert "**CLAIM:** WH-001" in appended_text
        assert "**METHOD:** numerical" in appended_text

    def test_actual_tool_call_takes_priority_over_text_extraction(self):
        """When both a real submit_verdict tool call and text syntax exist, tool wins."""
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        verdict_params = {
            "claim": "WH-002 — real tool",
            "method": "real method",
            "result": "real result",
            "verdict": "VERIFIED",
            "notes": "From tool.",
        }
        result = AgentResult(
            text='call submit_verdict(claim="WH-002", verdict="REFUTED", notes="from text")',
            tool_calls=[
                ToolCall("submit_verdict", verdict_params, "Verdict recorded: VERIFIED", False, 0.01),
            ],
            total_input_tokens=300,
            total_output_tokens=100,
            rounds=1,
        )

        task = Task(task_id="COMP-060", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist")
        agent.process_response(result, task, iteration=4)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "**VERDICT:** VERIFIED" in appended_text
        assert "real tool" in appended_text
        assert "REFUTED" not in appended_text

    def test_text_without_verdict_syntax_passes_through(self):
        """Regular text without submit_verdict syntax is used as-is."""
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""

        result = AgentResult(
            text="## COMP-070: Test\n**CLAIM:** WH-003\n**VERDICT:** VERIFIED\n**NOTES:** OK.",
            tool_calls=[],
            total_input_tokens=200,
            total_output_tokens=100,
            rounds=1,
        )

        task = Task(task_id="COMP-070", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist")
        agent.process_response(result, task, iteration=2)

        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "## COMP-070: Test" in appended_text
