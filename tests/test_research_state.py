"""Tests for the formal ResearchState module."""

import json
import pytest

from sciralph.research_state import (
    ResearchState,
    Hypothesis,
    HypothesisStatus,
    Evidence,
    VerificationResult,
    Verdict,
    Critique,
    Severity,
    CritiqueStatus,
    FailedApproach,
    BackgroundSurvey,
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

This has been verified.

## WH-003 — Third hypothesis

Entropy increases.

# Dead Ends

Nothing yet.
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
# Query methods
# ---------------------------------------------------------------------------

class TestQueryMethods:

    def _make_state(self) -> ResearchState:
        state = ResearchState(iteration=5)
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Test",
            status=HypothesisStatus.WORKING,
            critiques=["CRIT-001"],
            evidence=Evidence(type="compute", method="numerical", result="42"),
        )
        state.hypotheses["ER-002"] = Hypothesis(
            id="ER-002", statement="Verified",
            status=HypothesisStatus.ESTABLISHED,
            evidence=Evidence(type="compute", method="symbolic", result="ok"),
            verification=VerificationResult(verdict=Verdict.VERIFIED, reasoning="Confirmed."),
        )
        state.critiques["CRIT-001"] = Critique(id="CRIT-001", targets=["WH-001"],
                                                severity=Severity.HIGH,
                                                status=CritiqueStatus.ACTIVE)
        return state

    def test_has_verified_evidence(self):
        state = self._make_state()
        assert state.has_verified_evidence("ER-002")
        assert not state.has_verified_evidence("WH-001")

    def test_hypotheses_with_evidence(self):
        state = self._make_state()
        with_ev = state.hypotheses_with_evidence()
        assert len(with_ev) == 2
        ids = {h.id for h in with_ev}
        assert ids == {"WH-001", "ER-002"}

    def test_active_critiques_for(self):
        state = self._make_state()
        assert len(state.active_critiques_for("WH-001")) == 1
        assert len(state.active_critiques_for("ER-002")) == 0

    def test_unresolved_high_critiques(self):
        state = self._make_state()
        assert len(state.unresolved_high_critiques()) == 1

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
        state.hypotheses["WH-001"].evidence = Evidence(
            type="compute", method="numerical", result="42",
            confidence="exact", iteration=2,
        )
        state.critiques["CRIT-001"] = Critique(id="CRIT-001", targets=["WH-001"],
                                                severity=Severity.HIGH)
        state.failed_approaches.append(FailedApproach(
            description="Tried perturbation theory",
            reason="Divergent series",
            related_entities=["WH-001"],
            iteration=2,
        ))

        json_str = state.to_json()
        restored = ResearchState.from_json(json_str)

        assert restored.iteration == 3
        assert "WH-001" in restored.hypotheses
        assert restored.hypotheses["WH-001"].statement == "Test"
        assert restored.hypotheses["WH-001"].evidence is not None
        assert restored.hypotheses["WH-001"].evidence.result == "42"
        assert restored.critiques["CRIT-001"].severity == Severity.HIGH
        assert len(restored.failed_approaches) == 1
        assert restored.failed_approaches[0].description == "Tried perturbation theory"

    def test_save_and_load(self, tmp_path):
        state = ResearchState(iteration=7)
        state.hypotheses["ER-001"] = Hypothesis(id="ER-001", statement="E=mc2",
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
            related_entities=["WH-001"],
            iteration=3,
        ))
        state.failed_approaches.append(FailedApproach(
            description="INCONCLUSIVE on: WH-002 entropy",
            reason="Timeout",
            related_entities=["WH-002"],
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
            related_entities=["WH-001"],
            iteration=4,
        ))
        restored = ResearchState.from_json(state.to_json())
        assert len(restored.failed_approaches) == 1
        assert restored.failed_approaches[0].reason == "Wrong coefficient"
        assert restored.failed_approaches[0].related_entities == ["WH-001"]

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
# Fix: normalize_references (stale WH/ER backlinks in depends_on)
# ---------------------------------------------------------------------------

class TestNormalizeReferences:

    def test_updates_stale_depends_on_wh_to_er(self):
        """depends_on referencing WH-002 should be updated when hypothesis is ER-002."""
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(id="ER-002", status=HypothesisStatus.ESTABLISHED)
        state.hypotheses["WH-003"] = Hypothesis(
            id="WH-003", depends_on=["WH-002"],
        )
        state.normalize_references()
        assert state.hypotheses["WH-003"].depends_on == ["ER-002"]

    def test_updates_stale_depends_on_er_to_wh(self):
        """depends_on referencing ER-001 should be updated when hypothesis was demoted to WH-001."""
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001", status=HypothesisStatus.WORKING)
        state.hypotheses["WH-003"] = Hypothesis(
            id="WH-003", depends_on=["ER-001"],
        )
        state.normalize_references()
        assert state.hypotheses["WH-003"].depends_on == ["WH-001"]

    def test_idempotent(self):
        """Calling normalize_references twice produces the same result."""
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(id="ER-002", status=HypothesisStatus.ESTABLISHED)
        state.hypotheses["WH-003"] = Hypothesis(
            id="WH-003", depends_on=["WH-002"],
        )
        state.normalize_references()
        deps_after_first = list(state.hypotheses["WH-003"].depends_on)
        state.normalize_references()
        assert state.hypotheses["WH-003"].depends_on == deps_after_first

    def test_no_alias_match_preserves_dep(self):
        """depends_on targeting a number that doesn't exist in hypotheses is left alone."""
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.hypotheses["WH-002"] = Hypothesis(
            id="WH-002", depends_on=["WH-099"],
        )
        state.normalize_references()
        assert state.hypotheses["WH-002"].depends_on == ["WH-099"]


# ---------------------------------------------------------------------------
# New ResearchState fields
# ---------------------------------------------------------------------------

class TestNewResearchStateFields:

    def test_defaults(self):
        state = ResearchState()
        assert state.problem_statement == ""
        assert state.conventions == ""
        assert state.strategy == ""
        assert state.short_term_plan == ""
        assert state.research_notes == []
        assert state.status == "in_progress"
        assert state.title == ""
        assert state.background_survey is None

    def test_json_round_trip(self):
        state = ResearchState(
            iteration=3,
            problem_statement="Derive Hawking temperature.",
            conventions="Natural units: hbar = c = k_B = 1.",
            strategy="Focus on surface gravity approach.",
            short_term_plan="Verify temperature formula.",
            research_notes=[{"text": "Surface gravity confirmed.", "iteration": 1}],
            status="complete",
            title="Hawking Temperature",
        )
        restored = ResearchState.from_json(state.to_json())
        assert restored.problem_statement == "Derive Hawking temperature."
        assert restored.conventions == "Natural units: hbar = c = k_B = 1."
        assert restored.strategy == "Focus on surface gravity approach."
        assert restored.short_term_plan == "Verify temperature formula."
        assert restored.research_notes == [{"text": "Surface gravity confirmed.", "iteration": 1}]
        assert restored.status == "complete"
        assert restored.title == "Hawking Temperature"

    def test_backward_compat_missing_fields(self):
        """Loading old JSON without new fields should use defaults."""
        old_json = json.dumps({
            "iteration": 5,
            "hypotheses": {},
            "critiques": {},
            "failed_approaches": [],
        })
        state = ResearchState.from_json(old_json)
        assert state.iteration == 5
        assert state.problem_statement == ""
        assert state.conventions == ""
        assert state.status == "in_progress"
        assert state.title == ""
        assert state.background_survey is None

    def test_backward_compat_open_questions_ignored(self):
        """Old JSON with open_questions field loads fine (silently ignored)."""
        old_json = json.dumps({
            "iteration": 3,
            "open_questions": "What about grey-body factors?",
            "hypotheses": {},
            "critiques": {},
            "failed_approaches": [],
        })
        state = ResearchState.from_json(old_json)
        assert state.iteration == 3


class TestBackgroundSurveySerialization:

    def test_background_survey_default_none(self):
        state = ResearchState()
        assert state.background_survey is None

    def test_background_survey_json_round_trip(self):
        state = ResearchState(iteration=1)
        state.background_survey = BackgroundSurvey(
            survey_notes="Derive Hawking temperature via surface gravity.\n\nUse Killing vector method.",
            iteration_created=0,
            iteration_updated=0,
        )
        json_str = state.to_json()
        restored = ResearchState.from_json(json_str)
        assert restored.background_survey is not None
        survey = restored.background_survey
        assert "Hawking temperature" in survey.survey_notes
        assert "Killing vector" in survey.survey_notes
        assert survey.iteration_created == 0

    def test_missing_background_survey_loads_as_none(self):
        """JSON without background_survey loads fine."""
        old_json = json.dumps({
            "iteration": 5,
            "hypotheses": {},
            "critiques": {},
            "failed_approaches": [],
        })
        state = ResearchState.from_json(old_json)
        assert state.background_survey is None

    def test_background_survey_none_serializes_as_null(self):
        state = ResearchState()
        data = json.loads(state.to_json())
        assert data["background_survey"] is None


# ---------------------------------------------------------------------------
# Evidence and VerificationResult on Hypothesis
# ---------------------------------------------------------------------------

class TestEvidenceOnHypothesis:

    def test_evidence_defaults(self):
        ev = Evidence()
        assert ev.type == ""
        assert ev.reasoning == ""
        assert ev.approach == ""
        assert ev.scripts == []
        assert ev.output == ""
        assert ev.method == ""
        assert ev.result == ""
        assert ev.confidence == ""
        assert ev.iteration is None

    def test_evidence_json_round_trip(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001",
            evidence=Evidence(
                type="compute",
                approach="Direct numerical computation",
                scripts=["001_verify.py"],
                output="Result: 0.785",
                method="numerical integration",
                result="pi/4 ~ 0.785",
                confidence="approximate",
                iteration=3,
            ),
        )
        restored = ResearchState.from_json(state.to_json())
        ev = restored.hypotheses["WH-001"].evidence
        assert ev is not None
        assert ev.type == "compute"
        assert ev.approach == "Direct numerical computation"
        assert ev.scripts == ["001_verify.py"]
        assert ev.method == "numerical integration"
        assert ev.result == "pi/4 ~ 0.785"
        assert ev.confidence == "approximate"
        assert ev.iteration == 3

    def test_verification_result_defaults(self):
        vr = VerificationResult()
        assert vr.verdict == ""
        assert vr.reasoning == ""
        assert vr.critiques == []
        assert vr.iteration is None

    def test_verification_result_json_round_trip(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001",
            verification=VerificationResult(
                verdict="VERIFIED",
                reasoning="All checks pass.",
                critiques=[{"severity": "LOW", "argument": "Minor notation issue"}],
                iteration=5,
            ),
        )
        restored = ResearchState.from_json(state.to_json())
        vr = restored.hypotheses["WH-001"].verification
        assert vr is not None
        assert vr.verdict == "VERIFIED"
        assert vr.reasoning == "All checks pass."
        assert len(vr.critiques) == 1
        assert vr.critiques[0]["severity"] == "LOW"
        assert vr.iteration == 5

    def test_hypothesis_without_evidence_or_verification(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        restored = ResearchState.from_json(state.to_json())
        assert restored.hypotheses["WH-001"].evidence is None
        assert restored.hypotheses["WH-001"].verification is None

    def test_backward_compat_missing_evidence_fields(self):
        """Old JSON without evidence/verification on hypotheses uses None."""
        old_json = json.dumps({
            "iteration": 1,
            "hypotheses": {
                "WH-001": {
                    "id": "WH-001",
                    "statement": "Test hypothesis",
                    "status": "working",
                }
            },
            "critiques": {},
            "failed_approaches": [],
        })
        state = ResearchState.from_json(old_json)
        h = state.hypotheses["WH-001"]
        assert h.evidence is None
        assert h.verification is None


# ---------------------------------------------------------------------------
# has_verified_evidence and hypotheses_with_evidence
# ---------------------------------------------------------------------------

class TestNewQueryMethods:

    def test_has_verified_evidence_true(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001",
            verification=VerificationResult(verdict=Verdict.VERIFIED),
        )
        assert state.has_verified_evidence("WH-001") is True

    def test_has_verified_evidence_false_no_verification(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        assert state.has_verified_evidence("WH-001") is False

    def test_has_verified_evidence_false_inconclusive(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001",
            verification=VerificationResult(verdict=Verdict.INCONCLUSIVE),
        )
        assert state.has_verified_evidence("WH-001") is False

    def test_has_verified_evidence_false_missing(self):
        state = ResearchState()
        assert state.has_verified_evidence("WH-999") is False

    def test_hypotheses_with_evidence_returns_correct(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001",
            evidence=Evidence(type="compute", result="42"),
        )
        state.hypotheses["WH-002"] = Hypothesis(id="WH-002")
        state.hypotheses["WH-003"] = Hypothesis(
            id="WH-003",
            evidence=Evidence(type="research", result="derivation ok"),
        )
        result = state.hypotheses_with_evidence()
        assert len(result) == 2
        ids = {h.id for h in result}
        assert ids == {"WH-001", "WH-003"}

    def test_hypotheses_with_evidence_empty(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        assert state.hypotheses_with_evidence() == []


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
        new_id = state.demote_hypothesis("ER-002")
        assert new_id == "WH-002"
        assert "ER-002" not in state.hypotheses
        assert "WH-002" in state.hypotheses
        assert state.hypotheses["WH-002"].status == HypothesisStatus.WORKING
        assert state.hypotheses["WH-002"].statement == "Energy conserved"

    def test_fixes_depends_on_references(self):
        state = ResearchState()
        state.hypotheses["ER-002"] = Hypothesis(
            id="ER-002", status=HypothesisStatus.ESTABLISHED,
        )
        state.hypotheses["WH-003"] = Hypothesis(
            id="WH-003", depends_on=["ER-002"],
        )
        state.demote_hypothesis("ER-002")
        assert state.hypotheses["WH-003"].depends_on == ["WH-002"]

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
            promotion_justification="Verified by verifier.",
        )
        json_str = state.to_json()
        restored = ResearchState.from_json(json_str)

        assert restored.hypotheses["WH-001"].depends_on == ["ER-001", "WH-002"]
        assert restored.hypotheses["ER-002"].promotion_justification == "Verified by verifier."

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


# ---------------------------------------------------------------------------
# Phase 4b: Research Questions (B1+B3) tests
# ---------------------------------------------------------------------------

class TestResearchQuestionLifecycle:
    """Tests for ResearchQuestion entity and queries."""

    def test_json_round_trip(self):
        from sciralph.research_state import ResearchQuestion, RQStatus
        state = ResearchState()
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001",
            question="What is the leading-order correction?",
            context="Needed for next step",
            resolved_to=["WH-003"],
            status=RQStatus.RESOLVED,
            iteration_created=1,
            iteration_resolved=3,
        )
        json_str = state.to_json()
        restored = ResearchState.from_json(json_str)
        rq = restored.research_questions["RQ-001"]
        assert rq.question == "What is the leading-order correction?"
        assert rq.status == RQStatus.RESOLVED
        assert rq.resolved_to == ["WH-003"]
        assert rq.iteration_resolved == 3

    def test_json_backward_compat_no_rqs(self):
        data = {"hypotheses": {}}
        state = ResearchState.from_json(json.dumps(data))
        assert state.research_questions == {}

    def test_open_research_questions(self):
        from sciralph.research_state import ResearchQuestion, RQStatus
        state = ResearchState()
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="Open one", status=RQStatus.OPEN,
        )
        state.research_questions["RQ-002"] = ResearchQuestion(
            id="RQ-002", question="Resolved one", status=RQStatus.RESOLVED,
        )
        assert len(state.open_research_questions()) == 1
        assert state.open_research_questions()[0].id == "RQ-001"

    def test_next_rq_num(self):
        from sciralph.research_state import ResearchQuestion
        state = ResearchState()
        assert state.next_rq_num() == 1
        state.research_questions["RQ-001"] = ResearchQuestion(id="RQ-001")
        assert state.next_rq_num() == 2

    def test_normalize_references_remaps_resolved_to(self):
        from sciralph.research_state import ResearchQuestion
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="test", resolved_to=["WH-001"],
        )
        state.normalize_references()
        assert state.research_questions["RQ-001"].resolved_to == ["ER-001"]


# ---------------------------------------------------------------------------
# Evidence on ResearchQuestion
# ---------------------------------------------------------------------------

class TestResearchQuestionEvidence:
    """Evidence can be attached to research questions."""

    def test_rq_evidence_round_trip(self):
        from sciralph.research_state import ResearchQuestion
        state = ResearchState()
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is F?",
            evidence=Evidence(
                type="research", method="analysis",
                result="F = pi/4", confidence="exact", iteration=2,
            ),
        )
        restored = ResearchState.from_json(state.to_json())
        rq = restored.research_questions["RQ-001"]
        assert rq.evidence is not None
        assert rq.evidence.result == "F = pi/4"
        assert rq.evidence.type == "research"

    def test_rq_no_evidence_default(self):
        from sciralph.research_state import ResearchQuestion
        rq = ResearchQuestion(id="RQ-001", question="Test")
        assert rq.evidence is None
