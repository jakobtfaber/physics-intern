#!/usr/bin/env python3
"""Run open_dirac_oneshot N times on a single problem with concurrency control.

Reports pass@k results: how many runs produced a correct answer.

Usage:
    uv run python scripts/run_multiple_oneshot.py problems/critpt/yaml/Challenge_1_main.yaml --runs 10
    uv run python scripts/run_multiple_oneshot.py problems/critpt/yaml/Challenge_1_main.yaml --runs 10 --concurrency 5
    uv run python scripts/run_multiple_oneshot.py problems/critpt/yaml/Challenge_1_main.yaml --runs 20 --concurrency 10 --model claude-4.6-opus
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


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    run_index: int
    evaluation: str  # "correct", "incorrect", "error", "no_eval"
    duration_s: float
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    answer_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    response_text: str | None = None


# ---------------------------------------------------------------------------
# Stderr parsing
# ---------------------------------------------------------------------------

def parse_oneshot_stderr(stderr_text: str) -> dict:
    """Parse structured info from open_dirac_oneshot stderr output."""
    info: dict = {}

    m = re.search(r"Input tokens:\s+(\d+)", stderr_text)
    if m:
        info["input_tokens"] = int(m.group(1))
    m = re.search(r"Output tokens:\s+(\d+)", stderr_text)
    if m:
        info["output_tokens"] = int(m.group(1))
    m = re.search(r"Reasoning:\s+(\d+)", stderr_text)
    if m:
        info["reasoning_tokens"] = int(m.group(1))
    m = re.search(r"Answer:\s+(\d+)", stderr_text)
    if m:
        info["answer_tokens"] = int(m.group(1))
    m = re.search(r"Duration:\s+([\d.]+)s", stderr_text)
    if m:
        info["duration_s"] = float(m.group(1))
    m = re.search(r"Est\. cost:\s+\$([\d.]+)", stderr_text)
    if m:
        info["cost_usd"] = float(m.group(1))

    # Evaluation
    if "Evaluation:    CORRECT" in stderr_text:
        info["evaluation"] = "correct"
    elif "Evaluation:    INCORRECT" in stderr_text:
        info["evaluation"] = "incorrect"
    elif "Evaluation:    ERROR" in stderr_text:
        info["evaluation"] = "error"
    else:
        info["evaluation"] = "no_eval"

    return info


# ---------------------------------------------------------------------------
# Worker: run one instance
# ---------------------------------------------------------------------------

async def run_one(
    run_index: int,
    problem_path: Path,
    model_key: str | None,
    max_tokens: int | None,
    config_path: Path | None,
    timeout: float,
    semaphore: asyncio.Semaphore,
) -> RunResult:
    """Run a single open_dirac_oneshot subprocess."""
    cmd = ["uv", "run", "open_dirac_oneshot", str(problem_path)]
    if model_key:
        cmd.extend(["--model", model_key])
    if max_tokens is not None:
        cmd.extend(["--max-tokens", str(max_tokens)])
    if config_path:
        cmd.extend(["--config", str(config_path)])

    async with semaphore:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            elapsed = time.monotonic() - start

            stdout_text = stdout.decode(errors="replace")
            stderr_text = stderr.decode(errors="replace")

            info = parse_oneshot_stderr(stderr_text)

            return RunResult(
                run_index=run_index,
                evaluation=info.get("evaluation", "no_eval"),
                duration_s=info.get("duration_s", round(elapsed, 2)),
                input_tokens=info.get("input_tokens", 0),
                output_tokens=info.get("output_tokens", 0),
                reasoning_tokens=info.get("reasoning_tokens", 0),
                answer_tokens=info.get("answer_tokens", 0),
                cost_usd=info.get("cost_usd", 0.0),
                error=None if proc.returncode == 0 else f"exit code {proc.returncode}",
                response_text=stdout_text if stdout_text.strip() else None,
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
            return RunResult(
                run_index=run_index,
                evaluation="error",
                duration_s=round(elapsed, 2),
                error=f"timeout after {timeout:.0f}s",
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            return RunResult(
                run_index=run_index,
                evaluation="error",
                duration_s=round(elapsed, 2),
                error=f"{type(exc).__name__}: {exc}",
            )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run_multiple(args: argparse.Namespace) -> int:
    """Run N one-shot instances and report results."""
    n = args.runs
    concurrency = args.concurrency
    semaphore = asyncio.Semaphore(concurrency)

    print(f"Problem:     {args.problem.name}", file=sys.stderr)
    if args.model:
        print(f"Model:       {args.model}", file=sys.stderr)
    print(f"Runs:        {n}", file=sys.stderr)
    print(f"Concurrency: {concurrency}", file=sys.stderr)
    print(f"Timeout:     {args.timeout}s per run", file=sys.stderr)
    print("---", file=sys.stderr)

    start_time = datetime.now(timezone.utc)
    completed = 0
    all_results: list[RunResult] = []
    lock = asyncio.Lock()

    async def worker(run_index: int) -> RunResult:
        nonlocal completed
        result = await run_one(
            run_index, args.problem, args.model,
            args.max_tokens, args.config,
            args.timeout, semaphore,
        )
        async with lock:
            completed += 1
            all_results.append(result)
            status = result.evaluation.upper()
            if result.error and result.evaluation == "error":
                status = f"ERROR: {result.error}"
            print(
                f"[{completed}/{n}] run{run_index:03d} ({result.duration_s:.1f}s) {status}",
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

    # Sort results by run_index
    all_results.sort(key=lambda r: r.run_index)

    # Count outcomes
    counts: dict[str, int] = {"correct": 0, "incorrect": 0, "error": 0, "no_eval": 0}
    for r in all_results:
        counts[r.evaluation] = counts.get(r.evaluation, 0) + 1

    total_cost = sum(r.cost_usd for r in all_results)
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

    # Write JSON
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = (args.model or DEFAULTS["model"]).replace("/", "-").replace(":", "-")
    filename = f"{args.problem.stem}_{safe_model}_{timestamp}.json"
    output_path = output_dir / filename

    payload = {
        "mode": "oneshot",
        "problem": args.problem.stem,
        "problem_path": str(args.problem),
        "model": args.model or DEFAULTS["model"],
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
                "evaluation": r.evaluation,
                "duration_s": r.duration_s,
                "tokens": {
                    "input": r.input_tokens,
                    "output": r.output_tokens,
                    "reasoning": r.reasoning_tokens,
                    "answer": r.answer_tokens,
                },
                "cost_usd": r.cost_usd,
                "error": r.error,
            }
            for r in all_results
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Saved to {output_path}", file=sys.stderr)

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run open_dirac_oneshot N times on a single problem with concurrency.",
    )
    parser.add_argument("problem", type=Path, help="Path to problem YAML file")
    parser.add_argument("--model", type=str, default=None,
                        help=f"Model key from models.yaml (default: {DEFAULTS['model']})")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="Max output tokens per run")
    parser.add_argument("--config", type=Path, default=None,
                        help="Config YAML file to pass through")
    parser.add_argument("--runs", type=int, required=True,
                        help="Number of independent runs")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Max parallel runs (default: 10)")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Per-run timeout in seconds (default: 1800)")
    parser.add_argument("--output-dir", type=Path,
                        default=PROJECT_ROOT / "results" / "multiple_oneshot",
                        help="Output directory for results JSON")
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
