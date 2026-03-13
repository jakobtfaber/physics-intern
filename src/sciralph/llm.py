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
from .workspace import log_scaffold_event

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
            **config.reasoning,
        )
    return _provider_cache[key]


_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_TRANSIENT_EXC_NAMES = {"ConnectionError", "TimeoutError", "ReadTimeout",
                         "ConnectTimeout", "ConnectionResetError",
                         "RemoteDisconnected", "BrokenPipeError"}


def _is_tool_call_failure(exc: Exception) -> bool:
    """Return True if *exc* is a tool-call generation failure (model emitted invalid JSON)."""
    msg = str(exc)
    return "tool_use_failed" in msg or "Failed to parse tool call arguments" in msg


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
    if status is not None and int(status) in _TRANSIENT_STATUS_CODES:
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
                log_scaffold_event(workspace_dir, iteration, 1, "api_retry",
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


def call_llm(system: str, user_content: str, config: Config,
             agent_name: str = "", iteration: int = 0) -> LLMResponse:
    """Call the LLM with retry on transient errors."""
    provider = _get_provider(config)

    start = time.time()
    resp = _call_provider_with_retry(
        provider, config,
        workspace_dir=config.workspace_dir,
        iteration=iteration,
        model=config.model,
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
    )

    if config.audit_log:
        _write_audit_entry(config, llm_response, system, user_content,
                           agent_name, iteration)

    _write_conversation_log(config, llm_response, system, user_content,
                            agent_name, iteration)

    return llm_response


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
    total_input = 0
    total_output = 0
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
                model=config.model,
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
                    log_scaffold_event(config.workspace_dir, iteration, 2, "tool_call_failure_fallback",
                                       f"round={round_num}")
                break
            raise
        duration = time.time() - start

        total_input += resp.input_tokens
        total_output += resp.output_tokens
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
        )
        if config.audit_log:
            _write_audit_entry(config, round_resp, system, user_content,
                               agent_name, iteration, round=round_num)
        _write_conversation_log(config, round_resp, system, user_content,
                                agent_name, iteration)

        # end_turn: done
        if resp.stop_reason == "end_turn":
            return AgentResult(
                text=round_text,
                tool_calls=all_tool_calls,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                rounds=round_num,
                truncated=False,
                duration=time.time() - overall_start,
                stop_reason="end_turn",
                token_alert_fired=token_alert_fired,
            )

        # max_tokens: truncated
        if resp.stop_reason == "max_tokens":
            return AgentResult(
                text=round_text,
                tool_calls=all_tool_calls,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                rounds=round_num,
                truncated=True,
                duration=time.time() - overall_start,
                stop_reason="max_tokens",
                token_alert_fired=token_alert_fired,
            )

        # tool_use: execute tools and continue
        if resp.stop_reason == "tool_use":
            messages.append(provider.format_assistant_message(resp.raw_content))

            tool_results = []
            for tc_info in resp.tool_calls:
                tc = tool_executor.execute(tc_info["name"], tc_info["input"])
                all_tool_calls.append(tc)
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
                        log_scaffold_event(config.workspace_dir, iteration, 3, "tool_timeout",
                                           f"round={round_num}")
                    elif "[... truncated" in tc.output or "[...truncated" in tc.output:
                        log_scaffold_event(config.workspace_dir, iteration, 3, "tool_output_truncation",
                                           f"round={round_num}")

            # Notify caller about round progress
            round_tool_calls = [tc for tc in all_tool_calls[-len(tool_results):]]
            if on_round:
                on_round(round_num, resp.stop_reason, round_tool_calls,
                         total_input, total_output)

            # Track consecutive zero-text rounds for early bailout
            cumulative_text_len += len(round_text.strip())
            if len(round_text.strip()) == 0:
                zero_text_streak += 1
            else:
                zero_text_streak = 0
            if zero_text_streak >= config.zero_text_bailout:
                if config.workspace_dir:
                    log_scaffold_event(config.workspace_dir, iteration, 2, "zero_text_bailout",
                                       f"streak={zero_text_streak}")
                break  # Falls through to forced final call

            # Low-cumulative-text bailout at halfway point (only for longer runs)
            if halfway >= 3 and round_num == halfway and cumulative_text_len < 100:
                low_cumulative_bailout = True
                if config.workspace_dir:
                    log_scaffold_event(config.workspace_dir, iteration, 2, "low_text_bailout",
                                       f"chars={cumulative_text_len}")
                break  # Falls through to forced final call

            # Checkpoint nudge at halfway point
            if round_num == config.checkpoint_round:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "CHECKPOINT: You are running low on available rounds. "
                            "Write your COMP entry text now alongside any remaining "
                            "tool calls. Do not defer all text to the final round."
                        ),
                    }],
                })

            # Final warning near end of loop
            if round_num == max_rounds - 2 and max_rounds >= 5:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "FINAL WARNING: You have 2 rounds left before forced "
                            "termination. Begin writing your COMP entry text NOW. "
                            "If you need one more tool call, make it in your next "
                            "response, but you MUST include your verdict text in "
                            "that same response. Do not defer text to the final round."
                        ),
                    }],
                })

            # CRITICAL penultimate-round instruction
            if round_num == max_rounds - 1 and max_rounds >= 4:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "CRITICAL: This is your LAST round with tool access. "
                            "You MUST include your ## COMP-NNN verdict text in THIS "
                            "response. If you only call a tool without writing text, "
                            "your output will be recorded as INCONCLUSIVE.\n\n"
                            "Write your verdict NOW using this format:\n"
                            "## COMP-NNN: [description]\n"
                            "**CLAIM:** [claim]\n"
                            "**METHOD:** [method]\n"
                            "**RESULT:** [results]\n"
                            "**VERDICT:** [VERIFIED / REFUTED / INCONCLUSIVE]\n"
                            "**NOTES:** [notes]"
                        ),
                    }],
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
    if config.workspace_dir:
        if tool_call_failure:
            _reason = "tool_call_failure"
        elif zero_text_streak >= config.zero_text_bailout:
            _reason = "zero_text"
        elif low_cumulative_bailout:
            _reason = "low_cumulative"
        else:
            _reason = "max_rounds"
        log_scaffold_event(config.workspace_dir, iteration, 2, "forced_final_call", _reason)

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

    start = time.time()
    final_resp = _call_provider_with_retry(
        provider, config,
        workspace_dir=config.workspace_dir,
        iteration=iteration,
        model=config.model,
        max_tokens=config.max_tokens,
        system=forced_system,
        messages=messages,
        # No tools — forces text-only response
    )
    dur = time.time() - start

    total_input += final_resp.input_tokens
    total_output += final_resp.output_tokens
    final_text = final_resp.text

    if on_round:
        on_round(round_num + 1, "forced_partial", [], total_input, total_output)

    if config.audit_log:
        _write_audit_entry(config, LLMResponse(
            final_text, final_resp.input_tokens,
            final_resp.output_tokens, "forced_partial", dur
        ), forced_system, user_content, agent_name, iteration,
        round=round_num + 1)

    return AgentResult(
        text=final_text,
        tool_calls=all_tool_calls,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        rounds=round_num + 1,
        truncated=True,
        duration=time.time() - overall_start,
        stop_reason="max_rounds_forced",
        token_alert_fired=token_alert_fired,
    )


def _write_audit_entry(config: Config, resp: LLMResponse,
                       system: str, user_content: str,
                       agent_name: str, iteration: int, round: int = 0):
    """Append one JSON line to the audit log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent": agent_name,
        "iteration": iteration,
        "model": config.model,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "stop_reason": resp.stop_reason,
        "duration_s": _round_num(resp.duration, 2),
        "system_prompt_chars": len(system),
        "user_content_chars": len(user_content),
        "response_chars": len(resp.text),
    }
    if round > 0:
        entry["round"] = round
    try:
        with open(config.audit_log, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


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

    content = f"""# LLM Call — iter {iteration}, {agent}, seq {seq}

| Field | Value |
|-------|-------|
| Timestamp | {timestamp} |
| Model | {config.model} |
| Input tokens | {resp.input_tokens} |
| Output tokens | {resp.output_tokens} |
| Duration | {resp.duration:.2f}s |
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
