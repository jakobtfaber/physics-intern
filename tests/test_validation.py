"""Tests for validation.py — post-integration validation and termination gates."""

from sciralph.validation import (
    Violation,
    ViolationSeverity,
    validate_post_integration,
    can_terminate,
    check_er_demotion_safety,
    check_phantom_labels,
    check_stale_unverified_labels,
    check_critique_resolution_consistency,
)
from sciralph.research_state import (
    Computation,
    Critique,
    CritiqueStatus,
    Hypothesis,
    HypothesisStatus,
    ResearchQuestion,
    ResearchState,
    RQStatus,
    Severity,
    Verdict,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class MockMetrics:
    """Minimal mock for MetricsTracker with last_critic_iteration."""
    def __init__(self, last_critic_iteration: int = 0):
        self.last_critic_iteration = last_critic_iteration


class MockConfig:
    """Minimal mock for Config."""
    def __init__(self):
        self.min_er_for_completion = 1


class MockWorkspace:
    """Minimal workspace mock for can_terminate (still takes workspace param)."""
    def __init__(self):
        self.root = None


# ---------------------------------------------------------------------------
# Violation dataclass tests
# ---------------------------------------------------------------------------

class TestViolation:
    def test_creation(self):
        v = Violation(
            check="test_check", severity=ViolationSeverity.ERROR,
            message="test msg", file="test.md",
        )
        assert v.check == "test_check"
        assert v.severity == ViolationSeverity.ERROR
        assert v.message == "test msg"
        assert v.detail == ""

    def test_with_detail(self):
        v = Violation(
            check="x", severity=ViolationSeverity.WARNING,
            message="y", file="z", detail="d",
        )
        assert v.detail == "d"


# ---------------------------------------------------------------------------
# check_er_demotion_safety
# ---------------------------------------------------------------------------

class TestErDemotionSafety:
    def test_demotes_er_with_refuted_no_verified(self):
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", statement="T = 1/(8piM)",
            status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-001",
            verdict=Verdict.REFUTED, kind="verify", iteration=1,
        )
        violations = check_er_demotion_safety(state)
        assert len(violations) == 1
        assert "demoted" in violations[0].message
        # ER-001 should be demoted to WH-001
        assert "WH-001" in state.hypotheses
        assert "ER-001" not in state.hypotheses

    def test_no_demotion_when_verified_exists(self):
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", statement="T = 1/(8piM)",
            status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-001",
            verdict=Verdict.REFUTED, kind="verify", iteration=1,
        )
        state.computations["COMP-002"] = Computation(
            id="COMP-002", target_hypothesis="ER-001",
            verdict=Verdict.VERIFIED, kind="verify", iteration=2,
        )
        violations = check_er_demotion_safety(state)
        assert len(violations) == 0
        assert "ER-001" in state.hypotheses

    def test_no_demotion_when_no_refuted(self):
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-001",
            verdict=Verdict.VERIFIED, kind="verify", iteration=1,
        )
        violations = check_er_demotion_safety(state)
        assert len(violations) == 0

    def test_no_demotion_for_wh(self):
        """WH hypotheses are not demoted — only ER."""
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.WORKING,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.REFUTED, kind="verify", iteration=1,
        )
        violations = check_er_demotion_safety(state)
        assert len(violations) == 0

    def test_refuted_via_wh_alias(self):
        """REFUTED computation targeting WH-001 should demote ER-001."""
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.REFUTED, kind="verify", iteration=1,
        )
        violations = check_er_demotion_safety(state)
        assert len(violations) == 1
        assert "WH-001" in state.hypotheses


# ---------------------------------------------------------------------------
# check_phantom_labels
# ---------------------------------------------------------------------------

class TestPhantomLabels:
    def test_strips_unsubstantiated_verified(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", derivation="WH-001 is VERIFIED by computation.",
        )
        violations = check_phantom_labels(state)
        assert len(violations) == 1
        assert "[unverified]" in state.hypotheses["WH-001"].derivation

    def test_keeps_backed_verified(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", derivation="WH-001 is VERIFIED by computation.",
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED, kind="verify", iteration=1,
        )
        violations = check_phantom_labels(state)
        assert len(violations) == 0
        assert "VERIFIED" in state.hypotheses["WH-001"].derivation

    def test_no_action_without_verified(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", derivation="Some derivation text.",
        )
        violations = check_phantom_labels(state)
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# check_stale_unverified_labels
# ---------------------------------------------------------------------------

class TestStaleUnverifiedLabels:
    def test_promotes_unverified_to_verified(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", derivation="WH-001 is [unverified] pending computation.",
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED, kind="verify", iteration=1,
        )
        violations = check_stale_unverified_labels(state)
        # Note: the function returns [] (empty list) but mutates derivation
        assert "VERIFIED" in state.hypotheses["WH-001"].derivation
        assert "[unverified]" not in state.hypotheses["WH-001"].derivation

    def test_no_change_without_verified_computation(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", derivation="WH-001 is [unverified] pending computation.",
        )
        check_stale_unverified_labels(state)
        assert "[unverified]" in state.hypotheses["WH-001"].derivation


# ---------------------------------------------------------------------------
# check_critique_resolution_consistency
# ---------------------------------------------------------------------------

class TestCritiqueResolutionConsistency:
    def test_flags_vanished_target(self):
        state = ResearchState()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-099"],
            severity=Severity.HIGH,
            status=CritiqueStatus.RESOLVED,
        )
        violations = check_critique_resolution_consistency(state)
        assert len(violations) == 1
        assert "no longer exists" in violations[0].message

    def test_no_flag_when_target_exists(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-001"],
            severity=Severity.HIGH,
            status=CritiqueStatus.RESOLVED,
        )
        violations = check_critique_resolution_consistency(state)
        assert len(violations) == 0

    def test_flags_label_coexistence(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(id="WH-001")
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-001"],
            severity=Severity.HIGH,
            argument="Inconsistent label: WH-001 should be ER-001",
            status=CritiqueStatus.RESOLVED,
        )
        violations = check_critique_resolution_consistency(state)
        assert any("co-exist" in v.message for v in violations)

    def test_skips_active_critiques(self):
        state = ResearchState()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-099"],
            severity=Severity.HIGH,
            status=CritiqueStatus.ACTIVE,
        )
        violations = check_critique_resolution_consistency(state)
        assert len(violations) == 0

    def test_target_found_via_promoted_form(self):
        """If critique targets WH-001 but it was promoted to ER-001, no flag."""
        state = ResearchState()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-001"],
            severity=Severity.HIGH,
            status=CritiqueStatus.RESOLVED,
        )
        violations = check_critique_resolution_consistency(state)
        vanished = [v for v in violations if "no longer exists" in v.message]
        assert len(vanished) == 0


# ---------------------------------------------------------------------------
# validate_post_integration
# ---------------------------------------------------------------------------

class TestValidatePostIntegration:
    def test_returns_empty_for_clean_state(self):
        state = ResearchState()
        violations = validate_post_integration(state)
        assert violations == []

    def test_aggregates_violations(self):
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", derivation="WH-001 is VERIFIED.",
        )
        violations = validate_post_integration(state)
        assert len(violations) >= 1
        assert any(v.check == "phantom_labels" for v in violations)

    def test_runs_four_checks(self):
        """Pipeline has exactly 4 checks."""
        from sciralph.validation import _DEFAULT_CHECKS
        assert len(_DEFAULT_CHECKS) == 4


# ---------------------------------------------------------------------------
# can_terminate
# ---------------------------------------------------------------------------

class TestCanTerminate:
    def _make_state(self, **kwargs) -> ResearchState:
        return ResearchState(**kwargs)

    def test_allows_with_er_and_critic(self):
        state = self._make_state()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-001",
            verdict=Verdict.VERIFIED, kind="verify", iteration=1,
        )
        allowed, blockers = can_terminate(
            MockWorkspace(), MockConfig(), MockMetrics(last_critic_iteration=1),
            research_state=state,
        )
        assert allowed
        assert blockers == []

    def test_blocks_without_critic(self):
        state = self._make_state()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-001",
            verdict=Verdict.VERIFIED, kind="verify", iteration=1,
        )
        allowed, blockers = can_terminate(
            MockWorkspace(), MockConfig(), MockMetrics(last_critic_iteration=0),
            research_state=state,
        )
        assert not allowed
        assert any("critic" in b.lower() for b in blockers)

    def test_blocks_with_high_critiques(self):
        state = self._make_state()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.HIGH,
            status=CritiqueStatus.ACTIVE,
        )
        allowed, blockers = can_terminate(
            MockWorkspace(), MockConfig(), MockMetrics(last_critic_iteration=1),
            research_state=state,
        )
        assert not allowed
        assert any("HIGH" in b for b in blockers)

    def test_blocks_with_open_rq(self):
        state = self._make_state()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="Open question", status=RQStatus.OPEN,
        )
        allowed, blockers = can_terminate(
            MockWorkspace(), MockConfig(), MockMetrics(last_critic_iteration=1),
            research_state=state,
        )
        assert not allowed
        assert any("RQ-001" in b for b in blockers)

    def test_blocks_with_working_hypothesis(self):
        state = self._make_state()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.hypotheses["WH-002"] = Hypothesis(
            id="WH-002", status=HypothesisStatus.WORKING,
        )
        allowed, blockers = can_terminate(
            MockWorkspace(), MockConfig(), MockMetrics(last_critic_iteration=1),
            research_state=state,
        )
        assert not allowed
        assert any("WH-002" in b for b in blockers)

    def test_blocks_wh_with_verified_backing(self):
        """WH with VERIFIED backing should get specific promote/abandon message."""
        state = self._make_state()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.WORKING,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED, kind="verify", iteration=1,
        )
        allowed, blockers = can_terminate(
            MockWorkspace(), MockConfig(), MockMetrics(last_critic_iteration=1),
            research_state=state,
        )
        assert not allowed
        assert any("promote" in b.lower() for b in blockers)

    def test_blocks_numerical_requirement(self):
        state = self._make_state()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        allowed, blockers = can_terminate(
            MockWorkspace(), MockConfig(), MockMetrics(last_critic_iteration=1),
            {"requires_numerical": True},
            research_state=state,
        )
        assert not allowed
        assert any("numerical" in b.lower() for b in blockers)

    def test_numerical_requirement_satisfied(self):
        state = self._make_state()
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-001",
            verdict=Verdict.VERIFIED, kind="verify", iteration=1,
        )
        allowed, blockers = can_terminate(
            MockWorkspace(), MockConfig(), MockMetrics(last_critic_iteration=1),
            {"requires_numerical": True},
            research_state=state,
        )
        assert allowed
