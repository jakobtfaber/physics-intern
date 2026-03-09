"""Tests for workspace initialization."""

import inspect

from sciralph.config import Config
from sciralph.workspace import WorkspaceManager


def test_init_creates_research_state_without_warmups(tmp_path):
    """WorkspaceManager.init(problem) produces RESEARCH_STATE.md with no warm-up section."""
    config = Config(workspace_dir=str(tmp_path / "ws"))
    ws = WorkspaceManager(config)
    ws.init("Derive the result.")

    content = ws.read_file("RESEARCH_STATE.md")
    assert "# Problem Statement" in content
    assert "# Conventions" in content
    assert "# Established Results" in content
    assert "Warm-Up" not in content
    assert "warm_up" not in content
    assert "WU-" not in content


def test_init_signature_has_no_warm_ups_param():
    """init() should not accept a warm_ups parameter."""
    sig = inspect.signature(WorkspaceManager.init)
    assert "warm_ups" not in sig.parameters
