"""Anthropic API wrapper for SciRalph."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from .config import Config
from .tools import ToolCall, ToolExecutor

_call_seq: dict[int, int] = {}
_round_num = round  # save builtin before parameter shadowing


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
    """Call the Anthropic API. Stateless, no retry logic (caller handles that)."""
    client = anthropic.Anthropic(api_key=config.api_key)

    start = time.time()
    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    duration = time.time() - start

    text = response.content[0].text if response.content else ""

    llm_response = LLMResponse(
        text=text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason,
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
    client = anthropic.Anthropic(api_key=config.api_key)
    messages = [{"role": "user", "content": user_content}]

    all_tool_calls: list[ToolCall] = []
    total_input = 0
    total_output = 0
    zero_text_streak = 0
    token_alert_fired = False
    overall_start = time.time()

    for round_num in range(1, max_rounds + 1):
        start = time.time()
        response = client.messages.create(
            model=config.model,
            max_tokens=config.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
        duration = time.time() - start

        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens
        if total_input > config.computation_token_alert:
            token_alert_fired = True

        # Audit + conversation log for this round
        round_text = _extract_text(response.content)
        round_resp = LLMResponse(
            text=round_text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            duration=duration,
        )
        if config.audit_log:
            _write_audit_entry(config, round_resp, system, user_content,
                               agent_name, iteration, round=round_num)
        _write_conversation_log(config, round_resp, system, user_content,
                                agent_name, iteration)

        # end_turn: done
        if response.stop_reason == "end_turn":
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
        if response.stop_reason == "max_tokens":
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
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tc = tool_executor.execute(block.name, block.input)
                    all_tool_calls.append(tc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tc.output,
                        "is_error": tc.is_error,
                    })

            messages.append({"role": "user", "content": tool_results})

            # Notify caller about round progress
            round_tool_calls = [tc for tc in all_tool_calls[-len(tool_results):]]
            if on_round:
                on_round(round_num, response.stop_reason, round_tool_calls,
                         total_input, total_output)

            # Track consecutive zero-text rounds for early bailout
            if len(round_text.strip()) == 0:
                zero_text_streak += 1
            else:
                zero_text_streak = 0
            if zero_text_streak >= config.zero_text_bailout:
                break  # Falls through to forced final call

            # Checkpoint nudge at halfway point
            if round_num == config.checkpoint_round:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "CHECKPOINT: You have used half your available rounds. "
                            "Write your COMP entry text now alongside any remaining "
                            "tool calls. Do not defer all text to the final round."
                        ),
                    }],
                })

    # Exhausted max_rounds or zero-text bailout — force one final text-only call
    if zero_text_streak >= config.zero_text_bailout:
        reason = (
            "IMPORTANT: You were terminated early because you stopped producing "
            "text for multiple consecutive rounds. "
        )
    else:
        reason = (
            "IMPORTANT: You have reached the maximum number of tool-use rounds. "
        )
    forced_system = (
        system + "\n\n"
        + reason
        + "You cannot call any more tools. You MUST now write your final "
        "COMP-NNN entry with whatever results you have so far.\n\n"
        "Format: ## COMP-NNN header, **CLAIM**, **METHOD**, **RESULT**, "
        "**VERDICT** (use INCONCLUSIVE if incomplete), **NOTES**.\n"
        "Summarize what you computed successfully and what remains."
    )

    start = time.time()
    final_response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=forced_system,
        messages=messages,
        # NO tools parameter — forces text-only response
    )
    dur = time.time() - start

    total_input += final_response.usage.input_tokens
    total_output += final_response.usage.output_tokens
    final_text = _extract_text(final_response.content)

    if on_round:
        on_round(round_num + 1, "forced_partial", [], total_input, total_output)

    if config.audit_log:
        _write_audit_entry(config, LLMResponse(
            final_text, final_response.usage.input_tokens,
            final_response.usage.output_tokens, "forced_partial", dur
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


def _extract_text(content_blocks) -> str:
    """Concatenate all TextBlock.text from response content."""
    parts = []
    for block in content_blocks:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


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
