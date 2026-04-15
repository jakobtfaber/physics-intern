#!/usr/bin/env python3
"""Resolve vLLM serve config from models.yaml and emit shell variables.

Called by serve.slurm to avoid duplicating model-specific flags in bash.
Output is eval'd by the shell — all values are shlex-quoted for safety.
"""
import shlex
import sys
from pathlib import Path

import yaml


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} MODEL", file=sys.stderr)
        sys.exit(1)

    model = sys.argv[1]
    models_yaml = Path(__file__).resolve().parent.parent / "src" / "open_dirac" / "models.yaml"

    serve: dict = {}
    try:
        with open(models_yaml) as f:
            registry = yaml.safe_load(f)
        if registry and isinstance(registry, dict):
            entry = registry.get(model)
            if entry and isinstance(entry, dict):
                serve = entry.get("serve") or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"Warning: could not read {models_yaml}: {exc}", file=sys.stderr)

    gpus = str(serve.get("gpus_per_node", 1))
    parser = serve.get("reasoning_parser", "")
    vllm_args = serve.get("vllm_args", [])
    if not isinstance(vllm_args, list):
        print(f"Error: serve.vllm_args must be a list for {model}", file=sys.stderr)
        sys.exit(1)

    # Split each item on whitespace so "—flag value" becomes two tokens.
    tokens = []
    for arg in vllm_args:
        tokens.extend(shlex.split(str(arg)))

    print(f"DEFAULT_GPUS_PER_NODE={shlex.quote(gpus)}")
    print(f"DEFAULT_REASONING_PARSER={shlex.quote(parser)}")
    quoted = " ".join(shlex.quote(t) for t in tokens)
    print(f"DEFAULT_VLLM_ARGS=( {quoted} )")


if __name__ == "__main__":
    main()
