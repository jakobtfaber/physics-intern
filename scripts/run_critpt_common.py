"""Shared utilities for CritPt batch runner scripts.

Provides common infrastructure: problem discovery, resume logic,
submission JSON writing, batch metadata, and orchestration helpers.
"""

from __future__ import annotations

import json
import re
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML = PROJECT_ROOT / "src" / "open_dirac" / "models.yaml"
DEFAULT_PROBLEMS_DIR = PROJECT_ROOT / "problems" / "critpt" / "yaml"

sys.path.insert(0, str(PROJECT_ROOT / "src"))
from open_dirac.config import DEFAULTS  # noqa: E402


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
        model_id = entry.get("model_id", model_key)
        return f"{entry['provider']}/{model_id}"
    return model_key


def resolve_model(args, output_dir: Path | None = None) -> str:
    """Resolve model: explicit flag > previous run metadata > default.

    Mutates args.model in place and returns the resolved model key.
    """
    if args.model is None and output_dir and output_dir.exists():
        recovered = read_model_from_output_dir(output_dir)
        if recovered:
            args.model = recovered
            print(f"Resumed model from previous run: {recovered}", file=sys.stderr)
    if args.model is None:
        args.model = DEFAULTS["model"]
    return args.model


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
    workspace_dir: Path | None = None


# ---------------------------------------------------------------------------
# Resume logic
# ---------------------------------------------------------------------------

def read_model_from_output_dir(output_dir: Path) -> str | None:
    """Try to recover the model key from a previous run's output directory.

    Prioritizes submission JSONs over batch_metadata.json because the latter
    gets overwritten on every run (including failed resumes with wrong model).
    """
    for f in output_dir.glob("Challenge_*_main.json"):
        try:
            data = json.loads(f.read_text())
            model_key = data.get("generation_config", {}).get("model_key")
            if model_key:
                return model_key
        except (json.JSONDecodeError, KeyError):
            continue
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


def load_resume_config(resume_dir: Path) -> tuple[dict, dict]:
    """Load run parameters from batch_metadata.json for --resume.

    Returns (generation_config, run_config) dicts from the saved metadata.
    """
    meta_path = resume_dir / "batch_metadata.json"
    if not meta_path.exists():
        print(f"Error: no batch_metadata.json found in {resume_dir}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(meta_path.read_text())
    return data.get("generation_config", {}), data.get("run_config", {})


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
# Output directory
# ---------------------------------------------------------------------------

def make_output_dir(args, default_base: Path) -> Path:
    """Create and return the output directory for submission JSONs."""
    if args.output_dir:
        output_dir = args.output_dir
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_model = args.model.replace("/", "-").replace(":", "-")
        output_dir = default_base / safe_model / ts
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


# ---------------------------------------------------------------------------
# Raw response logging
# ---------------------------------------------------------------------------

def save_raw_response(
    logs_dir: Path | None,
    problem: Problem,
    stdout_text: str,
    stderr_text: str,
    success: bool,
) -> None:
    """Save raw stdout/stderr to logs/ for debugging."""
    if logs_dir is None:
        return
    prefix = "ok" if success else "FAIL"
    log_path = logs_dir / f"{prefix}_{problem.problem_id}.txt"
    try:
        with open(log_path, "w") as f:
            f.write(f"=== STDOUT ({len(stdout_text)} chars) ===\n")
            f.write(stdout_text)
            f.write(f"\n\n=== STDERR ({len(stderr_text)} chars) ===\n")
            f.write(stderr_text)
    except OSError:
        pass


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
    run_config: dict,
    start_time: datetime,
    end_time: datetime,
) -> None:
    """Write batch_metadata.json summarizing the run.

    Includes both current-run results and previously completed submissions
    found on disk, so metadata is accurate after resumed runs.  Cost/token
    stats and workspace paths are included when available.
    """
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

    summary: dict = {
        "total_submissions": len(all_submission_ids),
        "this_run_total": len(all_results),
        "this_run_success": n_run_success,
        "this_run_failed": n_run_failed,
        "total_compute_s": round(total_duration, 1),
        "wall_clock_s": round((end_time - start_time).total_seconds(), 1),
    }

    # Include cost/token stats when available
    total_cost = sum((r.stats or {}).get("cost_usd", 0.0) for r in all_results)
    if total_cost > 0:
        summary["total_cost_usd"] = round(total_cost, 4)
    total_input = sum((r.stats or {}).get("input_tokens", 0) for r in all_results)
    total_output = sum((r.stats or {}).get("output_tokens", 0) for r in all_results)
    if total_input > 0:
        summary["total_input_tokens"] = total_input
    if total_output > 0:
        summary["total_output_tokens"] = total_output

    # Build per-problem entries (include stats/workspace when present)
    problem_entries = []
    for r in sorted(all_results, key=lambda r: r.problem_n):
        entry: dict = {
            "problem_id": r.problem_id,
            "success": r.success,
            "duration_s": round(r.duration_s, 1),
            "error": r.error,
        }
        if r.stats is not None:
            entry["stats"] = r.stats
        if r.workspace_dir is not None:
            entry["workspace"] = str(r.workspace_dir)
        problem_entries.append(entry)

    metadata = {
        "model": critpt_model,
        "timestamp": end_time.isoformat(),
        "generation_config": generation_config,
        "run_config": run_config,
        "num_submissions": len(all_submission_ids),
        "problem_ids": all_submission_ids,
        "summary": summary,
        "problems": problem_entries,
    }
    (output_dir / "batch_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# Orchestration helpers
# ---------------------------------------------------------------------------

def setup_signal_handler(loop, tasks: list) -> None:
    """Set up Ctrl+C handler to cancel pending asyncio tasks."""
    cancelled = False

    def _handler():
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            print("\nInterrupted — cancelling pending tasks...", file=sys.stderr)
            for t in tasks:
                if not t.done():
                    t.cancel()

    loop.add_signal_handler(signal.SIGINT, _handler)


def print_final_summary(
    all_results: list[RunResult],
    total: int,
    succeeded: int,
    failed: int,
    start_time: datetime,
    end_time: datetime,
    output_dir: Path,
) -> None:
    """Print final batch summary to stderr."""
    wall_clock = (end_time - start_time).total_seconds()
    total_cost = sum((r.stats or {}).get("cost_usd", 0.0) for r in all_results)

    cost_str = f", ${total_cost:.2f} est. cost" if total_cost > 0 else ""
    print("---", file=sys.stderr)
    print(
        f"Done: {succeeded}/{total} succeeded, {failed} failed "
        f"({wall_clock:.0f}s wall clock{cost_str})",
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
