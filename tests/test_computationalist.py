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

    def test_research_explore_has_tools(self):
        from sciralph.agents.research_explore import ResearchExploreAgent
        assert len(ResearchExploreAgent.tools) == 2
        names = {t["function"]["name"] for t in ResearchExploreAgent.tools}
        assert names == {"submit_result", "report_progress"}

    def test_critic_has_tools(self):
        from sciralph.agents.critic import CriticAgent
        assert len(CriticAgent.tools) == 2
        names = {t["function"]["name"] for t in CriticAgent.tools}
        assert names == {"submit_critique", "finish_review"}

    def test_orchestrator_has_state_mutation_tools(self):
        from sciralph.agents.orchestrator import OrchestratorAgent
        assert len(OrchestratorAgent.tools) > 0
        tool_names = {t["function"]["name"] for t in OrchestratorAgent.tools}
        assert "set_next_task" in tool_names
        assert "add_hypothesis" in tool_names


def _make_agent():
    """Create a ComputationalistAgent with mocked dependencies."""
    from sciralph.research_state import ResearchState
    config = MagicMock()
    config.sympy_timeout_seconds = 10
    config.tool_output_limit = 10_000
    workspace = MagicMock()
    workspace.root = MagicMock()
    workspace.computations_dir = "/tmp"
    metrics = MagicMock()
    agent = ComputationalistAgent(config=config, workspace=workspace, metrics=metrics)
    agent.research_state = ResearchState()
    return agent


class TestAgenticResponse:
    def test_process_without_exit_tool_is_inconclusive(self):
        """Text with VERIFIED but no submit_verdict → INCONCLUSIVE (tool call required)."""
        agent = _make_agent()

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

        agent.process_response(result, Task(task_id="COMP-020", task_type=TaskType.COMPUTE_VERIFY, assigned_to="compute_verify"), iteration=7)

        comp = agent.research_state.computations["COMP-020"]
        assert comp.verdict.value == "INCONCLUSIVE"

    def test_process_agentic_response_empty_text(self):
        agent = _make_agent()

        result = AgentResult(
            text="",
            tool_calls=[],
            total_input_tokens=100,
            total_output_tokens=10,
            rounds=1,
        )

        agent.process_response(result, Task(task_id="TASK-010", task_type=TaskType.COMPUTE_VERIFY, assigned_to="compute_verify"), iteration=10)

        comp = agent.research_state.computations["TASK-010"]
        assert comp.verdict.value == "INCONCLUSIVE"

    def test_process_empty_text_extracts_claim_from_task_body(self):
        agent = _make_agent()

        result = AgentResult(
            text="",
            tool_calls=[],
            total_input_tokens=100,
            total_output_tokens=10,
            rounds=1,
        )

        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
            assigned_to="compute_verify",
            body="Verify WH-005 Maslov phase for ωT > π",
        )
        agent.process_response(result, task, iteration=5)

        comp = agent.research_state.computations["TASK-005"]
        assert comp.target_hypothesis == "WH-005"


class TestSubmitVerdictProcessing:
    """Test that process_response extracts data from submit_verdict tool calls."""

    def test_process_response_uses_submit_verdict_data(self):
        """Empty text + submit_verdict tool call → formatted COMP entry."""
        agent = _make_agent()

        verdict_params = {
            "target_id": "WH-003",
            "claim": "entropy scales as area",
            "method": "Numerical spot-checks at 5 test points",
            "result": "All 5 checks pass within rtol=1e-6",
            "verdict": "VERIFIED",
            "notes": "Entropy-area proportionality confirmed.",
        }
        result = AgentResult(
            text="",
            tool_calls=[
                ToolCall("execute_python", {"code": "print(1)"}, "1\n", False, 0.5),
                ToolCall("submit_verdict", verdict_params, "Verdict recorded: VERIFIED", False, 0.01),
            ],
            total_input_tokens=500,
            total_output_tokens=200,
            rounds=2,
        )

        task = Task(task_id="COMP-030", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify WH-003 entropy")
        agent.process_response(result, task, iteration=8)

        comp = agent.research_state.computations["COMP-030"]
        assert comp.id == "COMP-030"
        assert comp.verdict.value == "VERIFIED"

    def test_process_response_prefers_submit_verdict_over_text(self):
        """When both free text and submit_verdict exist, tool data wins."""
        agent = _make_agent()

        verdict_params = {
            "target_id": "WH-001",
            "claim": "temperature is correct",
            "method": "numerical",
            "result": "5/5 pass",
            "verdict": "VERIFIED",
            "notes": "Confirmed.",
        }
        result = AgentResult(
            text="## COMP-099\n**CLAIM:** wrong\n**VERDICT:** REFUTED",
            tool_calls=[
                ToolCall("submit_verdict", verdict_params, "Verdict recorded: VERIFIED", False, 0.01),
            ],
            total_input_tokens=300,
            total_output_tokens=100,
            rounds=1,
        )

        task = Task(task_id="COMP-050", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify")
        agent.process_response(result, task, iteration=3)

        comp = agent.research_state.computations["COMP-050"]
        assert comp.verdict.value == "VERIFIED"

    def test_process_response_inconclusive_without_submit_verdict(self):
        """No submit_verdict in tool_calls → INCONCLUSIVE stub."""
        agent = _make_agent()

        result = AgentResult(
            text="## COMP-010: Test\n**CLAIM:** x = 1\n**VERDICT:** VERIFIED\n**NOTES:** OK.",
            tool_calls=[
                ToolCall("execute_python", {"code": "print(1)"}, "1\n", False, 0.5),
            ],
            total_input_tokens=300,
            total_output_tokens=100,
            rounds=2,
        )

        task = Task(task_id="COMP-010", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify")
        agent.process_response(result, task, iteration=5)

        comp = agent.research_state.computations["COMP-010"]
        assert comp.verdict.value == "INCONCLUSIVE"


class TestZeroOutputOnMaxRoundsForced:
    """Test that zero_output=True when stop_reason='max_rounds_forced' even with non-empty text (A4)."""

    def test_max_rounds_forced_sets_zero_output(self):
        agent = _make_agent()
        from sciralph.research_state import ResearchState
        agent.research_state = ResearchState()

        result = AgentResult(
            text="Some partial analysis that did not complete...",
            tool_calls=[
                ToolCall("execute_python", {"code": "print(1)"}, "1\n", False, 0.5),
            ],
            total_input_tokens=500,
            total_output_tokens=200,
            rounds=10,
            stop_reason="max_rounds_forced",
        )

        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify",
                    body="Verify WH-002 temperature")
        agent.process_response(result, task, iteration=5)

        comp = agent.research_state.computations["TASK-005"]
        assert comp.zero_output is True

    def test_normal_end_turn_empty_text_sets_zero_output(self):
        agent = _make_agent()
        from sciralph.research_state import ResearchState
        agent.research_state = ResearchState()

        result = AgentResult(
            text="",
            tool_calls=[],
            total_input_tokens=100,
            total_output_tokens=10,
            rounds=1,
        )

        task = Task(task_id="TASK-006", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify",
                    body="Verify WH-003")
        agent.process_response(result, task, iteration=6)

        comp = agent.research_state.computations["TASK-006"]
        assert comp.zero_output is True


class TestNewAgentClasses:
    """Test the new split agent classes (Phase 4a)."""

    def test_compute_verify_has_correct_tools(self):
        from sciralph.agents.compute_verify import ComputeVerifyAgent
        names = {t["function"]["name"] for t in ComputeVerifyAgent.tools}
        assert names == {"execute_python", "submit_verdict", "report_progress"}

    def test_compute_explore_has_correct_tools(self):
        from sciralph.agents.compute_explore import ComputeExploreAgent
        names = {t["function"]["name"] for t in ComputeExploreAgent.tools}
        assert names == {"execute_python", "submit_result", "report_progress"}

    def test_research_verify_has_correct_tools(self):
        from sciralph.agents.research_verify import ResearchVerifyAgent
        names = {t["function"]["name"] for t in ResearchVerifyAgent.tools}
        assert names == {"submit_verdict", "report_progress"}

    def test_research_verify_no_execute_python(self):
        from sciralph.agents.research_verify import ResearchVerifyAgent
        names = {t["function"]["name"] for t in ResearchVerifyAgent.tools}
        assert "execute_python" not in names

    def test_research_verify_creates_research_verify_computation(self):
        """ResearchVerifyAgent.process_response sets kind='research_verify'."""
        from sciralph.agents.research_verify import ResearchVerifyAgent
        from sciralph.research_state import ResearchState

        agent = ResearchVerifyAgent(
            config=MagicMock(), workspace=MagicMock(), metrics=MagicMock(),
        )
        agent.research_state = ResearchState()

        verdict_params = {
            "target_id": "WH-001",
            "claim": "dimensional consistency",
            "method": "dimensional analysis",
            "result": "All dimensions match",
            "verdict": "VERIFIED",
            "notes": "Confirmed by analysis.",
        }
        result = AgentResult(
            text="",
            tool_calls=[
                ToolCall("submit_verdict", verdict_params, "Verdict recorded: VERIFIED", False, 0.01),
            ],
            total_input_tokens=500,
            total_output_tokens=200,
            rounds=1,
        )
        task = Task(task_id="TASK-005", task_type=TaskType.RESEARCH_VERIFY,
                    assigned_to="research_verify", body="Verify WH-001")
        agent.process_response(result, task, iteration=5)

        comp = agent.research_state.computations["TASK-005"]
        assert comp.kind == "research_verify"
        assert comp.verdict.value == "VERIFIED"

    def test_tools_for_task_type_research_verify(self):
        names = {t["function"]["name"] for t in ToolExecutor.tools_for_task_type(TaskType.RESEARCH_VERIFY)}
        assert names == {"submit_verdict", "report_progress"}


class TestFreeTextFallthrough:
    """Free text without submit_verdict results in INCONCLUSIVE."""

    def test_text_without_verdict_is_inconclusive(self):
        """Text with VERIFIED but no submit_verdict → INCONCLUSIVE stub."""
        agent = _make_agent()

        result = AgentResult(
            text="## COMP-070: Test\n**CLAIM:** WH-003\n**VERDICT:** VERIFIED\n**NOTES:** OK.",
            tool_calls=[],
            total_input_tokens=200,
            total_output_tokens=100,
            rounds=1,
        )

        task = Task(task_id="COMP-070", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify")
        agent.process_response(result, task, iteration=2)

        comp = agent.research_state.computations["COMP-070"]
        assert comp.verdict.value == "INCONCLUSIVE"
