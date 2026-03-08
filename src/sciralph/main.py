"""SciRalph entry point."""

import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import yaml

from .config import Config
from .engine import SciRalph


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m sciralph.main <problem.yaml> [options]")
        print("  --model MODEL           LLM model to use")
        print("  --max-iterations N      Maximum iterations")
        print("  --workspace-dir DIR     Workspace directory")
        sys.exit(1)

    # Parse problem file
    problem_path = Path(sys.argv[1])
    if not problem_path.exists():
        print(f"Error: problem file not found: {problem_path}")
        sys.exit(1)

    with open(problem_path) as f:
        problem_def = yaml.safe_load(f)

    problem = problem_def.get("problem", "")

    # Generate timestamped workspace directory
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_name = f"{timestamp}_{problem_path.stem}"
    workspace_dir = str(Path("workspaces") / run_name)

    # Parse optional flags
    config = Config(workspace_dir=workspace_dir)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            config.model = args[i + 1]
            i += 2
        elif args[i] == "--max-iterations" and i + 1 < len(args):
            config.max_iterations = int(args[i + 1])
            i += 2
        elif args[i] == "--workspace-dir" and i + 1 < len(args):
            config.workspace_dir = args[i + 1]
            i += 2
        else:
            print(f"Unknown argument: {args[i]}")
            sys.exit(1)

    # Run
    engine = SciRalph(problem, config=config)
    engine.run()


if __name__ == "__main__":
    main()
