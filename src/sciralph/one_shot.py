#!/usr/bin/env python3
"""One-shot LLM baseline for SciRalph problems.

Sends a single LLM call with the problem statement and collects the response.
Uses the provider layer (sciralph.providers + sciralph.config) but nothing from
the engine, agents, or tools — suitable for comparing raw model capability
against the multi-agent scaffolding.

Usage:
    uv run python -m sciralph.one_shot problems/tier1/hawking_temperature.yaml
    uv run python -m sciralph.one_shot problems/tier1/hawking_temperature.yaml --model gpt-5.4-high
    uv run python -m sciralph.one_shot problems/tier1/hawking_temperature.yaml -o result.md
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import yaml

from .config import Config
from .providers import create_provider, LLMProvider, ProviderResponse

# ---------------------------------------------------------------------------
# System prompt — distilled from the one-shot/prompt_template_default.yaml
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a physics research assistant specialising in solving complex, \
research-level problems using precise, step-by-step reasoning.

**Input**

Problems will be provided in Markdown format.

**Output (Markdown format)**

1. **Step-by-Step Derivation** — Show every non-trivial step in the solution. \
Justify steps using relevant physical laws, theorems, or mathematical identities.

2. **Mathematical Typesetting** — Use LaTeX for all mathematics: \
`$...$` for inline expressions, `$$...$$` for display equations.

3. **Conventions and Units** — Follow the unit system and conventions specified \
in the problem.

4. **Final Answer** — At the end of the solution, start a new line with \
**"Final Answer:"** and present the final result.

   For final answers involving numerical values, follow the precision \
requirements specified in the problem. If no precision is specified:
   - If an exact symbolic value is possible, provide it (e.g. $\\sqrt{2}$, $\\pi/4$).
   - If exact form is not feasible, retain at least 12 significant digits.

5. **Code Template** — If a Python code template is provided after the problem, \
populate your final answer into it. This is purely for formatting/display; \
do not perform additional reasoning or import modules beyond those already \
present in the template."""


# ---------------------------------------------------------------------------
# Transient-error retry (minimal, self-contained)
# ---------------------------------------------------------------------------

_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_TRANSIENT_EXC_NAMES = {
    "ConnectionError", "TimeoutError", "ReadTimeout",
    "ConnectTimeout", "ConnectionResetError",
    "RemoteDisconnected", "BrokenPipeError", "APITimeoutError",
}


def _is_transient(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is None:
        resp = getattr(exc, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
    if status is not None and int(status) in _TRANSIENT_STATUS_CODES:
        return True
    return any(cls.__name__ in _TRANSIENT_EXC_NAMES for cls in type(exc).__mro__)


def _call_with_retry(
    provider: LLMProvider, max_retries: int = 3, initial_delay: float = 2.0,
    **call_kwargs,
) -> ProviderResponse:
    delay = initial_delay
    for attempt in range(max_retries + 1):
        try:
            return provider.call(**call_kwargs)
        except Exception as exc:
            if not _is_transient(exc) or attempt == max_retries:
                raise
            print(
                f"  Transient error (attempt {attempt + 1}/{max_retries}): {exc}",
                file=sys.stderr,
            )
            time.sleep(min(delay, 60.0))
            delay *= 2
    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_user_message(problem_text: str, answer_template: str = "") -> str:
    """Build the user message from problem text and optional code template."""
    msg = problem_text.strip()
    if answer_template:
        msg += (
            "\n\n---\n\n"
            "**Answer template** — populate your final answer into this code template:\n\n"
            f"```python\n{answer_template.strip()}\n```"
        )
    return msg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sciralph.one_shot",
        description="One-shot LLM baseline for SciRalph problems.",
    )
    parser.add_argument("problem", type=Path, help="Path to problem YAML file")
    parser.add_argument(
        "--model", type=str, default="claude-sonnet-4.6",
        help="Model key from models.yaml (default: claude-sonnet-4.6)",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=65536,
        help="Max output tokens (default: 65536/)",
    )
    parser.add_argument(
        "--provider", type=str, default=None,
        help="Override provider (auto-resolved from --model if omitted)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Save response with metadata to a Markdown file",
    )
    args = parser.parse_args()

    # --- Load problem YAML ---
    if not args.problem.exists():
        print(f"Error: problem file not found: {args.problem}", file=sys.stderr)
        sys.exit(1)

    with open(args.problem) as f:
        problem_def = yaml.safe_load(f)

    problem_text = problem_def.get("problem", "")
    answer_template = problem_def.get("answer_template", "")

    if not problem_text:
        print("Error: problem YAML has no 'problem' field", file=sys.stderr)
        sys.exit(1)

    # --- Model / provider resolution ---
    config = Config(model=args.model, max_tokens=args.max_tokens)
    if args.provider:
        config.provider = args.provider
        # Re-trigger resolution if provider was overridden
        if not config.model_id or config.model_id == config.model:
            config.model_id = config.model

    provider = create_provider(
        config.provider,
        api_key=config.api_key,
        timeout=config.api_timeout,
        thinking_token_headroom=config.thinking_token_headroom,
        **config.reasoning,
    )

    # --- Build prompt ---
    user_message = build_user_message(problem_text, answer_template)

    print(f"Model:    {config.model} ({config.model_id})", file=sys.stderr)
    print(f"Provider: {config.provider}", file=sys.stderr)
    print(f"Problem:  {args.problem.name}", file=sys.stderr)
    print(f"Tokens:   {config.max_tokens} max output", file=sys.stderr)
    print("---", file=sys.stderr)

    # --- Single LLM call ---
    start = time.time()
    resp = _call_with_retry(
        provider,
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    duration = time.time() - start

    # --- Report stats to stderr ---
    print(f"Input tokens:  {resp.input_tokens}", file=sys.stderr)
    print(f"Output tokens: {resp.output_tokens}", file=sys.stderr)
    if resp.reasoning_tokens:
        print(f"  Reasoning:   {resp.reasoning_tokens}", file=sys.stderr)
        print(f"  Answer:      {resp.answer_tokens}", file=sys.stderr)
    print(f"Duration:      {duration:.1f}s", file=sys.stderr)
    print(f"Stop reason:   {resp.stop_reason}", file=sys.stderr)
    if config.input_cost or config.output_cost:
        cost = (
            resp.input_tokens * config.input_cost
            + resp.output_tokens * config.output_cost
        ) / 1_000_000
        print(f"Est. cost:     ${cost:.4f}", file=sys.stderr)
    print("---", file=sys.stderr)

    # --- Output response to stdout ---
    print(resp.text)

    # --- Optionally save structured report ---
    if args.output:
        reasoning_row = ""
        if resp.reasoning_tokens:
            reasoning_row = (
                f"| Reasoning tokens | {resp.reasoning_tokens} |\n"
                f"| Answer tokens | {resp.answer_tokens} |\n"
            )
        cost_row = ""
        if config.input_cost or config.output_cost:
            cost = (
                resp.input_tokens * config.input_cost
                + resp.output_tokens * config.output_cost
            ) / 1_000_000
            cost_row = f"| Est. cost | ${cost:.4f} |\n"

        report = (
            f"# One-Shot Result — {args.problem.stem}\n\n"
            f"| Field | Value |\n"
            f"|-------|-------|\n"
            f"| Model | {config.model} ({config.model_id}) |\n"
            f"| Provider | {config.provider} |\n"
            f"| Input tokens | {resp.input_tokens} |\n"
            f"| Output tokens | {resp.output_tokens} |\n"
            f"{reasoning_row}"
            f"| Duration | {duration:.1f}s |\n"
            f"| Stop reason | {resp.stop_reason} |\n"
            f"{cost_row}\n"
            f"## Problem\n\n{problem_text.strip()}\n\n"
            f"## Response\n\n{resp.text}\n"
        )
        args.output.write_text(report)
        print(f"\nSaved to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
