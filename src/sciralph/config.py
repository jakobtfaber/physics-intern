"""Configuration for SciRalph."""

from dataclasses import dataclass, field
import os


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
    api_key: str = ""

    def __post_init__(self):
        if not self.api_key:
            self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
