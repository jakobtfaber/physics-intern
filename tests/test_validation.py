"""Tests for validation.py — full post-integration validation pipeline and termination gates."""

from sciralph.validation import (
    Violation,
    ViolationSeverity,
    validate_post_integration,
    can_terminate,
    check_er_demotion_safety,
    check_phantom_labels,
    check_phantom_references,
    check_id_consistency,
    check_stale_unverified_labels,
    check_verified_frontmatter_backfill,
    check_critique_resolution_consistency,
    _build_task_comp_mapping,
)
from sciralph.markdown import render_frontmatter


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class MockWorkspace:
    """Simple mock workspace for validation tests."""

    def __init__(self, files: dict[str, str] | None = None):
        self._files = files or {}

    def read_file(self, filename: str) -> str:
        return self._files.get(filename, "")

    def write_file(self, filename: str, content: str):
        self._files[filename] = content


class MockMetrics:
    """Minimal mock for MetricsTracker with last_critic_iteration."""

    def __init__(self, last_critic_iteration: int = 0):
        self.last_critic_iteration = last_critic_iteration


# ---------------------------------------------------------------------------
# Violation dataclass tests (kept from original)
# ---------------------------------------------------------------------------

class TestViolation:
    def test_creation(self):
        v = Violation(
            check="test_check", severity=ViolationSeverity.ERROR,
            message="Test message", file="TEST.md",
        )
        assert v.check == "test_check"
        assert v.severity == ViolationSeverity.ERROR

    def test_frozen(self):
        v = Violation(check="x", severity=ViolationSeverity.WARNING, message="y", file="z")
        try:
            v.check = "new"
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_detail_default(self):
        v = Violation(check="c", severity=ViolationSeverity.ERROR, message="m", file="f")
        assert v.detail == ""

    def test_detail_set(self):
        v = Violation(check="c", severity=ViolationSeverity.ERROR, message="m", file="f", detail="ER-001")
        assert v.detail == "ER-001"


# ---------------------------------------------------------------------------
# check_er_demotion_safety
# ---------------------------------------------------------------------------

class TestCheckErDemotionSafety:
    def _comp_log_with_verified(self, er_id: str) -> str:
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        body = f"""# Computations

## COMP-001

**CLAIM**: Verify {er_id} Hawking temperature derivation
**VERDICT**: VERIFIED
**RESULT**:
Computation confirms the result.
"""
        return render_frontmatter(meta, body)

    def _comp_log_with_refuted(self, er_id: str) -> str:
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        body = f"""# Computations

## COMP-001

**CLAIM**: Verify {er_id} Hawking temperature derivation
**VERDICT**: REFUTED
**RESULT**:
Computation refutes the result.
"""
        return render_frontmatter(meta, body)

    def _state_with_er(self, er_id: str) -> str:
        return f"""# Working Hypotheses (WH) and Established Results (ER)

## {er_id} Hawking Temperature

T = hbar * kappa / (2 pi k_B)
"""

    def test_er_without_verified_comp_demoted(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": self._state_with_er("ER-001"),
            "COMPUTATION_LOG.md": self._comp_log_with_refuted("ER-001"),
        })
        violations = check_er_demotion_safety(ws)

        assert len(violations) == 1
        assert violations[0].severity == ViolationSeverity.WARNING
        assert violations[0].check == "er_demotion_safety"
        assert "REFUTED" in violations[0].message or "demoted" in violations[0].message
        assert "ER-001" in violations[0].detail
        # State should now have WH-001
        assert "## WH-001" in ws.read_file("RESEARCH_STATE.md")
        assert "## ER-001" not in ws.read_file("RESEARCH_STATE.md")

    def test_er_with_verified_comp_passes(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": self._state_with_er("ER-001"),
            "COMPUTATION_LOG.md": self._comp_log_with_verified("ER-001"),
        })
        violations = check_er_demotion_safety(ws)
        assert len(violations) == 0

    def test_er_with_verified_via_wh_alias(self):
        """ER-003 backed by a COMP entry mentioning WH-003 should pass."""
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        body = """# Computations

## COMP-001

**CLAIM**: Verify WH-003 partition function
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": self._state_with_er("ER-003"),
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_er_demotion_safety(ws)
        assert len(violations) == 0

    def test_no_ers_no_violations(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "# Working Hypotheses (WH) and Established Results (ER)\n\n## WH-001 something\n",
            "COMPUTATION_LOG.md": "",
        })
        violations = check_er_demotion_safety(ws)
        assert len(violations) == 0

    def test_empty_comp_log_no_demotion(self):
        """No REFUTED computation => ER stays (no demotion without explicit REFUTED)."""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": self._state_with_er("ER-005"),
            "COMPUTATION_LOG.md": "",
        })
        violations = check_er_demotion_safety(ws)
        assert len(violations) == 0
        assert "## ER-005" in ws.read_file("RESEARCH_STATE.md")

    def test_multiple_ers_no_demotion_without_refuted(self):
        """Two ERs: one has VERIFIED backing, one has no computations at all.
        Without explicit REFUTED, neither is demoted."""
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Good Result

All verified.

## ER-002 Bad Result

Not verified.
"""
        meta = {"total_computations": 1}
        body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001 derivation
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_er_demotion_safety(ws)
        assert len(violations) == 0
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "## ER-001" in updated  # kept
        assert "## ER-002" in updated  # kept (no REFUTED)


# ---------------------------------------------------------------------------
# check_phantom_labels
# ---------------------------------------------------------------------------

class TestCheckPhantomLabels:
    def test_unsubstantiated_verified_stripped(self):
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Some result

This result is VERIFIED by computation (ER-001).
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",  # no computations at all
        })
        violations = check_phantom_labels(ws)
        assert len(violations) == 1
        assert violations[0].check == "phantom_labels"
        assert violations[0].severity == ViolationSeverity.ERROR
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "[unverified]" in updated
        assert "VERIFIED" not in updated.split("##")[0]  # body text, not headers

    def test_real_verified_untouched(self):
        meta = {"total_computations": 1}
        body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001 Hawking temperature
**VERDICT**: VERIFIED
**RESULT**:
Correct.
"""
        state = """# Working Hypotheses (WH) and Established Results (ER)

This ER-001 result has been VERIFIED by COMP-001.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_phantom_labels(ws)
        assert len(violations) == 0
        # State unchanged
        assert "VERIFIED" in ws.read_file("RESEARCH_STATE.md")

    def test_no_ids_in_line_left_alone(self):
        """Lines with VERIFIED but no ER/WH IDs are left as prose."""
        state = "The method has been VERIFIED experimentally.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
        })
        violations = check_phantom_labels(ws)
        assert len(violations) == 0
        assert ws.read_file("RESEARCH_STATE.md") == state

    def test_header_lines_untouched(self):
        """## headers containing VERIFIED should not be modified."""
        state = "## ER-001 VERIFIED Result\n\nBody text.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
        })
        violations = check_phantom_labels(ws)
        assert len(violations) == 0

# ---------------------------------------------------------------------------
# check_phantom_references
# ---------------------------------------------------------------------------

class TestCheckPhantomReferences:
    def test_orphaned_ref_replaced(self):
        state = "Result backed by COMP-999 and TASK-888.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
        })
        violations = check_phantom_references(ws)
        assert len(violations) == 2
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "[COMP-999:unverified]" in updated
        assert "[TASK-888:unverified]" in updated

    def test_valid_ref_untouched(self):
        meta = {"total_computations": 1}
        body = """# Computations

## COMP-001

**CLAIM**: test
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        state = "Result backed by COMP-001.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_phantom_references(ws)
        assert len(violations) == 0
        assert ws.read_file("RESEARCH_STATE.md") == state

    def test_no_refs_no_violations(self):
        state = "Just some text with no COMP or TASK references.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
        })
        violations = check_phantom_references(ws)
        assert len(violations) == 0

    def test_mixed_valid_and_phantom(self):
        meta = {"total_computations": 1}
        body = """# Computations

## COMP-001

**CLAIM**: test
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        state = "See COMP-001 and also COMP-042 for details.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_phantom_references(ws)
        assert len(violations) == 1
        assert violations[0].detail == "COMP-042"
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "COMP-001" in updated  # valid, kept
        assert "[COMP-042:unverified]" in updated


# ---------------------------------------------------------------------------
# check_id_consistency
# ---------------------------------------------------------------------------

class TestCheckIdConsistency:
    def test_counter_mismatch_fixed(self):
        meta = {"total_computations": 0, "last_computation": "2026-03-10"}
        body = """# Computations

## COMP-001

**CLAIM**: test
**VERDICT**: VERIFIED
**RESULT**:
OK.

## COMP-002

**CLAIM**: test2
**VERDICT**: REFUTED
**RESULT**:
Fail.
"""
        ws = MockWorkspace({
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_id_consistency(ws)
        assert len(violations) == 1
        assert violations[0].severity == ViolationSeverity.WARNING
        assert "frontmatter=0" in violations[0].message
        assert "actual=2" in violations[0].message

    def test_consistent_no_violation(self):
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        body = """# Computations

## COMP-001

**CLAIM**: test
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        ws = MockWorkspace({
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_id_consistency(ws)
        assert len(violations) == 0

    def test_empty_comp_log_no_violation(self):
        ws = MockWorkspace({})
        violations = check_id_consistency(ws)
        assert len(violations) == 0

    def test_overcount_also_fixed(self):
        """Frontmatter says 5 but only 1 entry exists."""
        meta = {"total_computations": 5, "last_computation": "2026-03-10"}
        body = """# Computations

## COMP-001

**CLAIM**: test
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        ws = MockWorkspace({
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_id_consistency(ws)
        assert len(violations) == 1
        assert "frontmatter=5" in violations[0].message
        assert "actual=1" in violations[0].message

    def test_task_headers_counted(self):
        """TASK-NNN entries are counted — the renderer now uses TASK-NNN IDs."""
        meta = {"total_computations": 0, "last_computation": "2026-03-10"}
        body = """# Computations

## TASK-002: Computation

**CLAIM**: Verify heat capacity
**VERDICT**: VERIFIED
**RESULT**:
OK.

## TASK-003: Computation

**CLAIM**: Verify partition function
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        ws = MockWorkspace({
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_id_consistency(ws)
        assert len(violations) == 1
        assert "actual=2" in violations[0].message
        updated = ws.read_file("COMPUTATION_LOG.md")
        from sciralph.markdown import parse_frontmatter
        updated_meta, _ = parse_frontmatter(updated)
        assert updated_meta["total_computations"] == 2


# ---------------------------------------------------------------------------
# validate_post_integration (pipeline)
# ---------------------------------------------------------------------------

class TestValidatePostIntegration:
    def test_pipeline_runs_all_checks(self):
        """A workspace with multiple issues should trigger violations from different checks."""
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Unverified result

Backed by COMP-999 which doesn't exist.
"""
        # Add a REFUTED comp entry so er_demotion_safety triggers demotion
        meta_comp = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001 derivation
**VERDICT**: REFUTED
**RESULT**:
Failed.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta_comp, comp_body),
        })
        violations = validate_post_integration(ws)
        assert len(violations) > 0
        checks_triggered = {v.check for v in violations}
        # Should include phantom_references (COMP-999), er_demotion_safety (ER-001 REFUTED)
        assert "phantom_references" in checks_triggered
        assert "er_demotion_safety" in checks_triggered

    def test_empty_workspace_no_violations(self):
        ws = MockWorkspace({})
        violations = validate_post_integration(ws)
        assert len(violations) == 0

    def test_clean_workspace_no_violations(self):
        """A properly structured workspace should have no violations."""
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001 Hawking temperature
**VERDICT**: VERIFIED
**RESULT**:
T = hbar * kappa / (2 pi k_B). Correct.
"""
        state_meta = {"status": "in_progress", "verified_results": ["ER-001"]}
        state_body = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Hawking Temperature

T = hbar * kappa / (2 pi k_B)

Backed by COMP-001.
"""
        task = render_frontmatter(
            {"task_id": "TASK-002", "task_type": "research_explore", "assigned_to": "research_explore"},
            "Continue research.\n",
        )
        ws = MockWorkspace({
            "RESEARCH_STATE.md": render_frontmatter(state_meta, state_body),
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
            "CURRENT_TASK.md": task,
        })
        violations = validate_post_integration(ws)
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# can_terminate
# ---------------------------------------------------------------------------

class TestCanTerminate:
    def _base_config(self):
        """Return a minimal config-like object (unused by can_terminate but required by signature)."""
        return object()

    def _empty_rs(self):
        from sciralph.research_state import ResearchState
        return ResearchState()

    def _rs_with_er(self, er_id="ER-001"):
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus
        rs = ResearchState()
        rs.hypotheses[er_id] = Hypothesis(
            id=er_id, statement="Result", status=HypothesisStatus.ESTABLISHED,
        )
        return rs

    def _rs_with_wh(self, wh_id="WH-001"):
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus
        rs = ResearchState()
        rs.hypotheses[wh_id] = Hypothesis(
            id=wh_id, statement="Hypothesis", status=HypothesisStatus.WORKING,
        )
        return rs

    def _empty_ws(self):
        return MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })

    def test_clean_termination_allowed(self):
        """No ERs, no HIGH critiques, no numerical requirement => allowed."""
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=self._empty_rs())
        assert allowed is True
        assert blockers == []

    def test_blocked_by_no_critic_pass(self):
        """If VERIFIED computations exist but no critic pass has occurred, termination blocked."""
        meta = {"total_computations": 1}
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM**: Verify WH-001\n**VERDICT**: VERIFIED\n**RESULT**:\nOK.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            ws, self._base_config(), metrics, research_state=self._rs_with_er())
        assert allowed is False
        assert any("critic pass" in b.lower() for b in blockers)

    def test_allowed_with_critic_pass_done(self):
        """VERIFIED computations exist and a critic pass has occurred => gate 1 passes."""
        meta = {"total_computations": 1}
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM**: Verify WH-001\n**VERDICT**: VERIFIED\n**RESULT**:\nOK.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=3)
        allowed, blockers = can_terminate(
            ws, self._base_config(), metrics, research_state=self._rs_with_er())
        assert allowed is True

    def test_blocked_by_unresolved_high(self):
        critique = """# Active Critiques

## CRIT-001 [HIGH] [UNRESOLVED]

Something is wrong.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": critique,
        })
        metrics = MockMetrics(last_critic_iteration=2)
        allowed, blockers = can_terminate(
            ws, self._base_config(), metrics, research_state=self._empty_rs())
        assert allowed is False
        assert any("HIGH" in b for b in blockers)

    def test_medium_critique_does_not_block(self):
        critique = """# Active Critiques

## CRIT-001 [MEDIUM] [UNRESOLVED]

Minor issue.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": critique,
        })
        metrics = MockMetrics(last_critic_iteration=2)
        allowed, blockers = can_terminate(
            ws, self._base_config(), metrics, research_state=self._empty_rs())
        assert allowed is True

    def test_blocked_by_no_computations_when_required(self):
        metrics = MockMetrics(last_critic_iteration=2)
        problem_meta = {"requires_numerical": True}
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, problem_meta,
            research_state=self._empty_rs())
        assert allowed is False
        assert any("numerical" in b.lower() for b in blockers)

    def test_allowed_when_requires_numerical_false(self):
        metrics = MockMetrics(last_critic_iteration=0)
        problem_meta = {"requires_numerical": False}
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, problem_meta,
            research_state=self._empty_rs())
        assert allowed is True

    def test_allowed_with_computations_when_required(self):
        meta = {"total_computations": 1}
        body = """# Computations

## COMP-001

**CLAIM**: test
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=2)
        problem_meta = {"requires_numerical": True}
        allowed, blockers = can_terminate(
            ws, self._base_config(), metrics, problem_meta,
            research_state=self._empty_rs())
        assert allowed is True

    def test_multiple_blockers(self):
        """Multiple gates fail at once."""
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus, Computation, Verdict
        critique = """# Active Critiques

## CRIT-001 [HIGH] [UNRESOLVED]

Big problem.
"""
        meta = {"total_computations": 1}
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM**: Verify WH-001\n**VERDICT**: VERIFIED\n**RESULT**:\nOK.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
            "CRITIQUE_LOG.md": critique,
        })
        rs = ResearchState()
        rs.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", statement="Something", status=HypothesisStatus.ESTABLISHED,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        problem_meta = {"requires_numerical": True}
        allowed, blockers = can_terminate(
            ws, self._base_config(), metrics, problem_meta, research_state=rs)
        assert allowed is False
        # Should have at least 2 blockers: no critic, HIGH critique
        # (Gate 3 passes because comp_log has entries)
        assert len(blockers) >= 2

    def test_none_problem_meta_treated_as_empty(self):
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, None,
            research_state=self._empty_rs())
        assert allowed is True

    def test_bold_format_er_detected_for_critic_gate(self):
        """VERIFIED computation should trigger the critic-pass gate regardless of header format."""
        meta = {"total_computations": 1}
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM**: Verify ER-001 partition function\n**VERDICT**: VERIFIED\n**RESULT**:\nOK.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            ws, self._base_config(), metrics, research_state=self._rs_with_er())
        assert allowed is False
        assert any("critic pass" in b.lower() for b in blockers)

    def test_blocked_by_verified_comp_without_critic(self):
        """WHs only (no ERs) but VERIFIED computation exists => blocked by both critic gate and Gate 4."""
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus, Computation, Verdict
        meta = {"total_computations": 1}
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM**: Verify WH-001 partition function\n**VERDICT**: VERIFIED\n**RESULT**:\nOK.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
            "CRITIQUE_LOG.md": "",
        })
        rs = ResearchState()
        rs.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Partition Function", status=HypothesisStatus.WORKING,
        )
        rs.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001", verdict=Verdict.VERIFIED, iteration=1,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            ws, self._base_config(), metrics, research_state=rs)
        assert allowed is False
        assert any("critic pass" in b.lower() for b in blockers)
        assert any("WH-001" in b for b in blockers)


class TestCanTerminateGate5:
    """Gate 5: All RQs and WHs must be resolved/abandoned before termination (ResearchState path)."""

    def _base_config(self):
        return object()

    def _empty_ws(self):
        return MockWorkspace({
            "RESEARCH_STATE.md": "",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })

    def test_open_rq_blocks_termination(self):
        from sciralph.research_state import ResearchState, ResearchQuestion, RQStatus
        rs = ResearchState()
        rs.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is F(p)?", status=RQStatus.OPEN,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is False
        assert any("RQ-001" in b and "OPEN" in b for b in blockers)

    def test_resolved_rq_does_not_block(self):
        from sciralph.research_state import ResearchState, ResearchQuestion, RQStatus
        rs = ResearchState()
        rs.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="What is F(p)?", status=RQStatus.RESOLVED,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is True

    def test_abandoned_rq_does_not_block(self):
        from sciralph.research_state import ResearchState, ResearchQuestion, RQStatus
        rs = ResearchState()
        rs.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="Dead end", status=RQStatus.ABANDONED,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is True

    def test_working_wh_blocks_termination(self):
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus
        rs = ResearchState()
        rs.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="F(p) is rational", status=HypothesisStatus.WORKING,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is False
        assert any("WH-001" in b and "working hypothesis" in b.lower() for b in blockers)

    def test_working_wh_with_verified_backing_gives_specific_message(self):
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus, Computation, Verdict
        rs = ResearchState()
        rs.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="F(p) = ...", status=HypothesisStatus.WORKING,
        )
        rs.computations["TASK-001"] = Computation(
            id="TASK-001", target_hypothesis="WH-001", verdict=Verdict.VERIFIED,
            iteration=1,
        )
        metrics = MockMetrics(last_critic_iteration=3)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is False
        assert any("WH-001" in b and "VERIFIED" in b for b in blockers)

    def test_established_hypothesis_does_not_block(self):
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus
        rs = ResearchState()
        rs.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", statement="Proven", status=HypothesisStatus.ESTABLISHED,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is True

    def test_abandoned_hypothesis_does_not_block(self):
        from sciralph.research_state import ResearchState, Hypothesis, HypothesisStatus
        rs = ResearchState()
        rs.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", statement="Wrong", status=HypothesisStatus.ABANDONED,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is True

    def test_mixed_entities_all_resolved_allows_termination(self):
        from sciralph.research_state import (
            ResearchState, Hypothesis, HypothesisStatus,
            ResearchQuestion, RQStatus,
        )
        rs = ResearchState()
        rs.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="Q1", status=RQStatus.RESOLVED,
        )
        rs.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", statement="Result", status=HypothesisStatus.ESTABLISHED,
        )
        rs.hypotheses["WH-002"] = Hypothesis(
            id="WH-002", statement="Dead end", status=HypothesisStatus.ABANDONED,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is True

    def test_mixed_entities_some_dangling_blocks(self):
        from sciralph.research_state import (
            ResearchState, Hypothesis, HypothesisStatus,
            ResearchQuestion, RQStatus,
        )
        rs = ResearchState()
        rs.research_questions["RQ-001"] = ResearchQuestion(
            id="RQ-001", question="Q1", status=RQStatus.RESOLVED,
        )
        rs.research_questions["RQ-002"] = ResearchQuestion(
            id="RQ-002", question="Q2", status=RQStatus.OPEN,
        )
        rs.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", statement="Result", status=HypothesisStatus.ESTABLISHED,
        )
        rs.hypotheses["WH-003"] = Hypothesis(
            id="WH-003", statement="Dangling", status=HypothesisStatus.WORKING,
        )
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is False
        assert any("RQ-002" in b for b in blockers)
        assert any("WH-003" in b for b in blockers)
        assert len(blockers) == 2

    def test_empty_research_state_allows_termination(self):
        from sciralph.research_state import ResearchState
        rs = ResearchState()
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(
            self._empty_ws(), self._base_config(), metrics, research_state=rs)
        assert allowed is True


class TestErDemotionSafetyBoldFormat:
    """Test that check_er_demotion_safety handles bold ER entries."""

    def test_bold_er_detected_and_demoted(self):
        state = "**ER-001 — Partition Function Z**\nBody.\n"
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM**: Verify ER-001\n**VERDICT**: REFUTED\n**RESULT**:\nFailed.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_er_demotion_safety(ws)
        assert len(violations) == 1
        assert "demoted" in violations[0].message
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "**WH-001" in updated
        assert "**ER-001" not in updated

    def test_bold_er_with_verified_passes(self):
        state = "**ER-001 — Partition Function Z**\nBody.\n"
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM**: Verify ER-001\n**VERDICT**: VERIFIED\n**RESULT**:\nOK.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_er_demotion_safety(ws)
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Regression: colon-inside-bold verdict + WH IDs only in entry body
# ---------------------------------------------------------------------------

class TestErDemotionSafetyColonInsideBold:
    """Reproduce the QHO termination bug: **VERDICT:** format + WH IDs on bullet lines."""

    def test_verdict_colon_inside_bold_with_body_ids(self):
        """ER promotion should pass when VERDICT uses colon-inside-bold
        and WH IDs appear in the body (not the claim line)."""
        state = "## ER-001 Partition Function\nZ = exp(-x/2)/(1-exp(-x))\n"
        meta = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001: Verification

**CLAIM:** Four working hypotheses for QHO thermodynamics:
- WH-001: Z = exp(-x/2)/(1-exp(-x))
- WH-002: mean energy

**RESULT:**
All checks passed.

**VERDICT:** VERIFIED for all four working hypotheses (WH-001, WH-002).
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_er_demotion_safety(ws)
        assert len(violations) == 0, (
            "ER-001 should NOT be demoted — WH-001 is VERIFIED in the entry body"
        )


# ---------------------------------------------------------------------------
# Improvement 2: phantom flattening + TASK→COMP mapping
# ---------------------------------------------------------------------------

class TestBuildTaskCompMapping:
    """Tests for _build_task_comp_mapping helper."""

    def test_basic_mapping(self):
        entries = [
            {"id": "TASK-005", "claim": "test", "verdict": "", "result": "", "body": ""},
            {"id": "COMP-005", "claim": "test", "verdict": "VERIFIED", "result": "", "body": "from TASK-005"},
        ]
        mapping = _build_task_comp_mapping(entries)
        assert "TASK-005" in mapping
        assert "COMP-005" in mapping["TASK-005"]

    def test_no_matching_comp(self):
        entries = [
            {"id": "TASK-005", "claim": "test", "verdict": "", "result": "", "body": ""},
        ]
        mapping = _build_task_comp_mapping(entries)
        assert mapping == {}

    def test_comp_body_references_task(self):
        entries = [
            {"id": "COMP-010", "claim": "verify something", "verdict": "VERIFIED", "result": "", "body": "Based on TASK-003"},
        ]
        mapping = _build_task_comp_mapping(entries)
        assert "TASK-003" in mapping
        assert "COMP-010" in mapping["TASK-003"]


class TestPhantomReferencesFlattening:
    """Tests for phantom reference check with bracket flattening."""

    def test_phantom_flattens_nested(self):
        """Nested brackets get flattened before phantom detection."""
        state = "Result backed by [[COMP-001:unverified]:unverified].\n"
        meta = {"total_computations": 1}
        body = "# Computations\n\n## COMP-001\n\n**CLAIM**: test\n**VERDICT**: VERIFIED\n**RESULT**:\nOK.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_phantom_references(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        # Nested brackets should be flattened
        assert "[[" not in updated
        # COMP-001 is valid, so it should be kept (as [COMP-001:unverified] from flattening)
        assert "COMP-001" in updated

    def test_phantom_idempotent(self):
        """Running phantom check twice produces the same result."""
        state = "Result backed by COMP-999.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
        })
        check_phantom_references(ws)
        first_pass = ws.read_file("RESEARCH_STATE.md")
        check_phantom_references(ws)
        second_pass = ws.read_file("RESEARCH_STATE.md")
        assert first_pass == second_pass

    def test_task_accepted_when_comp_exists(self):
        """TASK-005 is accepted (not phantom) when COMP-005 exists."""
        meta = {"total_computations": 1}
        body = "# Computations\n\n## TASK-005\n\nPreamble.\n\n## COMP-005\n\n**CLAIM**: test\n**VERDICT**: VERIFIED\n**RESULT**:\nOK.\n"
        state = "See TASK-005 for the computation result.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_phantom_references(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        # TASK-005 should NOT be marked as phantom
        assert "[TASK-005:unverified]" not in updated
        assert "TASK-005" in updated


# ---------------------------------------------------------------------------
# Improvement 4: label propagation
# ---------------------------------------------------------------------------

class TestDemotionProsePropagation:
    """Tests for ER→WH prose reference propagation on demotion (Fix 6A)."""

    def test_demotion_propagates_all_prose_references(self):
        """When ER→WH demotion happens, prose references also get updated."""
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Hawking Temperature

T = hbar * kappa / (2 pi k_B)

This result ER-001 depends on the surface gravity calculation.
See ER-001 in the synthesis section.
"""
        # REFUTED computation triggers demotion
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001 Hawking temperature
**VERDICT**: REFUTED
**RESULT**:
Failed.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_er_demotion_safety(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        # All ER-001 references should be WH-001 now
        assert "ER-001" not in updated
        assert "WH-001" in updated
        assert "This result WH-001" in updated

    def test_frontmatter_verified_results_normalized_after_demotion(self):
        """verified_results entries are renamed ER→WH after demotion."""
        state_meta = {"status": "in_progress", "verified_results": ["ER-001"]}
        state_body = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Partition Function

Z = exp(-x/2)/(1-exp(-x))
"""
        state = render_frontmatter(state_meta, state_body)
        # REFUTED computation triggers demotion
        meta_comp = {"total_computations": 1, "last_computation": "2026-03-10"}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001 Partition Function
**VERDICT**: REFUTED
**RESULT**:
Failed.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta_comp, comp_body),
        })
        violations = check_er_demotion_safety(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        from sciralph.markdown import parse_frontmatter as pf
        meta, _ = pf(updated)
        # ER-001 should be WH-001 in verified_results
        vr = meta.get("verified_results", [])
        assert "WH-001" in vr
        assert "ER-001" not in vr


class TestBackfillUsesPromotedForm:
    """Tests for backfill using current header form (Fix 6C)."""

    def test_backfill_uses_promoted_er_form(self):
        """When WH-001 was promoted to ER-001, backfill uses ER-001."""
        meta_comp = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify WH-001 partition function
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        state_meta = {"status": "in_progress"}
        state_body = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Partition Function

Body.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": render_frontmatter(state_meta, state_body),
            "COMPUTATION_LOG.md": render_frontmatter(meta_comp, comp_body),
        })
        violations = check_verified_frontmatter_backfill(ws)
        assert len(violations) == 1
        from sciralph.markdown import parse_frontmatter as pf
        meta, _ = pf(ws.read_file("RESEARCH_STATE.md"))
        vr = meta.get("verified_results", [])
        # Should use ER-001 (promoted form) not WH-001
        assert "ER-001" in vr
        assert "WH-001" not in vr


class TestErPromotionProsePropagation:
    """Tests for WH→ER prose reference propagation (Improvement 4A)."""

    def test_promotion_updates_unverified_tags(self):
        """Stale unverified labels get WH→ER rename when promoted ER header exists."""
        meta = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify WH-001 partition function
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Partition Function

Body.

Some text with WH-001 [unverified] result.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_stale_unverified_labels(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        # WH-001 should become ER-001 and [unverified] should become VERIFIED
        assert "VERIFIED" in updated
        assert "[unverified]" not in updated.lower()


class TestVerifiedFrontmatterBackfill:
    """Tests for verified_results frontmatter backfill (Improvement 4C)."""

    def test_verified_frontmatter_backfill(self):
        meta_comp = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001 and WH-002 results
**VERDICT**: VERIFIED
**RESULT**:
All checks pass.
"""
        state_meta = {"status": "in_progress"}
        # Include ER-001 section header so backfill syncs it into verified_results
        state_body = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Some Result

Some findings.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": render_frontmatter(state_meta, state_body),
            "COMPUTATION_LOG.md": render_frontmatter(meta_comp, comp_body),
        })
        violations = check_verified_frontmatter_backfill(ws)
        assert len(violations) == 1
        from sciralph.markdown import parse_frontmatter
        updated_meta, _ = parse_frontmatter(ws.read_file("RESEARCH_STATE.md"))
        assert "ER-001" in updated_meta.get("verified_results", [])

    def test_backfill_idempotent(self):
        meta_comp = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        state_meta = {"status": "in_progress", "verified_results": ["ER-001"]}
        # Include ER-001 section header so verified_results stays in sync
        state_body = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Some Result

Body.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": render_frontmatter(state_meta, state_body),
            "COMPUTATION_LOG.md": render_frontmatter(meta_comp, comp_body),
        })
        violations = check_verified_frontmatter_backfill(ws)
        assert len(violations) == 0

    def test_stale_label_renames_wh_to_er(self):
        """When promoting [unverified] to VERIFIED, WH→ER rename happens if ER header exists."""
        meta_comp = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify WH-002 mean energy
**VERDICT**: VERIFIED
**RESULT**:
OK.
"""
        state = """---
status: in_progress
---

# Working Hypotheses (WH) and Established Results (ER)

## ER-002 Mean Energy

E = hbar*omega*(n + 1/2)

Reference: WH-002 [unverified] computation confirms.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta_comp, comp_body),
        })
        violations = check_stale_unverified_labels(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "VERIFIED" in updated
        assert "[unverified]" not in updated.lower()
        # WH-002 should be renamed to ER-002 since ER-002 header exists
        # Check the reference line was updated
        assert "ER-002" in updated


# ---------------------------------------------------------------------------
# Fix 5: Critique resolution consistency check
# ---------------------------------------------------------------------------

class TestCritiqueResolutionConsistency:
    """Tests for check_critique_resolution_consistency (Fix 5)."""

    def test_resolved_critique_with_consistent_labels_no_violation(self):
        """Resolved critique where labels are consistent -> no violation."""
        critique_log = """# Active Critiques

# Resolved Critiques

## CRIT-001 [HIGH] [RESOLVED]

**Target:** ER-001
The label inconsistency between WH-001 and ER-001 has been resolved.
"""
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Hawking Temperature

T = hbar * kappa / (2 pi k_B)
"""
        ws = MockWorkspace({
            "CRITIQUE_LOG.md": critique_log,
            "RESEARCH_STATE.md": state,
        })
        violations = check_critique_resolution_consistency(ws)
        assert len(violations) == 0

    def test_resolved_label_critique_with_wh_er_coexistence(self):
        """Resolved label critique but WH/ER co-exist -> violation."""
        critique_log = """# Active Critiques

# Resolved Critiques

## CRIT-001 [HIGH] [RESOLVED]

**Target:** ER-001
Label inconsistency: header uses ER-001 but prose uses WH-001.
"""
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Hawking Temperature

T = hbar * kappa / (2 pi k_B)

See WH-001 for the original derivation.
"""
        ws = MockWorkspace({
            "CRITIQUE_LOG.md": critique_log,
            "RESEARCH_STATE.md": state,
        })
        violations = check_critique_resolution_consistency(ws)
        assert len(violations) >= 1
        assert any("co-exist" in v.message for v in violations)

    def test_resolved_critique_target_vanished(self):
        """Resolved critique whose target vanished entirely -> violation."""
        critique_log = """# Active Critiques

# Resolved Critiques

## CRIT-002 [MEDIUM] [RESOLVED]

**Target:** WH-005
Math error in WH-005 derivation has been corrected.
"""
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Hawking Temperature

Only ER-001 exists here.
"""
        ws = MockWorkspace({
            "CRITIQUE_LOG.md": critique_log,
            "RESEARCH_STATE.md": state,
        })
        violations = check_critique_resolution_consistency(ws)
        assert len(violations) >= 1
        assert any("no longer appears" in v.message for v in violations)

    def test_non_label_critique_no_false_positive(self):
        """Non-label critique (math error) does not trigger label co-existence check."""
        critique_log = """# Active Critiques

# Resolved Critiques

## CRIT-003 [HIGH] [RESOLVED]

**Target:** ER-001
Mathematical sign error in the entropy derivation for ER-001.
"""
        # WH-001 and ER-001 both exist but critique is about math, not labels
        state = """# Working Hypotheses (WH) and Established Results (ER)

## ER-001 Hawking Temperature

T = hbar * kappa / (2 pi k_B)

Original derivation started as WH-001.
"""
        ws = MockWorkspace({
            "CRITIQUE_LOG.md": critique_log,
            "RESEARCH_STATE.md": state,
        })
        violations = check_critique_resolution_consistency(ws)
        # No label co-existence violation (math error, not label critique)
        label_violations = [v for v in violations if "co-exist" in v.message]
        assert len(label_violations) == 0

    def test_empty_critique_log_no_violation(self):
        """Empty critique log -> no violation."""
        ws = MockWorkspace({
            "CRITIQUE_LOG.md": "",
            "RESEARCH_STATE.md": "## ER-001 Something\n",
        })
        violations = check_critique_resolution_consistency(ws)
        assert len(violations) == 0

    def test_no_resolved_section_no_violation(self):
        """Critique log without resolved section -> no violation."""
        critique_log = """# Active Critiques

## CRIT-001 [HIGH] [UNRESOLVED]

Something is wrong.
"""
        ws = MockWorkspace({
            "CRITIQUE_LOG.md": critique_log,
            "RESEARCH_STATE.md": "## ER-001 Something\n",
        })
        violations = check_critique_resolution_consistency(ws)
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# ER demotion safety with ResearchState registry (Phase 2)
# ---------------------------------------------------------------------------

class TestERDemotionSafetyWithRegistry:
    """Test that check_er_demotion_safety uses formal registry when available."""

    def test_registry_based_demotion(self):
        """ER without VERIFIED in registry is demoted."""
        from sciralph.research_state import ResearchState, Computation, Verdict

        state = "## ER-001 — Test Result\n\nBody.\n"
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM:** Verify ER-001\n**VERDICT:** REFUTED\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter({"total_computations": 1}, comp_body),
        })
        rs = ResearchState()
        rs.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="ER-001", verdict=Verdict.REFUTED,
        )
        violations = check_er_demotion_safety(ws, research_state=rs)
        assert len(violations) == 1
        assert "demoted" in violations[0].message
        assert "## WH-001" in ws.read_file("RESEARCH_STATE.md")

    def test_none_registry_falls_back_to_substring(self):
        """When research_state is None, substring matching still works.
        ER-001 with VERIFIED comp and no REFUTED => no demotion."""
        state = "## ER-001 — Test\n\nBody.\n"
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM:** Verify ER-001\n**VERDICT:** VERIFIED\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter({"total_computations": 1}, comp_body),
        })
        violations = check_er_demotion_safety(ws, research_state=None)
        assert len(violations) == 0  # ER-001 stays (no REFUTED)
