#!/usr/bin/env python3
"""Two-step LLM baseline for PhysicsIntern problems.

Reproduces critpt's ``parsing=False`` (two-step) procedure as closely as
possible: a first call asks for a free-form derivation ending with
"Final Answer:" (problem text only — no code template, no template
mention in the system prompt); a second call hands the model the same
conversation plus a parse instruction + code template, and the model
populates the template without further reasoning.

System prompt and parse prompt are byte-for-byte copies of critpt's
rendered output (see ``baselines/prompts.py``).

Usage:
    uv run python -m physics_intern.two_steps problems/critpt/quantum_error_correction_main.yaml
    uv run python -m physics_intern.two_steps problems/critpt/quantum_error_correction_main.yaml --model gpt-5.4-high
    uv run python -m physics_intern.two_steps problems/critpt/quantum_error_correction_main.yaml -o result.md
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from ..baselines import (
    SYSTEM_PROMPT_TWO_STEP,
    add_common_args,
    build_parse_prompt,
    build_two_step_user_message,
    create_provider_from_config,
    load_problem,
    run_two_step_call,
    setup_workspace,
)
from ..core.config import Config, build_config
from ..providers import LLMProvider
from ..verification import (
    extract_answer_code,
    run_formal_evaluation,
    write_formal_eval_report,
)


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------


def _run_single(
    args: argparse.Namespace,
    config: Config,
    provider: LLMProvider,
    derive_user_message: str,
    parse_user_message: str,
    problem_def: dict,
    workspace_root,
) -> None:
    """Run two-step, evaluate against ground truth, write outputs."""
    result = run_two_step_call(
        provider,
        config,
        system=SYSTEM_PROMPT_TWO_STEP,
        derive_user_message=derive_user_message,
        parse_user_message=parse_user_message,
        agent_name="two_steps",
    )

    # --- Report stats to stderr ---
    per_call = result["per_call"]
    tokens = result["tokens"]
    print(
        f"Call 1 (derive): input={per_call['derive']['input']}, "
        f"output={per_call['derive']['output']}"
        + (
            f", reasoning={per_call['derive']['reasoning']}"
            if per_call["derive"]["reasoning"]
            else ""
        )
        + f", stop={per_call['derive']['stop_reason']}",
        file=sys.stderr,
    )
    print(
        f"Call 2 (parse):  input={per_call['parse']['input']}, "
        f"output={per_call['parse']['output']}"
        + (
            f", reasoning={per_call['parse']['reasoning']}"
            if per_call["parse"]["reasoning"]
            else ""
        )
        + f", stop={per_call['parse']['stop_reason']}",
        file=sys.stderr,
    )
    print(
        f"Total tokens:    input={tokens['input']}, output={tokens['output']}"
        + (f", reasoning={tokens['reasoning']}" if tokens["reasoning"] else ""),
        file=sys.stderr,
    )
    print(f"Duration:        {result['duration_s']:.1f}s", file=sys.stderr)
    print(f"Stop reason:     {result['stop_reason']}", file=sys.stderr)
    if result["cost_usd"]:
        print(f"Est. cost:       ${result['cost_usd']:.4f}", file=sys.stderr)

    # --- Persist derivation and answer to workspace ---
    (workspace_root / "DERIVATION.md").write_text(result["derivation_text"] + "\n")

    # Extract only the code block with `def answer` to keep ANSWER.md clean;
    # fall back to the full call-2 response if no fenced block is found.
    clean_code = extract_answer_code(result["response_text"])
    answer_content = clean_code if clean_code else result["response_text"]
    (workspace_root / "ANSWER.md").write_text(answer_content + "\n")

    # --- Formal evaluation (writes VERIFICATION.md with frontmatter) ---
    # Matching one-shot, we deliberately do not call render_formal_evaluation:
    # it would write to the Rich console singleton (stdout), corrupting the
    # stdout-is-response contract. The stderr verdict line below replaces it.
    ev = run_formal_evaluation(
        str(workspace_root),
        problem_def,
        problem_path=args.problem,
    )
    if ev.skipped:
        print(f"Evaluation:      SKIPPED ({ev.skip_reason})", file=sys.stderr)
    elif ev.correct is True:
        print(f"Evaluation:      CORRECT ({ev.method})", file=sys.stderr)
    elif ev.correct is False:
        print(f"Evaluation:      INCORRECT ({ev.method})", file=sys.stderr)
    else:
        print(f"Evaluation:      ERROR — {ev.error}", file=sys.stderr)
    write_formal_eval_report(ev, str(workspace_root))

    print("---", file=sys.stderr)

    # --- Output call-2 response (the populated code) to stdout ---
    print(result["response_text"])

    # --- Optional -o: persist just the call-2 response text ---
    if args.output:
        args.output.write_text(result["response_text"])
        print(f"Saved to {args.output}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="physics_intern.two_steps",
        description="Two-step LLM baseline (critpt parsing=False) for PhysicsIntern problems.",
    )
    add_common_args(parser)
    args = parser.parse_args()

    # --- Load problem YAML ---
    problem_def, problem_text, answer_template = load_problem(args.problem)

    if not answer_template:
        print(
            "Error: two-step mode requires an 'answer_template' in the problem "
            "YAML (the second call has nothing to populate without it). Use "
            "physics_intern.one_shot for problems without a template.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Model / provider resolution ---
    config = build_config(args)
    provider = create_provider_from_config(config)

    # --- Build prompts (call 1: problem only; call 2: parse instr + template) ---
    derive_user_message = build_two_step_user_message(problem_text)
    parse_user_message = build_parse_prompt(answer_template)

    # --- Workspace (lightweight, no git) ---
    workspace_root = setup_workspace(
        args,
        config,
        problem_def,
        problem_text,
        "two_steps",
    )

    print(f"Model:     {config.model} ({config.model_id})", file=sys.stderr)
    print(f"Provider:  {config.provider}", file=sys.stderr)
    print(f"Problem:   {args.problem.name}", file=sys.stderr)
    print(f"Tokens:    {config.max_tokens} max output (per call)", file=sys.stderr)
    print(f"Workspace: {workspace_root}", file=sys.stderr)
    print("---", file=sys.stderr)

    _run_single(
        args,
        config,
        provider,
        derive_user_message,
        parse_user_message,
        problem_def,
        workspace_root,
    )


if __name__ == "__main__":
    main()
