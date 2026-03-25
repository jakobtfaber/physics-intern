"""Tests for OrchestratorToolExecutor — state-mutation tools for the orchestrator."""

from unittest.mock import MagicMock

from sciralph.orchestrator_tools import OrchestratorToolExecutor
from sciralph.research_state import (
    ResearchState, Hypothesis, HypothesisStatus, Verdict,
    Critique, Severity, CritiqueStatus, FailedApproach,
    Evidence, ReviewResult,
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
    """State with a VERIFIED review result on *target*."""
    state = _make_state()
    state.hypotheses[target].review = ReviewResult(
        verdict=Verdict.VERIFIED, summary=f"Verified {target}", iteration=3,
    )
    return state


def _make_state_with_refuted(target: str = "WH-001") -> ResearchState:
    """State with a REFUTED review result on *target*."""
    state = _make_state()
    state.hypotheses[target].review = ReviewResult(
        verdict=Verdict.REFUTED, summary=f"Refuted {target}", iteration=3,
    )
    return state


def _make_state_with_refuted_and_verified(target: str = "WH-001") -> ResearchState:
    """State with a VERIFIED review result on *target* (supersedes earlier refutation)."""
    state = _make_state()
    state.hypotheses[target].review = ReviewResult(
        verdict=Verdict.VERIFIED, summary=f"Verified {target} (corrected)", iteration=4,
    )
    return state


def _make_state_with_high_critique(target: str = "WH-001") -> ResearchState:
    """State with VERIFIED review + unresolved HIGH critique targeting *target*."""
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
        from sciralph.research_state import ResearchQuestion, RQStatus
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-002"].status = HypothesisStatus.ESTABLISHED
        state.research_questions["RQ-003"] = ResearchQuestion(
            id="RQ-003", question="What is the third result?",
            iteration_created=2,
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_hypothesis", {
            "statement": "Third hypothesis",
            "derivation": "Some derivation.",
            "from_rq": "RQ-003",
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
        # RQ should be auto-resolved
        assert state.research_questions["RQ-003"].status == RQStatus.RESOLVED

    def test_rejects_without_from_rq(self):
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-002"].status = HypothesisStatus.ESTABLISHED
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_hypothesis", {
            "statement": "Third hypothesis",
            "derivation": "Some derivation.",
        })
        assert "from_rq is required" in tc.output

    def test_blocked_by_wh_cap(self):
        """Cannot create WH when >= 2 working hypotheses exist."""
        from sciralph.research_state import ResearchQuestion
        ws = _make_workspace()
        state = _make_state()  # 2 working WHs
        state.research_questions["RQ-003"] = ResearchQuestion(
            id="RQ-003", question="Q?", iteration_created=1,
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_hypothesis", {
            "statement": "Blocked", "from_rq": "RQ-003",
        })
        assert "already 2 working hypotheses" in tc.output
        assert "WH-003" not in state.hypotheses

    def test_blocked_by_unresolved_critiques(self):
        """Cannot create WH when unresolved critiques exist."""
        from sciralph.research_state import Critique, CritiqueStatus, ResearchQuestion, Severity
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-002"].status = HypothesisStatus.ESTABLISHED
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-001"], severity=Severity.HIGH,
            status=CritiqueStatus.ACTIVE, argument="Issue.",
        )
        state.research_questions["RQ-003"] = ResearchQuestion(
            id="RQ-003", question="Q?", iteration_created=1,
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_hypothesis", {
            "statement": "Blocked", "from_rq": "RQ-003",
        })
        assert "unresolved critique" in tc.output


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
        assert fa.derivation_excerpt == "Photon has spin-1."
        assert ex.mutations_applied

    def test_abandon_populates_related_entities(self):
        """Abandoning a hypothesis populates related_entities with the hypothesis ID."""
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        ex.execute("abandon_hypothesis", {
            "id": "WH-001", "reason": "Refuted.",
        })
        fa = state.failed_approaches[0]
        assert fa.related_entities == ["WH-001"]

    def test_abandon_long_derivation_truncated(self):
        """Long derivation is truncated to 300 chars in derivation_excerpt."""
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-001"].derivation = "A" * 500
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        ex.execute("abandon_hypothesis", {
            "id": "WH-001", "reason": "Too long.",
        })
        fa = state.failed_approaches[0]
        assert len(fa.derivation_excerpt) == 300

    def test_missing_id_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("abandon_hypothesis", {"id": "WH-099", "reason": "nope"})
        assert "not found" in tc.output

    def test_warns_when_dependents_exist(self):
        ws = _make_workspace()
        state = _make_state()
        # WH-002 depends on WH-001
        state.hypotheses["WH-002"].depends_on = ["WH-001"]
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("abandon_hypothesis", {
            "id": "WH-001",
            "reason": "Dead end.",
        })
        assert not tc.is_error
        assert state.hypotheses["WH-001"].status == HypothesisStatus.ABANDONED
        assert "Warning" in tc.output
        assert "WH-002" in tc.output

    def test_no_warning_when_dependent_already_abandoned(self):
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-002"].depends_on = ["WH-001"]
        state.hypotheses["WH-002"].status = HypothesisStatus.ABANDONED
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("abandon_hypothesis", {
            "id": "WH-001",
            "reason": "Dead end.",
        })
        assert not tc.is_error
        assert "Warning" not in tc.output


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
        """Promotion calls state.normalize_references() to update depends_on."""
        ws = _make_workspace()
        state = _make_state_with_verified("WH-001")
        # WH-002 depends on WH-001 — should be updated to ER-001 after promotion
        state.hypotheses["WH-002"].depends_on = ["WH-001"]
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Verified.",
        })
        # After normalize_references, depends_on should point to ER-001
        assert state.hypotheses["WH-002"].depends_on == ["ER-001"]

    def test_blocked_by_refuted_no_verified(self):
        ws = _make_workspace()
        state = _make_state_with_refuted("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Should fail.",
        })
        assert "Error" in tc.output
        assert "no VERIFIED" in tc.output
        # State unchanged
        assert "WH-001" in state.hypotheses
        assert "ER-001" not in state.hypotheses
        assert not ex.mutations_applied

    def test_succeeds_despite_high_critique(self):
        """HIGH critiques no longer block promotion."""
        ws = _make_workspace()
        state = _make_state_with_high_critique("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Promote despite HIGH critique.",
        })
        assert not tc.is_error
        assert "Promoted" in tc.output
        assert "ER-001" in state.hypotheses
        assert "WH-001" not in state.hypotheses
        assert ex.mutations_applied

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

    def test_promote_result_is_simple(self):
        """Promote tool result is a short confirmation (guidance moves to injection)."""
        ws = _make_workspace()
        state = ResearchState()
        state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Only hypothesis",
            status=HypothesisStatus.WORKING, derivation="Derivation.",
            iteration_created=1, iteration_modified=1,
        )
        state.hypotheses["WH-001"].review = ReviewResult(
            verdict=Verdict.VERIFIED, summary="Verified", iteration=3,
        )
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "All checks pass.",
        })
        assert not tc.is_error
        assert tc.output == "Promoted WH-001 → ER-001."


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

    def test_already_resolved_critique_is_idempotent(self):
        ws = _make_workspace()
        state = _make_state()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.LOW, status=CritiqueStatus.RESOLVED,
            targets=["ER-001"], argument="Minor issue.",
            resolution="Already fixed.", iteration_resolved=4,
        )
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("resolve_critique", {
            "critique_id": "CRIT-001",
            "resolution": "Attempting to re-resolve.",
        })
        assert not tc.is_error
        assert "already resolved" in tc.output
        # State unchanged
        c = state.critiques["CRIT-001"]
        assert c.resolution == "Already fixed."
        assert c.iteration_resolved == 4
        assert not ex.mutations_applied

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

class TestAppendConvention:
    def test_appends_conventions(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("append_convention", {
            "content": "- Natural units: h-bar = c = k_B = 1\n- Metric signature: (-,+,+,+)",
        })
        assert not tc.is_error
        assert "Natural units" in state.conventions
        assert ex.mutations_applied

    def test_conventions_append_to_existing(self):
        """Conventions are append-only — new content is appended, not replaced."""
        ws = _make_workspace()
        state = _make_state()
        state.conventions = "Existing conventions."
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("append_convention", {
            "content": "New convention added.",
        })
        assert not tc.is_error
        assert "Existing conventions." in state.conventions
        assert "New convention added." in state.conventions

    def test_empty_content_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("append_convention", {"content": "  "})
        assert "empty" in tc.output.lower()


class TestUpdateStrategy:
    def test_strategy_replaces(self):
        """Strategy can be updated by the orchestrator."""
        ws = _make_workspace()
        state = _make_state()
        state.strategy = "Original strategy."
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("update_strategy", {
            "content": "Focus on surface gravity approach first.",
        })
        assert "Updated" in tc.output
        assert state.strategy == "Focus on surface gravity approach first."


# ---------------------------------------------------------------------------
# append_note
# ---------------------------------------------------------------------------

class TestAppendNote:
    def test_appends_note_to_state(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=4, research_state=state)
        tc = ex.execute("append_note", {
            "text": "The sign convention must be checked.",
        })
        assert not tc.is_error
        assert len(state.research_notes) == 1
        assert state.research_notes[0]["text"] == "The sign convention must be checked."
        assert state.research_notes[0]["iteration"] == 4
        assert ex.mutations_applied

    def test_empty_note_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=4, research_state=state)
        tc = ex.execute("append_note", {"text": "   "})
        assert "empty" in tc.output.lower()
        assert not ex.mutations_applied

    def test_append_note_no_state_returns_error(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=4, research_state=None)
        tc = ex.execute("append_note", {"text": "test"})
        assert "no research state" in tc.output


# ---------------------------------------------------------------------------
# Dispatch tools
# ---------------------------------------------------------------------------

class TestDispatchTools:
    def test_dispatch_researcher_stores_task_data(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-001",
            "description": "Derive result.",
        })
        assert not tc.is_error
        assert ex.task_data["task_type"] == "research"

    def test_dispatch_computer_stores_task_data(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("dispatch_computer", {
            "target_claim": "WH-001",
            "description": "Compute numerically.",
        })
        assert not tc.is_error
        assert ex.task_data["task_type"] == "compute"

    def test_request_termination_sets_stop(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3)
        assert not ex.stop_after_round
        ex.execute("request_termination", {
            "reason": "Done.",
            "answer_ers": ["ER-001", "ER-003"],
        })
        assert ex.stop_after_round
        assert ex.task_data["task_type"] == "terminate"
        assert ex.task_data["answer_ers"] == ["ER-001", "ER-003"]

    def test_request_termination_without_reason(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3)
        tc = ex.execute("request_termination", {"answer_ers": []})
        assert not tc.is_error
        assert ex.stop_after_round
        assert ex.task_data["description"] == "Research complete."

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
            ("update_strategy", {"content": "test"}),
            ("append_convention", {"content": "test"}),
        ]
        for tool_name, tool_input in mutation_calls:
            tc = ex.execute(tool_name, tool_input)
            assert "no research state" in tc.output, (
                f"{tool_name} should error without research_state"
            )

    def test_dispatch_works_without_state(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=None)
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "RQ-001",
            "description": "Continue.",
        })
        assert not tc.is_error
        assert ex.task_data is not None
        assert ex.stop_after_round

    def test_request_termination_works_without_state(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=None)
        tc = ex.execute("request_termination", {})
        assert not tc.is_error
        assert ex.stop_after_round


class TestDependencyGraph:
    """Tests for depends_on in add_hypothesis and promotion dependency guardrail (B4)."""

    def test_add_hypothesis_with_depends_on(self):
        from sciralph.research_state import ResearchQuestion
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-002"].status = HypothesisStatus.ESTABLISHED
        state.research_questions["RQ-003"] = ResearchQuestion(
            id="RQ-003", question="Derived from WH-001?",
            iteration_created=2,
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_hypothesis", {
            "statement": "Depends on WH-001",
            "depends_on": ["WH-001"],
            "from_rq": "RQ-003",
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

    def test_promotion_blocked_without_review_evidence(self):
        """Promotion requires a VERIFIED review result."""
        ws = _make_workspace()
        state = _make_state()  # no review
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("promote_hypothesis", {
            "id": "WH-001",
            "justification": "Just because",
        })
        assert "no VERIFIED review" in tc.output

    def test_promotion_allowed_with_review_result(self):
        """A VERIFIED review result allows promotion."""
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-001"].review = ReviewResult(
            verdict=Verdict.VERIFIED, summary="Analytical review passed", iteration=2,
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
        state = _make_state()  # has WH-001, WH-002
        ex = OrchestratorToolExecutor(ws, iteration=2, research_state=state)
        tc = ex.execute("add_research_question", {
            "question": "What is the entropy correction?",
            "context": "Needed for WH-002",
        })
        assert not tc.is_error
        # Shared counter: next number after WH-001/WH-002 is 003
        assert "RQ-003" in tc.output
        assert "RQ-003" in state.research_questions
        rq = state.research_questions["RQ-003"]
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
            "reason": "Answered by WH-003",
        })
        assert not tc.is_error
        rq = state.research_questions["RQ-001"]
        assert rq.status == RQStatus.RESOLVED
        assert rq.resolved_to == []  # not populated by this tool
        assert rq.iteration_resolved == 3
        assert rq.resolution_reason == "Answered by WH-003"

    def test_resolve_research_question_no_reason(self):
        from sciralph.research_state import ResearchQuestion, RQStatus
        ws = _make_workspace()
        state = _make_state()
        state.research_questions["RQ-002"] = ResearchQuestion(
            id="RQ-002", question="Is X > 0?",
            iteration_created=1,
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("resolve_research_question", {"id": "RQ-002"})
        assert not tc.is_error
        rq = state.research_questions["RQ-002"]
        assert rq.status == RQStatus.RESOLVED
        assert rq.iteration_resolved == 3

    def test_resolve_missing_rq_returns_error(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("resolve_research_question", {"id": "RQ-999"})
        assert "not found" in tc.output

    def test_resolve_already_resolved_rq_is_idempotent(self):
        """Re-resolving an already-resolved RQ returns early without mutation."""
        from sciralph.research_state import ResearchQuestion, RQStatus
        ws = _make_workspace()
        state = _make_state()
        state.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is F(p)?",
            status=RQStatus.RESOLVED,
            iteration_created=1,
            iteration_resolved=2,
            resolution_reason="Answered during exploration",
        )
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("resolve_research_question", {
            "id": "RQ-001",
            "reason": "Trying to close again",
        })
        assert "already resolved" in tc.output
        rq = state.research_questions["RQ-001"]
        # Original resolution preserved
        assert rq.iteration_resolved == 2
        assert rq.resolution_reason == "Answered during exploration"
        # Should not count as a mutation
        assert not ex.mutations_applied

    def test_add_rq_blocked_by_cap(self):
        """Cannot create RQ when >= 3 open RQs exist."""
        from sciralph.research_state import ResearchQuestion
        ws = _make_workspace()
        state = _make_state()
        for i in range(3, 6):
            rq_id = f"RQ-{i:03d}"
            state.research_questions[rq_id] = ResearchQuestion(
                id=rq_id, question=f"Q{i}?", iteration_created=1,
            )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_research_question", {"question": "One more?"})
        assert "already 3 open RQs" in tc.output

    def test_add_rq_blocked_by_unresolved_critiques(self):
        """Cannot create RQ when unresolved critiques exist."""
        from sciralph.research_state import Critique, CritiqueStatus, Severity
        ws = _make_workspace()
        state = _make_state()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-001"], severity=Severity.HIGH,
            status=CritiqueStatus.ACTIVE, argument="Issue.",
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("add_research_question", {"question": "Blocked?"})
        assert "unresolved critique" in tc.output

    def test_add_hypothesis_from_already_resolved_rq_blocked(self):
        """Creating a WH from an already-resolved RQ is rejected."""
        from sciralph.research_state import ResearchQuestion, RQStatus
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-002"].status = HypothesisStatus.ESTABLISHED
        state.research_questions["RQ-003"] = ResearchQuestion(
            id="RQ-003", question="What is the entropy?",
            status=RQStatus.RESOLVED,
            iteration_created=1,
            iteration_resolved=2,
            resolution_reason="Answered during exploration",
        )
        ex = OrchestratorToolExecutor(ws, iteration=5, research_state=state)
        tc = ex.execute("add_hypothesis", {
            "statement": "S = 4 pi M^2",
            "from_rq": "RQ-003",
        })
        assert "already resolved" in tc.output
        # No WH created
        assert "WH-003" not in state.hypotheses


# ---------------------------------------------------------------------------
# Dispatch gate
# ---------------------------------------------------------------------------

class TestDispatchGate:
    """Tests for dispatch tool behavior with mutations."""

    @staticmethod
    def _state_with_open_rq():
        """State with WH-001 (working), WH-002 (established), and an open RQ-003 for add_hypothesis calls."""
        from sciralph.research_state import ResearchQuestion
        state = _make_state()
        state.hypotheses["WH-002"].status = HypothesisStatus.ESTABLISHED
        state.research_questions["RQ-003"] = ResearchQuestion(
            id="RQ-003", question="Placeholder for dispatch test",
            iteration_created=2,
        )
        return state

    def test_add_hypothesis_acts_as_dispatch(self):
        """add_hypothesis sets stop_after_round and task_data for auto-review."""
        ws = _make_workspace()
        state = self._state_with_open_rq()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        tc = ex.execute("add_hypothesis", {"statement": "New claim", "from_rq": "RQ-003"})
        assert not tc.is_error
        assert "WH-003" in state.hypotheses
        assert ex.stop_after_round
        assert ex.task_data is not None
        assert ex.task_data["task_type"] == "review"
        assert ex.task_data["target_claim"] == "WH-003"
        assert "Review will be dispatched automatically" in tc.output

    def test_multiple_entity_mutations_in_same_round(self):
        """Multiple entity-creating mutations in the same response all execute."""
        ws = _make_workspace()
        state = self._state_with_open_rq()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        tc1 = ex.execute("add_hypothesis", {"statement": "First", "from_rq": "RQ-003"})
        assert not tc1.is_error
        tc2 = ex.execute("add_research_question", {"question": "Question?"})
        assert not tc2.is_error
        assert "WH-003" in state.hypotheses
        assert "RQ-004" in state.research_questions

    def test_dispatch_alone_succeeds(self):
        """dispatch_researcher succeeds when no mutations occurred this round."""
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-001",
            "description": "Derive result.",
        })
        assert "Dispatched" in tc.output
        assert ex.task_data is not None
        assert ex.stop_after_round

    def test_dispatch_works_across_rounds(self):
        """Mutation in round 1 → begin_round → dispatch in round 2 succeeds."""
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        # Round 1: mutation only
        ex.execute("update_hypothesis", {"id": "WH-001", "statement": "Updated"})

        # Round 2: dispatch
        ex.begin_round()
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-001",
            "description": "Derive result.",
        })
        assert "Dispatched" in tc.output
        assert ex.stop_after_round
        assert ex.task_data["target_claim"] == "WH-001"

    def test_tools_in_prior_round_dont_block_dispatch(self):
        """Tool calls in an earlier response don't affect dispatch in a later one."""
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        # Round 1: mutations only
        ex.execute("update_hypothesis", {"id": "WH-001", "statement": "Updated"})
        assert ex._calls_this_round == 1

        # Round 2: new response, dispatch only
        ex.begin_round()
        assert ex._calls_this_round == 0
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-001",
            "description": "Derive result.",
        })
        assert "Dispatched" in tc.output
        assert ex.stop_after_round

    def test_non_entity_mutations_plus_dispatch_succeed(self):
        """update + resolve_critique + update_section + dispatch all succeed."""
        ws = _make_workspace()
        state = _make_state()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.HIGH, status=CritiqueStatus.ACTIVE,
            targets=["WH-001"], argument="Issue.",
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        ex.execute("update_hypothesis", {
            "id": "WH-001", "statement": "Updated title",
        })
        ex.execute("resolve_critique", {
            "critique_id": "CRIT-001", "resolution": "Fixed.",
        })
        ex.execute("update_section", {
            "section": "Conventions", "content": "Natural units.",
        })
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-001",
            "description": "Derive result.",
        })
        assert "Dispatched" in tc.output
        assert ex.stop_after_round
        assert ex.task_data is not None

    def test_promote_plus_dispatch_succeed(self):
        """promote + resolve_critique + dispatch in same round all succeed."""
        ws = _make_workspace()
        state = _make_state_with_verified("WH-001")
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.HIGH, status=CritiqueStatus.ACTIVE,
            targets=["WH-001"], argument="Issue.",
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        ex.execute("promote_hypothesis", {
            "id": "WH-001", "justification": "Verified by reviewer.",
        })
        ex.execute("resolve_critique", {
            "critique_id": "CRIT-001", "resolution": "Promoted WH-001.",
        })
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-002",
            "description": "Derive result.",
        })
        assert "Dispatched" in tc.output
        assert ex.stop_after_round


# ---------------------------------------------------------------------------
# State injection
# ---------------------------------------------------------------------------

class TestStateInjection:
    """Tests for the state injection rendered by end_round()."""

    def test_render_state_injection_after_mutations(self):
        """Injection includes applied mutations and state snapshot."""
        ws = _make_workspace()
        state = _make_state_with_verified("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        # Round 1: mutations
        ex.execute("promote_hypothesis", {
            "id": "WH-001", "justification": "Reviewer verified.",
        })

        # end_round returns injection for THIS round's mutations
        injection = ex.end_round()
        assert injection is not None
        assert "Promoted WH-001 → ER-001" in injection
        assert "State snapshot" in injection
        assert "ER-001" in injection

    def test_render_state_injection_none_without_mutations(self):
        """No injection when no mutations occurred."""
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        injection = ex.end_round()
        assert injection is None

    def test_injection_shows_unreviewed_wh(self):
        """WH with evidence + no review → guidance line in injection."""
        ws = _make_workspace()
        state = _make_state()
        state.hypotheses["WH-001"].evidence = [Evidence(
            type="research", summary="Some evidence", iteration=2,
        )]
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        # Trigger a mutation so injection fires
        ex.execute("append_note", {"text": "Test note"})
        injection = ex.end_round()
        assert injection is not None
        assert "WH-001 awaiting auto-review" in injection

    def test_injection_shows_verified_promote_guidance(self):
        """WH with VERIFIED review → promote guidance in injection."""
        ws = _make_workspace()
        state = _make_state_with_verified("WH-001")
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        ex.execute("append_note", {"text": "Test note"})
        injection = ex.end_round()
        assert injection is not None
        assert "WH-001 is VERIFIED but not yet promoted" in injection

    def test_injection_shows_budget_pressure(self):
        """Low iterations remaining → budget message in injection."""
        ws = _make_workspace()
        state = _make_state()
        # Promote WH-001 to ER so we have an established result
        state.hypotheses["WH-001"].review = ReviewResult(
            verdict=Verdict.VERIFIED, summary="Verified", iteration=2,
        )
        ex = OrchestratorToolExecutor(
            ws, iteration=18, research_state=state,
            max_iterations=20, budget_synthesis_margin=3,
        )

        ex.execute("promote_hypothesis", {
            "id": "WH-001", "justification": "Verified.",
        })
        injection = ex.end_round()
        assert injection is not None
        assert "BUDGET" in injection
        assert "2 iteration(s) remaining" in injection

    def test_injection_shows_completion_ready(self):
        """All resolved → terminate guidance in injection."""
        ws = _make_workspace()
        state = ResearchState()
        for i in range(1, 4):
            state.hypotheses[f"ER-{i:03d}"] = Hypothesis(
                id=f"ER-{i:03d}", status=HypothesisStatus.ESTABLISHED,
            )
        ex = OrchestratorToolExecutor(
            ws, iteration=5, research_state=state,
            min_er_for_completion=3,
        )

        ex.execute("append_note", {"text": "Final check"})
        injection = ex.end_round()
        assert injection is not None
        assert "All entities resolved" in injection
        assert "terminate" in injection.lower()

    def test_simplified_tool_results_no_nudge(self):
        """Tool results don't contain old _BATCH_NUDGE text."""
        ws = _make_workspace()
        state = _make_state()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", severity=Severity.HIGH, status=CritiqueStatus.ACTIVE,
            targets=["WH-001"], argument="Issue.",
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)

        nudge = "Call a dispatch tool"
        tc1 = ex.execute("add_hypothesis", {"statement": "Test"})
        assert nudge not in tc1.output
        tc2 = ex.execute("resolve_critique", {
            "critique_id": "CRIT-001", "resolution": "Fixed.",
        })
        assert nudge not in tc2.output
        tc3 = ex.execute("update_section", {
            "section": "Conventions", "content": "Natural units.",
        })
        assert nudge not in tc3.output
        tc4 = ex.execute("append_note", {"text": "Note"})
        assert nudge not in tc4.output


# ---------------------------------------------------------------------------
# Target claim validation
# ---------------------------------------------------------------------------

class TestTargetClaimValidation:
    """Tests for target_claim validation in dispatch tools."""

    def test_valid_wh_target_passes(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-001",
            "description": "Derive result.",
        })
        assert "Dispatched" in tc.output
        assert ex.stop_after_round

    def test_valid_rq_target_passes(self):
        from sciralph.research_state import ResearchQuestion
        ws = _make_workspace()
        state = _make_state()
        state.research_questions["RQ-003"] = ResearchQuestion(
            id="RQ-003", question="Test?", iteration_created=1,
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "RQ-003",
            "description": "Explore.",
        })
        assert "Dispatched" in tc.output
        assert ex.stop_after_round

    def test_invalid_target_rejected_with_listing(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-099",
            "description": "Verify.",
        })
        assert "Error" in tc.output
        assert "WH-099" in tc.output
        assert "WH-001" in tc.output  # listed as valid
        assert "WH-002" in tc.output
        assert ex.task_data is None
        assert not ex.stop_after_round

    def test_termination_skips_validation(self):
        ws = _make_workspace()
        state = _make_state()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("request_termination", {"reason": "Done."})
        assert "Termination" in tc.output
        assert ex.stop_after_round

    def test_valid_crit_target_passes(self):
        ws = _make_workspace()
        state = _make_state()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-001"], severity=Severity.HIGH,
            status=CritiqueStatus.ACTIVE, argument="Spin prediction may be wrong.",
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "CRIT-001",
            "description": "Investigate critique.",
        })
        assert "Dispatched" in tc.output
        assert ex.stop_after_round

    def test_invalid_crit_target_rejected(self):
        ws = _make_workspace()
        state = _make_state()
        state.critiques["CRIT-001"] = Critique(
            id="CRIT-001", targets=["WH-001"], severity=Severity.HIGH,
            status=CritiqueStatus.ACTIVE, argument="Spin prediction may be wrong.",
        )
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=state)
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "CRIT-099",
            "description": "Investigate critique.",
        })
        assert "Error" in tc.output
        assert "CRIT-099" in tc.output
        assert "CRIT-001" in tc.output  # listed as valid
        assert not ex.stop_after_round

    def test_skipped_when_research_state_is_none(self):
        ws = _make_workspace()
        ex = OrchestratorToolExecutor(ws, iteration=3, research_state=None)
        tc = ex.execute("dispatch_researcher", {
            "target_claim": "WH-099",
            "description": "Derive result.",
        })
        assert "Dispatched" in tc.output
        assert ex.stop_after_round
