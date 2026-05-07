#!/usr/bin/env python3
"""Resolve vLLM serve config from models.yaml and emit shell variables.

Called by serve.slurm to avoid duplicating model-specific flags in bash.
Output is eval'd by the shell — all values are shlex-quoted for safety.
"""

import shlex
import sys
from pathlib import Path
from typing import Any

import yaml


def optional_str(mapping: dict[str, Any], *keys: str) -> str:
    """Return the first present config value as a shell-friendly string."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return str(mapping[key])
    return ""


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} MODEL", file=sys.stderr)
        sys.exit(1)

    model = sys.argv[1]
    models_yaml = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "physics_intern"
        / "models.yaml"
    )

    serve: dict[str, Any] = {}
    model_id: str = model
    try:
        with open(models_yaml) as f:
            registry = yaml.safe_load(f)
        if registry and isinstance(registry, dict):
            entry = registry[model] if model in registry else None
            if entry and isinstance(entry, dict):
                serve_value = entry["serve"] if "serve" in entry else {}
                serve = serve_value if isinstance(serve_value, dict) else {}
                # Allow an alias key (e.g. zai-org/GLM-5.1-runai) to resolve
                # to a different upstream HF repo via `model_id`.
                model_id = str(entry["model_id"]) if "model_id" in entry else model
    except (OSError, yaml.YAMLError) as exc:
        print(f"Warning: could not read {models_yaml}: {exc}", file=sys.stderr)

    replicas = str(serve["replicas"]) if "replicas" in serve else "1"
    nodes_per_replica = optional_str(serve, "nodes_per_replica")
    nodes = optional_str(serve, "nodes") or nodes_per_replica
    gpus = optional_str(serve, "gpus_per_node") or "1"
    tp = optional_str(serve, "tp", "tensor_parallel_size")
    pp = optional_str(serve, "pp", "pipeline_parallel_size")
    dp = optional_str(serve, "dp", "data_parallel_size")
    parser = optional_str(serve, "reasoning_parser")
    vllm_args = serve["vllm_args"] if "vllm_args" in serve else []
    if not isinstance(vllm_args, list):
        print(f"Error: serve.vllm_args must be a list for {model}", file=sys.stderr)
        sys.exit(1)

    # Each YAML list item becomes one shell token. Items containing
    # whitespace (legacy "—flag value" style) are split with shlex;
    # single-token items are passed through verbatim to preserve JSON
    # values like '{"thinking":true}'.
    tokens = []
    for arg in vllm_args:
        s = str(arg)
        parts = shlex.split(s)
        if len(parts) <= 1:
            tokens.append(s)
        else:
            tokens.extend(parts)

    print(f"DEFAULT_MODEL_ID={shlex.quote(model_id)}")
    print(f"DEFAULT_REPLICAS={shlex.quote(str(replicas))}")
    print(f"DEFAULT_NODES={shlex.quote(nodes)}")
    print(f"DEFAULT_GPUS_PER_NODE={shlex.quote(gpus)}")
    print(f"DEFAULT_TP={shlex.quote(tp)}")
    print(f"DEFAULT_PP={shlex.quote(pp)}")
    print(f"DEFAULT_DP={shlex.quote(dp)}")
    print(f"DEFAULT_REASONING_PARSER={shlex.quote(parser)}")
    quoted = " ".join(shlex.quote(t) for t in tokens)
    print(f"DEFAULT_VLLM_ARGS=( {quoted} )")


if __name__ == "__main__":
    main()
