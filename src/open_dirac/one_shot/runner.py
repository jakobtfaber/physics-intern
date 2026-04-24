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
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from ..baselines import (
    SYSTEM_PROMPT,
    add_common_args,
    build_user_message,
    create_provider_from_config,
    load_problem,
    run_baseline_call,
    setup_workspace,
)
# Re-export for backward compatibility with tests/test_one_shot.py until Commit 3.
from ..baselines import build_user_message as _build_user_message  # noqa: F401
from ..config import Config, build_config
from ..providers import LLMProvider
from ..verification import evaluate_response, load_reference_file


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
    result = run_baseline_call(
        provider, config,
        system=SYSTEM_PROMPT, user_message=user_message,
        agent_name="one_shot",
    )

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
    add_common_args(parser)
    args = parser.parse_args()

    # --- Load problem YAML ---
    problem_def, problem_text, answer_template = load_problem(args.problem)

    # --- Model / provider resolution ---
    config = build_config(args)
    provider = create_provider_from_config(config)

    # --- Build prompt ---
    user_message = build_user_message(problem_text, answer_template)

    # --- Workspace (lightweight, no git) ---
    workspace_root = setup_workspace(
        args, config, problem_def, problem_text, "oneshot",
    )

    print(f"Model:     {config.model} ({config.model_id})", file=sys.stderr)
    print(f"Provider:  {config.provider}", file=sys.stderr)
    print(f"Problem:   {args.problem.name}", file=sys.stderr)
    print(f"Tokens:    {config.max_tokens} max output", file=sys.stderr)
    print(f"Workspace: {workspace_root}", file=sys.stderr)
    print("---", file=sys.stderr)

    _run_single(args, config, provider, user_message, problem_def, workspace_root)


if __name__ == "__main__":
    main()
