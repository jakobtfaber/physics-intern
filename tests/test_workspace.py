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
    assert "# Working Hypotheses (WH) and Established Results (ER)" in content
    assert "Warm-Up" not in content
    assert "warm_up" not in content
    assert "WU-" not in content


def test_init_signature_has_no_warm_ups_param():
    """init() should not accept a warm_ups parameter."""
    sig = inspect.signature(WorkspaceManager.init)
    assert "warm_ups" not in sig.parameters


class TestValidateCompReferences:
    """Tests for validate_comp_references (phantom reference stripping)."""

    def _make_workspace(self, tmp_path, research_state: str, comp_log: str):
        config = Config(workspace_dir=str(tmp_path / "ws"))
        ws = WorkspaceManager(config)
        ws.root.mkdir(parents=True, exist_ok=True)
        ws.write_file("RESEARCH_STATE.md", research_state)
        ws.write_file("COMPUTATION_LOG.md", comp_log)
        return ws

    def test_no_phantoms(self, tmp_path):
        """No phantom references -> empty list, file unchanged."""
        state = "Some text with COMP-001 reference."
        comp_log = "## COMP-001: Test\n**VERDICT:** VERIFIED\n"
        ws = self._make_workspace(tmp_path, state, comp_log)

        result = ws.validate_comp_references()
        assert result == []
        assert ws.read_file("RESEARCH_STATE.md") == state

    def test_strips_phantom_comp(self, tmp_path):
        """Phantom COMP-002 stripped, valid COMP-001 untouched."""
        state = "Result verified by COMP-001 and COMP-002."
        comp_log = "## COMP-001: Test\n**VERDICT:** VERIFIED\n"
        ws = self._make_workspace(tmp_path, state, comp_log)

        result = ws.validate_comp_references()
        assert result == ["COMP-002"]
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "COMP-001" in updated
        assert "[COMP-002:unverified]" in updated

    def test_strips_phantom_task(self, tmp_path):
        """Phantom TASK-005 stripped."""
        state = "See TASK-005 for details."
        comp_log = ""
        ws = self._make_workspace(tmp_path, state, comp_log)

        result = ws.validate_comp_references()
        assert result == ["TASK-005"]
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "[TASK-005:unverified]" in updated

    def test_multiple_phantoms_sorted(self, tmp_path):
        """Multiple phantoms returned sorted."""
        state = "COMP-003 and COMP-001 and TASK-002."
        comp_log = "## COMP-001: Test\n**VERDICT:** VERIFIED\n"
        ws = self._make_workspace(tmp_path, state, comp_log)

        result = ws.validate_comp_references()
        assert result == ["COMP-003", "TASK-002"]

    def test_no_references_at_all(self, tmp_path):
        """No COMP/TASK references in state -> empty list."""
        state = "Some plain text with no references."
        comp_log = "## COMP-001: Test\n**VERDICT:** VERIFIED\n"
        ws = self._make_workspace(tmp_path, state, comp_log)

        result = ws.validate_comp_references()
        assert result == []


class TestValidateCompRefsImprovements:
    """Tests for validate_comp_references improvements (Improvement 2D)."""

    def _make_workspace(self, tmp_path, research_state: str, comp_log: str):
        config = Config(workspace_dir=str(tmp_path / "ws"))
        ws = WorkspaceManager(config)
        ws.root.mkdir(parents=True, exist_ok=True)
        ws.write_file("RESEARCH_STATE.md", research_state)
        ws.write_file("COMPUTATION_LOG.md", comp_log)
        return ws

    def test_validate_comp_refs_idempotent(self, tmp_path):
        """Running validate_comp_references twice produces the same result."""
        state = "Result backed by COMP-999."
        ws = self._make_workspace(tmp_path, state, "")
        ws.validate_comp_references()
        first = ws.read_file("RESEARCH_STATE.md")
        ws.validate_comp_references()
        second = ws.read_file("RESEARCH_STATE.md")
        assert first == second

    def test_validate_comp_refs_task_with_comp(self, tmp_path):
        """TASK-005 accepted when COMP-005 exists."""
        comp_log = "## TASK-005\n\nPreamble.\n\n## COMP-005: Verification\n**CLAIM**: test\n**VERDICT**: VERIFIED\n"
        state = "See TASK-005 for the computation result."
        ws = self._make_workspace(tmp_path, state, comp_log)
        result = ws.validate_comp_references()
        assert "TASK-005" not in result
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "[TASK-005:unverified]" not in updated
        assert "TASK-005" in updated

    def test_validate_comp_refs_flattens_nested(self, tmp_path):
        """Nested bracket markers are flattened."""
        state = "Result: [[COMP-001:unverified]:unverified]."
        comp_log = "## COMP-001: Test\n**CLAIM**: test\n**VERDICT**: VERIFIED\n"
        ws = self._make_workspace(tmp_path, state, comp_log)
        ws.validate_comp_references()
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "[[" not in updated
