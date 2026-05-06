"""Tests for the max_tokens continuation retry mechanism.

Covers the shared helper :func:`open_dirac.llm.continue_on_max_tokens`
and its wiring into ``call_llm`` and the agentic loop.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from open_dirac.core.config import Config
from open_dirac.llm import (
    CONTINUATION_PROMPT,
    continue_on_max_tokens,
    _merge_responses,
)
from open_dirac.providers.base import ProviderResponse


def _make_config(**overrides) -> Config:
    defaults = dict(
        api_retry_max=0,  # no transient-error retries in unit tests
        api_retry_initial_delay=0.01,
        api_retry_max_delay=0.1,
        progress_check_interval=999,
        max_tokens_retries=2,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _truncated(text: str, **extra) -> ProviderResponse:
    """Build a ProviderResponse that looks like a max_tokens truncation."""
    kwargs = dict(
        text=text,
        input_tokens=100,
        output_tokens=50,
        stop_reason="max_tokens",
        reasoning_tokens=0,
        answer_tokens=50,
        tool_calls=None,
        raw_content=None,
    )
    kwargs.update(extra)
    return ProviderResponse(**kwargs)


def _ok(text: str, **extra) -> ProviderResponse:
    kwargs = dict(
        text=text,
        input_tokens=20,
        output_tokens=10,
        stop_reason="end_turn",
        reasoning_tokens=0,
        answer_tokens=10,
        tool_calls=None,
        raw_content=None,
    )
    kwargs.update(extra)
    return ProviderResponse(**kwargs)


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------


def test_skip_when_not_truncated():
    """Non-truncated response is returned unchanged, no provider calls made."""
    provider = MagicMock()
    resp = _ok("full answer")
    config = _make_config()

    result = continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is resp
    provider.call.assert_not_called()


def test_skip_when_tool_calls_present():
    """Truncated response with (partial) tool_calls is not continued."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    resp = _truncated(
        "reasoning...", tool_calls=[{"id": "1", "name": "foo", "input": {}}]
    )
    config = _make_config()

    result = continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is resp
    provider.call.assert_not_called()


def test_skip_when_visible_text_empty():
    """All-reasoning truncation (empty visible text) skips continuation."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    resp = _truncated("")
    config = _make_config()

    result = continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is resp
    provider.call.assert_not_called()


def test_skip_when_text_is_only_unclosed_think_tag():
    """OSS model truncated mid-<think> (no </think>, no visible answer yet)."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    resp = _truncated("<think>Let me think about this problem step by step, first")
    config = _make_config()

    result = continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    # strip_think_tags leaves empty visible content → skip.
    assert result is resp
    provider.call.assert_not_called()


def test_skip_when_max_tokens_retries_is_zero():
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    resp = _truncated("partial answer")
    config = _make_config(max_tokens_retries=0)

    result = continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is resp
    provider.call.assert_not_called()


# ---------------------------------------------------------------------------
# Successful continuation
# ---------------------------------------------------------------------------


def test_single_continuation_merges_text_and_tokens():
    """One continuation that ends cleanly produces a merged response."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    provider.call.return_value = _ok(" — and here is the rest.")

    resp = _truncated("Partial answer up to this point")
    config = _make_config(max_tokens_retries=2)

    result = continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert provider.call.call_count == 1
    # Merged text is original + continuation.
    assert result.text == "Partial answer up to this point — and here is the rest."
    assert result.stop_reason == "end_turn"
    # Tokens summed.
    assert result.input_tokens == 100 + 20
    assert result.output_tokens == 50 + 10


def test_continuation_sends_back_stripped_text_as_assistant():
    """For a truncated response with <think> tags, only the visible answer
    portion is sent back as the assistant turn on continuation."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    provider.call.return_value = _ok(" continued")

    resp = _truncated("<think>some reasoning</think>Partial visible answer")
    config = _make_config(max_tokens_retries=1)

    continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    # Inspect the messages passed to provider.call: should include
    # the visible-only assistant turn, NOT the <think> block.
    kwargs = provider.call.call_args.kwargs
    messages = kwargs["messages"]
    assert messages[0] == {"role": "user", "content": "q"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Partial visible answer"
    assert "<think>" not in messages[1]["content"]
    assert messages[2]["role"] == "user"
    assert messages[2]["content"] == CONTINUATION_PROMPT


def test_retries_exhausted_returns_merged_truncated():
    """If every continuation also truncates, the final merged response
    still has stop_reason=max_tokens (operator must raise max_output_tokens)."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    provider.call.side_effect = [
        _truncated(" chunk2"),
        _truncated(" chunk3"),
    ]

    resp = _truncated("chunk1")
    config = _make_config(max_tokens_retries=2)

    result = continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert provider.call.call_count == 2
    assert result.text == "chunk1 chunk2 chunk3"
    assert result.stop_reason == "max_tokens"
    # Sum of all three responses.
    assert result.output_tokens == 50 + 50 + 50


def test_continuation_can_produce_tool_call():
    """A truncated text-only response that continues into a tool call
    should surface the tool call in the merged response."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    tc = [{"id": "tc1", "name": "compute", "input": {"x": 1}}]
    provider.call.return_value = ProviderResponse(
        text="",
        input_tokens=20,
        output_tokens=5,
        stop_reason="tool_use",
        reasoning_tokens=0,
        answer_tokens=5,
        tool_calls=tc,
        raw_content=None,
    )

    resp = _truncated("Let me use the tool to compute this value,")
    config = _make_config(max_tokens_retries=1)

    result = continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result.stop_reason == "tool_use"
    assert result.tool_calls == tc


def test_continuation_call_uses_prepare_messages():
    """provider.prepare_messages is applied on the continuation call,
    so provider-specific context rewrites still fire."""
    provider = MagicMock()
    provider.prepare_messages.return_value = [
        {"role": "user", "content": "prepared"},
    ]
    provider.call.return_value = _ok(" end.")

    resp = _truncated("mid")
    config = _make_config(max_tokens_retries=1)

    continue_on_max_tokens(
        provider,
        resp,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    provider.prepare_messages.assert_called_once()
    # The call to provider.call should use the prepared messages.
    kwargs = provider.call.call_args.kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "prepared"}]


# ---------------------------------------------------------------------------
# Wiring into call_llm
# ---------------------------------------------------------------------------


def test_call_llm_invokes_continuation_on_truncation():
    """call_llm should auto-continue when the first response is truncated."""
    from open_dirac import llm

    config = _make_config(max_tokens_retries=1)

    fake_provider = MagicMock()
    fake_provider.prepare_messages.side_effect = lambda m: m
    fake_provider.call.side_effect = [
        _truncated("First half,"),
        _ok(" second half."),
    ]

    with patch.object(llm, "_get_provider", return_value=fake_provider):
        result = llm.call_llm("sys", "user q", config, agent_name="t", iteration=0)

    assert fake_provider.call.call_count == 2
    assert result.text == "First half, second half."
    assert result.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# Wiring into run_agent_loop
# ---------------------------------------------------------------------------


def test_run_agent_loop_continues_mid_round(tmp_path):
    """A truncated round that continues into a clean tool call should
    fold the continuation into the round and proceed normally."""
    from open_dirac import llm
    from open_dirac.state.tool_call import ToolCall

    config = _make_config(
        max_tokens_retries=1,
        max_tool_rounds=3,
        workspace_dir="",  # disable workspace logging
    )

    # Minimal tool executor mock.
    tool_executor = MagicMock()
    tool_executor.active_tools = None
    tool_executor.exit_tool_names = frozenset({"submit_answer"})
    tool_executor.ready_to_conclude_signaled = False
    tool_executor.stop_after_round = False
    tool_executor._script_names = []

    def fake_execute(name, args):
        tc = ToolCall(
            tool_name=name, tool_input=args, output="done", is_error=False, duration=0.0
        )
        # After tool call, signal stop so the loop ends cleanly.
        tool_executor.stop_after_round = True
        return tc

    tool_executor.execute.side_effect = fake_execute

    tools = [
        {
            "type": "function",
            "function": {"name": "submit_answer", "description": "", "parameters": {}},
        }
    ]

    fake_provider = MagicMock()
    fake_provider.prepare_messages.side_effect = lambda m: m
    fake_provider.format_assistant_message.side_effect = lambda c: {
        "role": "assistant",
        "content": str(c),
    }
    fake_provider.build_tool_result_messages.side_effect = lambda results: [
        {"role": "user", "content": f"result: {r['output']}"} for r in results
    ]

    # Round 1: truncated text, no tool call. Round 1-continuation: clean tool call.
    tc_info = [{"id": "tc1", "name": "submit_answer", "input": {"answer": "42"}}]
    fake_provider.call.side_effect = [
        _truncated("Let me submit"),
        ProviderResponse(
            text="",
            input_tokens=20,
            output_tokens=5,
            stop_reason="tool_use",
            reasoning_tokens=0,
            answer_tokens=5,
            tool_calls=tc_info,
            raw_content="raw",
        ),
    ]

    with patch.object(llm, "_get_provider", return_value=fake_provider):
        result = llm.run_agent_loop(
            system="s",
            user_content="q",
            config=config,
            tool_executor=tool_executor,
            tools=tools,
            max_rounds=3,
            agent_name="t",
            iteration=0,
        )

    # Two provider calls: initial (truncated) + continuation (produced the tool call).
    assert fake_provider.call.call_count == 2
    # The tool was executed.
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool_name == "submit_answer"
    assert result.truncated is False
    # Tokens from first (truncated) + continuation are summed.
    assert result.total_input_tokens == 100 + 20


def test_run_agent_loop_exits_truncated_when_continuation_fails(tmp_path):
    """If all continuation attempts also hit max_tokens, the loop exits
    with truncated=True as before."""
    from open_dirac import llm

    config = _make_config(
        max_tokens_retries=1,
        max_tool_rounds=3,
        workspace_dir="",
    )

    tool_executor = MagicMock()
    tool_executor.active_tools = None
    tool_executor.exit_tool_names = frozenset({"submit_answer"})
    tool_executor.ready_to_conclude_signaled = False
    tool_executor.stop_after_round = False
    tool_executor._script_names = []

    tools = [
        {
            "type": "function",
            "function": {"name": "submit_answer", "description": "", "parameters": {}},
        }
    ]

    fake_provider = MagicMock()
    fake_provider.prepare_messages.side_effect = lambda m: m
    fake_provider.format_assistant_message.side_effect = lambda c: {
        "role": "assistant",
        "content": str(c),
    }
    # Every call truncates — initial + continuation + (forced final).
    fake_provider.call.side_effect = [
        _truncated("first chunk"),
        _truncated(" second chunk"),
    ] + [_truncated(" more")] * 10  # forced-final path may call more times

    with patch.object(llm, "_get_provider", return_value=fake_provider):
        result = llm.run_agent_loop(
            system="s",
            user_content="q",
            config=config,
            tool_executor=tool_executor,
            tools=tools,
            max_rounds=3,
            agent_name="t",
            iteration=0,
        )

    # The round-level continuation fired once, both chunks merged, then the
    # loop exited with truncated=True.
    assert result.truncated is True
    assert "first chunk" in result.text
    # Merged tokens from round-1 initial + its continuation.
    assert result.total_input_tokens >= 100 + 100


# ---------------------------------------------------------------------------
# _merge_responses
# ---------------------------------------------------------------------------


def test_merge_responses_sums_tokens_and_concatenates_text():
    first = _truncated("AAA", reasoning_content="r1")
    cont = _ok("BBB", reasoning_content="r2")

    merged = _merge_responses(first, cont)

    assert merged.text == "AAABBB"
    assert merged.reasoning_content == "r1r2"
    assert merged.input_tokens == first.input_tokens + cont.input_tokens
    assert merged.output_tokens == first.output_tokens + cont.output_tokens
    # raw_content / tool_calls / stop_reason from the continuation.
    assert merged.stop_reason == cont.stop_reason


# ---------------------------------------------------------------------------
# Reasoning compaction
# ---------------------------------------------------------------------------


def test_compaction_fires_when_reasoning_content_available():
    """First continue attempt succeeds → 1 provider call."""
    from open_dirac.llm import _compact_reasoning

    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    provider.call.return_value = _ok("The answer is 42.")

    starved = _truncated(
        "",
        reasoning_content="Long reasoning about the problem...",
        reasoning_tokens=65536,
        answer_tokens=0,
    )
    config = _make_config(max_compaction_retries=2)

    result = _compact_reasoning(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is not None
    assert "The answer is 42." in result.text
    assert result.stop_reason == "end_turn"
    assert provider.call.call_count == 1


def test_compaction_uses_continue_prompt_first():
    """The first attempt uses the 'continue' prompt, not the force-answer prompt."""
    from open_dirac.llm import _compact_reasoning

    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    provider.call.return_value = _ok("Answer.")

    starved = _truncated(
        "",
        reasoning_content="Some reasoning...",
        reasoning_tokens=65536,
        answer_tokens=0,
    )
    config = _make_config(max_compaction_retries=2)

    _compact_reasoning(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    sent_messages = provider.call.call_args[1]["messages"]
    last_user_msg = sent_messages[-1]["content"]
    assert "Continue from where you left off" in last_user_msg
    assert "Do NOT continue reasoning" not in last_user_msg


def test_compaction_returns_none_without_reasoning():
    """Compaction is skipped when there is no reasoning_content."""
    from open_dirac.llm import _compact_reasoning

    provider = MagicMock()
    starved = _truncated("", reasoning_content="")
    config = _make_config(max_compaction_retries=2)

    result = _compact_reasoning(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is None
    provider.call.assert_not_called()


def test_compaction_returns_none_when_retries_zero():
    from open_dirac.llm import _compact_reasoning

    provider = MagicMock()
    starved = _truncated("", reasoning_content="some reasoning")
    config = _make_config(max_compaction_retries=0)

    result = _compact_reasoning(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is None


def test_compaction_continues_then_succeeds():
    """First continue starves, second continue succeeds → 2 calls, both 'continue'."""
    from open_dirac.llm import _compact_reasoning

    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m

    second_starve = ProviderResponse(
        text="",
        input_tokens=20,
        output_tokens=65536,
        stop_reason="max_tokens",
        reasoning_tokens=65536,
        answer_tokens=0,
        reasoning_content="Even more detailed reasoning...",
    )
    provider.call.side_effect = [second_starve, _ok("Final answer.")]

    starved = _truncated(
        "",
        reasoning_content="Initial reasoning...",
        reasoning_tokens=65536,
        answer_tokens=0,
    )
    config = _make_config(max_compaction_retries=2)

    result = _compact_reasoning(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is not None
    assert "Final answer." in result.text
    assert provider.call.call_count == 2
    # Both calls should have used the continue prompt
    for call_args in provider.call.call_args_list:
        last_msg = call_args[1]["messages"][-1]["content"]
        assert "Continue from where you left off" in last_msg


def test_compaction_force_answer_after_continues_exhausted():
    """All continue attempts starve → force-answer fires as final attempt.
    The force-answer prompt should contain accumulated reasoning from all
    prior attempts, not just the latest one."""
    from open_dirac.llm import _compact_reasoning

    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m

    starve_1 = ProviderResponse(
        text="",
        input_tokens=20,
        output_tokens=65536,
        stop_reason="max_tokens",
        reasoning_tokens=65536,
        answer_tokens=0,
        reasoning_content="Reasoning from attempt 1...",
    )
    starve_2 = ProviderResponse(
        text="",
        input_tokens=20,
        output_tokens=65536,
        stop_reason="max_tokens",
        reasoning_tokens=65536,
        answer_tokens=0,
        reasoning_content="Reasoning from attempt 2...",
    )
    # 2 continues starve, force-answer succeeds
    provider.call.side_effect = [starve_1, starve_2, _ok("Forced answer.")]

    starved = _truncated(
        "",
        reasoning_content="Initial reasoning...",
        reasoning_tokens=65536,
        answer_tokens=0,
    )
    config = _make_config(max_compaction_retries=2)

    result = _compact_reasoning(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is not None
    assert "Forced answer." in result.text
    # 2 continue + 1 force = 3 calls
    assert provider.call.call_count == 3
    # Last call should use force-answer prompt
    last_msg = provider.call.call_args_list[-1][1]["messages"][-1]["content"]
    assert "Do NOT continue reasoning" in last_msg
    # Force-answer should contain accumulated reasoning, not just the latest
    assert "Initial reasoning" in last_msg
    assert "Reasoning from attempt 1" in last_msg
    assert "Reasoning from attempt 2" in last_msg


def test_compaction_exhausted_returns_none():
    """All attempts (continues + force) starve → returns None."""
    from open_dirac.llm import _compact_reasoning

    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m

    always_starve = ProviderResponse(
        text="",
        input_tokens=20,
        output_tokens=65536,
        stop_reason="max_tokens",
        reasoning_tokens=65536,
        answer_tokens=0,
        reasoning_content="More reasoning that goes nowhere...",
    )
    provider.call.return_value = always_starve

    starved = _truncated(
        "",
        reasoning_content="Initial reasoning...",
        reasoning_tokens=65536,
        answer_tokens=0,
    )
    config = _make_config(max_compaction_retries=2)

    result = _compact_reasoning(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert result is None
    # 2 continue + 1 force = 3 total
    assert provider.call.call_count == 3


def test_compaction_integrates_with_continue_on_max_tokens():
    """End-to-end: continue_on_max_tokens calls compaction when reasoning is
    starved but reasoning_content is available."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m
    provider.call.return_value = _ok("Recovered answer.")

    starved = _truncated(
        "",
        reasoning_content="Deep physics reasoning about gauge invariance...",
        reasoning_tokens=65536,
        answer_tokens=0,
    )
    config = _make_config(max_compaction_retries=1)

    result = continue_on_max_tokens(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    assert "Recovered answer." in result.text
    assert result.stop_reason == "end_turn"
    # 1 continue attempt succeeds
    assert provider.call.call_count == 1


def test_compaction_truncated_answer_falls_through_to_continuation():
    """When compaction recovers visible text but is still truncated
    (stop_reason=max_tokens), continue_on_max_tokens runs the normal
    text-continuation path on it."""
    provider = MagicMock()
    provider.prepare_messages.side_effect = lambda m: m

    # Compaction produces partial answer, still truncated
    partial = ProviderResponse(
        text="The Hawking temperature is T = ",
        input_tokens=20,
        output_tokens=50,
        stop_reason="max_tokens",
    )
    # Text continuation completes it
    provider.call.side_effect = [partial, _ok("ℏκ/2π.")]

    starved = _truncated(
        "",
        reasoning_content="Deep reasoning about black hole thermodynamics...",
        reasoning_tokens=65536,
        answer_tokens=0,
    )
    config = _make_config(max_compaction_retries=1, max_tokens_retries=2)

    result = continue_on_max_tokens(
        provider,
        starved,
        config,
        model="m",
        max_tokens=100,
        system="s",
        messages=[{"role": "user", "content": "q"}],
    )

    # Should contain both the partial and the continuation
    assert "The Hawking temperature is T = " in result.text
    assert "ℏκ/2π." in result.text
    assert result.stop_reason == "end_turn"
    # 1 compaction call + 1 text continuation call = 2
    assert provider.call.call_count == 2
