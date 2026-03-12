"""Tests for validation.py — full post-integration validation pipeline and termination gates."""

from sciralph.validation import (
    Violation,
    ViolationSeverity,
    validate_post_integration,
    can_terminate,
    check_er_promotion_gate,
    check_phantom_labels,
    check_phantom_references,
    check_task_agent_routing,
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
# check_er_promotion_gate
# ---------------------------------------------------------------------------

class TestCheckErPromotionGate:
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
        return f"""# Established Results

## {er_id} Hawking Temperature

T = hbar * kappa / (2 pi k_B)
"""

    def test_er_without_verified_comp_demoted(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": self._state_with_er("ER-001"),
            "COMPUTATION_LOG.md": self._comp_log_with_refuted("ER-001"),
        })
        violations = check_er_promotion_gate(ws)

        assert len(violations) == 1
        assert violations[0].severity == ViolationSeverity.ERROR
        assert "demoted" in violations[0].message
        assert "ER-001" in violations[0].detail
        # State should now have WH-001
        assert "## WH-001" in ws.read_file("RESEARCH_STATE.md")
        assert "## ER-001" not in ws.read_file("RESEARCH_STATE.md")

    def test_er_with_verified_comp_passes(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": self._state_with_er("ER-001"),
            "COMPUTATION_LOG.md": self._comp_log_with_verified("ER-001"),
        })
        violations = check_er_promotion_gate(ws)
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
        violations = check_er_promotion_gate(ws)
        assert len(violations) == 0

    def test_no_ers_no_violations(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "# Working Hypotheses\n\n## WH-001 something\n",
            "COMPUTATION_LOG.md": "",
        })
        violations = check_er_promotion_gate(ws)
        assert len(violations) == 0

    def test_empty_comp_log_demotes(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": self._state_with_er("ER-005"),
            "COMPUTATION_LOG.md": "",
        })
        violations = check_er_promotion_gate(ws)
        assert len(violations) == 1
        assert "## WH-005" in ws.read_file("RESEARCH_STATE.md")

    def test_multiple_ers_partial_demotion(self):
        """Two ERs: one has VERIFIED backing, one does not."""
        state = """# Established Results

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
        violations = check_er_promotion_gate(ws)
        assert len(violations) == 1
        assert violations[0].detail == "ER-002"
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "## ER-001" in updated  # kept
        assert "## WH-002" in updated  # demoted
        assert "## ER-002" not in updated


# ---------------------------------------------------------------------------
# check_phantom_labels
# ---------------------------------------------------------------------------

class TestCheckPhantomLabels:
    def test_unsubstantiated_verified_stripped(self):
        state = """# Established Results

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
        state = """# Results

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

    def test_proposed_changes_also_checked(self):
        proposed = "WH-002 is VERIFIED per our analysis.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "",
            "PROPOSED_CHANGES.md": proposed,
            "COMPUTATION_LOG.md": "",
        })
        violations = check_phantom_labels(ws)
        assert len(violations) == 1
        assert violations[0].file == "PROPOSED_CHANGES.md"


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
# check_task_agent_routing
# ---------------------------------------------------------------------------

class TestCheckTaskAgentRouting:
    def _task_with_assigned(self, assigned_to: str) -> str:
        meta = {"task_id": "TASK-001", "task_type": "compute", "assigned_to": assigned_to}
        return render_frontmatter(meta, "Do something.\n")

    def test_alias_compute_resolved(self):
        ws = MockWorkspace({
            "CURRENT_TASK.md": self._task_with_assigned("compute"),
        })
        violations = check_task_agent_routing(ws)
        assert len(violations) == 1
        assert violations[0].severity == ViolationSeverity.WARNING
        assert "'compute'" in violations[0].message
        assert "'computationalist'" in violations[0].message
        # File rewritten with correct agent
        assert "computationalist" in ws.read_file("CURRENT_TASK.md")

    def test_alias_critique_resolved(self):
        ws = MockWorkspace({
            "CURRENT_TASK.md": self._task_with_assigned("critique"),
        })
        violations = check_task_agent_routing(ws)
        assert len(violations) == 1
        assert "'deep_critic'" in violations[0].message

    def test_alias_research_resolved(self):
        ws = MockWorkspace({
            "CURRENT_TASK.md": self._task_with_assigned("research"),
        })
        violations = check_task_agent_routing(ws)
        assert len(violations) == 1
        assert "'researcher'" in violations[0].message

    def test_alias_review_resolved(self):
        ws = MockWorkspace({
            "CURRENT_TASK.md": self._task_with_assigned("review"),
        })
        violations = check_task_agent_routing(ws)
        assert len(violations) == 1
        assert "'deep_critic'" in violations[0].message

    def test_valid_routing_no_violation(self):
        ws = MockWorkspace({
            "CURRENT_TASK.md": self._task_with_assigned("computationalist"),
        })
        violations = check_task_agent_routing(ws)
        assert len(violations) == 0

    def test_all_valid_agents_pass(self):
        for agent in ("orchestrator", "researcher", "computationalist", "deep_critic", "compressor"):
            ws = MockWorkspace({
                "CURRENT_TASK.md": self._task_with_assigned(agent),
            })
            violations = check_task_agent_routing(ws)
            assert len(violations) == 0, f"Agent '{agent}' should be valid"

    def test_unknown_agent_errors(self):
        ws = MockWorkspace({
            "CURRENT_TASK.md": self._task_with_assigned("magic_agent"),
        })
        violations = check_task_agent_routing(ws)
        assert len(violations) == 1
        assert violations[0].severity == ViolationSeverity.ERROR
        assert "'magic_agent'" in violations[0].message

    def test_empty_task_no_violation(self):
        ws = MockWorkspace({})
        violations = check_task_agent_routing(ws)
        assert len(violations) == 0


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

    def test_task_headers_excluded_from_count(self):
        """TASK-NNN headers in COMPUTATION_LOG should not be counted as computations."""
        meta = {"total_computations": 0, "last_computation": "2026-03-10"}
        body = """# Computations

## TASK-002: Computation

All checks resolve cleanly.

## COMP-002: Verification of QHO Heat Capacity

**CLAIM**: Verify heat capacity
**VERDICT**: VERIFIED
**RESULT**:
OK.

## TASK-003: Computation

More preamble.

## COMP-003: Partition Function Identity

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
        # Should count only COMP-002 and COMP-003, not TASK-002 and TASK-003
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
        state = """# Established Results

## ER-001 Unverified result

Backed by COMP-999 which doesn't exist.
"""
        task = render_frontmatter(
            {"task_id": "TASK-001", "task_type": "compute", "assigned_to": "compute"},
            "Do something.\n",
        )
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
            "CURRENT_TASK.md": task,
        })
        violations = validate_post_integration(ws)
        assert len(violations) > 0
        checks_triggered = {v.check for v in violations}
        # Should include phantom_references (COMP-999), er_promotion_gate (ER-001), task_agent_routing (compute alias)
        assert "phantom_references" in checks_triggered
        assert "er_promotion_gate" in checks_triggered
        assert "task_agent_routing" in checks_triggered

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
        state_body = """# Established Results

## ER-001 Hawking Temperature

T = hbar * kappa / (2 pi k_B)

Backed by COMP-001.
"""
        task = render_frontmatter(
            {"task_id": "TASK-002", "task_type": "research", "assigned_to": "researcher"},
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

    def test_clean_termination_allowed(self):
        """No ERs, no HIGH critiques, no numerical requirement => allowed."""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "# Results\n\nSome findings.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(ws, self._base_config(), metrics)
        assert allowed is True
        assert blockers == []

    def test_blocked_by_no_critic_pass(self):
        """If ERs exist but no critic pass has occurred, termination blocked."""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "## ER-001 Some result\n\nContent.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(ws, self._base_config(), metrics)
        assert allowed is False
        assert any("critic pass" in b.lower() for b in blockers)

    def test_allowed_with_critic_pass_done(self):
        """ERs exist and a critic pass has occurred => gate 1 passes."""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "## ER-001 Some result\n\nContent.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=3)
        allowed, blockers = can_terminate(ws, self._base_config(), metrics)
        assert allowed is True

    def test_blocked_by_unresolved_high(self):
        critique = """# Active Critiques

## CRIT-001 [HIGH] [UNRESOLVED]

Something is wrong.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "Some state.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": critique,
        })
        metrics = MockMetrics(last_critic_iteration=2)
        allowed, blockers = can_terminate(ws, self._base_config(), metrics)
        assert allowed is False
        assert any("HIGH" in b for b in blockers)

    def test_medium_critique_does_not_block(self):
        critique = """# Active Critiques

## CRIT-001 [MEDIUM] [UNRESOLVED]

Minor issue.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "Some state.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": critique,
        })
        metrics = MockMetrics(last_critic_iteration=2)
        allowed, blockers = can_terminate(ws, self._base_config(), metrics)
        assert allowed is True

    def test_blocked_by_no_computations_when_required(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "Some state.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=2)
        problem_meta = {"requires_numerical": True}
        allowed, blockers = can_terminate(ws, self._base_config(), metrics, problem_meta)
        assert allowed is False
        assert any("numerical" in b.lower() for b in blockers)

    def test_allowed_when_requires_numerical_false(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "Some state.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=0)
        problem_meta = {"requires_numerical": False}
        allowed, blockers = can_terminate(ws, self._base_config(), metrics, problem_meta)
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
            "RESEARCH_STATE.md": "Some state.\n",
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=2)
        problem_meta = {"requires_numerical": True}
        allowed, blockers = can_terminate(ws, self._base_config(), metrics, problem_meta)
        assert allowed is True

    def test_multiple_blockers(self):
        """Multiple gates fail at once."""
        critique = """# Active Critiques

## CRIT-001 [HIGH] [UNRESOLVED]

Big problem.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "## ER-001 Something\n\nContent.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": critique,
        })
        metrics = MockMetrics(last_critic_iteration=0)
        problem_meta = {"requires_numerical": True}
        allowed, blockers = can_terminate(ws, self._base_config(), metrics, problem_meta)
        assert allowed is False
        # Should have at least 3 blockers: no critic, HIGH critique, no computations
        assert len(blockers) >= 3

    def test_none_problem_meta_treated_as_empty(self):
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "Some state.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(ws, self._base_config(), metrics, None)
        assert allowed is True

    def test_bold_format_er_detected_for_critic_gate(self):
        """ERs in bold format (**ER-NNN**) should trigger the critic-pass gate."""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": "**ER-001 — Partition Function**\nBody.\n",
            "COMPUTATION_LOG.md": "",
            "CRITIQUE_LOG.md": "",
        })
        metrics = MockMetrics(last_critic_iteration=0)
        allowed, blockers = can_terminate(ws, self._base_config(), metrics)
        assert allowed is False
        assert any("critic pass" in b.lower() for b in blockers)


class TestErPromotionGateBoldFormat:
    """Test that check_er_promotion_gate handles bold ER entries."""

    def test_bold_er_detected_and_demoted(self):
        state = "**ER-001 — Partition Function Z**\nBody.\n"
        meta = {"total_computations": 1, "last_computation": "2026-03-10"}
        comp_body = "# Computations\n\n## COMP-001\n\n**CLAIM**: Verify ER-001\n**VERDICT**: REFUTED\n**RESULT**:\nFailed.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_er_promotion_gate(ws)
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
        violations = check_er_promotion_gate(ws)
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Regression: colon-inside-bold verdict + WH IDs only in entry body
# ---------------------------------------------------------------------------

class TestErPromotionGateColonInsideBold:
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
        violations = check_er_promotion_gate(ws)
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
        state = """# Established Results

## ER-001 Hawking Temperature

T = hbar * kappa / (2 pi k_B)

This result ER-001 depends on the surface gravity calculation.
See ER-001 in the synthesis section.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",  # no computations -> demotion
        })
        violations = check_er_promotion_gate(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        # All ER-001 references should be WH-001 now
        assert "ER-001" not in updated
        assert "WH-001" in updated
        assert "This result WH-001" in updated

    def test_frontmatter_verified_results_normalized_after_demotion(self):
        """verified_results entries are renamed ER→WH after demotion."""
        state_meta = {"status": "in_progress", "verified_results": ["ER-001"]}
        state_body = """# Established Results

## ER-001 Partition Function

Z = exp(-x/2)/(1-exp(-x))
"""
        state = render_frontmatter(state_meta, state_body)
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",  # no computations -> demotion
        })
        violations = check_er_promotion_gate(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        from sciralph.markdown import parse_frontmatter as pf
        meta, _ = pf(updated)
        # ER-001 should be WH-001 in verified_results
        vr = meta.get("verified_results", [])
        assert "WH-001" in vr
        assert "ER-001" not in vr

    def test_frontmatter_verified_results_normalized_after_promotion(self):
        """verified_results entries are renamed WH→ER after promotion."""
        state_meta = {"status": "in_progress", "verified_results": ["WH-003"]}
        state_body = """# Established Results

## WH-003 Hawking Temperature

T = hbar * kappa / (2 pi k_B)

Also see ER-003 in the synthesis section.
"""
        meta_comp = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify WH-003 Hawking temperature
**VERDICT**: VERIFIED
**RESULT**:
Correct.
"""
        state = render_frontmatter(state_meta, state_body)
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta_comp, comp_body),
        })
        violations = check_er_promotion_gate(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        from sciralph.markdown import parse_frontmatter as pf
        meta, _ = pf(updated)
        vr = meta.get("verified_results", [])
        assert "ER-003" in vr
        assert "WH-003" not in vr


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
        state_body = """# Established Results

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

    def test_promotion_propagates_prose(self):
        """When WH→ER promotion happens, prose references also get updated."""
        state = """# Established Results

## WH-003 Hawking Temperature

T = hbar * kappa / (2 pi k_B)

This result depends on WH-003 for the surface gravity calculation.
Also see ER-003 in the synthesis section.
"""
        meta = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify WH-003 Hawking temperature
**VERDICT**: VERIFIED
**RESULT**:
Correct.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_er_promotion_gate(ws)
        updated = ws.read_file("RESEARCH_STATE.md")
        # All WH-003 references should be ER-003 now
        assert "WH-003" not in updated
        assert "ER-003" in updated
        assert "depends on ER-003" in updated

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
        state = """# Established Results

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
        state_body = "# Results\n\nSome findings.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": render_frontmatter(state_meta, state_body),
            "COMPUTATION_LOG.md": render_frontmatter(meta_comp, comp_body),
        })
        violations = check_verified_frontmatter_backfill(ws)
        assert len(violations) == 1
        from sciralph.markdown import parse_frontmatter
        updated_meta, _ = parse_frontmatter(ws.read_file("RESEARCH_STATE.md"))
        assert "ER-001" in updated_meta.get("verified_results", [])
        assert "WH-002" in updated_meta.get("verified_results", [])

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
        state_body = "# Results\n"
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

# Established Results

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
        state = """# Established Results

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
        state = """# Established Results

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
        state = """# Established Results

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
        state = """# Established Results

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
