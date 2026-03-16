"""Tests for OrchestratorToolExecutor — state-mutation tools for the orchestrator."""

from unittest.mock import MagicMock

from sciralph.orchestrator_tools import OrchestratorToolExecutor
from sciralph.research_state import (
    ResearchState, Hypothesis, HypothesisStatus, Computation, Verdict,
    Critique, Severity, CritiqueStatus, FailedApproach,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workspace():
    ws = MagicMock()
    ws.root = MagicMock()
    ws.append_file = MagicMock()
    return ws


def _make_state() -> ResearchState:
    """ResearchState with two working hypotheses, no computations or critiques."""
    state = ResearchState()
    state.hypotheses["WH-001"] = Hypothesis(
        id="WH-001", statement="First hypothesis",
        status=HypothesisStatus.WORKING, derivation="Photon has spin-1.",
        iteration_created=1, iteration_modified=1,
    )
    state.hypotheses["WH-002"] = Hypothesis(
        id="WH-002", statement="Second hypothesis",
        status=HypothesisStatus.WORKING, derivation="Entropy increases in isolated systems.",
        iteration_created=2, iteration_modified=2,
    )
    return state


def _make_state_with_verified(target: str = "WH-001") -> ResearchState:
    """State with a VERIFIED computation targeting *target*."""
    state = _make_state()
    state.computations["COMP-001"] = Computation(
        id="COMP-001", target_hypothesis=target,
        verdict=Verdict.VERIFIED, claim=f"Verify {target}",
    )
    return state


def _make_state_with_refuted(target: str = "WH-001") -> ResearchState:
    """State with a REFUTED computation targeting *target*."""
    state = _make_state()
    state.computations["COMP-001"] = Computation(
        id="COMP-001", target_hypothesis=target,
        verdict=Verdict.REFUTED, claim=f"Verify {target}",
    )
    return state


def _make_state_with_refuted_and_verified(target: str = "WH-001") -> ResearchState:
    """State with both REFUTED and VERIFIED computations targeting *target*."""
    state = _make_state()
    state.computations["COMP-001"] = Computation(
        id="COMP-001", target_hypothesis=target,
        verdict=Verdict.REFUTED, claim=f"Verify {target} (first attempt)",
    )
    state.computations["COMP-002"] = Computation(
        id="COMP-002", target_hypothesis=target,
        verdict=Verdict.VERIFIED, claim=f"Verify {target} (corrected)",
    )
    return state


def _make_state_with_high_critique(target: str = "WH-001") -> ResearchState:
    """State with VERIFIED comp + unresolved HIGH critique targeting *target*."""
    state = _make_state_with_verified(target)
    state.critiques["CRIT-001"] = Critique(
        id="CRIT-001", targets=[target], severity=Severity.HIGH,
        status=CritiqueStatus.ACTIVE, argument="Spin prediction may be wrong.",
    )
    return state


# ---------------------------------------------------------------------------
# add_hypothesis
# ---------------------------------------------------------------------------

class TestAddHypothesis:
    def test_creates_wh003_in_state(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_hypothesis", {
            "statement": "Third hypothesis",
            "derivation": "Some derivation.",
        })
        assert not tc.is_error
        assert "WH-003" in tc.output
        assert "WH-003" in state.hypotheses
        h = state.hypotheses["WH-003"]
        assert h.statement == "Third hypothesis"
        assert h.derivation == "Some derivation."
        assert h.status == HypothesisStatus.WORKING
        assert h.iteration_created == 3
        assert h.iteration_modified == 3
        assert ex.mutations_applied


# ---------------------------------------------------------------------------
# update_hypothesis
# ---------------------------------------------------------------------------

class TestUpdateHypothesis:
    def test_updates_statement_and_derivation(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=4, research_state=state)
        tc = ex.execute("update_hypothesis", {
            "id": "WH-001",
            "statement": "Updated title",
            "derivation": "Updated derivation.",
        })
        assert not tc.is_error
        h = state.hypotheses["WH-001"]
        assert h.statement == "Updated title"
        assert h.derivation == "Updated derivation."
        assert h.iteration_modified == 4

    def test_updates_derivation_only_preserves_statement(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=4, research_state=state)
        ex.execute("update_hypothesis", {
            "id": "WH-001",
            "derivation": "New derivation only.",
        })
        h = state.hypotheses["WH-001"]
        assert h.statement == "First hypothesis"  # preserved
        assert h.derivation == "New derivation only."

    def test_updates_statement_only_preserves_derivation(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=4, research_state=state)
        ex.execute("update_hypothesis", {
            "id": "WH-001",
            "statement": "New title",
        })
        h = state.hypotheses["WH-001"]
        assert h.statement == "New title"
        assert h.derivation == "Photon has spin-1."  # preserved

    def test_missing_id_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=4, research_state=state)
        tc = ex.execute("update_hypothesis", {"id": "WH-099"})
        assert "not found" in tc.output


# ---------------------------------------------------------------------------
# abandon_hypothesis
# ---------------------------------------------------------------------------

class TestAbandonHypothesis:
    def test_sets_abandoned_and_creates_failed_approach(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("abandon_hypothesis", {
            "id": "WH-001",
            "reason": "Spin prediction was wrong.",
        })
        assert not tc.is_error
        assert state.hypotheses["WH-001"].status == HypothesisStatus.ABANDONED
        assert state.hypotheses["WH-001"].iteration_modified == 3
        assert len(state.failed_approaches) == 1
        fa = state.failed_approaches[0]
        assert "WH-001" in fa.description
        assert "Spin prediction was wrong." in fa.reason
        assert fa.iteration == 3
        assert ex.mutations_applied

    def test_missing_id_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("abandon_hypothesis", {"id": "WH-099", "reason": "nope"})
        assert "not found" in tc.output


# ---------------------------------------------------------------------------
# promote_hypothesis
# ---------------------------------------------------------------------------

class TestPromoteHypothesis:
    def test_successful_promotion(self):
        ws = _make_workspace()
        state = _make_state_with_verified("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "COMP-001 verified spin-1.",
        })
        assert not tc.is_error
        assert "Promoted" in tc.output
        assert "ER-001" in tc.output
        # WH-001 gone, ER-001 present
        assert "WH-001" not in state.hypotheses
        assert "ER-001" in state.hypotheses
        h = state.hypotheses["ER-001"]
        assert h.status == HypothesisStatus.ESTABLISHED
        assert h.iteration_modified == 5
        assert h.statement == "First hypothesis"
        assert ex.mutations_applied

    def test_normalizes_references(self):
        """Promotion calls state.normalize_references() to update comp targets."""
        ws = _make_workspace()
        state = _make_state_with_verified("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Verified.",
        })
        # After normalize_references, COMP-001 target should become ER-001
        assert state.computations["COMP-001"].target_hypothesis == "ER-001"

    def test_blocked_by_refuted_no_verified(self):
        ws = _make_workspace()
        state = _make_state_with_refuted("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Should fail.",
        })
        assert "Error" in tc.output
        assert "REFUTED" in tc.output
        # State unchanged
        assert "WH-001" in state.hypotheses
        assert "ER-001" not in state.hypotheses
        assert not ex.mutations_applied

    def test_blocked_by_high_critique(self):
        ws = _make_workspace()
        state = _make_state_with_high_critique("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Should fail due to critique.",
        })
        assert "Error" in tc.output
        assert "CRIT-001" in tc.output
        assert "WH-001" in state.hypotheses
        assert not ex.mutations_applied

    def test_non_wh_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "ER-001",
            "justification": "Not a WH.",
        })
        assert "Error" in tc.output
        assert "not a WH" in tc.output
        assert not ex.mutations_applied

    def test_missing_id_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-099",
            "justification": "Doesn't exist.",
        })
        assert "Error" in tc.output or "not found" in tc.output
        assert not ex.mutations_applied

    def test_refuted_plus_verified_allows_promotion(self):
        ws = _make_workspace()
        state = _make_state_with_refuted_and_verified("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "COMP-002 verified despite earlier REFUTED.",
        })
        assert not tc.is_error
        assert "Promoted" in tc.output
        assert "ER-001" in state.hypotheses
        assert "WH-001" not in state.hypotheses


# ---------------------------------------------------------------------------
# resolve_critique
# ---------------------------------------------------------------------------

class TestResolveCritique:
    def test_resolves_critique(self):
        ws = _make_workspace()
        state = _make_state()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.HIGH, status=CritiqueStatus.ACTIVE,
            targets=["WH-001"], argument="Spin prediction may be wrong.",
        )
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("resolve_critique", {
            "critique_id": "CRIT-001",
            "resolution": "Fixed spin prediction to spin-0.",
        })
        assert not tc.is_error
        c = state.critiques["CRIT-001"]
        assert c.status == CritiqueStatus.RESOLVED
        assert c.resolution == "Fixed spin prediction to spin-0."
        assert c.iteration_resolved == 5
        assert "CRIT-001" in ex.resolved_critique_ids
        assert ex.mutations_applied

    def test_missing_critique_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("resolve_critique", {
            "critique_id": "CRIT-999",
            "resolution": "Doesn't exist.",
        })
        assert "not found" in tc.output


# ---------------------------------------------------------------------------
# update_section
# ---------------------------------------------------------------------------

class TestUpdateSection:
    def test_updates_conventions(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("update_section", {
            "section": "Conventions",
            "content": "- Natural units: h-bar = c = k_B = 1\n- Metric signature: (-,+,+,+)",
        })
        assert not tc.is_error
        assert "Natural units" in state.conventions
        assert ex.mutations_applied

    def test_updates_open_questions(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("update_section", {
            "section": "Open Questions",
            "content": "- Is string theory testable?",
        })
        assert not tc.is_error
        assert "string theory" in state.open_questions
        assert ex.mutations_applied

    def test_unknown_section_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("update_section", {
            "section": "Nonexistent",
            "content": "x",
        })
        assert "not found" in tc.output or "unknown" in tc.output


# ---------------------------------------------------------------------------
# set_next_task
# ---------------------------------------------------------------------------

class TestSetNextTask:
    def test_stores_task_data(self):
        ws = _make_workspace()
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
        ws = _make_workspace()
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
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("nonexistent_tool", {})
        assert tc.is_error
        assert "Unknown tool" in tc.output


# ---------------------------------------------------------------------------
# No research_state
# ---------------------------------------------------------------------------

class TestNoResearchState:
    def test_mutation_tools_return_error_without_state(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=None)

        mutation_calls = [
            ("add_hypothesis", {"statement": "Test"}),
            ("update_hypothesis", {"id": "WH-001"}),
            ("abandon_hypothesis", {"id": "WH-001", "reason": "test"}),
            ("promote_hypothesis", {"id": "WH-001", "justification": "test"}),
            ("resolve_critique", {"critique_id": "CRIT-001", "resolution": "test"}),
            ("update_section", {"section": "Conventions", "content": "test"}),
        ]
        for tool_name, tool_input in mutation_calls:
            tc = ex.execute(tool_name, tool_input)
            assert "no research state" in tc.output, (
                f"{tool_name} should error without research_state"
            )

    def test_set_next_task_works_without_state(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=None)
        tc = ex.execute("set_next_task", {
            "task_type": "research",
            "description": "Continue.",
        })
        assert not tc.is_error
        assert ex.task_data is not None
        assert ex.stop_after_round


class TestDependencyGraph:
    """Tests for depends_on in add_hypothesis and promotion dependency guardrail (B4)."""

    def test_add_hypothesis_with_depends_on(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_hypothesis", {
            "statement": "Depends on WH-001",
            "depends_on": ["WH-001"],
        })
        assert not tc.is_error
        new_id = "WH-003"
        assert new_id in state.hypotheses
        assert state.hypotheses[new_id].depends_on == ["WH-001"]

    def test_promotion_blocked_by_unestablished_dependency(self):
        ws = _make_workspace()
        state = _make_state_with_verified("WH-002")
        state.hypotheses["WH-002"].depends_on = ["WH-001"]
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-002",
            "justification": "Evidence from COMP-001",
        })
        assert tc.is_error or "unestablished" in tc.output.lower()
        assert "WH-002" in state.hypotheses  # not promoted

    def test_promotion_allowed_when_dependency_established(self):
        ws = _make_workspace()
        state = _make_state_with_verified("WH-002")
        # Promote WH-001 to ER-001 first
        state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        del state.hypotheses["WH-001"]
        state.hypotheses["WH-002"].depends_on = ["ER-001"]
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-002",
            "justification": "Verified by COMP-001, dependency ER-001 established",
        })
        assert not tc.is_error
        assert "ER-002" in state.hypotheses
        assert state.hypotheses["ER-002"].promotion_justification == "Verified by COMP-001, dependency ER-001 established"

    def test_promotion_blocked_without_verification_evidence(self):
        """Promotion requires at least one VERIFIED verify/research_verify computation."""
        ws = _make_workspace()
        state = _make_state()  # no computations
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Just because",
        })
        assert "no VERIFIED computation" in tc.output

    def test_promotion_allowed_with_research_verify_evidence(self):
        """research_verify kind counts as verification evidence."""
        ws = _make_workspace()
        state = _make_state()
        state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED, kind="research_verify",
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Verified by analytical review",
        })
        assert not tc.is_error
        assert "ER-001" in state.hypotheses

    def test_promotion_stores_justification(self):
        ws = _make_workspace()
        state = _make_state_with_verified("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Strong evidence from COMP-001",
        })
        assert not tc.is_error
        assert "ER-001" in state.hypotheses
        assert state.hypotheses["ER-001"].promotion_justification == "Strong evidence from COMP-001"


class TestResearchQuestionTools:
    """Tests for add_research_question and resolve_research_question tools."""

    def test_add_research_question(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=2, research_state=state)
        tc = ex.execute("add_research_question", {
            "question": "What is the entropy correction?",
            "context": "Needed for WH-002",
        })
        assert not tc.is_error
        assert "RQ-001" in tc.output
        assert "RQ-001" in state.research_questions
        rq = state.research_questions["RQ-001"]
        assert rq.question == "What is the entropy correction?"
        assert rq.context == "Needed for WH-002"
        assert ex.mutations_applied

    def test_resolve_research_question(self):
        from sciralph.research_state import ResearchQuestion, RQStatus
        ws = _make_workspace()
        state = _make_state()
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is F(p)?",
            iteration_created=1,
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("resolve_research_question", {
            "id": "RQ-001",
            "resolved_to": ["WH-003"],
        })
        assert not tc.is_error
        rq = state.research_questions["RQ-001"]
        assert rq.status == RQStatus.RESOLVED
        assert rq.resolved_to == ["WH-003"]
        assert rq.iteration_resolved == 3

    def test_resolve_missing_rq_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("resolve_research_question", {
            "id": "RQ-999",
            "resolved_to": [],
        })
        assert "not found" in tc.output
