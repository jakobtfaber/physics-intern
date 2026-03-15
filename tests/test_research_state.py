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
