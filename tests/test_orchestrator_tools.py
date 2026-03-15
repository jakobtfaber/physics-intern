"""Tests for OrchestratorToolExecutor — state-mutation tools for the orchestrator."""

from unittest.mock import MagicMock

from sciralph.markdown import render_frontmatter
from sciralph.orchestrator_tools import (
    OrchestratorToolExecutor,
    _find_section_range,
    _find_h1_content_range,
    _next_hypothesis_num,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_STATE = """\
---
status: not_started
iteration: 3
---

# Problem Statement

Some problem.

# Conventions

(To be populated by the orchestrator as conventions become clear.)

# Working Hypotheses (WH) and Established Results (ER)

Claims use ## ER-NNN (established, verified) or ## WH-NNN (working hypothesis, pending).

## WH-001 — First hypothesis

Photon has spin-1.

## WH-002 — Second hypothesis

Entropy increases in isolated systems.

# Dead Ends

Nothing yet.

# Open Questions

What is dark energy?
"""

SAMPLE_CRITIQUE_LOG = """\
---
total_critiques: 1
unresolved_high: 1
unresolved_medium: 0
unresolved_low: 0
---

# Active Critiques

## CRIT-001 [HIGH] [UNRESOLVED]

**Target:** WH-001

Spin prediction may be wrong.

# Resolved Critiques
"""


def _make_workspace(files=None):
    ws = MagicMock()
    store = dict(files or {})
    ws.read_file = MagicMock(side_effect=lambda f: store.get(f, ""))
    ws.write_file = MagicMock(side_effect=lambda f, c: store.__setitem__(f, c))
    ws.delete_file = MagicMock(side_effect=lambda f: store.pop(f, None))
    ws.root = MagicMock()
    return ws, store


# ---------------------------------------------------------------------------
# Section range helpers
# ---------------------------------------------------------------------------

class TestFindSectionRange:
    def test_finds_wh(self):
        r = _find_section_range(SAMPLE_STATE, "WH-001")
        assert r is not None
        start, end = r
        assert "WH-001" in SAMPLE_STATE[start:end]
        assert "spin-1" in SAMPLE_STATE[start:end]

    def test_finds_wh_002(self):
        r = _find_section_range(SAMPLE_STATE, "WH-002")
        assert r is not None
        assert "Entropy" in SAMPLE_STATE[r[0]:r[1]]

    def test_missing_returns_none(self):
        assert _find_section_range(SAMPLE_STATE, "WH-099") is None


class TestFindH1ContentRange:
    def test_finds_conventions(self):
        r = _find_h1_content_range(SAMPLE_STATE, "Conventions")
        assert r is not None
        content = SAMPLE_STATE[r[0]:r[1]]
        assert "populated" in content

    def test_finds_dead_ends(self):
        r = _find_h1_content_range(SAMPLE_STATE, "Dead Ends")
        assert r is not None
        content = SAMPLE_STATE[r[0]:r[1]]
        assert "Nothing yet" in content

    def test_missing_returns_none(self):
        assert _find_h1_content_range(SAMPLE_STATE, "Nonexistent") is None


class TestNextHypothesisNum:
    def test_after_two(self):
        assert _next_hypothesis_num(SAMPLE_STATE) == 3

    def test_empty(self):
        assert _next_hypothesis_num("no hypotheses here") == 1


# ---------------------------------------------------------------------------
# Tool executor: add_hypothesis
# ---------------------------------------------------------------------------

class TestAddHypothesis:
    def test_adds_wh(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("add_hypothesis", {
            "statement": "Third hypothesis",
            "derivation": "Some derivation.",
        })
        assert not tc.is_error
        assert "WH-003" in tc.output
        updated = store["RESEARCH_STATE.md"]
        assert "## WH-003 — Third hypothesis" in updated
        assert "Some derivation." in updated
        assert ex.mutations_applied

    def test_inserted_before_dead_ends(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        ex.execute("add_hypothesis", {"statement": "New"})
        updated = store["RESEARCH_STATE.md"]
        wh3_pos = updated.find("WH-003")
        dead_pos = updated.find("# Dead Ends")
        assert wh3_pos < dead_pos


# ---------------------------------------------------------------------------
# Tool executor: update_hypothesis
# ---------------------------------------------------------------------------

class TestUpdateHypothesis:
    def test_updates_derivation(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("update_hypothesis", {
            "id": "WH-001",
            "derivation": "Updated: photon has spin-2.",
        })
        assert not tc.is_error
        updated = store["RESEARCH_STATE.md"]
        assert "spin-2" in updated
        assert "spin-1" not in updated

    def test_updates_statement(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        ex.execute("update_hypothesis", {
            "id": "WH-001",
            "statement": "Updated title",
        })
        updated = store["RESEARCH_STATE.md"]
        assert "## WH-001 — Updated title" in updated

    def test_missing_id_returns_error(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("update_hypothesis", {"id": "WH-099"})
        assert "not found" in tc.output

    def test_preserves_other_sections(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        ex.execute("update_hypothesis", {
            "id": "WH-001",
            "derivation": "Changed.",
        })
        updated = store["RESEARCH_STATE.md"]
        assert "## WH-002" in updated
        assert "Entropy increases" in updated


# ---------------------------------------------------------------------------
# Tool executor: abandon_hypothesis
# ---------------------------------------------------------------------------

class TestAbandonHypothesis:
    def test_moves_to_dead_ends(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("abandon_hypothesis", {
            "id": "WH-001",
            "reason": "Spin is actually 0.",
        })
        assert not tc.is_error
        updated = store["RESEARCH_STATE.md"]
        # Should be gone from WH/ER section
        assert "## WH-001" not in updated
        # Should appear in Dead Ends
        assert "WH-001" in updated
        assert "Spin is actually 0" in updated
        assert "# Dead Ends" in updated

    def test_missing_id(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("abandon_hypothesis", {"id": "WH-099", "reason": "nope"})
        assert "not found" in tc.output


# ---------------------------------------------------------------------------
# Tool executor: resolve_critique
# ---------------------------------------------------------------------------

class TestResolveCritique:
    def test_resolves_critique(self):
        ws, store = _make_workspace({
            "RESEARCH_STATE.md": SAMPLE_STATE,
            "CRITIQUE_LOG.md": SAMPLE_CRITIQUE_LOG,
        })
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("resolve_critique", {
            "critique_id": "CRIT-001",
            "resolution": "Fixed spin prediction to spin-0.",
        })
        assert not tc.is_error
        assert "CRIT-001" in ex.resolved_critique_ids
        updated = store["CRITIQUE_LOG.md"]
        assert "RESOLVED" in updated
        assert "Fixed spin prediction" in updated

    def test_sets_iteration_resolved_in_research_state(self):
        from sciralph.research_state import (
            ResearchState, Critique, Severity, CritiqueStatus,
        )
        rs = ResearchState()
        rs.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.HIGH, status=CritiqueStatus.ACTIVE,
        )
        ws, store = _make_workspace({
            "RESEARCH_STATE.md": SAMPLE_STATE,
            "CRITIQUE_LOG.md": SAMPLE_CRITIQUE_LOG,
        })
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=rs)
        ex.execute("resolve_critique", {
            "critique_id": "CRIT-001",
            "resolution": "Fixed.",
        })
        assert rs.critiques["CRIT-001"].iteration_resolved == 5
        assert rs.critiques["CRIT-001"].status == CritiqueStatus.RESOLVED
        assert rs.critiques["CRIT-001"].resolution == "Fixed."

    def test_no_research_state_still_resolves(self):
        ws, store = _make_workspace({
            "RESEARCH_STATE.md": SAMPLE_STATE,
            "CRITIQUE_LOG.md": SAMPLE_CRITIQUE_LOG,
        })
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=None)
        tc = ex.execute("resolve_critique", {
            "critique_id": "CRIT-001",
            "resolution": "Fixed.",
        })
        assert not tc.is_error


# ---------------------------------------------------------------------------
# Tool executor: update_section
# ---------------------------------------------------------------------------

class TestUpdateSection:
    def test_updates_conventions(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("update_section", {
            "section": "Conventions",
            "content": "- Natural units: ℏ = c = k_B = 1\n- Metric signature: (−,+,+,+)",
        })
        assert not tc.is_error
        updated = store["RESEARCH_STATE.md"]
        assert "Natural units" in updated
        assert "populated" not in updated  # old content replaced

    def test_updates_open_questions(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        ex.execute("update_section", {
            "section": "Open Questions",
            "content": "- Is string theory testable?",
        })
        updated = store["RESEARCH_STATE.md"]
        assert "string theory" in updated

    def test_missing_section(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("update_section", {
            "section": "Nonexistent",
            "content": "x",
        })
        assert "not found" in tc.output


# ---------------------------------------------------------------------------
# Tool executor: set_next_task
# ---------------------------------------------------------------------------

class TestSetNextTask:
    def test_stores_task_data(self):
        ws, store = _make_workspace({})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("set_next_task", {
            "task_type": "compute",
            "assigned_to": "computationalist",
            "priority": "high",
            "target_claim": "WH-001",
            "description": "Verify WH-001 numerically.",
        })
        assert not tc.is_error
        assert ex.task_data is not None
        assert ex.task_data["task_type"] == "compute"
        assert ex.task_data["target_claim"] == "WH-001"

    def test_sets_stop_after_round(self):
        ws, store = _make_workspace({})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        assert not ex.stop_after_round
        ex.execute("set_next_task", {
            "task_type": "terminate",
            "description": "Done.",
        })
        assert ex.stop_after_round


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------

class TestUnknownTool:
    def test_unknown_tool_returns_error(self):
        ws, store = _make_workspace({})
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("nonexistent_tool", {})
        assert tc.is_error
        assert "Unknown tool" in tc.output


# ---------------------------------------------------------------------------
# Integration: multiple tools in sequence
# ---------------------------------------------------------------------------

class TestAbandonRecordsFailure:
    """Phase 4: abandon_hypothesis records in research_state.failed_approaches."""

    def test_records_failed_approach(self):
        from sciralph.research_state import ResearchState
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        rs = ResearchState()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=rs)
        ex.execute("abandon_hypothesis", {
            "id": "WH-001",
            "reason": "Spin prediction was wrong.",
        })
        assert len(rs.failed_approaches) == 1
        assert "WH-001" in rs.failed_approaches[0].description
        assert "Spin prediction" in rs.failed_approaches[0].reason

    def test_marks_hypothesis_abandoned_in_state(self):
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus
        rs = ResearchState()
        rs.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="First", status=HypothesisStatus.WORKING,
        )
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=rs)
        ex.execute("abandon_hypothesis", {"id": "WH-001", "reason": "Wrong."})
        assert rs.hypotheses["WH-001"].status == HypothesisStatus.ABANDONED
        assert rs.hypotheses["WH-001"].iteration_modified == 3

    def test_no_research_state_still_works(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=None)
        tc = ex.execute("abandon_hypothesis", {
            "id": "WH-001",
            "reason": "Bad.",
        })
        assert not tc.is_error


class TestMultipleTools:
    def test_add_then_update(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)

        ex.execute("add_hypothesis", {
            "statement": "Third hypothesis",
            "derivation": "Initial derivation.",
        })
        ex.execute("update_hypothesis", {
            "id": "WH-003",
            "derivation": "Corrected derivation.",
        })

        updated = store["RESEARCH_STATE.md"]
        assert "## WH-003 — Third hypothesis" in updated
        assert "Corrected derivation" in updated
        assert "Initial derivation" not in updated

    def test_update_and_set_task(self):
        ws, store = _make_workspace({"RESEARCH_STATE.md": SAMPLE_STATE})
        ex = OrchestratorToolExecutor(ws, iteration=3)

        ex.execute("update_hypothesis", {
            "id": "WH-001",
            "derivation": "Photon has spin-0.",
        })
        ex.execute("set_next_task", {
            "task_type": "compute",
            "assigned_to": "computationalist",
            "target_claim": "WH-001",
            "description": "Verify spin-0 prediction.",
        })

        assert ex.mutations_applied
        assert ex.task_data["task_type"] == "compute"
        assert "spin-0" in store["RESEARCH_STATE.md"]
