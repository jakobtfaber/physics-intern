#!/usr/bin/env python3
"""Run all 70 CritPt benchmark problems through OpenDirac with rolling parallelism.

Produces CritPt-format submission JSONs progressively. Supports resume from
interrupted runs (both at the problem level and mid-run via --resume).

Usage:
    uv run python scripts/run_critpt_open_dirac.py
    uv run python scripts/run_critpt_open_dirac.py --model claude-4.6-opus --concurrency 5
    uv run python scripts/run_critpt_open_dirac.py --problems 1-10 --max-iterations 50
    uv run python scripts/run_critpt_open_dirac.py --dry-run
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML = PROJECT_ROOT / "src" / "open_dirac" / "models.yaml"
DEFAULT_PROBLEMS_DIR = PROJECT_ROOT / "problems" / "critpt" / "yaml"
DEFAULT_WORKSPACE_BASE = PROJECT_ROOT / "workspaces"
DEFAULT_RESULTS_BASE = PROJECT_ROOT / "results" / "critpt"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run CritPt benchmark problems through OpenDirac in parallel.",
    )
    p.add_argument("--model", default=None,
                   help="Model key from models.yaml (default: gemini-3-flash-preview)")
    p.add_argument("--max-iterations", type=int, default=200,
                   help="Max iterations per problem (default: 200)")
    p.add_argument("--config", type=Path, default=None,
                   help="Config YAML file to pass through to each run")
    p.add_argument("--concurrency", type=int, default=10,
                   help="Max parallel runs (default: 10)")
    p.add_argument("--timeout", type=int, default=3600,
                   help="Per-problem timeout in seconds (default: 3600)")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output directory for submission JSONs")
    p.add_argument("--problems-dir", type=Path, default=DEFAULT_PROBLEMS_DIR,
                   help="Directory of problem YAMLs")
    p.add_argument("--workspace-base", type=Path, default=DEFAULT_WORKSPACE_BASE,
                   help="Base directory for workspaces")
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
        return f"{entry['provider']}/{entry['model_id']}"
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
# Resume logic (3-tier)
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


def find_existing_workspace(
    problem_id: str,
    model_key: str,
    workspace_base: Path,
) -> Path | None:
    """Find the most recent workspace for a problem, if any."""
    safe_model = model_key.replace("/", "-").replace(":", "-")
    # Workspace dirs look like: YYYYMMDD_HHMMSS_Challenge_N_main_model
    matches: list[Path] = []
    if not workspace_base.exists():
        return None
    for d in workspace_base.iterdir():
        if d.is_dir() and problem_id in d.name and safe_model in d.name:
            matches.append(d)
    if not matches:
        return None
    # Most recent by name (timestamp prefix)
    matches.sort(key=lambda p: p.name, reverse=True)
    return matches[0]


@dataclass
class ResumeAction:
    """What to do for a problem on resume."""
    problem: Problem
    action: str  # "skip", "extract", "resume", "fresh"
    workspace: Path | None = None
    answer_code: str | None = None


def plan_actions(
    problems: list[Problem],
    output_dir: Path,
    workspace_base: Path,
    model_key: str,
    force: bool,
) -> list[ResumeAction]:
    """Determine the action for each problem."""
    completed = set() if force else find_completed_submissions(output_dir)
    actions: list[ResumeAction] = []
    for p in problems:
        if p.n in completed:
            actions.append(ResumeAction(problem=p, action="skip"))
            continue

        ws = find_existing_workspace(p.problem_id, model_key, workspace_base)
        if ws:
            answer_path = ws / "ANSWER.md"
            if answer_path.exists():
                code = answer_path.read_text().strip()
                if code and not code.startswith("FORMATTER_REJECTION"):
                    actions.append(ResumeAction(
                        problem=p, action="extract",
                        workspace=ws, answer_code=code,
                    ))
                    continue
            # Workspace exists but no valid answer — try to resume
            graph_path = ws / "RESEARCH_GRAPH.json"
            if graph_path.exists():
                actions.append(ResumeAction(
                    problem=p, action="resume", workspace=ws,
                ))
                continue
        # No workspace or empty workspace — fresh run
        actions.append(ResumeAction(problem=p, action="fresh"))
    return actions


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    problem_n: int
    problem_id: str
    success: bool
    workspace_dir: Path | None
    answer_code: str | None
    error: str | None
    duration_s: float
    returncode: int | None = None


# ---------------------------------------------------------------------------
# Worker: run one problem
# ---------------------------------------------------------------------------

async def run_one_problem(
    action: ResumeAction,
    model_key: str,
    max_iterations: int,
    config_path: Path | None,
    workspace_base: Path,
    timeout: float,
    semaphore: asyncio.Semaphore,
) -> RunResult:
    """Run a single CritPt problem as a subprocess."""
    problem = action.problem

    # "extract" actions don't need a subprocess
    if action.action == "extract":
        return RunResult(
            problem_n=problem.n,
            problem_id=problem.problem_id,
            success=True,
            workspace_dir=action.workspace,
            answer_code=action.answer_code,
            error=None,
            duration_s=0.0,
        )

    async with semaphore:
        # Build subprocess command
        if action.action == "resume" and action.workspace:
            # Auto-clean dirty workspace to avoid interactive prompt in
            # _handle_dirty_workspace (which would hang in a subprocess).
            ws = action.workspace
            p = await asyncio.create_subprocess_exec(
                "git", "checkout", ".", cwd=str(ws),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
            p = await asyncio.create_subprocess_exec(
                "git", "clean", "-fd", cwd=str(ws),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
            cmd = [
                "uv", "run", "open_dirac",
                "--resume", str(action.workspace),
                "--max-iterations", str(max_iterations),
            ]
            workspace_dir = action.workspace
        else:
            # Fresh run
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_model = model_key.replace("/", "-").replace(":", "-")
            ws_name = f"{timestamp}_{problem.problem_id}_{safe_model}"
            workspace_dir = workspace_base / ws_name

            cmd = [
                "uv", "run", "open_dirac",
                str(problem.yaml_path),
                "--model", model_key,
                "--max-iterations", str(max_iterations),
                "--workspace-dir", str(workspace_dir),
            ]

        if config_path:
            cmd.extend(["--config", str(config_path)])

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,  # new process group for clean kill
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            elapsed = time.monotonic() - start

            # Extract answer
            answer_code = None
            answer_path = workspace_dir / "ANSWER.md"
            if answer_path.exists():
                raw = answer_path.read_text().strip()
                if raw and not raw.startswith("FORMATTER_REJECTION"):
                    answer_code = raw

            success = answer_code is not None
            error = None
            if proc.returncode != 0 and answer_code is None:
                stderr_tail = stderr.decode(errors="replace")[-500:] if stderr else ""
                error = f"exit code {proc.returncode}: {stderr_tail}"
            elif answer_code is None:
                error = "no valid ANSWER.md produced"

            return RunResult(
                problem_n=problem.n,
                problem_id=problem.problem_id,
                success=success,
                workspace_dir=workspace_dir,
                answer_code=answer_code,
                error=error,
                duration_s=elapsed,
                returncode=proc.returncode,
            )

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            # Kill the entire process group (includes child computations)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            # Still try to extract answer from partial work
            answer_code = None
            answer_path = workspace_dir / "ANSWER.md"
            if answer_path.exists():
                raw = answer_path.read_text().strip()
                if raw and not raw.startswith("FORMATTER_REJECTION"):
                    answer_code = raw
            return RunResult(
                problem_n=problem.n,
                problem_id=problem.problem_id,
                success=answer_code is not None,
                workspace_dir=workspace_dir,
                answer_code=answer_code,
                error=None if answer_code else f"timeout after {timeout:.0f}s",
                duration_s=elapsed,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            return RunResult(
                problem_n=problem.n,
                problem_id=problem.problem_id,
                success=False,
                workspace_dir=workspace_dir,
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
    """Write batch_metadata.json summarizing the run.

    Includes both current-run results and previously completed submissions
    found on disk, so metadata is accurate after resumed runs.
    """
    # Collect all submission JSONs on disk (includes previous runs)
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
        },
        "problems": [
            {
                "problem_id": r.problem_id,
                "success": r.success,
                "duration_s": round(r.duration_s, 1),
                "error": r.error,
                "workspace": str(r.workspace_dir) if r.workspace_dir else None,
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
        args.model = "gemini-3-flash-preview"

    # Resolve model
    critpt_model = resolve_critpt_model_string(args.model)

    # Discover problems
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

    # Plan actions (resume logic)
    actions = plan_actions(
        problems, output_dir, args.workspace_base, args.model, args.force,
    )

    n_skip = sum(1 for a in actions if a.action == "skip")
    n_extract = sum(1 for a in actions if a.action == "extract")
    n_resume = sum(1 for a in actions if a.action == "resume")
    n_fresh = sum(1 for a in actions if a.action == "fresh")

    # Print plan
    print(f"Model:       {args.model} ({critpt_model})", file=sys.stderr)
    print(f"Problems:    {len(problems)} total", file=sys.stderr)
    print(f"  skip:      {n_skip} (submission exists)", file=sys.stderr)
    print(f"  extract:   {n_extract} (answer exists, write JSON)", file=sys.stderr)
    print(f"  resume:    {n_resume} (continue interrupted run)", file=sys.stderr)
    print(f"  fresh:     {n_fresh} (new run)", file=sys.stderr)
    print(f"Concurrency: {args.concurrency}", file=sys.stderr)
    print(f"Timeout:     {args.timeout}s per problem", file=sys.stderr)
    print(f"Output:      {output_dir}", file=sys.stderr)
    print("---", file=sys.stderr)

    # Filter to actionable items
    to_run = [a for a in actions if a.action != "skip"]
    if not to_run:
        print("All problems already completed.", file=sys.stderr)
        return 0

    if args.dry_run:
        for a in to_run:
            ws_note = ""
            if a.workspace:
                ws_note = f"  (ws: {a.workspace.name})"
            print(f"  [{a.action:7s}] {a.problem.problem_id}{ws_note}", file=sys.stderr)
        return 0

    # Generation config for submission metadata
    generation_config = {
        "system": "open_dirac",
        "model_key": args.model,
        "max_iterations": args.max_iterations,
        "config_file": str(args.config) if args.config else None,
        "use_python": True,
        "use_web_search": False,
        "use_golden_for_prev_steps": False,
        "parsing": False,
        "multiturn_with_answer": False,
    }

    # Run
    semaphore = asyncio.Semaphore(args.concurrency)
    start_time = datetime.now(timezone.utc)
    total = len(to_run)
    completed = 0
    succeeded = 0
    failed = 0
    all_results: list[RunResult] = []
    lock = asyncio.Lock()

    async def worker(action: ResumeAction) -> RunResult:
        nonlocal completed, succeeded, failed

        result = await run_one_problem(
            action, args.model, args.max_iterations,
            args.config, args.workspace_base,
            args.timeout, semaphore,
        )

        # Write submission JSON immediately
        if result.success:
            write_submission_json(result, output_dir, critpt_model, generation_config)

        # Update progress
        async with lock:
            completed += 1
            if result.success:
                succeeded += 1
            else:
                failed += 1
            all_results.append(result)

            status = "OK" if result.success else f"FAIL: {result.error}"
            if result.duration_s > 0:
                print(
                    f"[{completed}/{total}] C{result.problem_n} "
                    f"({action.action}, {result.duration_s:.0f}s) {status}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[{completed}/{total}] C{result.problem_n} "
                    f"({action.action}) {status}",
                    file=sys.stderr,
                )

        return result

    # Launch all workers (semaphore controls actual concurrency)
    tasks = [asyncio.create_task(worker(a)) for a in to_run]

    # Handle Ctrl+C gracefully
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

    # Wait for all tasks
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect any exceptions that weren't caught
    for i, r in enumerate(results):
        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
            p = to_run[i].problem
            async with lock:
                err_result = RunResult(
                    problem_n=p.n, problem_id=p.problem_id,
                    success=False, workspace_dir=None, answer_code=None,
                    error=str(r), duration_s=0,
                )
                all_results.append(err_result)
                failed += 1
                completed += 1

    end_time = datetime.now(timezone.utc)

    # Write batch metadata
    write_batch_metadata(
        output_dir, critpt_model, all_results,
        generation_config, start_time, end_time,
    )

    # Final summary
    wall_clock = (end_time - start_time).total_seconds()
    print("---", file=sys.stderr)
    print(
        f"Done: {succeeded}/{total} succeeded, {failed} failed "
        f"({wall_clock:.0f}s wall clock)",
        file=sys.stderr,
    )
    print(f"Output: {output_dir}", file=sys.stderr)

    if failed > 0:
        print("\nFailed problems:", file=sys.stderr)
        for r in sorted(all_results, key=lambda x: x.problem_n):
            if not r.success:
                print(f"  C{r.problem_n}: {r.error}", file=sys.stderr)

    # Count total submissions in output dir
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
