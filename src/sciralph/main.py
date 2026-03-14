"""SciRalph entry point."""

import argparse
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
    parser.add_argument("problem", type=Path, help="Path to problem YAML file")
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
    parser.add_argument("--critic-every-n", type=int, default=None,
                        help="Force critic pass every N iterations")
    parser.add_argument("--sympy-timeout-seconds", type=int, default=None,
                        help="Timeout for SymPy computations in seconds")
    parser.add_argument("--provider", type=str, default=None,
                        help="LLM provider (anthropic, openai, google, huggingface). "
                             "Auto-resolved from --model via models.yaml if omitted.")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Validate problem file
    if not args.problem.exists():
        print(f"Error: problem file not found: {args.problem}")
        sys.exit(1)

    with open(args.problem) as f:
        problem_def = yaml.safe_load(f)

    problem = problem_def.get("problem", "")

    # Build config (3-tier merge)
    config = build_config(args)

    # Generate timestamped workspace directory if not explicitly set
    if args.workspace_dir is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_model = config.model.replace("/", "-").replace(":", "-")
        run_name = f"{timestamp}_{args.problem.stem}_{safe_model}"
        config.workspace_dir = str(Path("workspaces") / run_name)

    # Build problem metadata for termination gates
    problem_meta = {
        "requires_numerical": problem_def.get("requires_numerical", False),
        "steps": problem_def.get("steps", []),
    }

    # Run
    engine = SciRalph(problem, config=config, problem_meta=problem_meta)
    engine.run()


if __name__ == "__main__":
    main()
