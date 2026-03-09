"""Configuration for SciRalph."""

import warnings
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path
import os

import yaml


@dataclass
class Config:
    """SciRalph configuration."""
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 16384
    max_iterations: int = 200
    critic_every_n: int = 4
    compress_threshold: dict[str, int] = field(default_factory=lambda: {
        "RESEARCH_STATE.md": 50_000,
        "CRITIQUE_LOG.md": 30_000,
        "COMPUTATION_LOG.md": 40_000,
    })
    max_retries_on_max_tokens: int = 2
    sympy_timeout_seconds: int = 60
    workspace_dir: str = "workspaces"
    audit_log: str = ""
    logs_dir: str = ""
    api_key: str = ""

    def __post_init__(self):
        if not self.api_key:
            self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")


# Fields settable via config.yaml (workspace_dir, audit_log, logs_dir, api_key excluded)
_YAML_CONFIG_FIELDS = frozenset({
    "model", "max_tokens", "max_iterations", "critic_every_n",
    "compress_threshold", "max_retries_on_max_tokens", "sympy_timeout_seconds",
})


def load_config_yaml(path: Path) -> dict:
    """Load config from a YAML file, filtering to allowed fields."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        if key in _YAML_CONFIG_FIELDS:
            result[key] = value
        else:
            warnings.warn(f"Unknown config key ignored: {key}")
    return result


def build_config(args: Namespace) -> Config:
    """Build Config with 3-tier precedence: CLI args > config.yaml > defaults."""
    kwargs: dict = {}

    # Layer 1: YAML config (if provided)
    if getattr(args, "config", None) is not None:
        kwargs.update(load_config_yaml(Path(args.config)))

    # Layer 2: CLI args override (only non-None values)
    cli_fields = {
        "model", "max_tokens", "max_iterations", "critic_every_n",
        "sympy_timeout_seconds", "workspace_dir",
    }
    for field_name in cli_fields:
        cli_name = field_name.replace("-", "_")
        value = getattr(args, cli_name, None)
        if value is not None:
            kwargs[field_name] = value

    return Config(**kwargs)
