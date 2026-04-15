#!/usr/bin/env python3
"""One-shot LLM baseline for OpenDirac problems.

Sends a single LLM call with the problem statement and collects the response.
Uses the provider layer (open_dirac.providers + open_dirac.config) but nothing from
the engine, agents, or tools — suitable for comparing raw model capability
against the multi-agent scaffolding.

Usage:
    uv run python -m open_dirac.one_shot problems/critpt/quantum_error_correction_main.yaml
    uv run python -m open_dirac.one_shot problems/critpt/quantum_error_correction_main.yaml --model gpt-5.4-high
    uv run python -m open_dirac.one_shot problems/critpt/quantum_error_correction_main.yaml -o result.md
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import yaml

from ..config import Config, DEFAULTS, build_config
from ..verification.evaluate import evaluate_response
from ..providers import create_provider, LLMProvider, ProviderResponse
from ..verification.verify import load_reference_file

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
# Single run helper
# ---------------------------------------------------------------------------

def _run_once(
    provider: LLMProvider,
    config: Config,
    user_message: str,
) -> dict:
    """Execute a single LLM call and return a structured result dict."""
    start = time.time()
    resp = _call_with_retry(
        provider,
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
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


# ---------------------------------------------------------------------------
# Ground-truth resolution (problem YAML → reference file fallback)
# ---------------------------------------------------------------------------

def _resolve_ground_truth(
    problem_def: dict, problem_path: Path,
) -> dict | None:
    """Return kwargs for ``evaluate_response``, or *None* if no ground truth."""
    answer_val = problem_def.get("answer")
    has_answer = answer_val is not None and (
        not isinstance(answer_val, str) or answer_val.strip()
    )

    if has_answer:
        return {"problem_def": problem_def}

    # Fallback: reference file
    ref_answer, _ = load_reference_file(problem_path)
    if ref_answer is None:
        return None

    if "def answer" in ref_answer:
        return {"problem_def": problem_def, "reference_code": ref_answer}

    # Legacy expression-style reference — inject into a shallow copy
    patched = dict(problem_def)
    patched["answer"] = ref_answer
    return {"problem_def": patched}


# ---------------------------------------------------------------------------
# Single-run mode (original behavior + evaluation)
# ---------------------------------------------------------------------------

def _write_verification_md(workspace_root: Path, ev: dict) -> None:
    """Render the evaluate_response() dict as a Markdown report."""
    if ev["correct"] is True:
        verdict = "CORRECT"
    elif ev["correct"] is False:
        verdict = "INCORRECT"
    else:
        verdict = "ERROR"

    lines = [
        "# Automated Evaluation",
        "",
        f"- **Method**: {ev.get('method', 'unknown')}",
        f"- **Result**: {verdict}",
    ]
    if ev.get("error"):
        lines.append(f"- **Error**: {ev['error']}")
    (workspace_root / "VERIFICATION.md").write_text("\n".join(lines) + "\n")


def _run_single(
    args: argparse.Namespace,
    config: Config,
    provider: LLMProvider,
    user_message: str,
    problem_def: dict,
    workspace_root: Path,
) -> None:
    """Run once, print to stdout, optionally save markdown — original behavior."""
    result = _run_once(provider, config, user_message)

    # --- Report stats to stderr ---
    tokens = result["tokens"]
    print(f"Input tokens:  {tokens['input']}", file=sys.stderr)
    print(f"Output tokens: {tokens['output']}", file=sys.stderr)
    if tokens["reasoning"]:
        print(f"  Reasoning:   {tokens['reasoning']}", file=sys.stderr)
        print(f"  Answer:      {tokens['answer']}", file=sys.stderr)
    print(f"Duration:      {result['duration_s']:.1f}s", file=sys.stderr)
    print(f"Stop reason:   {result['stop_reason']}", file=sys.stderr)
    if result["cost_usd"]:
        print(f"Est. cost:     ${result['cost_usd']:.4f}", file=sys.stderr)

    # --- Persist answer to workspace ---
    (workspace_root / "ANSWER.md").write_text(
        f"# Final Answer\n\n{result['response_text']}\n"
    )

    # --- Evaluation ---
    eval_kwargs = _resolve_ground_truth(problem_def, args.problem)
    if eval_kwargs is not None:
        ev = evaluate_response(result["response_text"], **eval_kwargs)
        if ev["correct"] is True:
            print(f"Evaluation:    CORRECT ({ev['method']})", file=sys.stderr)
        elif ev["correct"] is False:
            print(f"Evaluation:    INCORRECT ({ev['method']})", file=sys.stderr)
        else:
            print(f"Evaluation:    ERROR — {ev['error']}", file=sys.stderr)
        _write_verification_md(workspace_root, ev)

    print("---", file=sys.stderr)

    # --- Output response to stdout ---
    print(result["response_text"])

    # --- Optionally save structured report ---
    if args.output:
        reasoning_row = ""
        if tokens["reasoning"]:
            reasoning_row = (
                f"| Reasoning tokens | {tokens['reasoning']} |\n"
                f"| Answer tokens | {tokens['answer']} |\n"
            )
        cost_row = ""
        if result["cost_usd"]:
            cost_row = f"| Est. cost | ${result['cost_usd']:.4f} |\n"

        problem_text = problem_def.get("problem", "")
        report = (
            f"# One-Shot Result — {args.problem.stem}\n\n"
            f"| Field | Value |\n"
            f"|-------|-------|\n"
            f"| Model | {config.model} ({config.model_id}) |\n"
            f"| Provider | {config.provider} |\n"
            f"| Input tokens | {tokens['input']} |\n"
            f"| Output tokens | {tokens['output']} |\n"
            f"{reasoning_row}"
            f"| Duration | {result['duration_s']:.1f}s |\n"
            f"| Stop reason | {result['stop_reason']} |\n"
            f"{cost_row}\n"
            f"## Problem\n\n{problem_text.strip()}\n\n"
            f"## Response\n\n{result['response_text']}\n"
        )
        args.output.write_text(report)
        print(f"\nSaved to {args.output}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="open_dirac.one_shot",
        description="One-shot LLM baseline for OpenDirac problems.",
    )
    parser.add_argument("problem", type=Path, help="Path to problem YAML file")
    parser.add_argument(
        "--model", type=str, default=None,
        help=f"Model key from models.yaml (default: {DEFAULTS['model']})",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to config YAML file (overrides defaults)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Save response with metadata to a Markdown file",
    )
    parser.add_argument(
        "--workspace-dir", type=str, default=None,
        help="Workspace directory (default: auto-generated)",
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
    config = build_config(args)

    provider = create_provider(
        config.provider,
        api_key=config.api_key,
        timeout=config.api_timeout,
        **config.reasoning,
    )

    # --- Build prompt ---
    user_message = build_user_message(problem_text, answer_template)

    # --- Workspace (lightweight, no git) ---
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = config.model.replace("/", "-").replace(":", "-")
    workspace_root = Path(
        args.workspace_dir
        or f"workspaces/{timestamp}_{args.problem.stem}_{safe_model}_oneshot"
    )
    workspace_root.mkdir(parents=True, exist_ok=True)
    config.workspace_dir = str(workspace_root)

    # Seed the workspace with the problem before the LLM call so partial
    # failures still leave a trace.
    (workspace_root / "PROBLEM.md").write_text(f"# Problem\n\n{problem_text}\n")
    problem_data = dict(problem_def)
    problem_data["name"] = args.problem.stem
    with open(workspace_root / "problem.yaml", "w") as f:
        yaml.dump(problem_data, f, default_flow_style=False, sort_keys=False)
    config.save(workspace_root)

    print(f"Model:     {config.model} ({config.model_id})", file=sys.stderr)
    print(f"Provider:  {config.provider}", file=sys.stderr)
    print(f"Problem:   {args.problem.name}", file=sys.stderr)
    print(f"Tokens:    {config.max_tokens} max output", file=sys.stderr)
    print(f"Workspace: {workspace_root}", file=sys.stderr)
    print("---", file=sys.stderr)

    _run_single(args, config, provider, user_message, problem_def, workspace_root)


if __name__ == "__main__":
    main()
