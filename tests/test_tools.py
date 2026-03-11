"""Tests for ToolExecutor, execute_python tool, and run_agent_loop."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sciralph.config import Config
from sciralph.llm import AgentResult, run_agent_loop
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
        assert defs[0]["name"] == "execute_python"
        assert "input_schema" in defs[0]
        assert defs[0]["input_schema"]["required"] == ["code"]


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

def _mock_text_block(text: str):
    """Create a mock TextBlock."""
    block = SimpleNamespace(type="text", text=text)
    return block


def _mock_tool_use_block(tool_id: str, name: str, input_data: dict):
    """Create a mock ToolUseBlock."""
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_data)


def _mock_response(content, stop_reason: str, input_tokens: int = 100, output_tokens: int = 50):
    """Create a mock API response."""
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_config() -> Config:
    return Config(api_key="test-key", audit_log="", logs_dir="")


class TestAgentLoop:
    @patch("sciralph.llm.anthropic.Anthropic")
    def test_end_turn_first_call(self, mock_anthropic_cls):
        """LLM returns text with end_turn on the first call."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_response(
            [_mock_text_block("Final answer.")], "end_turn", 200, 100
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

    @patch("sciralph.llm.anthropic.Anthropic")
    def test_one_tool_call_then_end(self, mock_anthropic_cls):
        """LLM calls a tool in round 1, then returns text in round 2."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Round 1: tool_use
        tool_block = _mock_tool_use_block("t1", "execute_python", {"code": "print(42)"})
        round1 = _mock_response([tool_block], "tool_use", 200, 80)
        # Round 2: end_turn
        round2 = _mock_response([_mock_text_block("Done. VERDICT: VERIFIED")], "end_turn", 300, 100)
        mock_client.messages.create.side_effect = [round1, round2]

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

    @patch("sciralph.llm.anthropic.Anthropic")
    def test_max_rounds_exhausted(self, mock_anthropic_cls):
        """LLM always returns tool_use — stops at max_rounds with forced final call."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        tool_block = _mock_tool_use_block("t1", "execute_python", {"code": "print(1)"})
        tool_response = _mock_response([tool_block], "tool_use", 100, 50)
        text_response = _mock_response(
            [_mock_text_block("## COMP-001\n**VERDICT:** INCONCLUSIVE")],
            "end_turn", 150, 80
        )
        mock_client.messages.create.side_effect = [
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
        assert mock_client.messages.create.call_count == 4

    @patch("sciralph.llm.anthropic.Anthropic")
    def test_token_accumulation(self, mock_anthropic_cls):
        """Tokens are summed across rounds."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        tool_block = _mock_tool_use_block("t1", "execute_python", {"code": "print(1)"})
        round1 = _mock_response([tool_block], "tool_use", 200, 80)
        round2 = _mock_response([tool_block], "tool_use", 300, 90)
        round3 = _mock_response([_mock_text_block("Done.")], "end_turn", 400, 100)
        mock_client.messages.create.side_effect = [round1, round2, round3]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.total_input_tokens == 900  # 200+300+400
        assert result.total_output_tokens == 270  # 80+90+100
        assert result.rounds == 3

    @patch("sciralph.llm.anthropic.Anthropic")
    def test_max_tokens_stop(self, mock_anthropic_cls):
        """stop_reason=max_tokens in round 1 returns truncated."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_response(
            [_mock_text_block("Partial...")], "max_tokens", 200, 16384
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

    @patch("sciralph.llm.anthropic.Anthropic")
    def test_forced_call_has_no_tools_param(self, mock_anthropic_cls):
        """Last call should NOT include tools parameter."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        tool_block = _mock_tool_use_block("t1", "execute_python", {"code": "print(1)"})
        tool_response = _mock_response([tool_block], "tool_use", 100, 50)
        text_response = _mock_response(
            [_mock_text_block("## COMP-001\n**VERDICT:** INCONCLUSIVE")],
            "end_turn", 150, 80
        )
        mock_client.messages.create.side_effect = [
            tool_response, tool_response,  # 2 rounds of tool use
            text_response,  # forced final call
        ]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=2,
        )

        # Verify the last call had no tools parameter
        calls = mock_client.messages.create.call_args_list
        assert len(calls) == 3
        # First two calls should have tools
        assert "tools" in calls[0].kwargs or (len(calls[0].args) > 0)
        # Last call should NOT have tools
        last_call_kwargs = calls[2].kwargs
        assert "tools" not in last_call_kwargs

    @patch("sciralph.llm.anthropic.Anthropic")
    def test_forced_call_text_in_result(self, mock_anthropic_cls):
        """Result text should come from the forced final call."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        tool_block = _mock_tool_use_block("t1", "execute_python", {"code": "print(1)"})
        tool_response = _mock_response([tool_block], "tool_use", 100, 50)
        text_response = _mock_response(
            [_mock_text_block("Forced partial output here")],
            "end_turn", 150, 80
        )
        mock_client.messages.create.side_effect = [tool_response, text_response]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert result.text == "Forced partial output here"
        assert result.stop_reason == "max_rounds_forced"

    @patch("sciralph.llm.anthropic.Anthropic")
    def test_token_accumulation_includes_forced(self, mock_anthropic_cls):
        """Total tokens should include the forced call."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        tool_block = _mock_tool_use_block("t1", "execute_python", {"code": "print(1)"})
        tool_response = _mock_response([tool_block], "tool_use", 200, 80)
        text_response = _mock_response(
            [_mock_text_block("Final")], "end_turn", 300, 120
        )
        mock_client.messages.create.side_effect = [tool_response, text_response]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert result.total_input_tokens == 500  # 200 + 300
        assert result.total_output_tokens == 200  # 80 + 120
        assert result.rounds == 2  # 1 tool round + 1 forced

    @patch("sciralph.llm.anthropic.Anthropic")
    def test_stop_reason_is_max_rounds_forced(self, mock_anthropic_cls):
        """Stop reason should be 'max_rounds_forced'."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        tool_block = _mock_tool_use_block("t1", "execute_python", {"code": "print(1)"})
        tool_response = _mock_response([tool_block], "tool_use", 100, 50)
        text_response = _mock_response([_mock_text_block("Done")], "end_turn", 100, 50)
        mock_client.messages.create.side_effect = [tool_response, text_response]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert result.stop_reason == "max_rounds_forced"
        assert result.truncated is True
