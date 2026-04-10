#!/usr/bin/env python3
"""Run CritPt benchmark problems through RSA (Recursive Self-Aggregation).

Each problem = N*T LLM calls via `open_dirac.rsa`. Produces CritPt-format
submission JSONs progressively. Supports resume from interrupted runs.

Usage:
    uv run python scripts/run_critpt_rsa.py
    uv run python scripts/run_critpt_rsa.py --model claude-4.6-opus -N 6 -K 2 -T 4
    uv run python scripts/run_critpt_rsa.py --problems 1-10
    uv run python scripts/run_critpt_rsa.py --dry-run
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

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML = PROJECT_ROOT / "src" / "open_dirac" / "models.yaml"
DEFAULT_PROBLEMS_DIR = PROJECT_ROOT / "problems" / "critpt" / "yaml"
DEFAULT_RESULTS_BASE = PROJECT_ROOT / "results" / "critpt_rsa"

# Import extract_answer_code (pure regex, no heavy deps)
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from open_dirac.verification.evaluate import extract_answer_code  # noqa: E402


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run CritPt benchmark problems through RSA in parallel.",
    )
    p.add_argument("--model", default=None,
                   help="Model key from models.yaml (default: claude-4.6-sonnet)")
    p.add_argument("--max-tokens", type=int, default=128000,
                   help="Max output tokens per LLM call (default: 128000)")
    p.add_argument("-N", type=int, default=6,
                   help="RSA population size (default: 6)")
    p.add_argument("-K", type=int, default=2,
                   help="RSA aggregation subset size (default: 2)")
    p.add_argument("-T", type=int, default=4,
                   help="RSA number of rounds (default: 4)")
    p.add_argument("--concurrency", type=int, default=3,
                   help="Max parallel problems (default: 3)")
    p.add_argument("--timeout", type=int, default=1800,
                   help="Per-problem timeout in seconds (default: 1800)")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output directory for submission JSONs")
    p.add_argument("--problems-dir", type=Path, default=DEFAULT_PROBLEMS_DIR,
                   help="Directory of problem YAMLs")
    p.add_argument("--problems", type=str, default=None,
                   help='Subset of problems, e.g. "1-10" or "1,5,30-40"')
    p.add_argument("--force", action="store_true",
                   help="Re-run problems even if submission JSON already exists")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be run without executing")
    return p


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def resolve_critpt_model_string(model_key: str) -> str:
    """Convert OpenDirac model key to CritPt format 'provider/model_id'."""
    if not MODELS_YAML.exists():
        return model_key
    registry = yaml.safe_load(MODELS_YAML.read_text())
    entry = registry.get(model_key)
    if entry:
        model_id = entry.get('model_id', model_key)
        return f"{entry['provider']}/{model_id}"
    return model_key


# ---------------------------------------------------------------------------
# Problem discovery
# ---------------------------------------------------------------------------

def parse_problem_range(range_str: str) -> set[int]:
    """Parse '1-10,15,30-40' into a set of ints."""
    result: set[int] = set()
    for part in range_str.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo), int(hi) + 1))
        else:
            result.add(int(part))
    return result


@dataclass
class Problem:
    n: int
    problem_id: str
    yaml_path: Path


def discover_problems(
    problems_dir: Path,
    problem_range: str | None = None,
) -> list[Problem]:
    """Return sorted list of CritPt problems."""
    pattern = re.compile(r"Challenge_(\d+)_main\.yaml$")
    problems: list[Problem] = []
    for p in sorted(problems_dir.iterdir()):
        m = pattern.match(p.name)
        if m:
            n = int(m.group(1))
            problems.append(Problem(
                n=n,
                problem_id=f"Challenge_{n}_main",
                yaml_path=p.resolve(),
            ))
    if problem_range:
        wanted = parse_problem_range(problem_range)
        problems = [p for p in problems if p.n in wanted]
    problems.sort(key=lambda p: p.n)
    return problems


# ---------------------------------------------------------------------------
# Resume logic
# ---------------------------------------------------------------------------

def read_model_from_output_dir(output_dir: Path) -> str | None:
    """Try to recover the model key from a previous run's output directory.

    Prioritizes submission JSONs over batch_metadata.json because the latter
    gets overwritten on every run (including failed resumes with wrong model).
    """
    # Check submission JSONs first — these are written once per successful problem
    for f in output_dir.glob("Challenge_*_main.json"):
        try:
            data = json.loads(f.read_text())
            model_key = data.get("generation_config", {}).get("model_key")
            if model_key:
                return model_key
        except (json.JSONDecodeError, KeyError):
            continue
    # Fall back to batch_metadata.json
    meta_path = output_dir / "batch_metadata.json"
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text())
            model_key = data.get("generation_config", {}).get("model_key")
            if model_key:
                return model_key
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def find_completed_submissions(output_dir: Path) -> set[int]:
    """Return problem numbers that already have valid submission JSONs."""
    completed: set[int] = set()
    pattern = re.compile(r"Challenge_(\d+)_main\.json$")
    for f in output_dir.glob("Challenge_*_main.json"):
        m = pattern.match(f.name)
        if not m:
            continue
        try:
            data = json.loads(f.read_text())
            if data.get("problem_id") and data.get("generated_code"):
                completed.add(int(m.group(1)))
        except (json.JSONDecodeError, KeyError):
            pass
    return completed


# ---------------------------------------------------------------------------
# Stderr stats parser
# ---------------------------------------------------------------------------

def _parse_stderr_stats(stderr: str) -> dict | None:
    """Best-effort extraction of token/cost stats from RSA stderr."""
    stats: dict = {}
    for line in stderr.splitlines():
        try:
            if "total:" in line.lower() and "$" in line:
                # "Majority vote: 4/6 agree (total: 120.5s, $0.1234)"
                m = re.search(r"\$([0-9.]+)", line)
                if m:
                    stats["cost_usd"] = float(m.group(1))
                m = re.search(r"total:\s*([0-9.]+)s", line)
                if m:
                    stats["duration_s"] = float(m.group(1))
            elif "total calls" in line.lower():
                m = re.search(r"(\d+)\s+total calls", line, re.I)
                if m:
                    stats["total_calls"] = int(m.group(1))
            elif line.startswith("Tokens:"):
                # "Tokens: input=123456, output=78901"
                m = re.search(r"input=(\d+)", line)
                if m:
                    stats["input_tokens"] = int(m.group(1))
                m = re.search(r"output=(\d+)", line)
                if m:
                    stats["output_tokens"] = int(m.group(1))
        except (ValueError, IndexError):
            pass
    return stats if stats else None


# ---------------------------------------------------------------------------
# Raw response logging
# ---------------------------------------------------------------------------

_logs_dir: Path | None = None


def _save_raw_response(
    problem: Problem,
    stdout_text: str,
    stderr_text: str,
    success: bool,
) -> None:
    """Save raw stdout/stderr to logs/ for debugging."""
    if _logs_dir is None:
        return
    prefix = "ok" if success else "FAIL"
    log_path = _logs_dir / f"{prefix}_{problem.problem_id}.txt"
    try:
        with open(log_path, "w") as f:
            f.write(f"=== STDOUT ({len(stdout_text)} chars) ===\n")
            f.write(stdout_text)
            f.write(f"\n\n=== STDERR ({len(stderr_text)} chars) ===\n")
            f.write(stderr_text)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    problem_n: int
    problem_id: str
    success: bool
    answer_code: str | None
    error: str | None
    duration_s: float
    stats: dict | None = None
    returncode: int | None = None


# ---------------------------------------------------------------------------
# Worker: run one problem
# ---------------------------------------------------------------------------

async def run_one_problem(
    problem: Problem,
    model_key: str,
    max_tokens: int,
    rsa_N: int,
    rsa_K: int,
    rsa_T: int,
    timeout: float,
    semaphore: asyncio.Semaphore,
) -> RunResult:
    """Run a single CritPt problem via RSA subprocess."""
    async with semaphore:
        cmd = [
            "uv", "run", "python", "-m", "open_dirac.rsa",
            str(problem.yaml_path),
            "--model", model_key,
            "--max-tokens", str(max_tokens),
            "-N", str(rsa_N),
            "-K", str(rsa_K),
            "-T", str(rsa_T),
        ]

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

            # Extract answer code from LLM response
            if stdout_text.strip() and "```python" not in stdout_text:
                fenced = f"```python\n{stdout_text}\n```"
            else:
                fenced = stdout_text
            answer_code = extract_answer_code(fenced) if stdout_text.strip() else None

            stats = _parse_stderr_stats(stderr_text)

            success = answer_code is not None
            error = None
            if not success:
                if proc.returncode != 0:
                    error = f"exit code {proc.returncode}: {stderr_text[-500:]}"
                else:
                    error = "no answer code found in response"

            _save_raw_response(problem, stdout_text, stderr_text, success)

            return RunResult(
                problem_n=problem.n,
                problem_id=problem.problem_id,
                success=success,
                answer_code=answer_code,
                error=error,
                duration_s=elapsed,
                stats=stats,
                returncode=proc.returncode,
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
                problem_n=problem.n,
                problem_id=problem.problem_id,
                success=False,
                answer_code=None,
                error=f"timeout after {timeout:.0f}s",
                duration_s=elapsed,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            return RunResult(
                problem_n=problem.n,
                problem_id=problem.problem_id,
                success=False,
                answer_code=None,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=elapsed,
            )


# ---------------------------------------------------------------------------
# Submission JSON writing
# ---------------------------------------------------------------------------

def write_submission_json(
    result: RunResult,
    output_dir: Path,
    critpt_model: str,
    generation_config: dict,
) -> Path | None:
    """Write a CritPt-format submission JSON. Returns path or None."""
    if not result.answer_code:
        return None
    submission = {
        "problem_id": result.problem_id,
        "generated_code": result.answer_code,
        "model": critpt_model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "generation_config": generation_config,
        "messages": [],
    }
    out_path = output_dir / f"{result.problem_id}.json"
    out_path.write_text(json.dumps(submission, indent=2, ensure_ascii=False))
    return out_path


def write_batch_metadata(
    output_dir: Path,
    critpt_model: str,
    all_results: list[RunResult],
    generation_config: dict,
    start_time: datetime,
    end_time: datetime,
) -> None:
    """Write batch_metadata.json summarizing the run."""
    all_submission_ids: list[str] = []
    pattern = re.compile(r"Challenge_(\d+)_main\.json$")
    for f in sorted(output_dir.glob("Challenge_*_main.json")):
        m = pattern.match(f.name)
        if m:
            try:
                data = json.loads(f.read_text())
                if data.get("problem_id") and data.get("generated_code"):
                    all_submission_ids.append(data["problem_id"])
            except (json.JSONDecodeError, KeyError):
                pass

    n_run_success = sum(1 for r in all_results if r.success)
    n_run_failed = sum(1 for r in all_results if not r.success)
    total_duration = sum(r.duration_s for r in all_results)
    total_cost = sum(
        (r.stats or {}).get("cost_usd", 0.0) for r in all_results
    )
    total_input_tokens = sum(
        (r.stats or {}).get("input_tokens", 0) for r in all_results
    )
    total_output_tokens = sum(
        (r.stats or {}).get("output_tokens", 0) for r in all_results
    )

    metadata = {
        "model": critpt_model,
        "timestamp": end_time.isoformat(),
        "generation_config": generation_config,
        "num_submissions": len(all_submission_ids),
        "problem_ids": all_submission_ids,
        "summary": {
            "total_submissions": len(all_submission_ids),
            "this_run_total": len(all_results),
            "this_run_success": n_run_success,
            "this_run_failed": n_run_failed,
            "total_compute_s": round(total_duration, 1),
            "wall_clock_s": round((end_time - start_time).total_seconds(), 1),
            "total_cost_usd": round(total_cost, 4),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
        },
        "problems": [
            {
                "problem_id": r.problem_id,
                "success": r.success,
                "duration_s": round(r.duration_s, 1),
                "error": r.error,
                "stats": r.stats,
            }
            for r in sorted(all_results, key=lambda r: r.problem_n)
        ],
    }
    (output_dir / "batch_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run_batch(args: argparse.Namespace) -> int:
    """Main batch orchestrator. Returns exit code."""
    # Resolve model: explicit flag > previous run metadata > default
    if args.model is None and args.output_dir and args.output_dir.exists():
        recovered = read_model_from_output_dir(args.output_dir)
        if recovered:
            args.model = recovered
            print(f"Resumed model from previous run: {recovered}", file=sys.stderr)
    if args.model is None:
        args.model = "claude-4.6-sonnet"

    critpt_model = resolve_critpt_model_string(args.model)

    problems = discover_problems(args.problems_dir, args.problems)
    if not problems:
        print("Error: no problems found", file=sys.stderr)
        return 1

    # Output directory
    if args.output_dir:
        output_dir = args.output_dir
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_model = args.model.replace("/", "-").replace(":", "-")
        output_dir = DEFAULT_RESULTS_BASE / safe_model / ts
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create logs directory
    global _logs_dir
    _logs_dir = output_dir / "logs"
    _logs_dir.mkdir(exist_ok=True)

    # Resume: skip completed
    n_skip = 0
    if not args.force:
        completed = find_completed_submissions(output_dir)
        before = len(problems)
        problems = [p for p in problems if p.n not in completed]
        n_skip = before - len(problems)

    N, K, T = args.N, args.K, args.T

    # Print plan
    print(f"Model:       {args.model} ({critpt_model})", file=sys.stderr)
    print(f"RSA params:  N={N}, K={K}, T={T} ({N * T} calls/problem)",
          file=sys.stderr)
    print(f"Problems:    {len(problems) + n_skip} total, "
          f"{n_skip} skipped, {len(problems)} to run", file=sys.stderr)
    print(f"Concurrency: {args.concurrency} problems", file=sys.stderr)
    print(f"Timeout:     {args.timeout}s per problem", file=sys.stderr)
    print(f"Output:      {output_dir}", file=sys.stderr)
    print("---", file=sys.stderr)

    if not problems:
        print("All problems already completed.", file=sys.stderr)
        return 0

    if args.dry_run:
        for p in problems:
            print(f"  {p.problem_id}", file=sys.stderr)
        return 0

    # Generation config for submission metadata
    generation_config = {
        "system": "open_dirac_rsa",
        "model_key": args.model,
        "max_tokens": args.max_tokens,
        "rsa_N": N,
        "rsa_K": K,
        "rsa_T": T,
        "use_python": False,
        "use_web_search": False,
        "use_golden_for_prev_steps": False,
        "parsing": False,
        "multiturn_with_answer": False,
    }

    # Run with semaphore-controlled concurrency
    semaphore = asyncio.Semaphore(args.concurrency)
    start_time = datetime.now(timezone.utc)
    total = len(problems)
    completed_count = 0
    succeeded = 0
    failed = 0
    all_results: list[RunResult] = []
    lock = asyncio.Lock()

    async def worker(problem: Problem) -> RunResult:
        nonlocal completed_count, succeeded, failed

        result = await run_one_problem(
            problem, args.model, args.max_tokens,
            N, K, T,
            args.timeout, semaphore,
        )

        # Write submission JSON immediately
        if result.success:
            write_submission_json(result, output_dir, critpt_model, generation_config)

        # Update progress
        async with lock:
            completed_count += 1
            if result.success:
                succeeded += 1
            else:
                failed += 1
            all_results.append(result)

            status = "OK" if result.success else f"FAIL: {result.error}"
            cost_str = ""
            if result.stats and result.stats.get("cost_usd"):
                cost_str = f", ${result.stats['cost_usd']:.4f}"
            print(
                f"[{completed_count}/{total}] C{result.problem_n} "
                f"({result.duration_s:.0f}s{cost_str}) {status}",
                file=sys.stderr,
            )

        return result

    # Launch all workers
    tasks = [asyncio.create_task(worker(p)) for p in problems]

    # Handle Ctrl+C
    loop = asyncio.get_running_loop()
    cancelled = False

    def _signal_handler():
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            print("\nInterrupted — cancelling pending tasks...", file=sys.stderr)
            for t in tasks:
                if not t.done():
                    t.cancel()

    loop.add_signal_handler(signal.SIGINT, _signal_handler)

    # Wait for all
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect exceptions
    for i, r in enumerate(results):
        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
            p = problems[i]
            async with lock:
                all_results.append(RunResult(
                    problem_n=p.n, problem_id=p.problem_id,
                    success=False, answer_code=None,
                    error=str(r), duration_s=0,
                ))
                failed += 1
                completed_count += 1

    end_time = datetime.now(timezone.utc)

    # Write batch metadata
    write_batch_metadata(
        output_dir, critpt_model, all_results,
        generation_config, start_time, end_time,
    )

    # Final summary
    wall_clock = (end_time - start_time).total_seconds()
    total_cost = sum((r.stats or {}).get("cost_usd", 0.0) for r in all_results)
    print("---", file=sys.stderr)
    print(
        f"Done: {succeeded}/{total} succeeded, {failed} failed "
        f"({wall_clock:.0f}s wall clock, ${total_cost:.2f} est. cost)",
        file=sys.stderr,
    )
    print(f"Output: {output_dir}", file=sys.stderr)

    if failed > 0:
        print("\nFailed problems:", file=sys.stderr)
        for r in sorted(all_results, key=lambda x: x.problem_n):
            if not r.success:
                print(f"  C{r.problem_n}: {r.error}", file=sys.stderr)

    n_jsons = len(list(output_dir.glob("Challenge_*_main.json")))
    print(f"\nSubmission JSONs: {n_jsons}/70", file=sys.stderr)
    if n_jsons < 70:
        print(
            "Note: CritPt requires all 70 for batch submission. "
            "Re-run to attempt missing problems.",
            file=sys.stderr,
        )

    return 0 if failed == 0 else 1


def main():
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(run_batch(args)))


if __name__ == "__main__":
    main()
