#!/usr/bin/env python3
"""Run open_dirac_rsa N times on a single problem with concurrency control.

Reports pass@k results: how many RSA runs produced a correct majority-vote
answer, based on the evaluation block in each run's RSA JSON output.

Unlike vanilla/autophysicist, RSA has no workspace directory — each run
produces a JSON file (written by the RSA runner to its ``--output-dir``)
containing per-round metrics, token/cost totals, and evaluation. This script
gives every run its own output dir and aggregates the resulting JSONs.

Usage:
    uv run python scripts/run_multiple_rsa.py problems/critpt/yaml/Challenge_1_main.yaml --runs 10
    uv run python scripts/run_multiple_rsa.py problems/critpt/yaml/Challenge_1_main.yaml --runs 10 --concurrency 3
    uv run python scripts/run_multiple_rsa.py problems/critpt/yaml/Challenge_1_main.yaml --runs 20 --concurrency 3 --model claude-4.6-opus -N 6 -K 2 -T 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_dirac.config import DEFAULTS  # noqa: E402

DEFAULT_OUTPUT_BASE = PROJECT_ROOT / "results" / "multiple_rsa"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    run_index: int
    output_dir: Path
    evaluation: str  # "correct", "incorrect", "error", "no_eval"
    duration_s: float
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    answer_tokens: int = 0
    total_calls: int = 0
    majority_vote: dict | None = None
    rounds: list | None = None
    returncode: int | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Per-run result parsing (prefer the JSON written by the RSA runner)
# ---------------------------------------------------------------------------

def _load_rsa_json(run_dir: Path) -> dict | None:
    """Find and load the JSON file written by the RSA runner."""
    if not run_dir.is_dir():
        return None
    jsons = sorted(run_dir.glob("*_rsa.json"))
    if not jsons:
        return None
    try:
        return json.loads(jsons[-1].read_text())
    except Exception:
        return None


def _parse_stderr_fallback(stderr_text: str) -> dict:
    """Best-effort extraction of stats from RSA stderr if JSON missing."""
    info: dict = {
        "evaluation": "no_eval",
        "total_cost_usd": 0.0,
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "answer": 0},
    }
    if "Evaluation:  CORRECT" in stderr_text:
        info["evaluation"] = "correct"
    elif "Evaluation:  INCORRECT" in stderr_text:
        info["evaluation"] = "incorrect"
    elif "Evaluation:  ERROR" in stderr_text:
        info["evaluation"] = "error"
    m = re.search(r"total:\s*[\d.]+s,\s*\$([\d.]+)", stderr_text)
    if m:
        info["total_cost_usd"] = float(m.group(1))
    m = re.search(r"input=(\d+)", stderr_text)
    if m:
        info["tokens"]["input"] = int(m.group(1))
    m = re.search(r"output=(\d+)", stderr_text)
    if m:
        info["tokens"]["output"] = int(m.group(1))
    return info


def extract_results(run_dir: Path, stderr_text: str) -> dict:
    """Extract per-run metrics from the RSA JSON, falling back to stderr."""
    payload = _load_rsa_json(run_dir)
    if payload is None:
        return _parse_stderr_fallback(stderr_text)

    ev_block = payload.get("evaluation") or {}
    if ev_block.get("correct") is True:
        evaluation = "correct"
    elif ev_block.get("correct") is False:
        evaluation = "incorrect"
    elif ev_block.get("error"):
        evaluation = "error"
    else:
        evaluation = "no_eval"

    rsa_params = payload.get("rsa_params") or {}
    N = rsa_params.get("N") or 0
    T = rsa_params.get("T") or 0

    return {
        "evaluation": evaluation,
        "total_cost_usd": payload.get("total_cost_usd", 0.0),
        "tokens": payload.get("tokens") or {
            "input": 0, "output": 0, "reasoning": 0, "answer": 0,
        },
        "total_calls": N * T,
        "majority_vote": payload.get("majority_vote"),
        "rounds": payload.get("rounds"),
    }


# ---------------------------------------------------------------------------
# Worker: run one RSA instance
# ---------------------------------------------------------------------------

_ROUND_RE = re.compile(r"Round\s+(\d+)/(\d+)")


async def run_one(
    run_index: int,
    problem_path: Path,
    model_key: str | None,
    max_tokens: int | None,
    config_path: Path | None,
    rsa_N: int | None,
    rsa_K: int | None,
    rsa_T: int | None,
    rsa_concurrency: int | None,
    run_output_dir: Path,
    timeout: float,
    semaphore: asyncio.Semaphore,
    print_lock: asyncio.Lock,
) -> RunResult:
    """Run a single open_dirac_rsa subprocess, streaming round progress."""
    run_output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "uv", "run", "open_dirac_rsa",
        str(problem_path),
        "--output-dir", str(run_output_dir),
    ]
    if model_key:
        cmd.extend(["--model", model_key])
    if max_tokens is not None:
        cmd.extend(["--max-tokens", str(max_tokens)])
    if config_path:
        cmd.extend(["--config", str(config_path)])
    if rsa_N is not None:
        cmd.extend(["-N", str(rsa_N)])
    if rsa_K is not None:
        cmd.extend(["-K", str(rsa_K)])
    if rsa_T is not None:
        cmd.extend(["-T", str(rsa_T)])
    if rsa_concurrency is not None:
        cmd.extend(["--concurrency", str(rsa_concurrency)])

    async with semaphore:
        start = time.monotonic()
        try:
            # PYTHONUNBUFFERED: flush stderr per-line so we can detect
            # "Round X/Y" progress markers via readline().
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
                env=env,
            )

            stderr_lines: list[str] = []

            async def _drain_stdout():
                """RSA writes the winning response to stdout — discard it
                (per-run JSON already has what we need), just keep the pipe
                drained to avoid the child blocking on a full buffer."""
                assert proc.stdout is not None
                await proc.stdout.read()

            async def _stream_stderr():
                """Detect 'Round X/Y' markers and print progress; retain all
                lines so we can fall back to stderr parsing if needed."""
                assert proc.stderr is not None
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace")
                    stderr_lines.append(text)
                    m = _ROUND_RE.search(text)
                    if m:
                        elapsed_so_far = time.monotonic() - start
                        async with print_lock:
                            print(
                                f"  run{run_index:03d}  round {m.group(1)}/{m.group(2)}  "
                                f"({elapsed_so_far:.0f}s)",
                                file=sys.stderr,
                            )

            await asyncio.wait_for(
                asyncio.gather(_drain_stdout(), _stream_stderr(), proc.wait()),
                timeout=timeout,
            )
            elapsed = time.monotonic() - start

            stderr_text = "".join(stderr_lines)
            info = extract_results(run_output_dir, stderr_text)

            error = None
            if proc.returncode != 0:
                tail = stderr_text[-500:]
                error = f"exit code {proc.returncode}: {tail}"

            return RunResult(
                run_index=run_index,
                output_dir=run_output_dir,
                evaluation=info["evaluation"],
                duration_s=round(elapsed, 1),
                total_cost_usd=info.get("total_cost_usd", 0.0),
                input_tokens=info["tokens"].get("input", 0),
                output_tokens=info["tokens"].get("output", 0),
                reasoning_tokens=info["tokens"].get("reasoning", 0),
                answer_tokens=info["tokens"].get("answer", 0),
                total_calls=info.get("total_calls", 0),
                majority_vote=info.get("majority_vote"),
                rounds=info.get("rounds"),
                returncode=proc.returncode,
                error=error,
            )

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            # Salvage whatever the child managed to write before the timeout.
            info = extract_results(run_output_dir, "".join(stderr_lines) if 'stderr_lines' in locals() else "")
            evaluation = info["evaluation"] if info["evaluation"] != "no_eval" else "error"
            return RunResult(
                run_index=run_index,
                output_dir=run_output_dir,
                evaluation=evaluation,
                duration_s=round(elapsed, 1),
                total_cost_usd=info.get("total_cost_usd", 0.0),
                input_tokens=info["tokens"].get("input", 0),
                output_tokens=info["tokens"].get("output", 0),
                reasoning_tokens=info["tokens"].get("reasoning", 0),
                answer_tokens=info["tokens"].get("answer", 0),
                total_calls=info.get("total_calls", 0),
                error=f"timeout after {timeout:.0f}s",
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            return RunResult(
                run_index=run_index,
                output_dir=run_output_dir,
                evaluation="error",
                duration_s=round(elapsed, 1),
                error=f"{type(exc).__name__}: {exc}",
            )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run_multiple(args: argparse.Namespace) -> int:
    """Run N RSA instances and report aggregate results."""
    n = args.runs
    concurrency = args.concurrency
    semaphore = asyncio.Semaphore(concurrency)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = (args.model or DEFAULTS["model"]).replace("/", "-").replace(":", "-")
    problem_stem = args.problem.stem

    batch_dir = args.output_dir / f"{timestamp}_{problem_stem}_{safe_model}_rsa"
    batch_dir.mkdir(parents=True, exist_ok=True)

    run_dirs = [batch_dir / f"run{i:03d}" for i in range(n)]

    print(f"Problem:     {args.problem.name}", file=sys.stderr)
    if args.model:
        print(f"Model:       {args.model}", file=sys.stderr)
    rsa_parts: list[str] = []
    if args.N is not None:
        rsa_parts.append(f"N={args.N}")
    if args.K is not None:
        rsa_parts.append(f"K={args.K}")
    if args.T is not None:
        rsa_parts.append(f"T={args.T}")
    if rsa_parts:
        print(f"RSA params:  {', '.join(rsa_parts)}", file=sys.stderr)
    print(f"Runs:        {n}", file=sys.stderr)
    print(f"Concurrency: {concurrency} runs", file=sys.stderr)
    print(f"Timeout:     {args.timeout}s per run", file=sys.stderr)
    print(f"Output:      {batch_dir}/", file=sys.stderr)
    print("---", file=sys.stderr)

    start_time = datetime.now(timezone.utc)
    completed = 0
    all_results: list[RunResult] = []
    lock = asyncio.Lock()
    print_lock = asyncio.Lock()

    async def worker(run_index: int) -> RunResult:
        nonlocal completed
        result = await run_one(
            run_index, args.problem, args.model,
            args.max_tokens, args.config,
            args.N, args.K, args.T,
            args.rsa_concurrency,
            run_dirs[run_index],
            args.timeout, semaphore, print_lock,
        )
        async with lock:
            completed += 1
            all_results.append(result)
            status = result.evaluation.upper()
            if result.error and result.evaluation == "error":
                status = f"ERROR: {result.error[:80]}"
            cost_str = f", ${result.total_cost_usd:.4f}" if result.total_cost_usd else ""
            print(
                f"[{completed}/{n}] run{run_index:03d} ({result.duration_s:.0f}s{cost_str}) {status}",
                file=sys.stderr,
            )
        return result

    tasks = [asyncio.create_task(worker(i)) for i in range(n)]

    # Ctrl+C handler
    loop = asyncio.get_running_loop()
    cancelled = False

    def _handler():
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            print("\nInterrupted — cancelling pending runs...", file=sys.stderr)
            for t in tasks:
                if not t.done():
                    t.cancel()

    loop.add_signal_handler(signal.SIGINT, _handler)

    await asyncio.gather(*tasks, return_exceptions=True)
    end_time = datetime.now(timezone.utc)

    all_results.sort(key=lambda r: r.run_index)

    counts: dict[str, int] = {"correct": 0, "incorrect": 0, "error": 0, "no_eval": 0}
    for r in all_results:
        counts[r.evaluation] = counts.get(r.evaluation, 0) + 1

    total_cost = sum(r.total_cost_usd for r in all_results)
    wall_clock = (end_time - start_time).total_seconds()

    # Summary
    print("---", file=sys.stderr)
    parts = []
    if counts["correct"]:
        parts.append(f"{counts['correct']}/{n} correct")
    if counts["incorrect"]:
        parts.append(f"{counts['incorrect']} incorrect")
    if counts["error"]:
        parts.append(f"{counts['error']} errors")
    if counts["no_eval"]:
        parts.append(f"{counts['no_eval']} no evaluation")
    print(f"Results: {', '.join(parts) or '0 runs'}", file=sys.stderr)
    if total_cost > 0:
        print(f"Total cost: ${total_cost:.4f}", file=sys.stderr)
    print(f"Wall clock: {wall_clock:.0f}s", file=sys.stderr)

    # Write aggregate summary JSON
    summary_path = batch_dir / "summary.json"
    payload = {
        "mode": "rsa",
        "problem": problem_stem,
        "problem_path": str(args.problem),
        "model": args.model or DEFAULTS["model"],
        "rsa_params": {"N": args.N, "K": args.K, "T": args.T},
        "max_tokens": args.max_tokens,
        "num_runs": n,
        "concurrency": concurrency,
        "timeout_s": args.timeout,
        "timestamp": timestamp,
        "summary": counts,
        "total_cost_usd": round(total_cost, 6),
        "wall_clock_s": round(wall_clock, 1),
        "runs": [
            {
                "run_index": r.run_index,
                "output_dir": str(r.output_dir),
                "evaluation": r.evaluation,
                "duration_s": r.duration_s,
                "total_cost_usd": r.total_cost_usd,
                "total_calls": r.total_calls,
                "tokens": {
                    "input": r.input_tokens,
                    "output": r.output_tokens,
                    "reasoning": r.reasoning_tokens,
                    "answer": r.answer_tokens,
                },
                "majority_vote": r.majority_vote,
                "rounds": r.rounds,
                "returncode": r.returncode,
                "error": r.error,
            }
            for r in all_results
        ],
    }
    summary_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Saved to {summary_path}", file=sys.stderr)

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run open_dirac_rsa N times on a single problem with concurrency.",
    )
    parser.add_argument("problem", type=Path, help="Path to problem YAML file")
    parser.add_argument("--model", type=str, default=None,
                        help=f"Model key from models.yaml (default: {DEFAULTS['model']})")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="Max output tokens per LLM call (default: RSA runner default)")
    parser.add_argument("--config", type=Path, default=None,
                        help="Config YAML file to pass through")
    parser.add_argument("-N", type=int, default=None,
                        help="RSA population size (default: RSA runner default — 6)")
    parser.add_argument("-K", type=int, default=None,
                        help="RSA aggregation subset size (default: RSA runner default — 2)")
    parser.add_argument("-T", type=int, default=None,
                        help="RSA number of rounds (default: RSA runner default — 4)")
    parser.add_argument("--rsa-concurrency", type=int, default=None,
                        help="Max parallel LLM calls within a single RSA run "
                             "(default: RSA runner default — N)")
    parser.add_argument("--runs", type=int, required=True,
                        help="Number of independent RSA runs")
    parser.add_argument("--concurrency", type=int, default=3,
                        help="Max parallel RSA runs (default: 3). "
                             "Note: each RSA run itself fans out up to N calls, "
                             "so effective in-flight LLM calls ≈ concurrency × N.")
    parser.add_argument("--timeout", type=int, default=3600,
                        help="Per-run timeout in seconds (default: 3600)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_BASE,
                        help=f"Base directory for batch output "
                             f"(default: {DEFAULT_OUTPUT_BASE.relative_to(PROJECT_ROOT)}/)")
    args = parser.parse_args()

    if args.runs < 1:
        print("Error: --runs must be >= 1", file=sys.stderr)
        sys.exit(1)
    if not args.problem.exists():
        print(f"Error: problem file not found: {args.problem}", file=sys.stderr)
        sys.exit(1)

    sys.exit(asyncio.run(run_multiple(args)))


if __name__ == "__main__":
    main()