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
        assert len(defs) == 3
        names = {d["function"]["name"] for d in defs}
        assert names == {"execute_python", "submit_verdict", "report_progress"}
        for d in defs:
            assert d["type"] == "function"

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
        assert set(props.keys()) == {"target_id", "claim", "method", "result", "verdict", "notes"}
        assert props["verdict"]["enum"] == ["VERIFIED", "REFUTED", "INCONCLUSIVE"]
        assert set(func["parameters"]["required"]) == {"target_id", "claim", "method", "result", "verdict", "notes"}

    def test_report_progress_schema(self):
        func = ToolExecutor.TOOL_DEFINITIONS[2]["function"]
        assert func["name"] == "report_progress"
        props = func["parameters"]["properties"]
        assert set(props.keys()) == {"findings_so_far", "remaining_questions", "ready_to_conclude"}
        assert props["ready_to_conclude"]["type"] == "boolean"
        assert set(func["parameters"]["required"]) == {"findings_so_far", "remaining_questions", "ready_to_conclude"}


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
        params = {"target_id": "WH-001", "claim": "energy", "method": "numerical",
                  "result": "ok", "verdict": "VERIFIED", "notes": "All checks pass."}
        tc = executor.execute("submit_verdict", params)
        assert not tc.is_error
        assert "VERIFIED" in tc.output
        assert executor.stop_after_round is True

    def test_stores_last_verdict(self):
        executor = _make_executor()
        params = {"target_id": "WH-002", "claim": "partition", "method": "symbolic",
                  "result": "mismatch", "verdict": "REFUTED", "notes": "Discrepancy found."}
        executor.execute("submit_verdict", params)
        assert executor._last_verdict == params

    def test_output_message(self):
        executor = _make_executor()
        tc = executor.execute("submit_verdict", {"target_id": "WH-001", "claim": "c",
                                                  "method": "m", "result": "r",
                                                  "verdict": "INCONCLUSIVE", "notes": "n"})
        assert tc.output == "Verdict recorded: INCONCLUSIVE"
        assert tc.tool_name == "submit_verdict"


class TestSubmitResult:
    def test_sets_stop_flag(self):
        executor = _make_executor()
        params = {"target_id": "WH-001", "description": "Computed F(p)",
                  "method": "numerical", "result": "F(p) = 0.99",
                  "confidence": "approximate", "notes": "Convergent."}
        tc = executor.execute("submit_result", params)
        assert not tc.is_error
        assert "WH-001" in tc.output
        assert executor.stop_after_round is True

    def test_stores_last_result(self):
        executor = _make_executor()
        params = {"target_id": "WH-002", "description": "Computed entropy",
                  "method": "analytical", "result": "S = k ln(W)",
                  "confidence": "exact", "notes": "Standard formula."}
        executor.execute("submit_result", params)
        assert executor._last_result == params

    def test_output_message(self):
        executor = _make_executor()
        tc = executor.execute("submit_result", {"target_id": "WH-003",
                                                 "description": "d", "method": "m",
                                                 "result": "r", "confidence": "partial",
                                                 "notes": "n"})
        assert "WH-003" in tc.output
        assert "partial" in tc.output
        assert tc.tool_name == "submit_result"


class TestToolSetsForTaskType:
    def test_explore_tools(self):
        from sciralph.task import TaskType
        tools = ToolExecutor.tools_for_task_type(TaskType.COMPUTE_EXPLORE)
        names = {t["function"]["name"] for t in tools}
        assert names == {"execute_python", "submit_result", "report_progress"}

    def test_verify_tools(self):
        from sciralph.task import TaskType
        tools = ToolExecutor.tools_for_task_type(TaskType.COMPUTE_VERIFY)
        names = {t["function"]["name"] for t in tools}
        assert names == {"execute_python", "submit_verdict", "report_progress"}

    def test_legacy_compute_tools(self):
        from sciralph.task import TaskType
        tools = ToolExecutor.tools_for_task_type(TaskType.COMPUTE)
        names = {t["function"]["name"] for t in tools}
        assert names == {"execute_python", "submit_verdict", "report_progress"}


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


class TestReportProgress:
    def test_report_progress_does_not_stop(self):
        executor = _make_executor()
        params = {"findings_so_far": "42 found", "remaining_questions": "none",
                  "ready_to_conclude": True}
        tc = executor.execute("report_progress", params)
        assert not tc.is_error
        assert not hasattr(executor, "stop_after_round") or not getattr(executor, "stop_after_round", False)

    def test_report_progress_ready_message(self):
        executor = _make_executor()
        tc = executor.execute("report_progress", {
            "findings_so_far": "done", "remaining_questions": "",
            "ready_to_conclude": True,
        })
        assert "submit_verdict" in tc.output

    def test_report_progress_not_ready_message(self):
        executor = _make_executor()
        tc = executor.execute("report_progress", {
            "findings_so_far": "partial", "remaining_questions": "need more data",
            "ready_to_conclude": False,
        })
        assert "Continue" in tc.output
        assert "need more data" in tc.output


def _make_config(**overrides) -> Config:
    defaults = dict(api_key="test-key", logs_dir="", provider="anthropic",
                    progress_check_interval=999)  # disable progress checks by default in tests
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

        # Verify the last call had no tools parameter and system prompt is unchanged
        calls = provider.call.call_args_list
        assert len(calls) == 3
        # First two calls should have tools
        assert calls[0].kwargs.get("tools") is not None
        # Last call should NOT have tools
        assert calls[2].kwargs.get("tools") is None
        # System prompt is unchanged (no mutation)
        assert calls[2].kwargs.get("system") == "sys"

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



class TestProgressCheckInLoop:
    """Test progress check injection after consecutive execute_python calls."""

    @patch("sciralph.llm._get_provider")
    def test_progress_check_injected_after_n_rounds(self, mock_get_provider):
        """Progress check message injected after progress_check_interval consecutive exec_python."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        exec_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        # After 3 exec_python rounds, progress check is injected;
        # model calls report_progress then submit_verdict
        progress_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t2", "name": "report_progress",
                         "input": {"findings_so_far": "42", "remaining_questions": "",
                                   "ready_to_conclude": True}}],
        )
        verdict_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t3", "name": "submit_verdict",
                         "input": {"claim": "WH-001", "method": "num", "result": "ok",
                                   "verdict": "VERIFIED", "notes": "done"}}],
        )
        provider.call.side_effect = [exec_resp, exec_resp, exec_resp, progress_resp, verdict_resp]

        config = _make_config(progress_check_interval=3)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        assert result.stop_reason == "executor_stop"
        # Check progress check message was injected in round 4's messages
        calls = provider.call.call_args_list
        round4_messages = calls[3].kwargs["messages"]
        progress_found = any(
            isinstance(msg.get("content"), str) and "PROGRESS CHECK" in msg["content"]
            for msg in round4_messages
            if isinstance(msg, dict) and msg.get("role") == "user"
        )
        assert progress_found, "Progress check message should be injected after 3 exec_python rounds"

    @patch("sciralph.llm._get_provider")
    def test_no_progress_check_before_interval(self, mock_get_provider):
        """No progress check if fewer than N exec_python rounds."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        exec_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        final_resp = _mock_provider_response("Done.", "end_turn", 100, 50)
        provider.call.side_effect = [exec_resp, exec_resp, final_resp]

        config = _make_config(progress_check_interval=3)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        assert result.stop_reason == "end_turn"
        # No PROGRESS CHECK in any messages
        for call in provider.call.call_args_list:
            msgs = call.kwargs.get("messages", [])
            for msg in msgs:
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    assert "PROGRESS CHECK" not in msg["content"]

    @patch("sciralph.llm._get_provider")
    def test_progress_check_resets_after_report_progress(self, mock_get_provider):
        """Counter resets after report_progress; no injection at next interval unless earned."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        exec_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )
        progress_resp = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t2", "name": "report_progress",
                         "input": {"findings_so_far": "partial", "remaining_questions": "more",
                                   "ready_to_conclude": False}}],
        )
        final_resp = _mock_provider_response("Done.", "end_turn", 100, 50)
        # 2 exec → progress → 1 exec → end (no second progress check since only 1 after reset)
        provider.call.side_effect = [exec_resp, exec_resp, progress_resp, exec_resp, final_resp]

        config = _make_config(progress_check_interval=2)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="q",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        assert result.stop_reason == "end_turn"
        # Progress check should have fired once (after round 2), not after round 4.
        # Count PROGRESS CHECK messages in the LAST call's messages (they accumulate).
        last_messages = provider.call.call_args_list[-1].kwargs["messages"]
        progress_count = sum(
            1 for msg in last_messages
            if isinstance(msg, dict) and isinstance(msg.get("content"), str)
            and "PROGRESS CHECK" in msg["content"]
        )
        assert progress_count == 1



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
