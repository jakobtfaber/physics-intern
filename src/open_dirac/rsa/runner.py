#!/usr/bin/env python3
"""RSA (Recursive Self-Aggregation) runner for OpenDirac problems.

Implements the RSA algorithm: maintain a population of N candidate solutions,
iteratively refine by aggregating subsets of K candidates for T rounds.
Total LLM calls = N * T.

Uses the provider layer (open_dirac.providers + open_dirac.config) and reuses
one-shot utilities for initial generation and retry logic.

Usage:
    uv run python -m open_dirac.rsa problems/tier1/hawking_temperature.yaml
    uv run python -m open_dirac.rsa problems/tier1/hawking_temperature.yaml -N 6 -K 2 -T 4
    uv run python -m open_dirac.rsa problems/tier1/hawking_temperature.yaml --model gpt-5.4-high
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import yaml

from ..config import Config
from ..providers import create_provider, LLMProvider
from ..providers.base import strip_think_tags
from ..verification.evaluate import evaluate_response, extract_answer_code
from ..verification.verify import load_reference_file
from ..one_shot.runner import (
    SYSTEM_PROMPT,
    build_user_message,
    _call_with_retry,
    _resolve_ground_truth,
)


# ---------------------------------------------------------------------------
# Aggregation system prompt
# ---------------------------------------------------------------------------

AGGREGATION_SYSTEM_PROMPT = """\
You are a physics research assistant specialising in solving complex, \
research-level problems using precise, step-by-step reasoning.

You will be given a physics problem together with several candidate \
solutions. Your task is to:

1. **Analyse each candidate** — identify the approach taken, verify key \
mathematical steps, and note any errors, gaps, or unjustified leaps.

2. **Synthesise** — combine the strongest reasoning from the candidates \
into a single improved solution. Where candidates agree, confirm the \
logic. Where they disagree, determine the correct path by independent \
verification.

3. **Output format** — produce a complete, self-contained solution:
   - Show every non-trivial step with justification.
   - Use LaTeX: `$...$` inline, `$$...$$` display.
   - Follow the conventions and units specified in the problem.
   - End with a line starting **"Final Answer:"** and the result.
   - If a Python code template is provided, populate your answer into it."""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_aggregation_message(
    problem_text: str,
    answer_template: str,
    candidates: list[str],
) -> str:
    """Build the user message for an aggregation call.

    Includes the problem statement, K candidate solutions (with thinking
    traces stripped), and the code template if present.
    """
    parts: list[str] = []
    parts.append(problem_text.strip())
    parts.append("\n\n---\n")

    if len(candidates) == 1:
        parts.append(
            "Below is a candidate solution (it may contain errors). "
            "Refine it into an improved solution.\n"
        )
        parts.append(f"## Candidate\n\n{candidates[0].strip()}\n")
    else:
        parts.append(
            f"Below are {len(candidates)} candidate solutions (some may "
            "contain errors). Analyse them and produce a single improved "
            "solution.\n"
        )
        for i, cand in enumerate(candidates, 1):
            parts.append(f"## Candidate {i}\n\n{cand.strip()}\n")

    if answer_template:
        parts.append(
            "---\n\n"
            "**Answer template** — populate your final answer into this "
            "code template:\n\n"
            f"```python\n{answer_template.strip()}\n```"
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Single LLM call (reuses one-shot retry logic)
# ---------------------------------------------------------------------------

def _call_once(
    provider: LLMProvider,
    config: Config,
    system: str,
    user_message: str,
) -> dict:
    """Execute a single LLM call and return a structured result dict."""
    start = time.time()
    resp = _call_with_retry(
        provider,
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=system,
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
# Answer extraction for majority voting
# ---------------------------------------------------------------------------

_FINAL_ANSWER_RE = re.compile(
    r"(?:^|\n)\s*\**\s*Final\s+Answer\s*:?\s*\**\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)


def _extract_answer_key(response_text: str) -> str:
    """Extract a votable answer key from a response.

    Tries (in order):
    1. Python code with ``def answer`` (CritPt-style)
    2. Text after "Final Answer:" label
    3. Empty string (no answer found)
    """
    code = extract_answer_code(response_text)
    if code:
        return code.strip()

    m = _FINAL_ANSWER_RE.search(response_text)
    if m:
        return m.group(1).strip()

    return ""


def _majority_vote(
    responses: list[str],
) -> tuple[str, int, int]:
    """Pick the most common answer from a population of responses.

    Returns (winning_response_text, vote_count, num_valid).
    Groups by ``_extract_answer_key``; ties broken by first occurrence.
    """
    keys: list[str] = []
    for resp in responses:
        keys.append(_extract_answer_key(resp))

    # Group responses by key
    groups: dict[str, list[int]] = {}
    for idx, key in enumerate(keys):
        if not key:
            continue
        if key not in groups:
            groups[key] = []
        groups[key].append(idx)

    if not groups:
        # No valid answers — return first response as fallback
        return responses[0] if responses else "", 0, 0

    # Pick the largest group (ties: first occurrence wins)
    best_key = max(groups, key=lambda k: len(groups[k]))
    best_indices = groups[best_key]
    num_valid = sum(len(v) for v in groups.values())

    return responses[best_indices[0]], len(best_indices), num_valid


# ---------------------------------------------------------------------------
# RSA core algorithm
# ---------------------------------------------------------------------------

def _run_rsa_round(
    provider: LLMProvider,
    config: Config,
    problem_text: str,
    answer_template: str,
    population: list[str],
    K: int,
    max_workers: int,
) -> tuple[list[str], list[dict]]:
    """Run one aggregation round: produce N new candidates from the population.

    Returns (new_population, call_results) where call_results is a list of
    per-call dicts with token/cost info.
    """
    N = len(population)

    # Strip thinking traces from candidates before including in prompts
    clean_population = [strip_think_tags(resp) for resp in population]

    def _aggregate_one(slot: int) -> dict:
        subset_indices = random.sample(range(N), min(K, N))
        subset = [clean_population[i] for i in subset_indices]
        agg_message = build_aggregation_message(
            problem_text, answer_template, subset,
        )
        return _call_once(provider, config, AGGREGATION_SYSTEM_PROMPT, agg_message)

    new_population: list[str] = [None] * N  # type: ignore[list-item]
    call_results: list[dict] = [None] * N  # type: ignore[list-item]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_aggregate_one, i): i for i in range(N)}
        for future in as_completed(futures):
            slot = futures[future]
            result = future.result()
            new_population[slot] = result["response_text"]
            call_results[slot] = result

    return new_population, call_results


def run_rsa(
    provider: LLMProvider,
    config: Config,
    user_message: str,
    problem_text: str,
    answer_template: str,
    N: int,
    K: int,
    T: int,
    max_workers: int | None = None,
) -> dict:
    """Run the full RSA algorithm on a single problem.

    Returns a structured result dict with per-round metrics and the final
    majority-voted answer.
    """
    if max_workers is None:
        max_workers = N

    total_tokens = {"input": 0, "output": 0, "reasoning": 0, "answer": 0}
    total_cost = 0.0
    rounds_log: list[dict] = []
    start_time = time.time()

    def _accumulate(results: list[dict]) -> None:
        nonlocal total_cost
        for r in results:
            for k in total_tokens:
                total_tokens[k] += r["tokens"][k]
            total_cost += r["cost_usd"]

    # --- Round 0: generate initial population ---
    print(f"Round 0/{T-1}: generating {N} initial candidates...",
          file=sys.stderr, flush=True)

    init_results: list[dict] = [None] * N  # type: ignore[list-item]

    def _generate_one(slot: int) -> dict:
        return _call_once(provider, config, SYSTEM_PROMPT, user_message)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_generate_one, i): i for i in range(N)}
        for future in as_completed(futures):
            slot = futures[future]
            init_results[slot] = future.result()

    population = [r["response_text"] for r in init_results]
    _accumulate(init_results)

    # Log round 0 metrics
    keys_0 = [_extract_answer_key(r) for r in population]
    n_unique_0 = len(set(k for k in keys_0 if k))
    round_cost = sum(r["cost_usd"] for r in init_results)
    rounds_log.append({
        "round": 0,
        "type": "init",
        "n_unique_answers": n_unique_0,
        "cost_usd": round(round_cost, 6),
    })
    print(f"  {n_unique_0} unique answers, ${round_cost:.4f}",
          file=sys.stderr, flush=True)

    # --- Rounds 1..T-1: aggregation ---
    for t in range(1, T):
        print(f"Round {t}/{T-1}: aggregating (K={K})...",
              file=sys.stderr, flush=True)

        population, call_results = _run_rsa_round(
            provider, config, problem_text, answer_template,
            population, K, max_workers,
        )
        _accumulate(call_results)

        keys_t = [_extract_answer_key(r) for r in population]
        n_unique_t = len(set(k for k in keys_t if k))
        round_cost = sum(r["cost_usd"] for r in call_results)
        rounds_log.append({
            "round": t,
            "type": "aggregation",
            "n_unique_answers": n_unique_t,
            "cost_usd": round(round_cost, 6),
        })
        print(f"  {n_unique_t} unique answers, ${round_cost:.4f}",
              file=sys.stderr, flush=True)

    # --- Majority vote ---
    winning_response, vote_count, n_valid = _majority_vote(population)
    total_duration = time.time() - start_time

    print(f"Majority vote: {vote_count}/{n_valid} agree "
          f"(total: {total_duration:.1f}s, ${total_cost:.4f})",
          file=sys.stderr, flush=True)
    print(f"Tokens: input={total_tokens['input']}, "
          f"output={total_tokens['output']}",
          file=sys.stderr, flush=True)

    return {
        "N": N,
        "K": K,
        "T": T,
        "total_calls": N * T,
        "tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "total_duration_s": round(total_duration, 2),
        "majority_vote": {
            "vote_count": vote_count,
            "n_valid": n_valid,
            "population_size": N,
        },
        "rounds": rounds_log,
        "response_text": winning_response,
        "all_final_responses": population,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="open_dirac.rsa",
        description="RSA (Recursive Self-Aggregation) runner for OpenDirac problems.",
    )
    parser.add_argument("problem", type=Path, help="Path to problem YAML file")
    parser.add_argument(
        "--model", type=str, default="claude-4.6-sonnet",
        help="Model key from models.yaml (default: claude-4.6-sonnet)",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=128000,
        help="Max output tokens per LLM call (default: 128000)",
    )
    parser.add_argument(
        "--provider", type=str, default=None,
        help="Override provider (auto-resolved from --model if omitted)",
    )
    parser.add_argument(
        "-N", type=int, default=6,
        help="Population size (default: 6)",
    )
    parser.add_argument(
        "-K", type=int, default=2,
        help="Aggregation subset size (default: 2)",
    )
    parser.add_argument(
        "-T", type=int, default=4,
        help="Number of rounds (default: 4)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Save response with metadata to a Markdown file",
    )
    parser.add_argument(
        "--results-dir", type=Path, default=Path("results/rsa"),
        help="Directory for result JSON files (default: results/rsa/)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Max parallel LLM calls within a round (default: N)",
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
        if not config.model_id or config.model_id == config.model:
            config.model_id = config.model

    provider = create_provider(
        config.provider,
        api_key=config.api_key,
        timeout=config.api_timeout,
        **config.reasoning,
    )

    # --- Build initial prompt ---
    user_message = build_user_message(problem_text, answer_template)

    N, K, T = args.N, args.K, args.T
    max_workers = args.concurrency or N

    print(f"Model:       {config.model} ({config.model_id})", file=sys.stderr)
    print(f"Provider:    {config.provider}", file=sys.stderr)
    print(f"Problem:     {args.problem.name}", file=sys.stderr)
    print(f"RSA params:  N={N}, K={K}, T={T} ({N * T} total calls)", file=sys.stderr)
    print(f"Concurrency: {max_workers}", file=sys.stderr)
    print(f"Max tokens:  {config.max_tokens}", file=sys.stderr)
    print("---", file=sys.stderr)

    # --- Run RSA ---
    result = run_rsa(
        provider, config, user_message,
        problem_text, answer_template,
        N=N, K=K, T=T, max_workers=max_workers,
    )

    # --- Evaluation ---
    eval_kwargs = _resolve_ground_truth(problem_def, args.problem)
    if eval_kwargs is not None:
        ev = evaluate_response(result["response_text"], **eval_kwargs)
        if ev["correct"] is True:
            print(f"Evaluation:  CORRECT ({ev['method']})", file=sys.stderr)
        elif ev["correct"] is False:
            print(f"Evaluation:  INCORRECT ({ev['method']})", file=sys.stderr)
        else:
            print(f"Evaluation:  ERROR — {ev['error']}", file=sys.stderr)
        result["evaluation"] = ev

    print("---", file=sys.stderr)

    # --- Output winning response to stdout ---
    print(result["response_text"])

    # --- Save structured JSON ---
    if args.output:
        report = (
            f"# RSA Result — {args.problem.stem}\n\n"
            f"| Field | Value |\n"
            f"|-------|-------|\n"
            f"| Model | {config.model} ({config.model_id}) |\n"
            f"| Provider | {config.provider} |\n"
            f"| N | {N} |\n"
            f"| K | {K} |\n"
            f"| T | {T} |\n"
            f"| Total calls | {N * T} |\n"
            f"| Total cost | ${result['total_cost_usd']:.4f} |\n"
            f"| Duration | {result['total_duration_s']:.1f}s |\n"
            f"| Majority vote | {result['majority_vote']['vote_count']}"
            f"/{result['majority_vote']['n_valid']} |\n\n"
            f"## Response\n\n{result['response_text']}\n"
        )
        args.output.write_text(report)
        print(f"\nSaved to {args.output}", file=sys.stderr)

    # Save JSON results
    results_dir = args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = config.model.replace("/", "-").replace(":", "-")
    filename = f"{args.problem.stem}_{safe_model}_rsa_{timestamp}.json"
    output_path = results_dir / filename

    # Don't include full responses in JSON to keep file size reasonable
    payload = {
        "problem": args.problem.stem,
        "problem_path": str(args.problem),
        "model": config.model,
        "model_id": config.model_id,
        "provider": config.provider,
        "max_tokens": config.max_tokens,
        "rsa_params": {"N": N, "K": K, "T": T},
        "timestamp": timestamp,
        "tokens": result["tokens"],
        "total_cost_usd": result["total_cost_usd"],
        "total_duration_s": result["total_duration_s"],
        "majority_vote": result["majority_vote"],
        "rounds": result["rounds"],
        "evaluation": result.get("evaluation"),
        "response_text": result["response_text"],
    }
    output_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Saved to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
