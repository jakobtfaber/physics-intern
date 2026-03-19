"""Placeholder tests verifying that new agents (researcher, computer, verifier) exist and import correctly.

The old computationalist.py agent has been removed. This file ensures the new
agent modules are importable and have the expected structure. Full agent tests
for researcher, computer, and verifier will live in dedicated test files.
"""

import tempfile
from unittest.mock import MagicMock

from sciralph.sandbox import execute_python


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


class TestNewAgentImports:
    """Verify the new agent modules exist and are importable."""

    def test_researcher_agent_importable(self):
        from sciralph.agents.researcher import ResearcherAgent
        assert ResearcherAgent.name == "researcher"

    def test_computer_agent_importable(self):
        from sciralph.agents.computer import ComputerAgent
        assert ComputerAgent.name == "computer"

    def test_verifier_agent_importable(self):
        from sciralph.agents.reviewer import ReviewerAgent
        assert ReviewerAgent.name == "reviewer"


class TestNewAgentTools:
    """Verify the new agents have correct tool configurations."""

    def test_researcher_has_tools(self):
        from sciralph.agents.researcher import ResearcherAgent
        assert ResearcherAgent.tools
        names = {t["function"]["name"] for t in ResearcherAgent.tools}
        assert "submit_result" in names

    def test_computer_has_tools(self):
        from sciralph.agents.computer import ComputerAgent
        assert ComputerAgent.tools
        names = {t["function"]["name"] for t in ComputerAgent.tools}
        assert "execute_python" in names
        assert "submit_result" in names

    def test_reviewer_is_one_shot(self):
        from sciralph.agents.reviewer import ReviewerAgent
        assert ReviewerAgent.tools == []

    def test_critic_has_tools(self):
        from sciralph.agents.critic import CriticAgent
        assert len(CriticAgent.tools) == 1
        names = {t["function"]["name"] for t in CriticAgent.tools}
        assert names == {"submit_review"}

    def test_orchestrator_has_state_mutation_tools(self):
        from sciralph.agents.orchestrator import OrchestratorAgent
        assert len(OrchestratorAgent.tools) > 0
        tool_names = {t["function"]["name"] for t in OrchestratorAgent.tools}
        assert "set_next_task" in tool_names
        assert "add_hypothesis" in tool_names


class TestComputerProcessResponse:
    """Test ComputerAgent.process_response builds Evidence correctly."""

    def _make_agent(self):
        from sciralph.agents.computer import ComputerAgent
        from sciralph.research_state import ResearchState, ResearchQuestion
        agent = ComputerAgent.__new__(ComputerAgent)
        agent.research_state = ResearchState(problem_statement="test")
        rq_id = agent.research_state.next_entity_num()
        agent.research_state.research_questions[rq_id] = ResearchQuestion(id=rq_id, question="What is X?")
        agent._last_script_names = []
        return agent

    def _make_result(self, tool_calls):
        from sciralph.llm import AgentResult
        return AgentResult(text="", tool_calls=tool_calls)

    def _make_tc(self, name, tool_input, output="ok", is_error=False):
        from sciralph.tools import ToolCall
        return ToolCall(tool_name=name, tool_input=tool_input, output=output, is_error=is_error, duration=0.1)

    def test_approach_includes_assumptions_and_expected_outcome(self):
        from sciralph.task import Task, TaskType
        agent = self._make_agent()
        rq_id = list(agent.research_state.research_questions.keys())[0]
        task = Task(task_id="T1", task_type=TaskType.COMPUTE, assigned_to="computer", body=f"Compute {rq_id}", target_claim=rq_id)
        tool_calls = [
            self._make_tc("document_approach", {
                "approach": "Compute via SymPy",
                "assumptions": ["T > 0", "Natural units"],
                "expected_outcome": "Should match Hawking formula",
            }),
            self._make_tc("submit_result", {
                "target_id": rq_id,
                "method": "symbolic",
                "result": "T_H = 1/(8*pi*M)",
                "confidence": "exact",
            }),
        ]
        result = self._make_result(tool_calls)
        agent.process_response(result, task, iteration=1)
        evidence = agent.research_state.research_questions[rq_id].evidence
        assert evidence is not None
        assert "Compute via SymPy" in evidence.approach
        assert "Assumptions:" in evidence.approach
        assert "- T > 0" in evidence.approach
        assert "- Natural units" in evidence.approach
        assert "Expected outcome: Should match Hawking formula" in evidence.approach

    def test_approach_without_assumptions_or_expected_outcome(self):
        from sciralph.task import Task, TaskType
        agent = self._make_agent()
        rq_id = list(agent.research_state.research_questions.keys())[0]
        task = Task(task_id="T1", task_type=TaskType.COMPUTE, assigned_to="computer", body=f"Compute {rq_id}", target_claim=rq_id)
        tool_calls = [
            self._make_tc("document_approach", {"approach": "Compute via SymPy"}),
            self._make_tc("submit_result", {"target_id": rq_id, "method": "symbolic", "result": "42", "confidence": "exact"}),
        ]
        result = self._make_result(tool_calls)
        agent.process_response(result, task, iteration=1)
        evidence = agent.research_state.research_questions[rq_id].evidence
        assert evidence is not None
        assert evidence.approach == "Compute via SymPy"
        assert "Assumptions:" not in evidence.approach
        assert "Expected outcome:" not in evidence.approach


class TestResearcherProcessResponse:
    """Test ResearcherAgent.process_response builds Evidence correctly."""

    def _make_agent(self):
        from sciralph.agents.researcher import ResearcherAgent
        from sciralph.research_state import ResearchState, ResearchQuestion
        agent = ResearcherAgent.__new__(ResearcherAgent)
        agent.research_state = ResearchState(problem_statement="test")
        rq_id = f"RQ-{agent.research_state.next_entity_num():03d}"
        agent.research_state.research_questions[rq_id] = ResearchQuestion(id=rq_id, question="Derive X?")
        agent._last_script_names = []
        return agent, rq_id

    def _make_result(self, text="", tool_calls=None):
        from sciralph.llm import AgentResult
        return AgentResult(text=text, tool_calls=tool_calls or [])

    def _make_tc(self, name, tool_input, output="ok", is_error=False):
        from sciralph.tools import ToolCall
        return ToolCall(tool_name=name, tool_input=tool_input, output=output, is_error=is_error, duration=0.1)

    def test_evidence_from_submit_result(self):
        from sciralph.task import Task, TaskType
        agent, rq_id = self._make_agent()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body=f"Derive for {rq_id}", target_claim=rq_id)
        tool_calls = [
            self._make_tc("submit_result", {
                "reasoning": "By direct calculation...",
                "result": "T_H = 1/(8*pi*M)",
                "method": "Euclidean path integral",
                "confidence": "exact",
                "summary": "Hawking temperature derived",
            }),
        ]
        result = self._make_result(text="Full derivation text here...", tool_calls=tool_calls)
        agent.process_response(result, task, iteration=1)
        evidence = agent.research_state.research_questions[rq_id].evidence
        assert evidence is not None
        assert evidence.type == "research"
        assert evidence.method == "Euclidean path integral"
        assert evidence.result == "T_H = 1/(8*pi*M)"
        assert evidence.confidence == "exact"
        assert evidence.summary == "Hawking temperature derived"

    def test_reasoning_from_response_text(self):
        """Evidence.reasoning should prefer response.text over tool params."""
        from sciralph.task import Task, TaskType
        agent, rq_id = self._make_agent()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body=f"Derive for {rq_id}", target_claim=rq_id)
        derivation_text = "Starting from the metric ds^2 = -(1-2M/r)dt^2 + ... we find..."
        tool_calls = [
            self._make_tc("submit_result", {
                "reasoning": "short summary in tool",
                "result": "T_H = 1/(8*pi*M)",
                "method": "analytical",
                "confidence": "exact",
                "summary": "Derived T_H",
            }),
        ]
        result = self._make_result(text=derivation_text, tool_calls=tool_calls)
        agent.process_response(result, task, iteration=1)
        evidence = agent.research_state.research_questions[rq_id].evidence
        assert evidence.reasoning == derivation_text

    def test_reasoning_falls_back_to_tool_params(self):
        """When response.text is empty, reasoning comes from tool params."""
        from sciralph.task import Task, TaskType
        agent, rq_id = self._make_agent()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body=f"Derive for {rq_id}", target_claim=rq_id)
        tool_calls = [
            self._make_tc("submit_result", {
                "reasoning": "Detailed reasoning in tool params",
                "result": "T = 42",
                "method": "algebra",
                "confidence": "exact",
                "summary": "Found T",
            }),
        ]
        result = self._make_result(text="", tool_calls=tool_calls)
        agent.process_response(result, task, iteration=1)
        evidence = agent.research_state.research_questions[rq_id].evidence
        assert evidence.reasoning == "Detailed reasoning in tool params"

    def test_target_from_task_target_claim(self):
        """Target ID comes from task.target_claim, not tool params."""
        from sciralph.task import Task, TaskType
        agent, rq_id = self._make_agent()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Some task", target_claim=rq_id)
        tool_calls = [
            self._make_tc("submit_result", {
                "reasoning": "...", "result": "42", "method": "m",
                "confidence": "exact", "summary": "s",
            }),
        ]
        result = self._make_result(tool_calls=tool_calls)
        agent.process_response(result, task, iteration=1)
        assert agent.research_state.research_questions[rq_id].evidence is not None

    def test_fallback_no_tool_call(self):
        """When no tool call, build minimal evidence from response text."""
        from sciralph.task import Task, TaskType
        agent, rq_id = self._make_agent()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body=f"Derive for {rq_id}", target_claim=rq_id)
        result = self._make_result(text="Partial derivation that got cut off...")
        agent.process_response(result, task, iteration=1)
        evidence = agent.research_state.research_questions[rq_id].evidence
        assert evidence is not None
        assert evidence.confidence == "partial"
        assert "Partial derivation" in evidence.reasoning


class TestToolsForTaskType:
    """Test tools_for_task_type returns correct tool sets."""

    def test_research_tools(self):
        from sciralph.task import TaskType
        from sciralph.tools import ToolExecutor
        names = {t["function"]["name"] for t in ToolExecutor.tools_for_task_type(TaskType.RESEARCH)}
        assert "submit_result" in names

    def test_compute_tools(self):
        from sciralph.task import TaskType
        from sciralph.tools import ToolExecutor
        names = {t["function"]["name"] for t in ToolExecutor.tools_for_task_type(TaskType.COMPUTE)}
        assert "execute_python" in names
        assert "submit_result" in names
