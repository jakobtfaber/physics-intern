"""Tests for the formal ResearchState module."""

import json
from unittest.mock import MagicMock

import pytest

from sciralph.research_state import (
    ResearchState,
    Hypothesis,
    HypothesisStatus,
    Computation,
    Verdict,
    Critique,
    Severity,
    CritiqueStatus,
    FailedApproach,
    build_from_workspace,
    _extract_hypothesis_sections,
    _extract_h1_section,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_RESEARCH_STATE = """\
---
problem_id: research-session
title: Test problem
status: not_started
iteration: 5
---

# Problem Statement

Some problem.

# Working Hypotheses (WH) and Established Results (ER)

## WH-001 — First hypothesis

The photon has spin-1.

Some derivation details here.

## ER-002 — Verified result

Energy is conserved.

This has been verified by COMP-001.

## WH-003 — Third hypothesis

Entropy increases.

# Dead Ends

Nothing yet.
"""

SAMPLE_COMPUTATION_LOG = """\
---
total_computations: 2
---

# Computations

## COMP-001: Verification of ER-002

**CLAIM:** Verify that energy is conserved per ER-002

**VERDICT:** VERIFIED

**METHOD:**
Numerical simulation of energy.

**RESULT:**
Energy conserved to 1e-12.

**NOTES:**
All good.

## COMP-002: Test of WH-001

**CLAIM:** Check spin-1 prediction per WH-001

**VERDICT:** REFUTED

**METHOD:**
Computed spin eigenvalue.

**RESULT:**
Got spin-0 instead.

**NOTES:**
The model predicts spin-0 not spin-1.
"""

SAMPLE_CRITIQUE_LOG = """\
---
total_critiques: 3
unresolved_high: 1
unresolved_medium: 0
unresolved_low: 0
---

# Active Critiques

## CRIT-001 [HIGH] [UNRESOLVED]

**Target:** WH-001

### Phase 1: Reproduce
Spin prediction is wrong.

### Phase 2: Objection
WH-001 predicts spin-1 but experiment shows spin-0.

## CRIT-002 [MEDIUM] [WITHDRAWN]

**Target:** WH-003

Entropy claim is trivial.

# Resolved Critiques

## CRIT-003 [LOW] [RESOLVED]

**Target:** ER-002

Energy conservation is obvious.
- **Resolution:** Acknowledged as a valid simplification.
"""


# ---------------------------------------------------------------------------
# Hypothesis section extraction
# ---------------------------------------------------------------------------

class TestExtractHypothesisSections:

    def test_extracts_wh_and_er(self):
        sections = _extract_hypothesis_sections(SAMPLE_RESEARCH_STATE)
        ids = [s[0] for s in sections]
        assert ids == ["WH-001", "ER-002", "WH-003"]

    def test_title_extracted(self):
        sections = _extract_hypothesis_sections(SAMPLE_RESEARCH_STATE)
        assert sections[0][1] == "First hypothesis"
        assert sections[1][1] == "Verified result"

    def test_body_contains_content(self):
        sections = _extract_hypothesis_sections(SAMPLE_RESEARCH_STATE)
        assert "photon has spin-1" in sections[0][2]

    def test_empty_body(self):
        sections = _extract_hypothesis_sections("No sections here.")
        assert sections == []

    def test_section_body_stops_at_h1(self):
        sections = _extract_hypothesis_sections(SAMPLE_RESEARCH_STATE)
        # WH-003's body should not contain "# Dead Ends"
        assert "Dead Ends" not in sections[2][2]


# ---------------------------------------------------------------------------
# build_from_workspace
# ---------------------------------------------------------------------------

class TestBuildFromWorkspace:

    def _make_workspace(self, files: dict[str, str]):
        ws = MagicMock()
        ws.read_file = MagicMock(side_effect=lambda f: files.get(f, ""))
        return ws

    def test_parses_hypotheses(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert "WH-001" in state.hypotheses
        assert "ER-002" in state.hypotheses
        assert "WH-003" in state.hypotheses
        assert state.hypotheses["WH-001"].status == HypothesisStatus.WORKING
        assert state.hypotheses["ER-002"].status == HypothesisStatus.ESTABLISHED

    def test_parses_computations(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert "COMP-001" in state.computations
        assert "COMP-002" in state.computations
        assert state.computations["COMP-001"].verdict == Verdict.VERIFIED
        assert state.computations["COMP-002"].verdict == Verdict.REFUTED
        assert state.computations["COMP-001"].target_hypothesis == "ER-002"
        assert state.computations["COMP-002"].target_hypothesis == "WH-001"

    def test_links_comps_to_hypotheses(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert "COMP-001" in state.hypotheses["ER-002"].supporting_comps
        assert "COMP-002" in state.hypotheses["WH-001"].supporting_comps

    def test_parses_critiques(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": SAMPLE_CRITIQUE_LOG,
        })
        state = build_from_workspace(ws)
        assert "CRIT-001" in state.critiques
        assert "CRIT-002" in state.critiques
        assert "CRIT-003" in state.critiques
        assert state.critiques["CRIT-001"].severity == Severity.HIGH
        assert state.critiques["CRIT-001"].status == CritiqueStatus.ACTIVE
        assert state.critiques["CRIT-002"].status == CritiqueStatus.WITHDRAWN
        assert state.critiques["CRIT-003"].status == CritiqueStatus.RESOLVED
        assert "simplification" in state.critiques["CRIT-003"].resolution

    def test_links_critiques_to_hypotheses(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": SAMPLE_CRITIQUE_LOG,
        })
        state = build_from_workspace(ws)
        assert "CRIT-001" in state.hypotheses["WH-001"].critiques
        assert "CRIT-003" in state.hypotheses["ER-002"].critiques

    def test_iteration_from_frontmatter(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert state.iteration == 5

    def test_empty_workspace(self):
        ws = self._make_workspace({})
        state = build_from_workspace(ws)
        assert state.iteration == 0
        assert state.hypotheses == {}
        assert state.computations == {}
        assert state.critiques == {}

    def test_full_integration(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": SAMPLE_CRITIQUE_LOG,
        })
        state = build_from_workspace(ws)
        assert len(state.hypotheses) == 3
        assert len(state.computations) == 2
        assert len(state.critiques) == 3


# ---------------------------------------------------------------------------
# Query methods
# ---------------------------------------------------------------------------

class TestQueryMethods:

    def _make_state(self) -> ResearchState:
        state = ResearchState(iteration=5)
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001", statement="Test",
                                                  status=HypothesisStatus.WORKING,
                                                  supporting_comps=["COMP-001"],
                                                  critiques=["CRIT-001"])
        state.hypotheses["ER-002"] = Hypothesis(id="ER-002", statement="Verified",
                                                  status=HypothesisStatus.ESTABLISHED,
                                                  supporting_comps=["COMP-002"])
        state.computations["COMP-001"] = Computation(id="COMP-001", target_hypothesis="WH-001",
                                                       verdict=Verdict.REFUTED)
        state.computations["COMP-002"] = Computation(id="COMP-002", target_hypothesis="ER-002",
                                                       verdict=Verdict.VERIFIED)
        state.critiques["CRIT-001"] = Critique(id="CRIT-001", targets=["WH-001"],
                                                severity=Severity.HIGH,
                                                status=CritiqueStatus.ACTIVE)
        return state

    def test_verified_comps_for(self):
        state = self._make_state()
        assert len(state.verified_comps_for("ER-002")) == 1
        assert len(state.verified_comps_for("WH-001")) == 0

    def test_has_verified_backing(self):
        state = self._make_state()
        assert state.has_verified_backing("ER-002")
        assert not state.has_verified_backing("WH-001")

    def test_active_critiques_for(self):
        state = self._make_state()
        assert len(state.active_critiques_for("WH-001")) == 1
        assert len(state.active_critiques_for("ER-002")) == 0

    def test_unresolved_high_critiques(self):
        state = self._make_state()
        assert len(state.unresolved_high_critiques()) == 1

    def test_comps_for_hypothesis(self):
        state = self._make_state()
        assert len(state.comps_for_hypothesis("WH-001")) == 1

    def test_established_hypotheses(self):
        state = self._make_state()
        assert len(state.established_hypotheses()) == 1
        assert state.established_hypotheses()[0].id == "ER-002"

    def test_working_hypotheses(self):
        state = self._make_state()
        assert len(state.working_hypotheses()) == 1
        assert state.working_hypotheses()[0].id == "WH-001"


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

class TestSerialization:

    def test_json_round_trip(self):
        state = ResearchState(iteration=3)
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001", statement="Test",
                                                  status=HypothesisStatus.WORKING)
        state.computations["COMP-001"] = Computation(id="COMP-001",
                                                       target_hypothesis="WH-001",
                                                       verdict=Verdict.VERIFIED)
        state.critiques["CRIT-001"] = Critique(id="CRIT-001", targets=["WH-001"],
                                                severity=Severity.HIGH)
        state.failed_approaches.append(FailedApproach(
            description="Tried perturbation theory",
            reason="Divergent series",
            related_comps=["COMP-001"],
            iteration=2,
        ))

        json_str = state.to_json()
        restored = ResearchState.from_json(json_str)

        assert restored.iteration == 3
        assert "WH-001" in restored.hypotheses
        assert restored.hypotheses["WH-001"].statement == "Test"
        assert restored.computations["COMP-001"].verdict == Verdict.VERIFIED
        assert restored.critiques["CRIT-001"].severity == Severity.HIGH
        assert len(restored.failed_approaches) == 1
        assert restored.failed_approaches[0].description == "Tried perturbation theory"

    def test_save_and_load(self, tmp_path):
        state = ResearchState(iteration=7)
        state.hypotheses["ER-001"] = Hypothesis(id="ER-001", statement="E=mc²",
                                                  status=HypothesisStatus.ESTABLISHED)
        state.save(tmp_path)

        loaded = ResearchState.load(tmp_path)
        assert loaded.iteration == 7
        assert "ER-001" in loaded.hypotheses
        assert loaded.hypotheses["ER-001"].status == HypothesisStatus.ESTABLISHED

    def test_load_missing_file(self, tmp_path):
        loaded = ResearchState.load(tmp_path)
        assert loaded.iteration == 0
        assert loaded.hypotheses == {}

    def test_json_is_valid(self):
        state = ResearchState(iteration=1)
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        parsed = json.loads(state.to_json())
        assert parsed["iteration"] == 1
        assert "WH-001" in parsed["hypotheses"]


# ---------------------------------------------------------------------------
# Phase 4: Failure tracking
# ---------------------------------------------------------------------------

class TestFailureTracking:

    def test_failures_for_hypothesis(self):
        state = ResearchState()
        state.failed_approaches.append(FailedApproach(
            description="REFUTED on: WH-001 spin prediction",
            reason="Got spin-0 instead of spin-1",
            related_comps=["COMP-002"],
            iteration=3,
        ))
        state.failed_approaches.append(FailedApproach(
            description="INCONCLUSIVE on: WH-002 entropy",
            reason="Timeout",
            related_comps=["COMP-005"],
            iteration=4,
        ))
        wh1_failures = state.failures_for_hypothesis("WH-001")
        assert len(wh1_failures) == 1
        assert "spin prediction" in wh1_failures[0].description

    def test_failures_round_trip(self):
        state = ResearchState(iteration=5)
        state.failed_approaches.append(FailedApproach(
            description="REFUTED on: WH-001",
            reason="Wrong coefficient",
            related_comps=["COMP-003"],
            iteration=4,
        ))
        restored = ResearchState.from_json(state.to_json())
        assert len(restored.failed_approaches) == 1
        assert restored.failed_approaches[0].reason == "Wrong coefficient"
        assert restored.failed_approaches[0].related_comps == ["COMP-003"]

    def test_no_failures_returns_empty(self):
        state = ResearchState()
        assert state.failures_for_hypothesis("WH-001") == []

    def test_abandoned_hypotheses_query(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.ABANDONED,
        )
        state.hypotheses["WH-002"] = Hypothesis(
            id="WH-002", status=HypothesisStatus.WORKING,
        )
        state.hypotheses["ER-003"] = Hypothesis(
            id="ER-003", status=HypothesisStatus.ESTABLISHED,
        )
        assert len(state.abandoned_hypotheses()) == 1
        assert state.abandoned_hypotheses()[0].id == "WH-001"

    def test_abandoned_status_round_trip(self):
        state = ResearchState(iteration=5)
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Bad idea",
            status=HypothesisStatus.ABANDONED, iteration_modified=3,
        )
        restored = ResearchState.from_json(state.to_json())
        assert restored.hypotheses["WH-001"].status == HypothesisStatus.ABANDONED
        assert restored.hypotheses["WH-001"].iteration_modified == 3


# ---------------------------------------------------------------------------
# Fix: phantom TASK-* stubs filtered from graph
# ---------------------------------------------------------------------------

class TestPhantomTaskStubFiltering:

    def test_build_from_workspace_skips_task_entries(self):
        """TASK-* entries in COMPUTATION_LOG should not appear in state.computations."""
        comp_log = """\
---
total_computations: 2
---

# Computations

## TASK-001: Computation

**CLAIM:** unknown
**VERDICT:** INCONCLUSIVE
**NOTES:** Agent produced no text output.

## COMP-001: Verification of WH-001

**CLAIM:** Verify WH-001 result
**VERDICT:** VERIFIED
**RESULT:**
OK.
"""
        ws = MagicMock()
        ws.read_file = MagicMock(side_effect=lambda f: {
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": comp_log,
            "CRITIQUE_LOG.md": "",
        }.get(f, ""))
        state = build_from_workspace(ws)
        assert "COMP-001" in state.computations
        assert "TASK-001" not in state.computations
        assert len(state.computations) == 1


# ---------------------------------------------------------------------------
# Fix: normalize_references (stale WH↔ER backlinks)
# ---------------------------------------------------------------------------

class TestNormalizeReferences:

    def test_updates_stale_wh_to_er(self):
        """COMP targeting WH-002 should be updated when hypothesis is ER-002."""
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(id="ER-002", status=HypothesisStatus.ESTABLISHED)
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-002", verdict=Verdict.VERIFIED,
        )
        state.normalize_references()
        assert state.computations["COMP-001"].target_hypothesis == "ER-002"
        assert "COMP-001" in state.hypotheses["ER-002"].supporting_comps

    def test_updates_stale_er_to_wh(self):
        """COMP targeting ER-001 should be updated when hypothesis was demoted to WH-001."""
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001", status=HypothesisStatus.WORKING)
        state.computations["COMP-003"] = Computation(
            id="COMP-003", target_hypothesis="ER-001", verdict=Verdict.REFUTED,
        )
        state.normalize_references()
        assert state.computations["COMP-003"].target_hypothesis == "WH-001"
        assert "COMP-003" in state.hypotheses["WH-001"].supporting_comps

    def test_rebuilds_supporting_comps(self):
        """After normalization, supporting_comps contains all matching COMPs."""
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(
            id="ER-002", status=HypothesisStatus.ESTABLISHED,
            supporting_comps=["COMP-999"],  # stale entry
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-002", verdict=Verdict.VERIFIED,
        )
        state.computations["COMP-005"] = Computation(
            id="COMP-005", target_hypothesis="ER-002", verdict=Verdict.VERIFIED,
        )
        state.normalize_references()
        comps = state.hypotheses["ER-002"].supporting_comps
        assert "COMP-001" in comps
        assert "COMP-005" in comps
        assert "COMP-999" not in comps  # stale entry removed

    def test_idempotent(self):
        """Calling normalize_references twice produces the same result."""
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(id="ER-002", status=HypothesisStatus.ESTABLISHED)
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-002", verdict=Verdict.VERIFIED,
        )
        state.normalize_references()
        target_after_first = state.computations["COMP-001"].target_hypothesis
        comps_after_first = list(state.hypotheses["ER-002"].supporting_comps)
        state.normalize_references()
        assert state.computations["COMP-001"].target_hypothesis == target_after_first
        assert state.hypotheses["ER-002"].supporting_comps == comps_after_first

    def test_empty_target_ignored(self):
        """COMPs with empty target_hypothesis are not modified."""
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="", verdict=Verdict.INCONCLUSIVE,
        )
        state.normalize_references()
        assert state.computations["COMP-001"].target_hypothesis == ""
        assert state.hypotheses["WH-001"].supporting_comps == []

    def test_no_alias_match_preserves_target(self):
        """COMP targeting a number that doesn't exist in hypotheses is left alone."""
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-099", verdict=Verdict.VERIFIED,
        )
        state.normalize_references()
        assert state.computations["COMP-001"].target_hypothesis == "WH-099"


# ---------------------------------------------------------------------------
# New ResearchState fields
# ---------------------------------------------------------------------------

class TestNewResearchStateFields:

    def test_defaults(self):
        state = ResearchState()
        assert state.problem_statement == ""
        assert state.conventions == ""
        assert state.open_questions == ""
        assert state.status == "in_progress"
        assert state.title == ""

    def test_json_round_trip(self):
        state = ResearchState(
            iteration=3,
            problem_statement="Derive Hawking temperature.",
            conventions="Natural units: hbar = c = k_B = 1.",
            open_questions="What about grey-body factors?",
            status="complete",
            title="Hawking Temperature",
        )
        restored = ResearchState.from_json(state.to_json())
        assert restored.problem_statement == "Derive Hawking temperature."
        assert restored.conventions == "Natural units: hbar = c = k_B = 1."
        assert restored.open_questions == "What about grey-body factors?"
        assert restored.status == "complete"
        assert restored.title == "Hawking Temperature"

    def test_backward_compat_missing_fields(self):
        """Loading old JSON without new fields should use defaults."""
        old_json = json.dumps({
            "iteration": 5,
            "hypotheses": {},
            "computations": {},
            "critiques": {},
            "failed_approaches": [],
        })
        state = ResearchState.from_json(old_json)
        assert state.iteration == 5
        assert state.problem_statement == ""
        assert state.conventions == ""
        assert state.open_questions == ""
        assert state.status == "in_progress"
        assert state.title == ""


# ---------------------------------------------------------------------------
# New Computation fields
# ---------------------------------------------------------------------------

class TestNewComputationFields:

    def test_defaults(self):
        comp = Computation(id="COMP-001")
        assert comp.kind == "verify"
        assert comp.zero_output is False
        assert comp.confidence == ""
        assert comp.notes == ""
        assert comp.result == ""

    def test_json_round_trip(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(
            id="COMP-001",
            target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED,
            kind="explore",
            zero_output=True,
            confidence="approximate",
            notes="Ran 1000 iterations.",
            result="pi/4 ~ 0.785",
        )
        restored = ResearchState.from_json(state.to_json())
        comp = restored.computations["COMP-001"]
        assert comp.kind == "explore"
        assert comp.zero_output is True
        assert comp.confidence == "approximate"
        assert comp.notes == "Ran 1000 iterations."
        assert comp.result == "pi/4 ~ 0.785"

    def test_backward_compat_missing_fields(self):
        """Old JSON without new Computation fields uses defaults."""
        old_json = json.dumps({
            "iteration": 1,
            "hypotheses": {},
            "computations": {
                "COMP-001": {
                    "id": "COMP-001",
                    "target_hypothesis": "WH-001",
                    "verdict": "VERIFIED",
                }
            },
            "critiques": {},
            "failed_approaches": [],
        })
        state = ResearchState.from_json(old_json)
        comp = state.computations["COMP-001"]
        assert comp.kind == "verify"
        assert comp.zero_output is False
        assert comp.confidence == ""
        assert comp.notes == ""
        assert comp.result == ""


# ---------------------------------------------------------------------------
# explore_only_hypotheses
# ---------------------------------------------------------------------------

class TestExploreOnlyHypotheses:

    def test_returns_wh_with_explore_but_no_verified(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.WORKING,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED, kind="explore",
        )
        result = state.explore_only_hypotheses()
        assert len(result) == 1
        assert result[0].id == "WH-001"

    def test_excludes_wh_with_verified_verify(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.WORKING,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED, kind="explore",
        )
        state.computations["COMP-002"] = Computation(
            id="COMP-002", target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED, kind="verify",
        )
        result = state.explore_only_hypotheses()
        assert result == []

    def test_excludes_er_hypotheses(self):
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-001",
            verdict=Verdict.VERIFIED, kind="explore",
        )
        assert state.explore_only_hypotheses() == []

    def test_excludes_wh_without_explore(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.WORKING,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.INCONCLUSIVE, kind="verify",
        )
        assert state.explore_only_hypotheses() == []

    def test_empty_state(self):
        state = ResearchState()
        assert state.explore_only_hypotheses() == []


# ---------------------------------------------------------------------------
# refuted_targets
# ---------------------------------------------------------------------------

class TestRefutedTargets:

    def test_returns_refuted_ids(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001", verdict=Verdict.REFUTED,
        )
        state.computations["COMP-002"] = Computation(
            id="COMP-002", target_hypothesis="WH-002", verdict=Verdict.VERIFIED,
        )
        state.computations["COMP-003"] = Computation(
            id="COMP-003", target_hypothesis="WH-001", verdict=Verdict.INCONCLUSIVE,
        )
        assert state.refuted_targets() == {"WH-001"}

    def test_multiple_refuted(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001", verdict=Verdict.REFUTED,
        )
        state.computations["COMP-002"] = Computation(
            id="COMP-002", target_hypothesis="WH-002", verdict=Verdict.REFUTED,
        )
        assert state.refuted_targets() == {"WH-001", "WH-002"}

    def test_empty_target_excluded(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="", verdict=Verdict.REFUTED,
        )
        assert state.refuted_targets() == set()

    def test_empty_state(self):
        state = ResearchState()
        assert state.refuted_targets() == set()


# ---------------------------------------------------------------------------
# demote_hypothesis
# ---------------------------------------------------------------------------

class TestDemoteHypothesis:

    def test_demotes_er_to_wh(self):
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(
            id="ER-002", statement="Energy conserved",
            status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-002", verdict=Verdict.VERIFIED,
        )
        new_id = state.demote_hypothesis("ER-002")
        assert new_id == "WH-002"
        assert "ER-002" not in state.hypotheses
        assert "WH-002" in state.hypotheses
        assert state.hypotheses["WH-002"].status == HypothesisStatus.WORKING
        assert state.hypotheses["WH-002"].statement == "Energy conserved"

    def test_fixes_computation_references(self):
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(
            id="ER-002", status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-002", verdict=Verdict.VERIFIED,
        )
        state.demote_hypothesis("ER-002")
        assert state.computations["COMP-001"].target_hypothesis == "WH-002"
        assert "COMP-001" in state.hypotheses["WH-002"].supporting_comps

    def test_returns_none_for_wh(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.WORKING,
        )
        assert state.demote_hypothesis("WH-001") is None
        # WH-001 should be unchanged
        assert "WH-001" in state.hypotheses
        assert state.hypotheses["WH-001"].status == HypothesisStatus.WORKING

    def test_returns_none_for_missing(self):
        state = ResearchState()
        assert state.demote_hypothesis("ER-999") is None

    def test_preserves_other_hypotheses(self):
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(
            id="ER-002", status=HypothesisStatus.ESTABLISHED,
        )
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.WORKING,
        )
        state.demote_hypothesis("ER-002")
        assert "WH-001" in state.hypotheses
        assert state.hypotheses["WH-001"].status == HypothesisStatus.WORKING


# ---------------------------------------------------------------------------
# next_*_num methods
# ---------------------------------------------------------------------------

class TestNextNumMethods:

    def test_next_hypothesis_num_basic(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.hypotheses["ER-003"] = Hypothesis(id="ER-003")
        assert state.next_hypothesis_num() == 4

    def test_next_hypothesis_num_empty(self):
        state = ResearchState()
        assert state.next_hypothesis_num() == 1

    def test_next_hypothesis_num_gap(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.hypotheses["WH-005"] = Hypothesis(id="WH-005")
        assert state.next_hypothesis_num() == 6

    def test_next_computation_num_basic(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(id="COMP-001")
        state.computations["COMP-002"] = Computation(id="COMP-002")
        assert state.next_computation_num() == 3

    def test_next_computation_num_empty(self):
        state = ResearchState()
        assert state.next_computation_num() == 1

    def test_next_computation_num_gap(self):
        state = ResearchState()
        state.computations["COMP-001"] = Computation(id="COMP-001")
        state.computations["COMP-010"] = Computation(id="COMP-010")
        assert state.next_computation_num() == 11

    def test_next_critique_num_basic(self):
        state = ResearchState()
        state.critiques["CRIT-001"] = Critique(id="CRIT-001")
        state.critiques["CRIT-002"] = Critique(id="CRIT-002")
        assert state.next_critique_num() == 3

    def test_next_critique_num_empty(self):
        state = ResearchState()
        assert state.next_critique_num() == 1

    def test_next_critique_num_non_sequential(self):
        state = ResearchState()
        state.critiques["CRIT-003"] = Critique(id="CRIT-003")
        state.critiques["CRIT-007"] = Critique(id="CRIT-007")
        assert state.next_critique_num() == 8


# ---------------------------------------------------------------------------
# _extract_h1_section
# ---------------------------------------------------------------------------

class TestExtractH1Section:

    MULTI_SECTION_DOC = """\
# Problem Statement

Derive the Hawking temperature from first principles.

# Conventions

Natural units: hbar = c = k_B = 1.

# Working Hypotheses (WH) and Established Results (ER)

## WH-001 — Something

Details here.

# Open Questions

What about grey-body factors?

# Dead Ends

Nothing yet.
"""

    def test_extracts_problem_statement(self):
        result = _extract_h1_section(self.MULTI_SECTION_DOC, "Problem Statement")
        assert "Hawking temperature" in result
        assert "Conventions" not in result

    def test_extracts_conventions(self):
        result = _extract_h1_section(self.MULTI_SECTION_DOC, "Conventions")
        assert "Natural units" in result
        assert "Working Hypotheses" not in result

    def test_extracts_open_questions(self):
        result = _extract_h1_section(self.MULTI_SECTION_DOC, "Open Questions")
        assert "grey-body factors" in result
        assert "Dead Ends" not in result

    def test_extracts_last_section(self):
        result = _extract_h1_section(self.MULTI_SECTION_DOC, "Dead Ends")
        assert "Nothing yet." in result

    def test_missing_section_returns_empty(self):
        result = _extract_h1_section(self.MULTI_SECTION_DOC, "Nonexistent")
        assert result == ""

    def test_empty_body_returns_empty(self):
        result = _extract_h1_section("", "Problem Statement")
        assert result == ""

    def test_single_section(self):
        doc = "# Problem Statement\n\nJust one section."
        result = _extract_h1_section(doc, "Problem Statement")
        assert result == "Just one section."


# ---------------------------------------------------------------------------
# build_from_workspace: new top-level fields
# ---------------------------------------------------------------------------

SAMPLE_RESEARCH_STATE_FULL = """\
---
problem_id: research-session
title: Hawking Temperature
status: in_progress
iteration: 7
---

# Problem Statement

Derive the Hawking temperature for a Schwarzschild black hole.

# Conventions

Natural units: hbar = c = k_B = 1.
Signature: (-,+,+,+).

# Working Hypotheses (WH) and Established Results (ER)

## WH-001 — Surface gravity approach

Use surface gravity kappa to derive T_H = kappa / (2 pi).

## ER-002 — Unruh effect analogy

The Unruh effect gives T = a / (2 pi) in Rindler space.

# Open Questions

How do grey-body factors modify the spectrum?

# Dead Ends

Nothing yet.
"""


class TestBuildFromWorkspaceNewFields:

    def _make_workspace(self, files: dict[str, str]):
        ws = MagicMock()
        ws.read_file = MagicMock(side_effect=lambda f: files.get(f, ""))
        return ws

    def test_populates_title(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE_FULL,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert state.title == "Hawking Temperature"

    def test_populates_status(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE_FULL,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert state.status == "in_progress"

    def test_populates_problem_statement(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE_FULL,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert "Schwarzschild black hole" in state.problem_statement

    def test_populates_conventions(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE_FULL,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert "Natural units" in state.conventions
        assert "Signature" in state.conventions

    def test_populates_open_questions(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE_FULL,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert "grey-body factors" in state.open_questions

    def test_missing_sections_default_empty(self):
        """RESEARCH_STATE without Conventions/Open Questions sections."""
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,  # original fixture, no Conventions/OQ
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert state.problem_statement == "Some problem."
        assert state.conventions == ""
        assert state.open_questions == ""

    def test_existing_fixture_title_and_status(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        state = build_from_workspace(ws)
        assert state.title == "Test problem"
        assert state.status == "not_started"


# ---------------------------------------------------------------------------
# build_from_workspace: COMPUTATION_INDEX.jsonl enrichment
# ---------------------------------------------------------------------------

class TestBuildFromWorkspaceJsonlEnrichment:

    def _make_workspace(self, files: dict[str, str]):
        ws = MagicMock()
        ws.read_file = MagicMock(side_effect=lambda f: files.get(f, ""))
        return ws

    def test_enriches_kind_from_jsonl(self):
        jsonl = json.dumps({"id": "COMP-001", "kind": "explore", "verdict": "VERIFIED"})
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": jsonl,
        })
        state = build_from_workspace(ws)
        assert state.computations["COMP-001"].kind == "explore"
        # COMP-002 not in JSONL, keeps default
        assert state.computations["COMP-002"].kind == "verify"

    def test_enriches_confidence_from_jsonl(self):
        jsonl = json.dumps({
            "id": "COMP-001", "kind": "explore",
            "confidence": "approximate", "verdict": "VERIFIED",
        })
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": jsonl,
        })
        state = build_from_workspace(ws)
        assert state.computations["COMP-001"].confidence == "approximate"

    def test_enriches_notes_from_jsonl(self):
        jsonl = json.dumps({
            "id": "COMP-001", "kind": "verify",
            "notes": "JSONL override notes", "verdict": "VERIFIED",
        })
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": jsonl,
        })
        state = build_from_workspace(ws)
        assert state.computations["COMP-001"].notes == "JSONL override notes"

    def test_enriches_result_from_jsonl(self):
        jsonl = json.dumps({
            "id": "COMP-001", "kind": "verify",
            "result": "T_H = 1/(8*pi*M)", "verdict": "VERIFIED",
        })
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": jsonl,
        })
        state = build_from_workspace(ws)
        assert state.computations["COMP-001"].result == "T_H = 1/(8*pi*M)"

    def test_zero_output_from_no_exit_tool_call(self):
        jsonl = json.dumps({
            "id": "COMP-001", "kind": "verify",
            "notes": "no exit tool call — forced partial output",
            "verdict": "INCONCLUSIVE",
        })
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": jsonl,
        })
        state = build_from_workspace(ws)
        assert state.computations["COMP-001"].zero_output is True

    def test_ignores_unknown_comp_ids(self):
        jsonl = json.dumps({"id": "COMP-999", "kind": "explore", "verdict": "VERIFIED"})
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": jsonl,
        })
        state = build_from_workspace(ws)
        assert "COMP-999" not in state.computations
        # Existing comps unaffected
        assert state.computations["COMP-001"].kind == "verify"

    def test_handles_malformed_jsonl_lines(self):
        jsonl_lines = [
            json.dumps({"id": "COMP-001", "kind": "explore", "verdict": "VERIFIED"}),
            "not valid json",
            "",
            json.dumps({"id": "COMP-002", "kind": "verify", "verdict": "REFUTED"}),
        ]
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": "\n".join(jsonl_lines),
        })
        state = build_from_workspace(ws)
        assert state.computations["COMP-001"].kind == "explore"
        assert state.computations["COMP-002"].kind == "verify"

    def test_multiple_jsonl_entries(self):
        jsonl_lines = [
            json.dumps({"id": "COMP-001", "kind": "explore", "confidence": "exact", "verdict": "VERIFIED"}),
            json.dumps({"id": "COMP-002", "kind": "verify", "notes": "spin check", "verdict": "REFUTED"}),
        ]
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": "\n".join(jsonl_lines),
        })
        state = build_from_workspace(ws)
        assert state.computations["COMP-001"].kind == "explore"
        assert state.computations["COMP-001"].confidence == "exact"
        assert state.computations["COMP-002"].kind == "verify"
        assert state.computations["COMP-002"].notes == "spin check"

    def test_empty_jsonl(self):
        ws = self._make_workspace({
            "RESEARCH_STATE.md": SAMPLE_RESEARCH_STATE,
            "COMPUTATION_LOG.md": SAMPLE_COMPUTATION_LOG,
            "CRITIQUE_LOG.md": "",
            "COMPUTATION_INDEX.jsonl": "",
        })
        state = build_from_workspace(ws)
        # Defaults preserved
        assert state.computations["COMP-001"].kind == "verify"
        assert state.computations["COMP-001"].confidence == ""


# ---------------------------------------------------------------------------
# Phase 3: Justification graph (B4) tests
# ---------------------------------------------------------------------------

class TestHypothesisDependsOn:
    """Tests for depends_on and promotion_justification fields."""

    def test_json_round_trip_new_fields(self):
        """New fields survive JSON serialization round-trip."""
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="A depends on B",
            depends_on=["ER-001", "WH-002"],
        )
        state.hypotheses["ER-002"] = Hypothesis(
            id="ER-002", statement="Promoted result",
            status=HypothesisStatus.ESTABLISHED,
            promotion_justification="Verified by COMP-001.",
        )
        json_str = state.to_json()
        restored = ResearchState.from_json(json_str)

        assert restored.hypotheses["WH-001"].depends_on == ["ER-001", "WH-002"]
        assert restored.hypotheses["ER-002"].promotion_justification == "Verified by COMP-001."

    def test_json_backward_compat_missing_fields(self):
        """Old JSON without depends_on/promotion_justification loads with defaults."""
        data = {
            "hypotheses": {
                "WH-001": {"id": "WH-001", "statement": "Old hypothesis"},
            },
        }
        state = ResearchState.from_json(json.dumps(data))
        assert state.hypotheses["WH-001"].depends_on == []
        assert state.hypotheses["WH-001"].promotion_justification == ""


class TestNormalizeReferencesDependsOn:
    """Tests for normalize_references remapping depends_on entries."""

    def test_depends_on_remapped_on_promotion(self):
        """When WH-001 is promoted to ER-001, depends_on entries are remapped."""
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.hypotheses["WH-002"] = Hypothesis(
            id="WH-002", depends_on=["WH-001"],
        )
        state.normalize_references()
        assert state.hypotheses["WH-002"].depends_on == ["ER-001"]

    def test_depends_on_unchanged_when_no_rename(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.hypotheses["WH-002"] = Hypothesis(
            id="WH-002", depends_on=["WH-001"],
        )
        state.normalize_references()
        assert state.hypotheses["WH-002"].depends_on == ["WH-001"]


class TestUnestablishedDependencies:
    """Tests for unestablished_dependencies query."""

    def test_returns_working_dependency(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.hypotheses["WH-002"] = Hypothesis(
            id="WH-002", depends_on=["WH-001"],
        )
        assert state.unestablished_dependencies("WH-002") == ["WH-001"]

    def test_returns_empty_when_dependency_established(self):
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.hypotheses["WH-002"] = Hypothesis(
            id="WH-002", depends_on=["ER-001"],
        )
        assert state.unestablished_dependencies("WH-002") == []

    def test_returns_empty_for_no_dependencies(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        assert state.unestablished_dependencies("WH-001") == []

    def test_returns_missing_dependency(self):
        """Dependency pointing to non-existent ID is unestablished."""
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", depends_on=["WH-099"],
        )
        assert state.unestablished_dependencies("WH-001") == ["WH-099"]

    def test_returns_empty_for_unknown_hypothesis(self):
        state = ResearchState()
        assert state.unestablished_dependencies("WH-999") == []
