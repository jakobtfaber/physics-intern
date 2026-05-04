"""LLM call helpers shared by the one-shot, two-step and RSA baselines.

Wraps ``providers.retry.call_with_retry`` with an agent-agnostic retry
callback that logs transient errors to stderr (baselines have no workspace
log channel by default), applies ``llm.continue_on_max_tokens`` when the
response is truncated, and computes USD cost from the model-registry
entries on the ``Config``. Returns a structured dict.
"""

from __future__ import annotations

import sys
import time

from ..core.config import Config
from ..llm import continue_on_max_tokens
from ..providers import LLMProvider, ProviderResponse, call_with_retry


def run_baseline_call(
    provider: LLMProvider,
    config: Config,
    *,
    system: str,
    user_message: str,
    agent_name: str,
) -> dict:
    """Execute one LLM call with retry + max-tokens continuation + cost calc.

    Returns a dict with keys::

        tokens: {"input", "output", "reasoning", "answer"}
        duration_s, cost_usd, stop_reason, response_text
    """

    def _on_retry(exc: Exception, attempt: int, max_retries: int) -> None:
        print(
            f"  Transient error (attempt {attempt + 1}/{max_retries}): {exc}",
            file=sys.stderr,
        )

    start = time.time()
    initial_messages = [{"role": "user", "content": user_message}]
    resp = call_with_retry(
        provider,
        max_retries=config.api_retry_max,
        initial_delay=config.api_retry_initial_delay,
        max_delay=config.api_retry_max_delay,
        on_retry=_on_retry,
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=system,
        messages=initial_messages,
    )
    if resp.stop_reason == "max_tokens":
        resp = continue_on_max_tokens(
            provider,
            resp,
            config,
            model=config.model_id,
            max_tokens=config.max_tokens,
            system=system,
            messages=initial_messages,
            workspace_dir=config.workspace_dir,
            agent_name=agent_name,
        )
    duration = time.time() - start

    cost_usd = 0.0
    if config.input_cost or config.output_cost:
        cost_usd = (
            resp.input_tokens * config.input_cost
            + resp.output_tokens * config.output_cost
        ) / 1_000_000

    return {
        "tokens": {
            "input": resp.input_tokens,
            "output": resp.output_tokens,
            "reasoning": resp.reasoning_tokens or 0,
            "answer": resp.answer_tokens or 0,
        },
        "duration_s": round(duration, 2),
        "cost_usd": round(cost_usd, 6),
        "stop_reason": resp.stop_reason,
        "response_text": resp.text,
    }


def _single_call(
    provider: LLMProvider,
    config: Config,
    *,
    system: str,
    messages: list[dict],
    agent_name: str,
    on_retry,
) -> ProviderResponse:
    """One ``call_with_retry`` + ``continue_on_max_tokens`` round-trip.

    Returns the (possibly continuation-merged) ``ProviderResponse``.
    """
    resp = call_with_retry(
        provider,
        max_retries=config.api_retry_max,
        initial_delay=config.api_retry_initial_delay,
        max_delay=config.api_retry_max_delay,
        on_retry=on_retry,
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=system,
        messages=messages,
    )
    if resp.stop_reason == "max_tokens":
        resp = continue_on_max_tokens(
            provider,
            resp,
            config,
            model=config.model_id,
            max_tokens=config.max_tokens,
            system=system,
            messages=messages,
            workspace_dir=config.workspace_dir,
            agent_name=agent_name,
        )
    return resp


def run_two_step_call(
    provider: LLMProvider,
    config: Config,
    *,
    system: str,
    derive_user_message: str,
    parse_user_message: str,
    agent_name: str,
) -> dict:
    """Execute critpt's two-step procedure: derive, then populate template.

    Reproduces ``solve_with_parse(__parse=False)`` from
    ``../critpt/src/critpt/generation/solve_with_parse.py``:

    1. Send the problem text only with the two-step system prompt → derivation.
    2. Append the assistant turn (plain text — matching critpt's default
       ``keep_reasoning_block=False`` path) and a new user turn carrying the
       parse instruction + code template; same system prompt is reused.
    3. The second response is the populated code block.

    Returns a dict shaped like ``run_baseline_call`` (so the runner can reuse
    the same stderr/cost machinery) plus an extra ``derivation_text`` key for
    the call-1 output, and ``per_call`` with per-call breakdowns.
    """

    def _on_retry(exc: Exception, attempt: int, max_retries: int) -> None:
        print(
            f"  Transient error (attempt {attempt + 1}/{max_retries}): {exc}",
            file=sys.stderr,
        )

    start = time.time()

    # --- Call 1: derive ---
    derive_messages = [{"role": "user", "content": derive_user_message}]
    resp1 = _single_call(
        provider, config,
        system=system,
        messages=derive_messages,
        agent_name=agent_name,
        on_retry=_on_retry,
    )
    derivation_text = resp1.text or ""

    # --- Call 2: parse ---
    parse_messages = list(derive_messages) + [
        {"role": "assistant", "content": derivation_text},
        {"role": "user", "content": parse_user_message},
    ]
    resp2 = _single_call(
        provider, config,
        system=system,
        messages=parse_messages,
        agent_name=agent_name,
        on_retry=_on_retry,
    )

    duration = time.time() - start

    # Aggregated tokens across both calls
    total_input = resp1.input_tokens + resp2.input_tokens
    total_output = resp1.output_tokens + resp2.output_tokens
    total_reasoning = (resp1.reasoning_tokens or 0) + (resp2.reasoning_tokens or 0)
    total_answer = (resp1.answer_tokens or 0) + (resp2.answer_tokens or 0)

    cost_usd = 0.0
    if config.input_cost or config.output_cost:
        cost_usd = (
            total_input * config.input_cost
            + total_output * config.output_cost
        ) / 1_000_000

    def _per_call(resp: ProviderResponse) -> dict:
        return {
            "input": resp.input_tokens,
            "output": resp.output_tokens,
            "reasoning": resp.reasoning_tokens or 0,
            "answer": resp.answer_tokens or 0,
            "stop_reason": resp.stop_reason,
        }

    return {
        "tokens": {
            "input": total_input,
            "output": total_output,
            "reasoning": total_reasoning,
            "answer": total_answer,
        },
        "duration_s": round(duration, 2),
        "cost_usd": round(cost_usd, 6),
        "stop_reason": resp2.stop_reason,
        "response_text": resp2.text,
        "derivation_text": derivation_text,
        "per_call": {
            "derive": _per_call(resp1),
            "parse": _per_call(resp2),
        },
    }
