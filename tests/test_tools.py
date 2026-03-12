"""Tests for ToolExecutor, execute_python tool, and run_agent_loop."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from sciralph.config import Config
from sciralph.llm import AgentResult, run_agent_loop
from sciralph.providers.base import ProviderResponse
from sciralph.tools import ToolExecutor, ToolCall


def _make_executor(timeout: int = 60) -> ToolExecutor:
    """Create a ToolExecutor with a temp workspace."""
    root = Path(tempfile.mkdtemp())
    return ToolExecutor(workspace_root=root, timeout=timeout)


class TestExecutePython:
    def test_simple_script(self):
        executor = _make_executor()
        tc = executor.execute("execute_python", {"code": "print('hello')"})
        assert tc.output.strip() == "hello"
        assert not tc.is_error
        assert tc.tool_name == "execute_python"
        assert tc.duration >= 0

    def test_writes_file(self):
        executor = _make_executor()
        executor.execute("execute_python", {"code": "print(1)"})
        script = executor._computations_dir / "tool_exec_001.py"
        assert script.exists()
        assert "print(1)" in script.read_text()

    def test_error_script(self):
        executor = _make_executor()
        tc = executor.execute("execute_python", {"code": "x = 1 +\n"})
        assert tc.is_error
        assert "SyntaxError" in tc.output or "Error" in tc.output

    def test_timeout(self):
        executor = _make_executor(timeout=1)
        tc = executor.execute("execute_python", {"code": "import time; time.sleep(10)"})
        assert tc.is_error
        assert "TIMEOUT" in tc.output
        assert "Suggestions" in tc.output

    def test_output_truncation(self):
        executor = _make_executor()
        # Generate >10K chars of output
        code = "print('x' * 20000)"
        tc = executor.execute("execute_python", {"code": code})
        assert not tc.is_error
        assert len(tc.output) <= 11_000  # 10K + truncation message
        assert "truncated" in tc.output

    def test_unknown_tool_raises(self):
        executor = _make_executor()
        try:
            executor.execute("nonexistent_tool", {"code": "print(1)"})
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Unknown tool" in str(e)

    def test_counter_increments(self):
        executor = _make_executor()
        executor.execute("execute_python", {"code": "print(1)"})
        executor.execute("execute_python", {"code": "print(2)"})
        assert (executor._computations_dir / "tool_exec_001.py").exists()
        assert (executor._computations_dir / "tool_exec_002.py").exists()


class TestToolDefinitions:
    def test_definitions_format(self):
        defs = ToolExecutor.TOOL_DEFINITIONS
        assert len(defs) == 1
        assert defs[0]["type"] == "function"
        func = defs[0]["function"]
        assert func["name"] == "execute_python"
        assert "parameters" in func
        assert func["parameters"]["required"] == ["code"]


class TestTruncation:
    def test_short_text_unchanged(self):
        assert ToolExecutor._truncate_output("short") == "short"

    def test_long_text_truncated(self):
        text = "a" * 20_000
        result = ToolExecutor._truncate_output(text, limit=10_000)
        assert len(result) < 11_000
        assert "truncated" in result
        # Preserves head and tail
        assert result.startswith("a" * 100)
        assert result.endswith("a" * 100)


# --- Agent loop tests ---

def _mock_provider_response(text="", stop_reason="end_turn",
                             input_tokens=100, output_tokens=50,
                             tool_calls=None):
    """Create a mock ProviderResponse."""
    return ProviderResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        raw_content=None,
    )


def _make_config() -> Config:
    return Config(api_key="test-key", audit_log="", logs_dir="", provider="anthropic")


def _mock_provider():
    """Create a mock provider with sensible defaults for format methods."""
    provider = MagicMock()
    provider.format_assistant_message.return_value = {"role": "assistant", "content": "mock"}
    provider.build_tool_result_messages.return_value = [{"role": "user", "content": []}]
    return provider


class TestAgentLoop:
    @patch("sciralph.llm._get_provider")
    def test_end_turn_first_call(self, mock_get_provider):
        """LLM returns text with end_turn on the first call."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider
        provider.call.return_value = _mock_provider_response(
            "Final answer.", "end_turn", 200, 100
        )

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert isinstance(result, AgentResult)
        assert result.text == "Final answer."
        assert result.rounds == 1
        assert result.tool_calls == []
        assert not result.truncated
        assert result.stop_reason == "end_turn"

    @patch("sciralph.llm._get_provider")
    def test_one_tool_call_then_end(self, mock_get_provider):
        """LLM calls a tool in round 1, then returns text in round 2."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        # Round 1: tool_use
        round1 = _mock_provider_response(
            "", "tool_use",  200, 80,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(42)"}}],
        )
        # Round 2: end_turn
        round2 = _mock_provider_response("Done. VERDICT: VERIFIED", "end_turn", 300, 100)
        provider.call.side_effect = [round1, round2]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.rounds == 2
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "execute_python"
        assert "VERDICT: VERIFIED" in result.text
        assert not result.truncated

    @patch("sciralph.llm._get_provider")
    def test_max_rounds_exhausted(self, mock_get_provider):
        """LLM always returns tool_use — stops at max_rounds with forced final call."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_response = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        text_response = _mock_provider_response(
            "## COMP-001\n**VERDICT:** INCONCLUSIVE", "end_turn", 150, 80
        )
        provider.call.side_effect = [
            tool_response, tool_response, tool_response,  # 3 tool-use rounds
            text_response,  # forced final call
        ]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=3,
        )

        assert result.rounds == 4  # 3 tool rounds + 1 forced
        assert result.truncated
        assert result.stop_reason == "max_rounds_forced"
        assert len(result.tool_calls) == 3
        assert provider.call.call_count == 4

    @patch("sciralph.llm._get_provider")
    def test_token_accumulation(self, mock_get_provider):
        """Tokens are summed across rounds."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        round1 = _mock_provider_response(
            "", "tool_use", 200, 80,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        round2 = _mock_provider_response(
            "", "tool_use", 300, 90,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        round3 = _mock_provider_response("Done.", "end_turn", 400, 100)
        provider.call.side_effect = [round1, round2, round3]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.total_input_tokens == 900  # 200+300+400
        assert result.total_output_tokens == 270  # 80+90+100
        assert result.rounds == 3

    @patch("sciralph.llm._get_provider")
    def test_max_tokens_stop(self, mock_get_provider):
        """stop_reason=max_tokens in round 1 returns truncated."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider
        provider.call.return_value = _mock_provider_response(
            "Partial...", "max_tokens", 200, 16384
        )

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.truncated
        assert result.stop_reason == "max_tokens"
        assert result.rounds == 1


class TestForcedPartialOutput:
    """Test the forced text-only final call when max_rounds is exhausted."""

    @patch("sciralph.llm._get_provider")
    def test_forced_call_has_no_tools_param(self, mock_get_provider):
        """Last call should NOT include tools parameter."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_response = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        text_response = _mock_provider_response(
            "## COMP-001\n**VERDICT:** INCONCLUSIVE", "end_turn", 150, 80
        )
        provider.call.side_effect = [
            tool_response, tool_response,  # 2 rounds of tool use
            text_response,  # forced final call
        ]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=2,
        )

        # Verify the last call had no tools parameter (tools=None by default)
        calls = provider.call.call_args_list
        assert len(calls) == 3
        # First two calls should have tools
        assert calls[0].kwargs.get("tools") is not None
        # Last call should NOT have tools
        assert calls[2].kwargs.get("tools") is None

    @patch("sciralph.llm._get_provider")
    def test_forced_call_text_in_result(self, mock_get_provider):
        """Result text should come from the forced final call."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_response = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        text_response = _mock_provider_response(
            "Forced partial output here", "end_turn", 150, 80
        )
        provider.call.side_effect = [tool_response, text_response]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert result.text == "Forced partial output here"
        assert result.stop_reason == "max_rounds_forced"

    @patch("sciralph.llm._get_provider")
    def test_token_accumulation_includes_forced(self, mock_get_provider):
        """Total tokens should include the forced call."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_response = _mock_provider_response(
            "", "tool_use", 200, 80,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        text_response = _mock_provider_response("Final", "end_turn", 300, 120)
        provider.call.side_effect = [tool_response, text_response]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert result.total_input_tokens == 500  # 200 + 300
        assert result.total_output_tokens == 200  # 80 + 120
        assert result.rounds == 2  # 1 tool round + 1 forced

    @patch("sciralph.llm._get_provider")
    def test_stop_reason_is_max_rounds_forced(self, mock_get_provider):
        """Stop reason should be 'max_rounds_forced'."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_response = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        text_response = _mock_provider_response("Done", "end_turn", 100, 50)
        provider.call.side_effect = [tool_response, text_response]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert result.stop_reason == "max_rounds_forced"
        assert result.truncated is True
