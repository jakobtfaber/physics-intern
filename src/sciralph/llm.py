"""Anthropic API wrapper for SciRalph."""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from .config import Config

_call_seq: dict[int, int] = {}


@dataclass
class LLMResponse:
    """Response from an LLM call."""
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    duration: float


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


def _write_audit_entry(config: Config, resp: LLMResponse,
                       system: str, user_content: str,
                       agent_name: str, iteration: int):
    """Append one JSON line to the audit log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent": agent_name,
        "iteration": iteration,
        "model": config.model,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "stop_reason": resp.stop_reason,
        "duration_s": round(resp.duration, 2),
        "system_prompt_chars": len(system),
        "user_content_chars": len(user_content),
        "response_chars": len(resp.text),
    }
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
