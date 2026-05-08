#!/usr/bin/env python3
"""Run all 70 CritPt benchmark problems through PhysicsIntern with rolling parallelism.

Produces CritPt-format submission JSONs progressively. Supports resume from
interrupted runs (both at the problem level and mid-run via --resume).

Usage:
    uv run python scripts/run_critpt_physics_intern.py
    uv run python scripts/run_critpt_physics_intern.py --model claude-4.6-opus --concurrency 5
    uv run python scripts/run_critpt_physics_intern.py --problems 1-10 --config config.cluster.yaml
    uv run python scripts/run_critpt_physics_intern.py --resume results/critpt/model/run_dir/
    uv run python scripts/run_critpt_physics_intern.py --dry-run

Engine-side parameters (max_iterations, max_wall_seconds,
max_total_output_tokens, max_cost_usd) live in --config; defaults come
from src/physics_intern/config.default.yaml. See config.cluster.yaml for the
cluster preset.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from run_critpt_common import (
    PROJECT_ROOT,
    DEFAULT_PROBLEMS_DIR,
    DEFAULTS,
    Problem,
    RunResult,
    resolve_critpt_model_string,
    discover_problems,
    load_resume_config,
    find_completed_submissions,
    resolve_model,
    make_output_dir,
    write_submission_json,
    write_batch_metadata,
    write_initial_batch_metadata,
    setup_signal_handler,
    print_final_summary,
)

from physics_intern.core.config import load_config_yaml  # noqa: E402

DEFAULT_WORKSPACE_BASE = PROJECT_ROOT / "workspaces"
DEFAULT_RESULTS_BASE = PROJECT_ROOT / "results" / "critpt"
FORMATTER_REJECTION_PREFIX = "FORMATTER_REJECTION"


# ---------------------------------------------------------------------------
# Engine config resolution
# ---------------------------------------------------------------------------


def resolve_engine_params(config_path: Path | None) -> dict:
    """Merge config.default.yaml with the user's --config override.

    Returns the engine-side parameters the runner needs to know about
    (for header printing, batch metadata, and output-dir naming). The
    engine itself re-merges the same way when it reads --config; the
    runner just mirrors that resolution.
    """
    merged = dict(DEFAULTS)
    if config_path is not None:
        merged.update(load_config_yaml(config_path))
    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run CritPt benchmark problems through PhysicsIntern in parallel.",
    )
    p.add_argument(
        "--resume",
        type=Path,
        default=None,
        help="Resume from an existing output directory (recovers all params)",
    )
    p.add_argument(
        "--model",
        default=None,
        help=f"Model key from models.yaml (default: {DEFAULTS['model']})",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Config YAML file passed through to each engine subprocess. "
            "Engine-side parameters (max_iterations, max_wall_seconds, "
            "max_total_output_tokens, max_cost_usd, ...) are read from "
            "this file (merged on top of config.default.yaml). See "
            "config.cluster.yaml for the cluster preset."
        ),
    )
    p.add_argument(
        "--concurrency", type=int, default=64, help="Max parallel runs (default: 64)"
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for submission JSONs",
    )
    p.add_argument(
        "--problems-dir",
        type=Path,
        default=DEFAULT_PROBLEMS_DIR,
        help="Directory of problem YAMLs",
    )
    p.add_argument(
        "--workspace-base",
        type=Path,
        default=DEFAULT_WORKSPACE_BASE,
        help="Base directory for workspaces",
    )
    p.add_argument(
        "--problems",
        type=str,
        default=None,
        help='Subset of problems, e.g. "1-10" or "1,5,30-40"',
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-run problems even if submission JSON already exists",
    )
    p.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore existing workspaces; start every problem from scratch",
    )
    p.add_argument(
        "--no-sibling-history",
        action="store_true",
        help="Do not fold prior attempts from sibling output dirs "
        "into batch_metadata.json",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be run without executing",
    )
    return p


# ---------------------------------------------------------------------------
# Workspace-based resume logic
# ---------------------------------------------------------------------------


# Suffixes used by other pipelines — must not be matched by multi-agent resume.
_NON_AGENT_SUFFIXES = ("_oneshot", "_rsa", "_autophysicist")


def find_existing_workspace(
    problem_id: str,
    model_key: str,
    workspace_base: Path,
) -> Path | None:
    """Find the most recent *multi-agent* workspace for a problem, if any."""
    safe_model = model_key.replace("/", "-").replace(":", "-")
    # Workspace dirs look like: YYYYMMDD_HHMMSS_Challenge_N_main_model
    matches: list[Path] = []
    if not workspace_base.exists():
        return None
    for d in workspace_base.iterdir():
        if not d.is_dir():
            continue
        if problem_id not in d.name or safe_model not in d.name:
            continue
        if d.name.endswith(_NON_AGENT_SUFFIXES):
            continue
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
    cleanup_answer_before_resume: bool = False
    cleanup_answer_reason: str = ""


def _is_formatter_rejection_answer(answer_text: str) -> bool:
    """Return whether ``ANSWER.md`` contains a formatter rejection marker."""
    return answer_text.strip().startswith(FORMATTER_REJECTION_PREFIX)


def _commit_answer_cleanup_before_resume(workspace: Path, reason: str) -> None:
    """Remove an invalid final answer and commit that cleanup before resume.

    ``physics_intern.main --resume`` refuses any workspace with ``ANSWER.md``
    because that file is the canonical completion signal. Empty or explicit
    formatter-rejection answers are not valid submissions, so the batch runner
    removes them and records the cleanup in the challenge workspace git history
    before granting more budget.
    """
    answer_path = workspace / "ANSWER.md"
    if not answer_path.exists():
        return

    answer_text = answer_path.read_text()
    if answer_text.strip() and not _is_formatter_rejection_answer(answer_text):
        return

    answer_path.unlink()
    add_proc = subprocess.run(
        ["git", "add", "-A", "ANSWER.md"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if add_proc.returncode != 0:
        raise RuntimeError(
            f"Failed to stage ANSWER.md cleanup in {workspace}: "
            f"{add_proc.stderr.strip()}"
        )

    status_proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "ANSWER.md"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if status_proc.returncode != 0:
        raise RuntimeError(
            f"Failed to inspect ANSWER.md cleanup in {workspace}: "
            f"{status_proc.stderr.strip()}"
        )
    if not status_proc.stdout.strip():
        return

    commit_proc = subprocess.run(
        ["git", "commit", "-m", f"Remove invalid ANSWER.md before resume: {reason}"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_proc.returncode != 0:
        raise RuntimeError(
            f"Failed to commit ANSWER.md cleanup in {workspace}: "
            f"{commit_proc.stderr.strip()}"
        )


def plan_actions(
    problems: list[Problem],
    output_dir: Path,
    workspace_base: Path,
    model_key: str,
    force: bool,
    fresh: bool = False,
) -> list[ResumeAction]:
    """Determine the action for each problem."""
    completed = set() if force else find_completed_submissions(output_dir)
    actions: list[ResumeAction] = []
    for p in problems:
        if p.n in completed:
            actions.append(ResumeAction(problem=p, action="skip"))
            continue

        ws = (
            None
            if fresh
            else find_existing_workspace(p.problem_id, model_key, workspace_base)
        )
        if ws:
            answer_path = ws / "ANSWER.md"
            cleanup_answer_before_resume = False
            cleanup_answer_reason = ""
            if answer_path.exists():
                code = answer_path.read_text().strip()
                if code and not _is_formatter_rejection_answer(code):
                    actions.append(
                        ResumeAction(
                            problem=p,
                            action="extract",
                            workspace=ws,
                            answer_code=code,
                        )
                    )
                    continue
                cleanup_answer_before_resume = True
                cleanup_answer_reason = (
                    "formatter rejection"
                    if _is_formatter_rejection_answer(code)
                    else "empty answer"
                )
            # Workspace exists but no valid answer — try to resume
            graph_path = ws / "RESEARCH_GRAPH.json"
            if graph_path.exists():
                actions.append(
                    ResumeAction(
                        problem=p,
                        action="resume",
                        workspace=ws,
                        cleanup_answer_before_resume=cleanup_answer_before_resume,
                        cleanup_answer_reason=cleanup_answer_reason,
                    )
                )
                continue
        # No workspace or empty workspace — fresh run
        actions.append(ResumeAction(problem=p, action="fresh"))
    return actions


# ---------------------------------------------------------------------------
# Live progress surfacing (per-iteration line + API/stall warnings)
# ---------------------------------------------------------------------------

_ITERATION_RE = re.compile(r"ITERATION\s+(\d+)")
# Matches: "Transient API error (attempt K/M): {exc}"  — see src/physics_intern/llm.py
_RETRY_RE = re.compile(r"Transient API error \(attempt (\d+)/(\d+)\)(?::\s*(.+))?")

_STALL_WARN_AFTER_S = 15 * 60  # first warning after 15 min of silence
_STALL_REWARN_EVERY_S = 30 * 60  # re-warn every 30 min while still silent
_WATCHDOG_TICK_S = 60

# Per-problem live state, keyed by problem_id. Accessed by the streamer,
# the watchdog, and the final summary print. Writes are guarded by the
# print lock that protects terminal output.
_running: dict[str, dict] = {}


def _exc_label(tail: str | None) -> str:
    """Produce a short, informative label for the text after 'attempt K/M: '.

    Notes `llm.py` emits `str(exc)`, not `type(exc).__name__`, so an
    `APITimeoutError` appears here as `Request timed out.`. We map the
    common shapes explicitly.
    """
    if not tail:
        return "unknown"
    tail = tail.strip()
    if tail.startswith("Error code:"):
        m = re.match(r"Error code:\s*(\d+)", tail)
        if m:
            return f"HTTP {m.group(1)}"
    lower = tail.lower()
    if "timed out" in lower or "timeout" in lower:
        return "Timeout"
    if "connection" in lower:
        return "ConnectionError"
    # Fallback: first capitalised token (works when a class name is in the message)
    m = re.match(r"([A-Z][A-Za-z0-9_]*Error)", tail)
    if m:
        return m.group(1)
    return tail[:40]


def _read_soft_exit_reason(workspace_dir: Path) -> str | None:
    """Detect whether the most recent run ended via the forced formatter.

    Returns the soft-exit reason (e.g. ``"max_wall_seconds"``,
    ``"max_iterations"``) when ``RESEARCH_GRAPH.json`` shows
    ``status == "partially_complete"``; the specific reason is parsed
    from the ``Iteration N: forced formatter (<reason>)`` commit message
    in the workspace's git log. Returns the literal ``"unknown"`` if
    status indicates a soft-exit but the reason couldn't be recovered,
    and ``None`` for normal-completion runs or unreadable workspaces.
    """
    state_path = workspace_dir / "RESEARCH_GRAPH.json"
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("status") != "partially_complete":
        return None
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--grep=forced formatter", "--pretty=%s"],
            cwd=str(workspace_dir),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except (OSError, ValueError):
        return "unknown"
    m = re.search(r"forced formatter \(([^)]+)\)", out)
    return m.group(1) if m else "unknown"


async def _stall_watchdog(print_lock: asyncio.Lock) -> None:
    """Tick every 60s; warn when any running problem has been silent too long."""
    try:
        while True:
            await asyncio.sleep(_WATCHDOG_TICK_S)
            now = time.monotonic()
            to_warn: list[tuple[int, int]] = []  # (problem_n, silent_minutes)
            for state in list(_running.values()):
                silent_s = now - state["last_line_at"]
                if silent_s < _STALL_WARN_AFTER_S:
                    continue
                last_warn = state.get("last_stall_warn_at")
                if last_warn is not None and (now - last_warn) < _STALL_REWARN_EVERY_S:
                    continue
                state["last_stall_warn_at"] = now
                to_warn.append((state["problem_n"], int(silent_s // 60)))
            if to_warn:
                async with print_lock:
                    for pn, mins in to_warn:
                        print(
                            f"  C{pn}   ⚠ stalled {mins}m, no output",
                            file=sys.stderr,
                        )
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Worker: run one problem
# ---------------------------------------------------------------------------


async def run_one_problem(
    action: ResumeAction,
    model_key: str,
    config_path: Path | None,
    workspace_base: Path,
    semaphore: asyncio.Semaphore,
    print_lock: asyncio.Lock,
) -> RunResult:
    """Run a single CritPt problem as a subprocess."""
    problem = action.problem

    # "extract" actions don't need a subprocess
    if action.action == "extract":
        return RunResult(
            problem_n=problem.n,
            problem_id=problem.problem_id,
            success=True,
            answer_code=action.answer_code,
            error=None,
            duration_s=0.0,
            workspace_dir=action.workspace,
        )

    async with semaphore:
        # Build subprocess command
        if action.action == "resume" and action.workspace:
            # Auto-clean dirty workspace to avoid interactive prompt in
            # _handle_dirty_workspace (which would hang in a subprocess).
            ws = action.workspace
            p = await asyncio.create_subprocess_exec(
                "git",
                "checkout",
                ".",
                cwd=str(ws),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
            p = await asyncio.create_subprocess_exec(
                "git",
                "clean",
                "-fd",
                cwd=str(ws),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
            if action.cleanup_answer_before_resume:
                _commit_answer_cleanup_before_resume(
                    ws,
                    action.cleanup_answer_reason,
                )
            cmd = [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "physics_intern.main",
                "--resume",
                str(action.workspace),
            ]
            workspace_dir = action.workspace
        else:
            # Fresh run
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_model = model_key.replace("/", "-").replace(":", "-")
            ws_name = f"{timestamp}_{problem.problem_id}_{safe_model}"
            workspace_dir = workspace_base / ws_name

            cmd = [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "physics_intern.main",
                str(problem.yaml_path),
                "--model",
                model_key,
                "--workspace-dir",
                str(workspace_dir),
            ]

        if config_path:
            cmd.extend(["--config", str(config_path)])

        start = time.monotonic()
        state = {
            "problem_n": problem.n,
            "start_at": start,
            "last_line_at": start,
            "iter": 0,
            "api_retries": 0,
            "last_stall_warn_at": None,
        }
        _running[problem.problem_id] = state
        stats = {"api_retries": 0}
        stderr_tail: list[str] = []
        proc = None
        try:
            # PYTHONUNBUFFERED ensures stdout is flushed per-line so we can
            # detect iteration markers and API-retry lines in real time.
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,  # new process group for clean kill
                env=env,
            )

            async def _stream_stdout():
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace")
                    now = time.monotonic()
                    state["last_line_at"] = now
                    state["last_stall_warn_at"] = None  # any output resets stall

                    m = _ITERATION_RE.search(text)
                    if m:
                        state["iter"] = int(m.group(1))
                        elapsed_so_far = now - start
                        async with print_lock:
                            print(
                                f"  C{problem.n}   iter {m.group(1)}   "
                                f"({elapsed_so_far:.0f}s)",
                                file=sys.stderr,
                            )
                        continue

                    m = _RETRY_RE.search(text)
                    if m:
                        state["api_retries"] += 1
                        attempt = int(m.group(1))
                        max_att = int(m.group(2))
                        exc_label = _exc_label(m.group(3))
                        if attempt >= 2:
                            async with print_lock:
                                print(
                                    f"  C{problem.n}   ⚠ API  "
                                    f"attempt {attempt}/{max_att}  "
                                    f"({exc_label})",
                                    file=sys.stderr,
                                )
                        continue

            async def _drain_stderr():
                assert proc.stderr is not None
                data = await proc.stderr.read()
                text = data.decode(errors="replace")
                for line in text.splitlines():
                    stderr_tail.append(line)
                if len(stderr_tail) > 50:
                    del stderr_tail[:-50]

            # Wall-clock and token budgets are enforced inside the engine
            # via the soft-exit gates configured in --config (defaulting to
            # config.default.yaml). When any gate fires the engine soft-exits
            # cleanly, so we just await the subprocess to finish.
            await asyncio.gather(_stream_stdout(), _drain_stderr(), proc.wait())
            elapsed = time.monotonic() - start
            stats["api_retries"] = state["api_retries"]

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
                tail = "".join(stderr_tail)[-500:]
                error = f"exit code {proc.returncode}: {tail}"
            elif answer_code is None:
                error = "no valid ANSWER.md produced"

            return RunResult(
                problem_n=problem.n,
                problem_id=problem.problem_id,
                success=success,
                answer_code=answer_code,
                error=error,
                duration_s=elapsed,
                returncode=proc.returncode,
                workspace_dir=workspace_dir,
                stats=stats,
                soft_exit_reason=_read_soft_exit_reason(workspace_dir),
            )

        except Exception as exc:
            elapsed = time.monotonic() - start
            stats["api_retries"] = state["api_retries"]
            return RunResult(
                problem_n=problem.n,
                problem_id=problem.problem_id,
                success=False,
                answer_code=None,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=elapsed,
                workspace_dir=workspace_dir,
                stats=stats,
            )
        finally:
            _running.pop(problem.problem_id, None)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def run_batch(args: argparse.Namespace) -> int:
    """Main batch orchestrator. Returns exit code."""
    # Handle --resume: restore params from saved batch_metadata.json.
    # Engine-side params (max_iterations, max_wall_seconds, ...) are not
    # restored individually — they live in the saved config_file, which is
    # re-applied verbatim. If a legacy run was recorded without a
    # config_file, the engine falls back to config.default.yaml; we warn
    # so the user knows to re-supply --config if they want exact parity.
    legacy_resume_max_iterations: int | None = None
    if args.resume:
        if not args.resume.is_dir():
            print(f"Error: resume directory not found: {args.resume}", file=sys.stderr)
            return 1
        gen_cfg, run_cfg = load_resume_config(args.resume)
        args.output_dir = args.resume
        if args.model is None:
            args.model = gen_cfg.get("model_key")
        if args.config is None and gen_cfg.get("config_file"):
            args.config = Path(gen_cfg["config_file"])
        if run_cfg.get("problems_dir"):
            args.problems_dir = Path(run_cfg["problems_dir"])
        if run_cfg.get("problems_subset"):
            args.problems = run_cfg["problems_subset"]
        if run_cfg.get("workspace_base"):
            args.workspace_base = Path(run_cfg["workspace_base"])
        # Legacy compat: pre-refactor metadata recorded max_iterations on
        # gen_cfg without a config_file. Surface it so the user can act.
        if args.config is None and gen_cfg.get("max_iterations") is not None:
            legacy_resume_max_iterations = gen_cfg.get("max_iterations")
        print(f"Resuming from {args.resume}", file=sys.stderr)

    eng = resolve_engine_params(args.config)
    resolve_model(args, args.output_dir, config_model=eng.get("model"))

    critpt_model = resolve_critpt_model_string(args.model)

    problems = discover_problems(args.problems_dir, args.problems)
    if not problems:
        print("Error: no problems found", file=sys.stderr)
        return 1

    output_dir = make_output_dir(args, DEFAULT_RESULTS_BASE, create=not args.dry_run)

    # Plan actions (workspace-based resume logic)
    actions = plan_actions(
        problems,
        output_dir,
        args.workspace_base,
        args.model,
        args.force,
        fresh=args.fresh,
    )

    n_skip = sum(1 for a in actions if a.action == "skip")
    n_extract = sum(1 for a in actions if a.action == "extract")
    n_resume = sum(1 for a in actions if a.action == "resume")
    n_fresh = sum(1 for a in actions if a.action == "fresh")

    # Print plan
    config_label = str(args.config) if args.config else "(defaults only)"
    wall_s = int(eng.get("max_wall_seconds", 0) or 0)
    wall_str = f"{wall_s}s" if wall_s > 0 else "disabled"
    tok_budget = int(eng.get("max_total_output_tokens", 0) or 0)
    tok_str = f"{tok_budget:,}" if tok_budget > 0 else "disabled"
    cost_budget = float(eng.get("max_cost_usd", 0.0) or 0.0)
    cost_str = f"${cost_budget:.2f}" if cost_budget > 0 else "disabled"

    print(f"Model:           {args.model} ({critpt_model})", file=sys.stderr)
    print(f"Config:          {config_label}", file=sys.stderr)
    print(f"Max iterations:  {eng['max_iterations']}", file=sys.stderr)
    print(f"Max wall time:   {wall_str}", file=sys.stderr)
    print(f"Max out tokens:  {tok_str}", file=sys.stderr)
    print(f"Max cost:        {cost_str}", file=sys.stderr)
    print(f"Problems:        {len(problems)} total", file=sys.stderr)
    print(f"  skip:          {n_skip} (submission exists)", file=sys.stderr)
    print(f"  extract:       {n_extract} (answer exists, write JSON)", file=sys.stderr)
    print(f"  resume:        {n_resume} (continue interrupted run)", file=sys.stderr)
    print(f"  fresh:         {n_fresh} (new run)", file=sys.stderr)
    print(f"Concurrency:     {args.concurrency}", file=sys.stderr)
    print(f"Output:          {output_dir}", file=sys.stderr)
    if (
        legacy_resume_max_iterations is not None
        and legacy_resume_max_iterations != eng["max_iterations"]
    ):
        print(
            f"Warning: resuming a legacy run that recorded "
            f"max_iterations={legacy_resume_max_iterations} but no config_file. "
            f"Engine will use {eng['max_iterations']} (from defaults). "
            f"Re-supply --config to restore exact parity.",
            file=sys.stderr,
        )
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

    generation_config = {
        "system": "physics_intern",
        "model_key": args.model,
        "max_iterations": eng["max_iterations"],
        "max_wall_seconds": eng.get("max_wall_seconds", 0),
        "max_total_output_tokens": eng.get("max_total_output_tokens", 0),
        "max_cost_usd": eng.get("max_cost_usd", 0.0),
        "config_file": str(args.config) if args.config else None,
        "use_python": True,
        "use_web_search": False,
        "use_golden_for_prev_steps": False,
        "parsing": False,
        "multiturn_with_answer": False,
    }
    run_config = {
        "problems_dir": str(args.problems_dir),
        "problems_subset": args.problems,
        "workspace_base": str(args.workspace_base),
    }

    # Run
    semaphore = asyncio.Semaphore(args.concurrency)
    start_time = datetime.now(timezone.utc)
    write_initial_batch_metadata(
        output_dir,
        critpt_model,
        generation_config,
        run_config,
        start_time,
    )
    total = len(to_run)
    completed = 0
    succeeded = 0
    failed = 0
    all_results: list[RunResult] = []
    lock = asyncio.Lock()
    print_lock = asyncio.Lock()

    async def worker(action: ResumeAction) -> RunResult:
        nonlocal completed, succeeded, failed

        result = await run_one_problem(
            action,
            args.model,
            args.config,
            args.workspace_base,
            semaphore,
            print_lock,
        )

        if result.success:
            write_submission_json(result, output_dir, critpt_model, generation_config)

        async with lock:
            completed += 1
            if result.success:
                succeeded += 1
            else:
                failed += 1
            all_results.append(result)

            if result.success:
                if result.soft_exit_reason:
                    status = f"OK (soft-exit: {result.soft_exit_reason})"
                else:
                    status = "OK"
            else:
                status = f"FAIL: {result.error}"
            retries = (result.stats or {}).get("api_retries", 0) if result.stats else 0
            retry_note = f", {retries} API retries" if retries else ""
            if result.duration_s > 0:
                print(
                    f"[{completed}/{total}] C{result.problem_n} "
                    f"({action.action}, {result.duration_s:.0f}s) "
                    f"{status}{retry_note}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[{completed}/{total}] C{result.problem_n} "
                    f"({action.action}) {status}{retry_note}",
                    file=sys.stderr,
                )

        return result

    tasks = [asyncio.create_task(worker(a)) for a in to_run]
    watchdog = asyncio.create_task(_stall_watchdog(print_lock))

    loop = asyncio.get_running_loop()
    setup_signal_handler(loop, tasks)

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except (asyncio.CancelledError, Exception):
            pass

    for i, r in enumerate(results):
        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
            p = to_run[i].problem
            async with lock:
                all_results.append(
                    RunResult(
                        problem_n=p.n,
                        problem_id=p.problem_id,
                        success=False,
                        answer_code=None,
                        error=str(r),
                        duration_s=0,
                    )
                )
                failed += 1
                completed += 1

    end_time = datetime.now(timezone.utc)

    write_batch_metadata(
        output_dir,
        critpt_model,
        all_results,
        generation_config,
        run_config,
        start_time,
        end_time,
        include_sibling_history=not args.resume and not args.no_sibling_history,
    )
    print_final_summary(
        all_results,
        total,
        succeeded,
        failed,
        start_time,
        end_time,
        output_dir,
    )

    return 0 if failed == 0 else 1


def main():
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(run_batch(args)))


if __name__ == "__main__":
    main()
