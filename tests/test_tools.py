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
        assert len(defs) == 2
        assert defs[0]["type"] == "function"
        assert defs[1]["type"] == "function"
        names = {d["function"]["name"] for d in defs}
        assert names == {"execute_python", "submit_verdict"}

    def test_execute_python_requires_purpose(self):
        func = ToolExecutor.TOOL_DEFINITIONS[0]["function"]
        assert func["name"] == "execute_python"
        props = func["parameters"]["properties"]
        assert "purpose" in props
        assert props["purpose"]["type"] == "string"
        assert set(func["parameters"]["required"]) == {"purpose", "code"}

    def test_submit_verdict_schema(self):
        func = ToolExecutor.TOOL_DEFINITIONS[1]["function"]
        assert func["name"] == "submit_verdict"
        props = func["parameters"]["properties"]
        assert set(props.keys()) == {"claim", "method", "result", "verdict", "notes"}
        assert props["verdict"]["enum"] == ["VERIFIED", "REFUTED", "INCONCLUSIVE"]
        assert set(func["parameters"]["required"]) == {"claim", "method", "result", "verdict", "notes"}


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


class TestSubmitVerdict:
    def test_sets_stop_flag(self):
        executor = _make_executor()
        params = {"claim": "WH-001", "method": "numerical", "result": "ok",
                  "verdict": "VERIFIED", "notes": "All checks pass."}
        tc = executor.execute("submit_verdict", params)
        assert not tc.is_error
        assert "VERIFIED" in tc.output
        assert executor.stop_after_round is True

    def test_stores_last_verdict(self):
        executor = _make_executor()
        params = {"claim": "WH-002", "method": "symbolic", "result": "mismatch",
                  "verdict": "REFUTED", "notes": "Discrepancy found."}
        executor.execute("submit_verdict", params)
        assert executor._last_verdict == params

    def test_output_message(self):
        executor = _make_executor()
        tc = executor.execute("submit_verdict", {"claim": "c", "method": "m",
                                                  "result": "r", "verdict": "INCONCLUSIVE",
                                                  "notes": "n"})
        assert tc.output == "Verdict recorded: INCONCLUSIVE"
        assert tc.tool_name == "submit_verdict"


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


def _make_config(**overrides) -> Config:
    defaults = dict(api_key="test-key", logs_dir="", provider="anthropic",
                    text_checkpoint_interval=999)  # disable checkpoints by default in tests
    defaults.update(overrides)
    return Config(**defaults)


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


class TestEmptyTextFallthrough:
    """Test Gap A: end_turn with empty text falls through to forced final call."""

    @patch("sciralph.llm._get_provider")
    def test_empty_end_turn_falls_through_to_forced_call(self, mock_get_provider):
        """end_turn with empty text after tool calls triggers forced final call."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        # Round 1: tool_use with code execution
        round1 = _mock_provider_response(
            "", "tool_use", 200, 80,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(42)"}}],
        )
        # Round 2: end_turn but empty text (the Gemini gap)
        round2 = _mock_provider_response("", "end_turn", 150, 0)
        # Round 3: forced final call produces text
        round3 = _mock_provider_response(
            "## COMP-001: Result\n**VERDICT:** INCONCLUSIVE", "end_turn", 300, 100
        )
        provider.call.side_effect = [round1, round2, round3]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert "COMP-001" in result.text
        assert result.stop_reason == "max_rounds_forced"
        assert provider.call.call_count == 3  # tool_use + end_turn + forced

    @patch("sciralph.llm._get_provider")
    def test_empty_end_turn_with_text_returns_normally(self, mock_get_provider):
        """end_turn with actual text should return normally (no fallthrough)."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        # Round 1: tool_use
        round1 = _mock_provider_response(
            "", "tool_use", 200, 80,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        # Round 2: end_turn with real text
        round2 = _mock_provider_response("## COMP-001\n**VERDICT:** VERIFIED", "end_turn", 300, 100)
        provider.call.side_effect = [round1, round2]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.stop_reason == "end_turn"
        assert "VERIFIED" in result.text
        assert provider.call.call_count == 2  # no forced call


class TestForcedCallRetry:
    """Test Gap B: forced final call produces empty text."""

    @patch("sciralph.llm._get_provider")
    def test_forced_call_retry_on_empty(self, mock_get_provider):
        """Empty forced final call triggers one retry; retry text is used."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_response = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        # Forced final call: empty text
        empty_response = _mock_provider_response("", "end_turn", 150, 0)
        # Retry: produces text
        retry_response = _mock_provider_response(
            "## COMP-001: Retry result\n**VERDICT:** INCONCLUSIVE", "end_turn", 200, 80
        )
        provider.call.side_effect = [tool_response, empty_response, retry_response]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert "COMP-001" in result.text
        assert "Retry result" in result.text
        assert result.stop_reason == "max_rounds_forced"
        # 1 tool round + 1 forced (empty) + 1 retry = 3 calls
        assert provider.call.call_count == 3
        # Tokens from retry are accumulated
        assert result.total_input_tokens == 100 + 150 + 200
        assert result.total_output_tokens == 50 + 0 + 80

    @patch("sciralph.llm._get_provider")
    def test_forced_call_stub_on_double_empty(self, mock_get_provider):
        """Both forced call and retry return empty — engine-generated stub is used."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_response = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        # Both forced final call and retry: empty text
        empty1 = _mock_provider_response("", "end_turn", 150, 0)
        empty2 = _mock_provider_response("   ", "end_turn", 120, 0)
        provider.call.side_effect = [tool_response, empty1, empty2]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert "COMP-000: Incomplete verification" in result.text
        assert "**VERDICT:** INCONCLUSIVE" in result.text
        assert result.stop_reason == "max_rounds_forced"
        assert provider.call.call_count == 3


class TestInterleavedTextCheckpoint:
    """Test interleaved text checkpoints that fire before zero_text_bailout."""

    @patch("sciralph.llm._get_provider")
    def test_checkpoint_fires_at_interval(self, mock_get_provider):
        """Checkpoint fires after text_checkpoint_interval consecutive zero-text rounds."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        # Checkpoint call returns text
        checkpoint_resp = _mock_provider_response("Intermediate: result is 42", "end_turn", 80, 40)
        # Round 3 continues with tool use then produces text
        final_resp = _mock_provider_response("## COMP-001\n**VERDICT:** VERIFIED", "end_turn", 200, 100)
        provider.call.side_effect = [
            tool_resp,    # round 1: tool, no text → streak=1
            tool_resp,    # round 2: tool, no text → streak=2 → checkpoint fires
            checkpoint_resp,  # checkpoint call → streak resets to 0
            final_resp,   # round 3: end_turn with text
        ]

        config = _make_config()
        config.text_checkpoint_interval = 2
        config.zero_text_bailout = 3
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert "VERIFIED" in result.text
        assert result.stop_reason == "end_turn"
        # Checkpoint tokens are accumulated
        assert result.total_input_tokens == 100 + 100 + 80 + 200
        assert result.total_output_tokens == 50 + 50 + 40 + 100

    @patch("sciralph.llm._get_provider")
    def test_checkpoint_failure_allows_bailout(self, mock_get_provider):
        """If checkpoint produces no text, streak stays → bailout fires next round."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        # Checkpoint returns empty text
        empty_checkpoint = _mock_provider_response("", "end_turn", 80, 0)
        # Forced final call
        forced_resp = _mock_provider_response("## COMP-001\n**VERDICT:** INCONCLUSIVE", "end_turn", 200, 100)
        provider.call.side_effect = [
            tool_resp,         # round 1: streak=1
            tool_resp,         # round 2: streak=2 → checkpoint fires
            empty_checkpoint,  # checkpoint: no text → streak stays 2
            tool_resp,         # round 3: streak=3 → bailout
            forced_resp,       # forced final call
        ]

        config = _make_config()
        config.text_checkpoint_interval = 2
        config.zero_text_bailout = 3
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        assert result.stop_reason == "max_rounds_forced"
        assert "INCONCLUSIVE" in result.text

    @patch("sciralph.llm._get_provider")
    def test_no_checkpoint_when_text_present(self, mock_get_provider):
        """No checkpoint fires when model produces text in every round."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        text_tool_resp = _mock_provider_response(
            "working on it...", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        final_resp = _mock_provider_response("## COMP-001\n**VERDICT:** VERIFIED", "end_turn", 200, 100)
        provider.call.side_effect = [text_tool_resp, text_tool_resp, final_resp]

        config = _make_config()
        config.text_checkpoint_interval = 2
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.stop_reason == "end_turn"
        # Only 3 calls (no checkpoint call)
        assert provider.call.call_count == 3

    @patch("sciralph.llm._get_provider")
    def test_checkpoint_tokens_accumulated(self, mock_get_provider):
        """Checkpoint call tokens are included in totals."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        checkpoint_resp = _mock_provider_response("mid-result", "end_turn", 120, 60)
        final_resp = _mock_provider_response("Done", "end_turn", 200, 100)
        provider.call.side_effect = [tool_resp, tool_resp, checkpoint_resp, final_resp]

        config = _make_config()
        config.text_checkpoint_interval = 2
        config.zero_text_bailout = 3
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.total_input_tokens == 100 + 100 + 120 + 200
        assert result.total_output_tokens == 50 + 50 + 60 + 100

    @patch("sciralph.llm._get_provider")
    def test_checkpoint_does_not_fire_at_bailout_threshold(self, mock_get_provider):
        """Checkpoint should not fire when streak equals zero_text_bailout."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        forced_resp = _mock_provider_response("## COMP-001\n**VERDICT:** INCONCLUSIVE", "end_turn", 200, 100)

        # With interval=3 and bailout=3, checkpoint should NOT fire at streak=3
        # because the guard requires streak < bailout
        provider.call.side_effect = [tool_resp, tool_resp, tool_resp, forced_resp]

        config = _make_config()
        config.text_checkpoint_interval = 3
        config.zero_text_bailout = 3
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        assert result.stop_reason == "max_rounds_forced"
        # 3 tool rounds + 1 forced = 4 (no checkpoint call)
        assert provider.call.call_count == 4


class TestToolHistorySynthesis:
    """Test _synthesize_from_tool_history helper."""

    def test_successful_calls(self):
        from sciralph.llm import _synthesize_from_tool_history
        tc = ToolCall(
            tool_name="execute_python",
            tool_input={"code": "print(42)"},
            output="42",
            is_error=False,
            duration=0.1,
        )
        result = _synthesize_from_tool_history([tc])
        assert "COMP-000" in result
        assert "INCONCLUSIVE" in result
        assert "print(42)" in result
        assert "42" in result

    def test_errored_calls(self):
        from sciralph.llm import _synthesize_from_tool_history
        tc = ToolCall(
            tool_name="execute_python",
            tool_input={"code": "1/0"},
            output="ZeroDivisionError: division by zero",
            is_error=True,
            duration=0.1,
        )
        result = _synthesize_from_tool_history([tc])
        assert "COMP-000" in result
        assert "all errored" in result
        assert "ZeroDivisionError" in result

    def test_empty_calls(self):
        from sciralph.llm import _synthesize_from_tool_history
        result = _synthesize_from_tool_history([])
        assert "COMP-000" in result
        assert "no tool output" in result.lower()

    @patch("sciralph.llm._get_provider")
    def test_synthesis_replaces_old_stub(self, mock_get_provider):
        """Double-empty forced call now uses tool-history synthesis."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tool_response = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python",
                         "input": {"code": "import numpy; print(numpy.pi)"}}],
        )
        empty1 = _mock_provider_response("", "end_turn", 150, 0)
        empty2 = _mock_provider_response("", "end_turn", 120, 0)
        provider.call.side_effect = [tool_response, empty1, empty2]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=1,
        )

        assert "COMP-000" in result.text
        assert "INCONCLUSIVE" in result.text
        # Should contain actual code from tool history
        assert "numpy" in result.text


class TestSubmitVerdictInLoop:
    """Test submit_verdict triggers executor_stop in agent loop."""

    @patch("sciralph.llm._get_provider")
    def test_submit_verdict_stops_loop(self, mock_get_provider):
        """Round 1: execute_python, round 2: submit_verdict → executor_stop."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        # Round 1: tool_use with execute_python
        round1 = _mock_provider_response(
            "Computing fidelity...", "tool_use", 200, 80,
            tool_calls=[{"id": "t1", "name": "execute_python",
                         "input": {"purpose": "Check fidelity", "code": "print(1.0)"}}],
        )
        # Round 2: tool_use with submit_verdict
        round2 = _mock_provider_response(
            "", "tool_use", 150, 60,
            tool_calls=[{"id": "t2", "name": "submit_verdict",
                         "input": {"claim": "WH-002", "method": "numerical",
                                   "result": "fidelity=1.0", "verdict": "VERIFIED",
                                   "notes": "All checks pass."}}],
        )
        provider.call.side_effect = [round1, round2]

        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=_make_config(), tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        assert result.stop_reason == "executor_stop"
        assert result.rounds == 2
        assert len(result.tool_calls) == 2
        assert result.tool_calls[1].tool_name == "submit_verdict"
        assert not result.truncated
        # No forced final call — only 2 provider calls
        assert provider.call.call_count == 2
