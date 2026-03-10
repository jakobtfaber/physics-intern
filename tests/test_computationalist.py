"""Tests for computationalist agent response parsing."""

import re
import tempfile
from unittest.mock import MagicMock, patch, call

from sciralph.agents.computationalist import ComputationalistAgent
from sciralph.llm import LLMResponse
from sciralph.sandbox import execute_python


class TestResultRegex:
    """Test that RESULT placeholder insertion handles colon placement variants."""

    def _strip_result(self, text: str) -> str:
        """Apply the same regex the agent uses to strip LLM RESULT content."""
        return re.sub(
            r'(\*\*RESULT:?\*\*\s*:?)[\s\S]*?(?=\*\*(VERDICT|NOTES):?\*\*)',
            r'\1\n[execution output inserted below]\n\n',
            text,
        )

    def test_result_colon_inside_bold(self):
        text = "**RESULT:**\nsome text\n**VERDICT:**"
        result = self._strip_result(text)
        assert "[execution output inserted below]" in result
        assert "some text" not in result

    def test_result_colon_outside_bold(self):
        text = "**RESULT**:\nsome text\n**VERDICT**:"
        result = self._strip_result(text)
        assert "[execution output inserted below]" in result
        assert "some text" not in result

    def test_fill_result_no_duplicate(self):
        """Full _parse_computation_response + _fill_result_section flow produces one RESULT."""
        llm_text = (
            "## TASK-001: Computation\n\n"
            "**RESULT:**\nLLM guessed output\n\n"
            "**VERDICT:** PASS\n\n"
            "**NOTES:** None\n\n"
            "```python\nprint('hello')\n```"
        )
        task = {"task_id": "TASK-001"}
        # Use the static/class methods directly
        agent_cls = ComputationalistAgent
        # _parse_computation_response is an instance method, but we can test the regex path
        # by calling the static _fill_result_section after regex substitution
        stripped = re.sub(
            r'(\*\*RESULT:?\*\*\s*:?)[\s\S]*?(?=\*\*(VERDICT|NOTES):?\*\*)',
            r'\1\n[execution output inserted below]\n\n',
            llm_text,
        )
        filled = agent_cls._fill_result_section(stripped, "```\nhello\n```")
        # Count occurrences of **RESULT
        result_count = len(re.findall(r'\*\*RESULT', filled))
        assert result_count == 1, f"Expected 1 RESULT heading, found {result_count}"
        assert "hello" in filled
        assert "LLM guessed output" not in filled


class TestResultRegexNoVerdict:
    """Test that RESULT stripping works when VERDICT/NOTES are absent (two-pass flow)."""

    def test_result_stripped_without_verdict(self):
        text = "## COMP-007\n**RESULT:**\nLLM guess\n"
        stripped = re.sub(
            r'(\*\*RESULT:?\*\*\s*:?)[\s\S]*?(?=\*\*(VERDICT|NOTES):?\*\*|\n- \*\*Iteration|\n- \*\*Code|\Z)',
            r'\1\n[execution output inserted below]\n\n',
            text,
        )
        assert "[execution output inserted below]" in stripped
        assert "LLM guess" not in stripped


def _make_agent():
    """Create a ComputationalistAgent with mocked dependencies."""
    config = MagicMock()
    config.sympy_timeout_seconds = 10
    workspace = MagicMock()
    workspace.root = MagicMock()
    workspace.computations_dir = "/tmp"
    metrics = MagicMock()
    return ComputationalistAgent(config=config, workspace=workspace, metrics=metrics)


class TestReviewVerdictAppended:
    """Test that the review call's VERDICT/NOTES are appended to the log entry."""

    @patch("sciralph.agents.computationalist.execute_python")
    @patch("sciralph.agents.computationalist.call_llm")
    def test_review_verdict_appended(self, mock_call_llm, mock_exec):
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""
        agent.workspace.read_file_tail.return_value = ""

        # First call result (main LLM pass) — not used here since we pass
        # an LLMResponse directly to process_response
        # Second call result (review pass)
        review_response = LLMResponse(
            text="**VERDICT:** VERIFIED\n**NOTES:** All checks passed.",
            input_tokens=500, output_tokens=50, duration=1.0,
            stop_reason="end_turn",
        )
        mock_call_llm.return_value = review_response

        exec_result = MagicMock()
        exec_result.stdout = "result: 42\n"
        exec_result.stderr = ""
        exec_result.returncode = 0
        exec_result.timed_out = False
        mock_exec.return_value = exec_result

        # Simulate the first-pass LLM output (no VERDICT/NOTES)
        first_pass = LLMResponse(
            text=(
                "## COMP-010: Test computation\n\n"
                "**CLAIM:** x = 42\n"
                "**METHOD:** Direct computation\n"
                "**RESULT:**\n\n"
                "```python\nprint('result: 42')\n```"
            ),
            input_tokens=1000, output_tokens=200, duration=2.0,
            stop_reason="end_turn",
        )

        # Mock resolve() on workspace.root / code_filename
        mock_path = MagicMock()
        mock_path.resolve.return_value = "/tmp/comp_test.py"
        agent.workspace.root.__truediv__ = MagicMock(return_value=mock_path)

        agent.process_response(first_pass, {"task_id": "COMP-010"}, iteration=5)

        # Verify the review call was made
        mock_call_llm.assert_called_once()

        # Verify the appended log contains the review's VERDICT
        appended_text = agent.workspace.append_file.call_args[0][1]
        assert "**VERDICT:** VERIFIED" in appended_text
        assert "**NOTES:** All checks passed." in appended_text


class TestReviewSeesExecutionFailure:
    """Test that the review call receives the EXECUTION FAILED banner."""

    @patch("sciralph.agents.computationalist.execute_python")
    @patch("sciralph.agents.computationalist.call_llm")
    def test_review_called_with_execution_output(self, mock_call_llm, mock_exec):
        agent = _make_agent()
        agent.workspace.read_file.return_value = ""
        agent.workspace.read_file_tail.return_value = ""

        review_response = LLMResponse(
            text="**VERDICT:** INCONCLUSIVE\n**NOTES:** SyntaxError in code.",
            input_tokens=500, output_tokens=50, duration=1.0,
            stop_reason="end_turn",
        )
        mock_call_llm.return_value = review_response

        exec_result = MagicMock()
        exec_result.stdout = ""
        exec_result.stderr = "SyntaxError: invalid character"
        exec_result.returncode = 1
        exec_result.timed_out = False
        mock_exec.return_value = exec_result

        first_pass = LLMResponse(
            text=(
                "## COMP-005: Unicode test\n\n"
                "**CLAIM:** E = mc²\n"
                "**METHOD:** Symbolic check\n"
                "**RESULT:**\n\n"
                "```python\nprint(x²)\n```"
            ),
            input_tokens=1000, output_tokens=200, duration=2.0,
            stop_reason="end_turn",
        )

        mock_path = MagicMock()
        mock_path.resolve.return_value = "/tmp/comp_test.py"
        agent.workspace.root.__truediv__ = MagicMock(return_value=mock_path)

        agent.process_response(first_pass, {"task_id": "COMP-005"}, iteration=3)

        # The review call should receive the log entry with EXECUTION FAILED
        review_call_args = mock_call_llm.call_args
        user_content = review_call_args[0][1]  # second positional arg
        assert "EXECUTION FAILED" in user_content


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
