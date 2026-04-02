"""SciRalph entry point."""

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import yaml

from .config import Config, DEFAULTS, build_config
from .engine import SciRalph


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="sciralph",
        description="Multi-agent scaffolding for autonomous scientific research.",
    )
    parser.add_argument("problem", type=Path, nargs="?", default=None,
                        help="Path to problem YAML file")
    parser.add_argument("--resume", type=Path, default=None,
                        help="Path to workspace directory to resume")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to config YAML file")
    parser.add_argument("--model", type=str, default=None,
                        help="LLM model to use")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="Max output tokens per LLM call")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="Maximum iterations")
    parser.add_argument("--workspace-dir", type=str, default=None,
                        help="Workspace directory (default: auto-generated)")
    return parser


def _handle_dirty_workspace(workspace_path: Path) -> None:
    """Check for uncommitted changes and prompt user to clean them."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(workspace_path),
        capture_output=True, text=True, check=False,
    )
    dirty = result.stdout.strip()
    if not dirty:
        return

    print(f"Warning: workspace has uncommitted changes:\n{dirty}")
    answer = input("Reset to last committed state? [y/N] ").strip().lower()
    if answer == "y":
        subprocess.run(["git", "checkout", "."], cwd=str(workspace_path),
                        capture_output=True, check=False)
        subprocess.run(["git", "clean", "-fd"], cwd=str(workspace_path),
                        capture_output=True, check=False)
        print("Workspace cleaned.")
    else:
        print("Aborting resume — clean the workspace first.")
        sys.exit(1)


def _main_fresh(args) -> None:
    """Run a fresh research session."""
    if args.problem is None:
        print("Error: problem file is required for a fresh run (use --resume to resume)")
        sys.exit(1)

    if not args.problem.exists():
        print(f"Error: problem file not found: {args.problem}")
        sys.exit(1)

    with open(args.problem) as f:
        problem_def = yaml.safe_load(f)

    problem = problem_def.get("problem", "")
    answer_template = problem_def.get("answer_template", "")

    # Build config (3-tier merge)
    config = build_config(args)

    # Generate timestamped workspace directory if not explicitly set
    if args.workspace_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_model = config.model.replace("/", "-").replace(":", "-")
        run_name = f"{timestamp}_{args.problem.stem}_{safe_model}"
        config.workspace_dir = str(Path("workspaces") / run_name)

    # Build problem metadata for termination gates
    problem_meta = {
        "steps": problem_def.get("steps", []),
    }

    # Run
    engine = SciRalph(problem, config=config, problem_meta=problem_meta,
                       answer_template=answer_template)

    # Persist problem.yaml and config.json for future resume
    shutil.copy2(str(args.problem), str(engine.workspace.root / "problem.yaml"))
    config.save(engine.workspace.root)
    engine.workspace.git_commit("Persist problem.yaml and config.json for resume")

    engine.run()


def _main_resume(args) -> None:
    """Resume an interrupted research session."""
    workspace_path = args.resume.resolve()
    if not workspace_path.exists():
        print(f"Error: workspace not found: {workspace_path}")
        sys.exit(1)

    _handle_dirty_workspace(workspace_path)

    # Collect CLI overrides that should apply on resume
    overrides = {}
    for key in ("model", "max_tokens", "max_iterations"):
        value = getattr(args, key.replace("-", "_"), None)
        if value is not None:
            overrides[key] = value

    engine = SciRalph.resume(workspace_path, config_overrides=overrides or None)
    engine.run()


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.resume is not None:
        _main_resume(args)
    else:
        _main_fresh(args)


if __name__ == "__main__":
    main()
