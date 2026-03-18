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
        from sciralph.agents.verifier import VerifierAgent
        assert VerifierAgent.name == "verifier"


class TestNewAgentTools:
    """Verify the new agents have correct tool configurations."""

    def test_researcher_has_tools(self):
        from sciralph.agents.researcher import ResearcherAgent
        assert ResearcherAgent.tools
        names = {t["function"]["name"] for t in ResearcherAgent.tools}
        assert "submit_result" in names
        assert "report_progress" in names

    def test_computer_has_tools(self):
        from sciralph.agents.computer import ComputerAgent
        assert ComputerAgent.tools
        names = {t["function"]["name"] for t in ComputerAgent.tools}
        assert "execute_python" in names
        assert "submit_result" in names
        assert "report_progress" in names

    def test_verifier_has_tools(self):
        from sciralph.agents.verifier import VerifierAgent
        assert VerifierAgent.tools
        names = {t["function"]["name"] for t in VerifierAgent.tools}
        assert "submit_verdict" in names

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


class TestToolsForTaskType:
    """Test tools_for_task_type returns correct tool sets."""

    def test_research_tools(self):
        from sciralph.task import TaskType
        from sciralph.tools import ToolExecutor
        names = {t["function"]["name"] for t in ToolExecutor.tools_for_task_type(TaskType.RESEARCH)}
        assert "submit_result" in names
        assert "report_progress" in names

    def test_compute_tools(self):
        from sciralph.task import TaskType
        from sciralph.tools import ToolExecutor
        names = {t["function"]["name"] for t in ToolExecutor.tools_for_task_type(TaskType.COMPUTE)}
        assert "execute_python" in names
        assert "submit_result" in names
        assert "report_progress" in names
