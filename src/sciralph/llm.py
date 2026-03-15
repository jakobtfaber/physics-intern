"""LLM wrapper for SciRalph — provider-agnostic via providers/ adapters."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from .config import Config
from .providers import LLMProvider, ProviderResponse, create_provider
from .tools import ToolCall, ToolExecutor
from .categories import CompensationCategory as CC
from .workspace import log_llm_call, log_scaffold_event

console = Console()

_call_seq: dict[int, int] = {}
_round_num = round  # save builtin before parameter shadowing

# Provider cache: (provider_name, api_key) -> LLMProvider
_provider_cache: dict[tuple[str, str], LLMProvider] = {}


def _get_provider(config: Config) -> LLMProvider:
    """Create or retrieve a cached provider instance."""
    key = (config.provider, config.api_key)
    if key not in _provider_cache:
        _provider_cache[key] = create_provider(
            config.provider, api_key=config.api_key,
            timeout=config.api_timeout,
            thinking_token_headroom=config.thinking_token_headroom,
            **config.reasoning,
        )
    return _provider_cache[key]


_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_TRANSIENT_EXC_NAMES = {"ConnectionError", "TimeoutError", "ReadTimeout",
                         "ConnectTimeout", "ConnectionResetError",
                         "RemoteDisconnected", "BrokenPipeError",
                         "APITimeoutError"}


def _is_tool_call_failure(exc: Exception) -> bool:
    """Return True if *exc* is a tool-call generation failure.

    Covers both JSON parse failures and OSS models ignoring tool_choice=none.
    """
    msg = str(exc).lower()
    return any(p in msg for p in (
        "tool_use_failed",
        "failed to parse tool call arguments",
        "output_parse_failed",      # HF backend can't parse non-tool output
        "tool choice",              # "Tool choice is none, but model called a tool"
    ))


_PROVIDER_SIDE_400_PATTERNS = {
    "post processor",       # HuggingFace "gpt oss post processor" internal error
    "internal error",
    "backend error",
}


def _is_transient(exc: Exception) -> bool:
    """Return True if *exc* looks like a transient / retryable API error."""
    # Tool-call generation failures are stochastic — retry may produce valid JSON
    if _is_tool_call_failure(exc):
        return True
    # Check HTTP status code — try direct attrs first, then exc.response.status_code
    # (httpx / huggingface_hub store the code on a nested response object)
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is None:
        resp = getattr(exc, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
    if status is not None:
        status = int(status)
        if status in _TRANSIENT_STATUS_CODES:
            return True
        # Some providers return 400 for server-side processing failures —
        # treat as transient when the message matches known patterns
        if status == 400:
            msg_lower = str(exc).lower()
            if any(p in msg_lower for p in _PROVIDER_SIDE_400_PATTERNS):
                return True
    # Check exception type name anywhere in the MRO
    for cls in type(exc).__mro__:
        if cls.__name__ in _TRANSIENT_EXC_NAMES:
            return True
    return False


def _call_provider_with_retry(provider: LLMProvider, config: Config,
                               workspace_dir: str | Path = "",
                               iteration: int = 0,
                               **call_kwargs) -> ProviderResponse:
    """Retry provider.call() on transient errors with exponential backoff."""
    delay = config.api_retry_initial_delay
    for attempt in range(config.api_retry_max + 1):
        try:
            return provider.call(**call_kwargs)
        except Exception as exc:
            if not _is_transient(exc) or attempt == config.api_retry_max:
                raise
            console.print(
                f"[yellow]Transient API error (attempt {attempt + 1}/"
                f"{config.api_retry_max}): {exc}[/yellow]"
            )
            if workspace_dir:
                log_scaffold_event(workspace_dir, iteration, CC.CALL_RELIABILITY, "api_retry",
                                   f"attempt={attempt + 1}/{config.api_retry_max}, {type(exc).__name__}")
            time.sleep(min(delay, config.api_retry_max_delay))
            delay *= 2
    # Unreachable — the loop always returns or raises
    raise RuntimeError("unreachable")  # pragma: no cover


@dataclass
class LLMResponse:
    """Response from an LLM call."""
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    duration: float
    reasoning_tokens: int = 0
    answer_tokens: int = 0


@dataclass
class AgentResult:
    """Result from a multi-round tool-use agent loop."""
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    rounds: int = 0
    truncated: bool = False
    duration: float = 0.0
    stop_reason: str = "end_turn"
    token_alert_fired: bool = False
    total_reasoning_tokens: int = 0
    total_answer_tokens: int = 0


def call_llm(system: str, user_content: str, config: Config,
             agent_name: str = "", iteration: int = 0) -> LLMResponse:
    """Call the LLM with retry on transient errors."""
    provider = _get_provider(config)

    start = time.time()
    resp = _call_provider_with_retry(
        provider, config,
        workspace_dir=config.workspace_dir,
        iteration=iteration,
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    duration = time.time() - start

    llm_response = LLMResponse(
        text=resp.text,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        stop_reason=resp.stop_reason,
        duration=duration,
        reasoning_tokens=resp.reasoning_tokens,
        answer_tokens=resp.answer_tokens,
    )

    if config.workspace_dir:
        log_llm_call(
            config.workspace_dir, agent_name, iteration, config.model,
            llm_response.input_tokens, llm_response.output_tokens,
            llm_response.stop_reason, round(llm_response.duration, 2),
            len(system), len(user_content), len(llm_response.text),
            reasoning_tokens=llm_response.reasoning_tokens,
            answer_tokens=llm_response.answer_tokens,
        )

    _write_conversation_log(config, llm_response, system, user_content,
                            agent_name, iteration)

    return llm_response


def _make_text_checkpoint_call(
    provider: LLMProvider, config: Config, system: str,
    messages: list[dict], round_num: int, iteration: int,
    agent_name: str = "",
) -> tuple[str, int, int, int, int]:
    """Force a text-only checkpoint call mid-loop to extract intermediate findings.

    Returns (text, input_tokens, output_tokens, reasoning_tokens, answer_tokens).
    """
    checkpoint_system = (
        system + "\n\n"
        "TEXT CHECKPOINT: You have been executing tools without writing any text. "
        "Pause and write a brief summary of your intermediate findings so far — "
        "what you have computed, what the results show, and what remains. "
        "You will be able to continue using tools afterward."
    )
    start = time.time()
    resp = _call_provider_with_retry(
        provider, config,
        workspace_dir=config.workspace_dir,
        iteration=iteration,
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=checkpoint_system,
        messages=messages,
        # No tools — forces text-only
    )
    dur = time.time() - start

    text = resp.text.strip()

    if config.workspace_dir:
        log_llm_call(
            config.workspace_dir, agent_name, iteration, config.model,
            resp.input_tokens, resp.output_tokens, "text_checkpoint",
            _round_num(dur, 2), len(checkpoint_system), 0, len(text),
            reasoning_tokens=resp.reasoning_tokens,
            answer_tokens=resp.answer_tokens, round=round_num,
        )

    return text, resp.input_tokens, resp.output_tokens, resp.reasoning_tokens, resp.answer_tokens


def _synthesize_from_tool_history(all_tool_calls: list[ToolCall]) -> str:
    """Build a COMP entry from tool execution history when the model produced no text."""
    successful = [tc for tc in all_tool_calls if not tc.is_error]
    errored = [tc for tc in all_tool_calls if tc.is_error]

    if successful:
        last = successful[-1]
        code_excerpt = (last.tool_input.get("code", "") if isinstance(last.tool_input, dict)
                        else "")[:500]
        output_excerpt = last.output[:500]
        method = f"Executed {len(successful)} script(s); last code:\n```python\n{code_excerpt}\n```"
        result = f"Last output:\n```\n{output_excerpt}\n```"
        if errored:
            result += f"\n({len(errored)} execution(s) errored)"
    elif errored:
        last_err = errored[-1]
        err_excerpt = last_err.output[:500]
        method = f"Attempted {len(errored)} script(s), all errored"
        result = f"Last error:\n```\n{err_excerpt}\n```"
    else:
        method = "Agent produced no tool output"
        result = "No results obtained"

    return (
        "## COMP-000: Incomplete verification\n"
        f"**CLAIM:** (unable to extract)\n"
        f"**METHOD:** {method}\n"
        f"**RESULT:** {result}\n"
        "**VERDICT:** INCONCLUSIVE\n"
        "**NOTES:** Agent completed tool calls but failed to produce "
        "written output after forced retry.\n"
    )


def run_agent_loop(
    system: str,
    user_content: str,
    config: Config,
    tool_executor: ToolExecutor,
    tools: list[dict],
    max_rounds: int = 10,
    agent_name: str = "",
    iteration: int = 0,
    on_round: Callable[[int, str, list[ToolCall], int, int], None] | None = None,
) -> AgentResult:
    """Run a tool-use agent loop until end_turn, max_rounds, or max_tokens."""
    provider = _get_provider(config)
    messages = [{"role": "user", "content": user_content}]

    all_tool_calls: list[ToolCall] = []
    round_log: list[dict] = []
    total_input = 0
    total_output = 0
    total_reasoning = 0
    total_answer = 0
    zero_text_streak = 0
    cumulative_text_len = 0
    low_cumulative_bailout = False
    halfway = max_rounds // 2
    token_alert_fired = False
    overall_start = time.time()

    tool_call_failure = False
    for round_num in range(1, max_rounds + 1):
        start = time.time()
        try:
            resp = _call_provider_with_retry(
                provider, config,
                workspace_dir=config.workspace_dir,
                iteration=iteration,
                model=config.model_id,
                max_tokens=config.max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )
        except Exception as exc:
            if _is_tool_call_failure(exc):
                console.print(
                    f"[yellow]Tool-call generation failed after retries "
                    f"(round {round_num}): {exc} — falling back to "
                    f"text-only response[/yellow]"
                )
                tool_call_failure = True
                if config.workspace_dir:
                    log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY, "tool_call_failure_fallback",
                                       f"round={round_num}")
                break
            raise
        duration = time.time() - start

        round_log.append({
            "kind": "llm_response", "round": round_num,
            "text": resp.text, "tool_calls": resp.tool_calls,
            "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
            "reasoning_tokens": resp.reasoning_tokens, "answer_tokens": resp.answer_tokens,
            "stop_reason": resp.stop_reason, "duration": duration,
        })

        total_input += resp.input_tokens
        total_output += resp.output_tokens
        total_reasoning += resp.reasoning_tokens
        total_answer += resp.answer_tokens
        if total_input > config.computation_token_alert:
            token_alert_fired = True

        # Audit + conversation log for this round
        round_text = resp.text
        round_resp = LLMResponse(
            text=round_text,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            stop_reason=resp.stop_reason,
            duration=duration,
            reasoning_tokens=resp.reasoning_tokens,
            answer_tokens=resp.answer_tokens,
        )
        if config.workspace_dir:
            log_llm_call(
                config.workspace_dir, agent_name, iteration, config.model,
                round_resp.input_tokens, round_resp.output_tokens,
                round_resp.stop_reason, _round_num(round_resp.duration, 2),
                len(system), len(user_content), len(round_resp.text),
                reasoning_tokens=round_resp.reasoning_tokens,
                answer_tokens=round_resp.answer_tokens, round=round_num,
            )

        # end_turn: done (unless empty text after tool calls — fall through to forced final call)
        if resp.stop_reason == "end_turn":
            if not round_text.strip() and all_tool_calls:
                # Model ended turn with no text after tool calls — force a text-only final call
                if config.workspace_dir:
                    log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY,
                                       "empty_end_turn_fallthrough", f"rounds={round_num}")
                break  # fall through to forced final call
            result = AgentResult(
                text=round_text,
                tool_calls=all_tool_calls,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                rounds=round_num,
                truncated=False,
                duration=time.time() - overall_start,
                stop_reason="end_turn",
                token_alert_fired=token_alert_fired,
                total_reasoning_tokens=total_reasoning,
                total_answer_tokens=total_answer,
            )
            _write_agent_conversation_log(
                config, system, user_content, agent_name,
                iteration, round_log, result)
            return result

        # max_tokens: truncated
        if resp.stop_reason == "max_tokens":
            result = AgentResult(
                text=round_text,
                tool_calls=all_tool_calls,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                rounds=round_num,
                truncated=True,
                duration=time.time() - overall_start,
                stop_reason="max_tokens",
                token_alert_fired=token_alert_fired,
                total_reasoning_tokens=total_reasoning,
                total_answer_tokens=total_answer,
            )
            _write_agent_conversation_log(
                config, system, user_content, agent_name,
                iteration, round_log, result)
            return result

        # tool_use: execute tools and continue
        if resp.stop_reason == "tool_use":
            messages.append(provider.format_assistant_message(resp.raw_content))

            tool_results = []
            for tc_info in resp.tool_calls:
                tc = tool_executor.execute(tc_info["name"], tc_info["input"])
                all_tool_calls.append(tc)
                round_log.append({
                    "kind": "tool_result", "round": round_num,
                    "tool_name": tc.tool_name, "tool_input": tc.tool_input,
                    "output": tc.output, "is_error": tc.is_error,
                    "duration": tc.duration,
                })
                tool_results.append({
                    "tool_call_id": tc_info["id"],
                    "name": tc_info["name"],
                    "output": tc.output,
                    "is_error": tc.is_error,
                })

            messages.extend(provider.build_tool_result_messages(tool_results))

            if config.workspace_dir:
                for tc in all_tool_calls[-len(tool_results):]:
                    if tc.output.startswith("TIMEOUT:"):
                        log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY, "tool_timeout",
                                           f"round={round_num}")
                    elif "[... truncated" in tc.output or "[...truncated" in tc.output:
                        log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY, "tool_output_truncation",
                                           f"round={round_num}")

            # Notify caller about round progress
            round_tool_calls = [tc for tc in all_tool_calls[-len(tool_results):]]
            if on_round:
                on_round(round_num, resp.stop_reason, round_tool_calls,
                         total_input, total_output)

            # Executor-signaled early stop (e.g., orchestrator's set_next_task)
            if getattr(tool_executor, "stop_after_round", False):
                if config.workspace_dir:
                    log_scaffold_event(config.workspace_dir, iteration, CC.LOOP_CONTROL,
                                       "executor_stop_signal",
                                       f"round={round_num}, agent={agent_name}")
                result = AgentResult(
                    text=round_text,
                    tool_calls=all_tool_calls,
                    total_input_tokens=total_input,
                    total_output_tokens=total_output,
                    total_reasoning_tokens=total_reasoning,
                    total_answer_tokens=total_answer,
                    rounds=round_num,
                    truncated=False,
                    stop_reason="executor_stop",
                    duration=time.time() - overall_start,
                    token_alert_fired=token_alert_fired,
                )
                _write_agent_conversation_log(
                    config, system, user_content, agent_name,
                    iteration, round_log, result)
                return result

            # Track consecutive zero-text rounds for early bailout
            cumulative_text_len += len(round_text.strip())
            if len(round_text.strip()) == 0:
                zero_text_streak += 1
            else:
                zero_text_streak = 0
            # Interleaved text checkpoint: force text output before bailout fires
            if (zero_text_streak > 0
                    and zero_text_streak % config.text_checkpoint_interval == 0
                    and zero_text_streak < config.zero_text_bailout):
                if config.workspace_dir:
                    log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY,
                                       "text_checkpoint", f"streak={zero_text_streak}")
                round_log.append({
                    "kind": "scaffold_injection", "round": round_num,
                    "label": "text_checkpoint",
                    "content": "TEXT CHECKPOINT: Force text-only call to extract intermediate findings.",
                })
                try:
                    cp_text, cp_in, cp_out, cp_reasoning, cp_answer = _make_text_checkpoint_call(
                        provider, config, system, messages,
                        round_num, iteration, agent_name,
                    )
                    total_input += cp_in
                    total_output += cp_out
                    total_reasoning += cp_reasoning
                    total_answer += cp_answer
                    round_log.append({
                        "kind": "checkpoint_response", "round": round_num,
                        "text": cp_text,
                        "input_tokens": cp_in, "output_tokens": cp_out,
                    })
                    if cp_text:
                        zero_text_streak = 0
                        cumulative_text_len += len(cp_text)
                        messages.append({"role": "assistant", "content": cp_text})
                        messages.append({"role": "user", "content": "Good. Continue with your tool calls."})
                except Exception as exc:
                    console.print(
                        f"[yellow]Text checkpoint failed (round {round_num}): "
                        f"{type(exc).__name__}: {exc} — continuing[/yellow]"
                    )
                    if config.workspace_dir:
                        log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY,
                                           "text_checkpoint_failed",
                                           f"round={round_num}, {type(exc).__name__}")

            if zero_text_streak >= config.zero_text_bailout:
                if config.workspace_dir:
                    log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY, "zero_text_bailout",
                                       f"streak={zero_text_streak}")
                break  # Falls through to forced final call

            # Low-cumulative-text bailout at halfway point (only for longer runs)
            if halfway >= 3 and round_num == halfway and cumulative_text_len < config.low_text_bailout_chars:
                low_cumulative_bailout = True
                if config.workspace_dir:
                    log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY, "low_text_bailout",
                                       f"chars={cumulative_text_len}")
                break  # Falls through to forced final call

            # Checkpoint nudge at halfway point
            if round_num == config.checkpoint_round:
                _nudge_msg = (
                    "CHECKPOINT: You are running low on available rounds. "
                    "Finish your analysis and call `submit_verdict` with your "
                    "findings, or write your COMP entry text now alongside any "
                    "remaining tool calls. Do not defer all text to the final round."
                )
                messages.append({"role": "user", "content": _nudge_msg})
                round_log.append({
                    "kind": "scaffold_injection", "round": round_num,
                    "label": "checkpoint_nudge", "content": _nudge_msg,
                })

            # Final warning near end of loop
            if round_num == max_rounds - 2 and max_rounds >= 5:
                _final_warn_msg = (
                    "FINAL WARNING: You have 2 rounds left before forced "
                    "termination. Call `submit_verdict` with your findings NOW. "
                    "If you need one more tool call, make it in your next "
                    "response, but you MUST call `submit_verdict` or include "
                    "your verdict text in that same response. Do not defer "
                    "to the final round."
                )
                messages.append({"role": "user", "content": _final_warn_msg})
                round_log.append({
                    "kind": "scaffold_injection", "round": round_num,
                    "label": "final_warning", "content": _final_warn_msg,
                })

            # CRITICAL penultimate-round instruction
            if round_num == max_rounds - 1 and max_rounds >= 4:
                _crit_msg = (
                    "CRITICAL: This is your LAST round with tool access. "
                    "You MUST call `submit_verdict` NOW with your findings. "
                    "If you cannot use `submit_verdict`, write your "
                    "## COMP-NNN verdict text in THIS response instead. "
                    "If you only call `execute_python` without a verdict, "
                    "your output will be recorded as INCONCLUSIVE.\n\n"
                    "Preferred: call `submit_verdict` with claim, method, "
                    "result, verdict, and notes.\n\n"
                    "Alternative — write free text:\n"
                    "## COMP-NNN: [description]\n"
                    "**CLAIM:** [claim]\n"
                    "**METHOD:** [method]\n"
                    "**RESULT:** [results]\n"
                    "**VERDICT:** [VERIFIED / REFUTED / INCONCLUSIVE]\n"
                    "**NOTES:** [notes]"
                )
                messages.append({"role": "user", "content": _crit_msg})
                round_log.append({
                    "kind": "scaffold_injection", "round": round_num,
                    "label": "critical_warning", "content": _crit_msg,
                })

    # Exhausted max_rounds or zero-text/low-cumulative/tool-failure bailout — force one final text-only call
    if tool_call_failure:
        reason = (
            "IMPORTANT: The tool-calling interface is unavailable due to a "
            "provider error. You cannot call any tools. "
        )
    elif zero_text_streak >= config.zero_text_bailout:
        reason = (
            "IMPORTANT: You were terminated early because you stopped producing "
            "text for multiple consecutive rounds. "
        )
    elif low_cumulative_bailout:
        reason = (
            "IMPORTANT: You were terminated early because you produced very little "
            "text output across multiple rounds. You must write substantive analysis "
            "alongside tool calls, not defer all text to the end. "
        )
    else:
        reason = (
            "IMPORTANT: You have reached the maximum number of tool-use rounds. "
        )
    if tool_call_failure:
        _reason = "tool_call_failure"
    elif zero_text_streak >= config.zero_text_bailout:
        _reason = "zero_text"
    elif low_cumulative_bailout:
        _reason = "low_cumulative"
    else:
        _reason = "max_rounds"
    if config.workspace_dir:
        log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY, "forced_final_call", _reason)

    forced_system = (
        system + "\n\n"
        + reason
        + "You cannot call any more tools. You MUST now write your final "
        "COMP-NNN entry with whatever results you have so far.\n\n"
        "Use this exact format:\n\n"
        "## COMP-NNN: [description]\n"
        "**CLAIM:** [claim being verified]\n"
        "**METHOD:** [approach used]\n"
        "**RESULT:** [numerical results or observations]\n"
        "**VERDICT:** INCONCLUSIVE\n"
        "**NOTES:** [what was computed and what remains]\n\n"
        "If you have no results at all, write:\n"
        "## COMP-NNN: Incomplete verification\n"
        "**CLAIM:** [original claim]\n"
        "**METHOD:** Attempted numerical verification\n"
        "**RESULT:** No conclusive results obtained within round limit.\n"
        "**VERDICT:** INCONCLUSIVE\n"
        "**NOTES:** Verification incomplete — ran out of tool-use rounds.\n"
    )

    final_text = ""
    final_in = final_out = final_reasoning = final_answer = 0
    final_dur = 0.0
    try:
        start = time.time()
        final_resp = _call_provider_with_retry(
            provider, config,
            workspace_dir=config.workspace_dir,
            iteration=iteration,
            model=config.model_id,
            max_tokens=config.max_tokens,
            system=forced_system,
            messages=messages,
            # No tools — forces text-only response
        )
        final_dur = time.time() - start
        final_in = final_resp.input_tokens
        final_out = final_resp.output_tokens
        final_reasoning = final_resp.reasoning_tokens
        final_answer = final_resp.answer_tokens
        total_input += final_in
        total_output += final_out
        total_reasoning += final_reasoning
        total_answer += final_answer
        final_text = final_resp.text.strip()
    except Exception as exc:
        console.print(
            f"[yellow]Forced final call failed: {type(exc).__name__}: {exc} "
            f"— synthesizing from tool history[/yellow]"
        )
        if config.workspace_dir:
            log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY,
                               "forced_final_call_failed",
                               f"{type(exc).__name__}: {str(exc)[:200]}")

    if not final_text:
        # Forced call produced nothing or failed — retry once with minimal prompt
        if config.workspace_dir:
            log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY,
                               "forced_call_retry", "empty forced final call")
        try:
            retry_resp = _call_provider_with_retry(
                provider, config,
                workspace_dir=config.workspace_dir,
                iteration=iteration,
                model=config.model_id,
                max_tokens=config.max_tokens,
                system=forced_system,
                messages=messages + [{"role": "assistant", "content": ""},
                                     {"role": "user", "content": "Write ONLY a COMP entry with VERDICT: INCONCLUSIVE. Nothing else."}],
            )
            final_text = retry_resp.text.strip()
            total_input += retry_resp.input_tokens
            total_output += retry_resp.output_tokens
            total_reasoning += retry_resp.reasoning_tokens
            total_answer += retry_resp.answer_tokens
        except Exception:
            final_text = ""

        if not final_text:
            # Still nothing — synthesize from tool history so the entry exists
            if config.workspace_dir:
                log_scaffold_event(config.workspace_dir, iteration, CC.CALL_RELIABILITY,
                                   "tool_history_synthesis",
                                   f"tool_calls={len(all_tool_calls)}")
            final_text = _synthesize_from_tool_history(all_tool_calls)

    if on_round:
        on_round(round_num + 1, "forced_partial", [], total_input, total_output)

    round_log.append({
        "kind": "forced_final_call", "round": round_num + 1,
        "reason": _reason, "text": final_text,
        "input_tokens": final_in, "output_tokens": final_out,
        "reasoning_tokens": final_reasoning, "answer_tokens": final_answer,
        "duration": final_dur,
    })

    if config.workspace_dir:
        log_llm_call(
            config.workspace_dir, agent_name, iteration, config.model,
            final_in, final_out, "forced_partial",
            _round_num(final_dur, 2), len(forced_system), len(user_content),
            len(final_text), reasoning_tokens=final_reasoning,
            answer_tokens=final_answer, round=round_num + 1,
        )

    result = AgentResult(
        text=final_text,
        tool_calls=all_tool_calls,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        rounds=round_num + 1,
        truncated=True,
        duration=time.time() - overall_start,
        stop_reason="max_rounds_forced",
        token_alert_fired=token_alert_fired,
        total_reasoning_tokens=total_reasoning,
        total_answer_tokens=total_answer,
    )
    _write_agent_conversation_log(
        config, system, user_content, agent_name,
        iteration, round_log, result)
    return result


def _write_conversation_log(config: Config, resp: LLMResponse,
                            system: str, user_content: str,
                            agent_name: str, iteration: int):
    """Write a per-call Markdown file with full prompts and response."""
    if not config.logs_dir:
        return

    seq = _call_seq.get(iteration, 0) + 1
    _call_seq[iteration] = seq

    agent = agent_name or "unknown"
    filename = f"iter{iteration:03d}_{agent}_{seq}.md"
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    reasoning_rows = ""
    if resp.reasoning_tokens > 0:
        reasoning_rows = (
            f"| Reasoning tokens | {resp.reasoning_tokens} |\n"
            f"| Answer tokens | {resp.answer_tokens} |\n"
        )

    content = f"""# LLM Call — iter {iteration}, {agent}, seq {seq}

| Field | Value |
|-------|-------|
| Timestamp | {timestamp} |
| Model | {config.model} |
| Input tokens | {resp.input_tokens} |
| Output tokens | {resp.output_tokens} |
{reasoning_rows}| Duration | {resp.duration:.2f}s |
| Stop reason | {resp.stop_reason} |

## System Prompt

{system}

## User Content

{user_content}

## Response

{resp.text}
"""
    try:
        Path(config.logs_dir, filename).write_text(content)
    except OSError:
        pass


def _render_tool_input(name: str, input_data) -> str:
    """Render tool input for conversation log display."""
    if name == "execute_python" and isinstance(input_data, dict) and "code" in input_data:
        purpose = input_data.get("purpose", "")
        purpose_line = f"**Purpose:** {purpose}\n\n" if purpose else ""
        return f"{purpose_line}~~~python\n{input_data['code']}\n~~~"
    if name == "submit_verdict" and isinstance(input_data, dict):
        verdict = input_data.get("verdict", "?")
        claim = input_data.get("claim", "?")
        return f"**Verdict: {verdict}** for {claim}"
    try:
        return f"```json\n{json.dumps(input_data, indent=2)}\n```"
    except (TypeError, ValueError):
        return f"```\n{input_data}\n```"


def _write_agent_conversation_log(
    config: Config, system: str, user_content: str,
    agent_name: str, iteration: int,
    round_log: list[dict], result: AgentResult,
):
    """Write a single comprehensive Markdown log for a tool-use agent invocation."""
    if not config.logs_dir:
        return

    seq = _call_seq.get(iteration, 0) + 1
    _call_seq[iteration] = seq

    agent = agent_name or "unknown"
    filename = f"iter{iteration:03d}_{agent}_{seq}.md"

    lines: list[str] = []
    lines.append(f"# Agent Conversation — iter {iteration}, {agent}\n")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Model | {config.model} |")
    lines.append(f"| Total rounds | {result.rounds} |")
    lines.append(f"| Total input tokens | {result.total_input_tokens} |")
    lines.append(f"| Total output tokens | {result.total_output_tokens} |")
    if result.total_reasoning_tokens > 0:
        lines.append(f"| Total reasoning tokens | {result.total_reasoning_tokens} |")
        lines.append(f"| Total answer tokens | {result.total_answer_tokens} |")
    lines.append(f"| Total duration | {result.duration:.1f}s |")
    lines.append(f"| Stop reason | {result.stop_reason} |")
    lines.append("")

    # System prompt in collapsible section
    lines.append("## System Prompt\n")
    lines.append("<details>")
    lines.append(f"<summary>System prompt ({len(system)} chars)</summary>\n")
    lines.append(system)
    lines.append("\n</details>\n")

    # User content in collapsible section
    lines.append("## User Content\n")
    lines.append("<details>")
    lines.append(f"<summary>User content ({len(user_content)} chars)</summary>\n")
    lines.append(user_content)
    lines.append("\n</details>\n")

    # Render chronological events
    for entry in round_log:
        kind = entry["kind"]

        if kind == "llm_response":
            lines.append("---\n")
            lines.append(f"## Round {entry['round']}\n")
            lines.append("### LLM Response")
            tok_str = f"{entry['input_tokens']} in / {entry['output_tokens']} out"
            if entry.get("reasoning_tokens", 0) > 0:
                tok_str += f" ({entry['reasoning_tokens']} reasoning, {entry['answer_tokens']} answer)"
            lines.append(
                f"**Tokens:** {tok_str} | **Duration:** {entry['duration']:.1f}s "
                f"| **Stop:** {entry['stop_reason']}\n"
            )
            text = entry.get("text", "")
            if text and text.strip():
                lines.append(text.strip())
                lines.append("")
            else:
                lines.append("*(no text output)*\n")
            if entry.get("tool_calls"):
                for tc_info in entry["tool_calls"]:
                    tc_name = tc_info.get("name", "unknown")
                    tc_input = tc_info.get("input", {})
                    lines.append(f"**Tool call: {tc_name}**")
                    lines.append(_render_tool_input(tc_name, tc_input))
                    lines.append("")

        elif kind == "tool_result":
            status = "error" if entry.get("is_error") else "success"
            dur_str = f"{entry['duration']:.1f}s, " if entry.get("duration") else ""
            lines.append(f"### Tool Result — {entry['tool_name']} ({dur_str}{status})")
            lines.append(f"```\n{entry['output']}\n```\n")

        elif kind == "scaffold_injection":
            lines.append("---\n")
            lines.append(f"### Scaffold — {entry['label']}\n")
            lines.append(entry.get("content", ""))
            lines.append("")

        elif kind == "checkpoint_response":
            lines.append("### Checkpoint Response")
            lines.append(
                f"**Tokens:** {entry['input_tokens']} in / {entry['output_tokens']} out\n"
            )
            text = entry.get("text", "")
            if text and text.strip():
                lines.append(text.strip())
            else:
                lines.append("*(no text output)*")
            lines.append("")

        elif kind == "forced_final_call":
            lines.append("---\n")
            lines.append(f"## Forced Final Call (reason: {entry.get('reason', 'unknown')})")
            tok_str = f"{entry['input_tokens']} in / {entry['output_tokens']} out"
            if entry.get("reasoning_tokens", 0) > 0:
                tok_str += f" ({entry['reasoning_tokens']} reasoning, {entry['answer_tokens']} answer)"
            dur_str = f" | **Duration:** {entry['duration']:.1f}s" if entry.get("duration") else ""
            lines.append(f"**Tokens:** {tok_str}{dur_str}\n")
            text = entry.get("text", "")
            if text and text.strip():
                lines.append(text.strip())
            else:
                lines.append("*(no text output)*")
            lines.append("")

    content = "\n".join(lines) + "\n"
    try:
        Path(config.logs_dir, filename).write_text(content)
    except OSError:
        pass
