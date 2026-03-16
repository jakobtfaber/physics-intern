"""Tests for SciRalph engine (compression thresholds, research status, budget enforcement, overrides)."""

from unittest.mock import MagicMock, patch, PropertyMock, call

from sciralph.config import Config
from sciralph.engine import LoopState
from sciralph.markdown import parse_frontmatter, render_frontmatter
from sciralph.task import Task, TaskType
from sciralph.validation import Violation, ViolationSeverity


class TestCheckCompression:
    """Test _check_compression with various file sizes vs thresholds."""

    def _make_engine(self, file_size_map: dict[str, int]):
        """Create a SciRalph instance with mocked workspace and compressor."""
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.file_size = MagicMock(side_effect=lambda f: file_size_map.get(f, 0))

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(compress_threshold={"TEST.md": 10_000})
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.compressor = MagicMock()
            engine.iteration = 1
        return engine

    def test_no_compression_below_threshold(self):
        engine = self._make_engine({"TEST.md": 5_000})
        engine._check_compression()
        engine.compressor.run.assert_not_called()
        engine.metrics.alert.assert_not_called()

    def test_alert_only_between_1x_and_1_5x(self):
        engine = self._make_engine({"TEST.md": 12_000})  # 1.2x
        engine._check_compression()
        engine.metrics.alert.assert_called_once()
        engine.compressor.run.assert_not_called()

    def test_compression_at_1_5x(self):
        engine = self._make_engine({"TEST.md": 16_000})  # 1.6x
        engine._check_compression()
        engine.metrics.alert.assert_called_once()
        engine.compressor.run.assert_called_once()
        task_arg = engine.compressor.run.call_args[0][0]
        assert task_arg.target_file == "TEST.md"

    def test_compression_at_2x_still_compresses(self):
        """2.5x exceeds soft multiplier, so compression triggers."""
        engine = self._make_engine({"TEST.md": 25_000})  # 2.5x
        engine._check_compression()
        engine.metrics.alert.assert_called_once()
        engine.compressor.run.assert_called_once()
        task_arg = engine.compressor.run.call_args[0][0]
        assert task_arg.target_file == "TEST.md"


class TestSetResearchStatus:
    """Test _set_research_status updates frontmatter correctly."""

    def test_set_research_status(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            original = render_frontmatter(
                {"status": "in_progress", "title": "Test"},
                "# Problem\n\nSome content\n",
            )
            ws.read_file = MagicMock(return_value=original)
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws

            engine._set_research_status("completed")

            assert "RESEARCH_STATE.md" in written
            meta, body = parse_frontmatter(written["RESEARCH_STATE.md"])
            assert meta["status"] == "completed"
            assert meta["title"] == "Test"
            assert "Some content" in body


class TestEnrichComputeTask:
    """Test compute task enrichment with prior failure context (item 5)."""

    COMP_LOG_WITH_FAILURES = """\
## COMP-001: Check WH-003
- **CLAIM**: Verify WH-003 Chandrasekhar mass limit
- **VERDICT**: INCONCLUSIVE
- **RESULT**:
  Relative error: 13.6%. Expected 1.44 M_sun, got 1.24 M_sun.

## COMP-002: Retry WH-003
- **CLAIM**: Verify WH-003 mass limit with improved integration
- **VERDICT**: INCONCLUSIVE
- **RESULT**:
  Still 8% error after improving step size.
"""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 3
        return engine, ws, written

    def test_enrich_compute_task_appends_context(self):
        """Prior failures exist -> CURRENT_TASK enriched."""
        engine, ws, written = self._make_engine()
        ws.read_file = MagicMock(side_effect=lambda f: {
            "COMPUTATION_LOG.md": self.COMP_LOG_WITH_FAILURES,
            "CURRENT_TASK.md": "---\ntask_type: compute\n---\n\nVerify WH-003 mass.",
        }.get(f, ""))

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify WH-003 mass limit")
        engine._enrich_compute_task_with_prior_failures(task)

        assert "CURRENT_TASK.md" in written
        enriched = written["CURRENT_TASK.md"]
        assert "Prior Computation Failure Context" in enriched
        assert "2 prior failure(s)" in enriched
        assert "ROOT CAUSE" in enriched

    def test_enrich_compute_task_no_match(self):
        """No prior failures -> unchanged."""
        engine, ws, written = self._make_engine()
        ws.read_file = MagicMock(side_effect=lambda f: {
            "COMPUTATION_LOG.md": self.COMP_LOG_WITH_FAILURES,
            "CURRENT_TASK.md": "---\ntask_type: compute\n---\n\nVerify WH-099 something.",
        }.get(f, ""))

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify WH-099 something new")
        engine._enrich_compute_task_with_prior_failures(task)

        assert "CURRENT_TASK.md" not in written  # write_file not called


class TestRefutedRecompute:
    """Test REFUTED verdict triggers forced recompute next iteration."""

    COMP_LOG_REFUTED = """\
## COMP-001: Check WH-001
**CLAIM**: Verify formula X = Y
**VERDICT**: REFUTED
**NOTES**: Numerical checks fail consistently.
"""

    COMP_LOG_VERIFIED = """\
## COMP-001: Check WH-001
**CLAIM**: Verify formula X = Y
**VERDICT**: VERIFIED
**NOTES**: All checks pass.
"""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 3
            engine._state = LoopState()
        return engine, ws, written

    def test_refuted_sets_pending_recompute(self):
        """REFUTED verdict sets _state.pending_recompute_claim."""
        engine, ws, _ = self._make_engine()
        ws.read_file = MagicMock(return_value=self.COMP_LOG_REFUTED)

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert engine._state.pending_recompute_claim is not None
        assert "formula X = Y" in engine._state.pending_recompute_claim

    def test_verified_no_pending(self):
        """VERIFIED verdict does not set _state.pending_recompute_claim."""
        engine, ws, _ = self._make_engine()
        ws.read_file = MagicMock(return_value=self.COMP_LOG_VERIFIED)

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert engine._state.pending_recompute_claim is None

    def test_make_recompute_task(self):
        """_make_recompute_task creates a valid compute task."""
        engine, _, written = self._make_engine()

        task = engine._make_recompute_task("Test claim")

        assert task.task_type == TaskType.COMPUTE
        assert task.priority == "high"
        assert "CURRENT_TASK.md" in written
        assert "Re-verification After REFUTED Verdict" in written["CURRENT_TASK.md"]
        assert "Test claim" in written["CURRENT_TASK.md"]

    def test_pending_recompute_consumed_on_next_iteration(self):
        """Pending recompute claim is consumed and cleared."""
        engine, ws, written = self._make_engine()
        engine._state.pending_recompute_claim = "Verify formula X = Y"

        # Simulate what happens at top of loop
        claim = engine._state.pending_recompute_claim
        engine._state.pending_recompute_claim = None
        task = Task(task_id="TASK-003", task_type=TaskType.RESEARCH, assigned_to="researcher")
        if task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
            task = engine._make_recompute_task(claim)

        assert task.task_type == TaskType.COMPUTE
        assert engine._state.pending_recompute_claim is None

    def test_pending_recompute_skipped_on_synthesize(self):
        """Pending recompute is NOT forced during synthesize/terminate."""
        engine, _, _ = self._make_engine()
        engine._state.pending_recompute_claim = "Verify formula X = Y"

        task = Task(task_id="TASK-003", task_type=TaskType.SYNTHESIZE, assigned_to="researcher")
        claim = engine._state.pending_recompute_claim
        engine._state.pending_recompute_claim = None
        if task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
            task = engine._make_recompute_task(claim)

        assert task.task_type == TaskType.SYNTHESIZE


class TestComputeVerdictTracking:
    """Test dispatch-level verdict tracking with failure counter."""

    COMP_LOG_REFUTED = """\
## COMP-001: Check WH-001
**CLAIM**: Verify formula X = Y
**VERDICT**: REFUTED
**NOTES**: Numerical checks fail consistently.
"""

    COMP_LOG_INCONCLUSIVE = """\
## COMP-001: Check WH-001
**CLAIM**: Verify formula X = Y
**VERDICT**: INCONCLUSIVE
**NOTES**: Could not determine.
"""

    COMP_LOG_VERIFIED = """\
## COMP-001: Check WH-001
**CLAIM**: Verify formula X = Y
**VERDICT**: VERIFIED
**NOTES**: All checks pass.
"""

    def _make_engine(self, comp_log="", stall_recompute_limit=2):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)
            ws.read_file = MagicMock(return_value=comp_log)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(stall_recompute_limit=stall_recompute_limit)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 3
            engine._state = LoopState()
        return engine, ws, written

    def test_first_refuted_allows_recompute(self):
        """First REFUTED sets _state.pending_recompute_claim (count=1 < limit=2)."""
        engine, ws, _ = self._make_engine(self.COMP_LOG_REFUTED)
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert engine._state.pending_recompute_claim is not None
        assert any(v == 1 for v in engine._state.claim_failure_count.values())

    def test_first_inconclusive_allows_recompute(self):
        """INCONCLUSIVE also counted and triggers recompute."""
        engine, ws, _ = self._make_engine(self.COMP_LOG_INCONCLUSIVE)
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert engine._state.pending_recompute_claim is not None
        # Counter should be 1
        assert any(v == 1 for v in engine._state.claim_failure_count.values())

    def test_second_failure_escalates_to_stall(self):
        """Pre-set count=1, second failure adds to _state.stalled_claims, no _state.pending_recompute_claim."""
        engine, ws, _ = self._make_engine(self.COMP_LOG_REFUTED)
        # Pre-set count to 1 for the claim key
        from sciralph.markdown import _normalize_claim_key
        key = _normalize_claim_key("Verify formula X = Y")
        engine._state.claim_failure_count[key] = 1

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert engine._state.pending_recompute_claim is None
        assert key in engine._state.stalled_claims
        assert engine._state.claim_failure_count[key] == 2

    def test_verified_resets_counter(self):
        """VERIFIED clears the failure counter for that claim."""
        engine, ws, _ = self._make_engine(self.COMP_LOG_VERIFIED)
        from sciralph.markdown import _normalize_claim_key
        key = _normalize_claim_key("Verify formula X = Y")
        engine._state.claim_failure_count[key] = 1

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert key not in engine._state.claim_failure_count
        assert engine._state.pending_recompute_claim is None

    def test_verified_clears_stalled_claim(self):
        """VERIFIED resets counter AND removes from _state.stalled_claims."""
        engine, ws, _ = self._make_engine(self.COMP_LOG_VERIFIED)
        from sciralph.markdown import _normalize_claim_key
        key = _normalize_claim_key("Verify formula X = Y")
        engine._state.claim_failure_count[key] = 2
        engine._state.stalled_claims.add(key)

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert key not in engine._state.claim_failure_count
        assert key not in engine._state.stalled_claims  # cleared on VERIFIED

    def test_different_claims_tracked_independently(self):
        """Two different WH IDs have separate counters."""
        comp_log_wh002 = """\
## COMP-001: Check WH-002
**CLAIM**: Verify WH-002 temperature
**VERDICT**: REFUTED
**NOTES**: Wrong.
"""
        engine, ws, _ = self._make_engine(comp_log_wh002)
        from sciralph.markdown import _normalize_claim_key
        key1 = _normalize_claim_key("Verify WH-001 formula")
        engine._state.claim_failure_count[key1] = 1  # pre-existing failure on WH-001

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify WH-002 temperature")
        engine._track_compute_verdict(task)

        # WH-001 counter unchanged
        assert engine._state.claim_failure_count[key1] == 1
        # WH-002 has its own counter
        key2 = _normalize_claim_key("Verify WH-002 temperature")
        assert engine._state.claim_failure_count.get(key2, 0) == 1

    def test_stall_escalation_injects_violation(self):
        """Stall escalation adds Violation with check='computation_stall'."""
        engine, ws, _ = self._make_engine(self.COMP_LOG_REFUTED)
        from sciralph.markdown import _normalize_claim_key
        key = _normalize_claim_key("Verify formula X = Y")
        engine._state.claim_failure_count[key] = 1  # next will be 2 == limit

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert len(engine._state.pending_violations) == 1
        assert engine._state.pending_violations[0].check == "computation_stall"
        assert "failed verification" in engine._state.pending_violations[0].message

    def test_limit_of_1_escalates_immediately(self):
        """With stall_recompute_limit=1, first failure escalates."""
        engine, ws, _ = self._make_engine(self.COMP_LOG_REFUTED, stall_recompute_limit=1)

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert engine._state.pending_recompute_claim is None
        assert len(engine._state.stalled_claims) > 0
        assert len(engine._state.pending_violations) == 1

    def test_empty_comp_log_noop(self):
        """No entries, nothing happens."""
        engine, ws, _ = self._make_engine("")

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert engine._state.pending_recompute_claim is None
        assert len(engine._state.claim_failure_count) == 0
        assert len(engine._state.stalled_claims) == 0


class TestCriticCleanSignal:
    """Test that NO_CRITIQUES_FILED injects a violation for the orchestrator."""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 5
            engine._state = LoopState()
            engine.critic = MagicMock()
        return engine

    def test_no_critiques_filed_injects_violation(self):
        """NO_CRITIQUES_FILED in critic response adds a violation for orchestrator."""
        engine = self._make_engine()
        response = MagicMock()
        response.text = "NO_CRITIQUES_FILED"
        engine.critic.run = MagicMock(return_value=response)

        task = Task(task_id="TASK-005", task_type=TaskType.CRITIQUE, assigned_to="deep_critic")
        engine._dispatch(task)

        assert len(engine._state.pending_violations) == 1
        assert engine._state.pending_violations[0].check == "critic_clean"
        assert "NO issues" in engine._state.pending_violations[0].message

    def test_normal_critique_no_violation(self):
        """Normal critic output does NOT inject a violation."""
        engine = self._make_engine()
        response = MagicMock()
        response.text = "## CRIT-001\nSome real critique here."
        engine.critic.run = MagicMock(return_value=response)

        task = Task(task_id="TASK-005", task_type=TaskType.CRITIQUE, assigned_to="deep_critic")
        engine._dispatch(task)

        assert len(engine._state.pending_violations) == 0


class TestApplyOverrides:
    """Test the consolidated _apply_overrides method."""

    def _make_engine(self, max_iterations=20, current_iteration=5,
                     last_critic_iteration=0, critic_every_n=4,
                     pending_recompute=None, pending_recompute_verdict=None):
        """Create a SciRalph instance with mocked workspace for override testing."""
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)
            ws.read_file = MagicMock(return_value="")

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(
                max_iterations=max_iterations,
                critic_every_n=critic_every_n,
            )
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.metrics.last_critic_iteration = last_critic_iteration
            engine.iteration = current_iteration
            engine._state = LoopState(
                pending_recompute_claim=pending_recompute,
                pending_recompute_verdict=pending_recompute_verdict,
                last_content_iteration=current_iteration,
            )
        return engine, written

    def test_refuted_recompute_when_applicable(self):
        """REFUTED recompute (P4) replaces non-terminal tasks."""
        engine, written = self._make_engine(
            max_iterations=20, current_iteration=5,
            last_critic_iteration=4,  # critic not overdue
            pending_recompute="Verify formula X = Y",
        )
        task = Task(
            task_id="TASK-005", task_type=TaskType.RESEARCH,
            assigned_to="researcher", iteration=5,
        )
        result = engine._apply_overrides(task)

        assert result.task_type == TaskType.COMPUTE
        assert "Re-verification After REFUTED Verdict" in result.body
        assert engine._state.pending_recompute_claim is None  # consumed

    def test_enrichment_non_overriding(self):
        """Enrichment mutates task body without changing task type."""
        engine, written = self._make_engine(
            max_iterations=20, current_iteration=5,
            last_critic_iteration=4,  # critic not overdue
        )
        # Set up workspace to return comp log with prior failures
        engine.workspace.read_file = MagicMock(side_effect=lambda f: {
            "COMPUTATION_LOG.md": (
                "## COMP-001: Check WH-003\n"
                "- **CLAIM**: Verify WH-003 mass limit\n"
                "- **VERDICT**: INCONCLUSIVE\n"
                "- **RESULT**: error\n"
            ),
            "CURRENT_TASK.md": "---\ntask_type: compute\n---\n\nVerify WH-003 mass.",
            "RESEARCH_STATE.md": "",
        }.get(f, ""))

        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE,
            assigned_to="computationalist", iteration=5,
            body="Verify WH-003 mass limit",
        )
        result = engine._apply_overrides(task)

        # Task type unchanged
        assert result.task_type == TaskType.COMPUTE
        assert result is task  # same object returned

    def test_synthesize_never_overridden(self):
        """SYNTHESIZE tasks pass through all overrides unchanged."""
        engine, written = self._make_engine(
            max_iterations=20, current_iteration=5,
            last_critic_iteration=0, critic_every_n=4,  # critic overdue
            pending_recompute="some claim",  # recompute pending
        )
        task = Task(
            task_id="TASK-005", task_type=TaskType.SYNTHESIZE,
            assigned_to="researcher", iteration=5,
        )
        result = engine._apply_overrides(task)

        # P3 skips synthesize, P4 skips synthesize/terminate
        assert result.task_type == TaskType.SYNTHESIZE

    def test_terminate_never_overridden(self):
        """TERMINATE tasks pass through all overrides unchanged."""
        engine, written = self._make_engine(
            max_iterations=10, current_iteration=10,
        )
        task = Task(
            task_id="TASK-010", task_type=TaskType.TERMINATE,
            assigned_to="orchestrator", iteration=10,
        )
        result = engine._apply_overrides(task)

        assert result.task_type == TaskType.TERMINATE


class TestTerminationGate:
    """Test the termination gate in the main loop."""

    def _make_engine(self):
        """Create a SciRalph instance for termination gate testing."""
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)
            ws.read_file = MagicMock(return_value="")

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 5
            engine._state = LoopState()
            engine.problem_meta = {}
        return engine, written

    def test_terminate_allowed_when_stub(self):
        """Stub can_terminate always returns True, so TERMINATE proceeds."""
        from sciralph.validation import can_terminate
        engine, _ = self._make_engine()

        # The stub always allows termination
        allowed, blockers = can_terminate(
            engine.workspace, engine.config, engine.metrics, engine.problem_meta)
        assert allowed is True
        assert blockers == []

    def test_build_context_prefix_with_violations(self):
        """Context prefix includes pending violations."""
        engine, _ = self._make_engine()
        engine._state.pending_violations = [
            Violation(
                check="test_check", severity=ViolationSeverity.ERROR,
                message="Something wrong", file="TEST.md",
            ),
        ]
        prefix = engine._build_context_prefix()

        assert "POST-INTEGRATION VIOLATIONS" in prefix
        assert "test_check" in prefix
        assert "Something wrong" in prefix
        assert len(engine._state.pending_violations) == 0  # consumed

    def test_build_context_prefix_with_blockers(self):
        """Context prefix includes termination blockers."""
        engine, _ = self._make_engine()
        engine._state.pending_termination_blockers = [
            "Missing numerical verification",
            "Unresolved critiques remain",
        ]
        prefix = engine._build_context_prefix()

        assert "TERMINATION BLOCKED" in prefix
        assert "Missing numerical verification" in prefix
        assert "Unresolved critiques remain" in prefix
        assert len(engine._state.pending_termination_blockers) == 0  # consumed

    def test_build_context_prefix_empty_when_no_issues(self):
        """Context prefix is empty when no violations or blockers."""
        engine, _ = self._make_engine()
        prefix = engine._build_context_prefix()
        assert prefix == ""

    def test_context_prefix_includes_er_demotion_safety(self):
        """ER demotion safety violations now appear in context (no longer silently filtered)."""
        engine, _ = self._make_engine()
        engine._state.pending_violations = [
            Violation(
                check="er_demotion_safety", severity=ViolationSeverity.WARNING,
                message="ER-001 has REFUTED computation with no VERIFIED — demoted to WH-001",
                file="RESEARCH_STATE.md", detail="ER-001",
            ),
            Violation(
                check="phantom_references", severity=ViolationSeverity.ERROR,
                message="Phantom reference COMP-999", file="RESEARCH_STATE.md",
            ),
        ]
        prefix = engine._build_context_prefix()

        assert "POST-INTEGRATION VIOLATIONS" in prefix
        assert "phantom_references" in prefix
        assert "COMP-999" in prefix
        # ER demotion safety violations now appear in context prefix
        assert "er_demotion_safety" in prefix
        assert len(engine._state.pending_violations) == 0  # consumed


class TestCheckStatusField:
    """Test the renamed _check_status_field (formerly _should_terminate)."""

    def _make_engine(self, state_text=""):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.read_file = MagicMock(return_value=state_text)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.iteration = 0
        return engine

    def test_completed_status(self):
        engine = self._make_engine("status: completed\n# Problem")
        assert engine._check_status_field() is True

    def test_abandoned_status(self):
        engine = self._make_engine('status: "abandoned"\n# Problem')
        assert engine._check_status_field() is True

    def test_partially_complete_status(self):
        engine = self._make_engine("status: partially_complete\n# Problem")
        assert engine._check_status_field() is True

    def test_in_progress_status(self):
        engine = self._make_engine("status: in_progress\n# Problem")
        assert engine._check_status_field() is False

    def test_empty_state(self):
        engine = self._make_engine("")
        assert engine._check_status_field() is False


class TestZeroOutputStallHandling:
    """Tests for zero-output stall detection and enrichment (Improvement 1C-1D)."""

    COMP_LOG_ZERO_OUTPUT = """\
---
total_computations: 1
---

# Computations

## COMP-001: Check WH-001
**CLAIM**: Verify WH-001 formula
**VERDICT**: INCONCLUSIVE

Agent produced no text output. Writing INCONCLUSIVE stub.
"""

    def _make_engine(self, comp_log=""):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.metrics.last_critic_iteration = 4
            engine.iteration = 5
            engine._state = LoopState(last_content_iteration=5)
        return engine, ws, written

    def test_enrich_flags_zero_output_stall(self):
        """Enrichment adds ZERO-OUTPUT STALL instructions when prior has no-text marker."""
        engine, ws, written = self._make_engine()
        zero_output_prior = (
            "Agent produced no text output. Writing INCONCLUSIVE stub.\n"
            "Used 100K tokens with no result."
        )
        ws.read_file = MagicMock(side_effect=lambda f: {
            "COMPUTATION_LOG.md": """\
## COMP-001: Check WH-003
- **CLAIM**: Verify WH-003 mass limit
- **VERDICT**: INCONCLUSIVE
- **RESULT**:
  Agent produced no text output. Writing INCONCLUSIVE stub.
""",
            "CURRENT_TASK.md": "---\ntask_type: compute\n---\n\nVerify WH-003 mass.",
        }.get(f, ""))
        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE,
            assigned_to="computationalist", body="Verify WH-003 mass limit",
        )
        engine._enrich_compute_task_with_prior_failures(task)
        assert "CURRENT_TASK.md" in written
        enriched = written["CURRENT_TASK.md"]
        assert "ZERO-OUTPUT STALL DETECTED" in enriched


class TestDispatchRoutingValidation:
    """Tests for dispatch cross-validation (Improvement 6B)."""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 5
            engine._state = LoopState(last_content_iteration=5)
            engine.researcher = MagicMock()
            engine.computationalist = MagicMock()
            engine.critic = MagicMock()
        return engine

    def test_dispatch_logs_routing_conflict(self):
        """Mismatched assigned_to logs a warning but routes correctly."""
        engine = self._make_engine()
        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE,
            assigned_to="researcher",  # wrong for compute
            iteration=5, body="Verify something.",
        )
        agent_name, _ = engine._dispatch(task)
        assert agent_name == "computationalist"
        engine.metrics.alert.assert_called()
        alert_msg = engine.metrics.alert.call_args[0][1]
        assert "Routing conflict" in alert_msg

    def test_dispatch_infers_from_empty_assigned_to(self):
        """Empty assigned_to gets inferred from task_type."""
        engine = self._make_engine()
        task = Task(
            task_id="TASK-005", task_type=TaskType.RESEARCH,
            assigned_to="",  # empty
            iteration=5, body="Research something.",
        )
        agent_name, _ = engine._dispatch(task)
        assert agent_name == "researcher"
        engine.metrics.alert.assert_called()
        alert_msg = engine.metrics.alert.call_args[0][1]
        assert "Routing fix" in alert_msg


class TestUpdateResearchIteration:
    """Test engine-side iteration counter update (Fix 3)."""

    def _make_engine(self, state_text=""):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)
            ws.read_file = MagicMock(return_value=state_text)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 3
        return engine, written

    def test_iteration_field_updated(self):
        """iteration field in frontmatter is updated and body preserved."""
        state = render_frontmatter(
            {"status": "in_progress", "iteration": 1},
            "# Working Hypotheses (WH) and Established Results (ER)\n\nSome findings.\n",
        )
        engine, written = self._make_engine(state)
        engine.iteration = 5
        engine._update_research_iteration()

        assert "RESEARCH_STATE.md" in written
        meta, body = parse_frontmatter(written["RESEARCH_STATE.md"])
        assert meta["iteration"] == 5
        assert "Some findings" in body

    def test_other_frontmatter_preserved(self):
        """Other frontmatter fields are not disturbed."""
        state = render_frontmatter(
            {"status": "in_progress", "iteration": 2, "verified_results": ["ER-001"]},
            "# Body\n",
        )
        engine, written = self._make_engine(state)
        engine.iteration = 7
        engine._update_research_iteration()

        meta, _ = parse_frontmatter(written["RESEARCH_STATE.md"])
        assert meta["iteration"] == 7
        assert meta["status"] == "in_progress"
        assert "ER-001" in meta["verified_results"]

    def test_empty_file_early_return(self):
        """Empty RESEARCH_STATE.md -> no write."""
        engine, written = self._make_engine("")
        engine._update_research_iteration()
        assert "RESEARCH_STATE.md" not in written

    def test_missing_iteration_field_created(self):
        """If iteration field is absent, it's created."""
        state = render_frontmatter(
            {"status": "in_progress"},
            "# Body\n",
        )
        engine, written = self._make_engine(state)
        engine.iteration = 4
        engine._update_research_iteration()

        meta, _ = parse_frontmatter(written["RESEARCH_STATE.md"])
        assert meta["iteration"] == 4


class TestDispatchFailureRecovery:
    """Test that transient dispatch failures are caught and the loop continues."""

    def _make_engine(self):
        """Create a SciRalph instance with mocked agents for dispatch testing."""
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.read_file = MagicMock(return_value="")
            ws.write_file = MagicMock()
            ws.file_size = MagicMock(return_value=0)
            ws.validate_comp_references = MagicMock(return_value=[])
            ws.git_commit = MagicMock()

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(max_iterations=3)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.metrics.last_critic_iteration = 0
            engine.metrics.alerts = []
            engine.metrics.calls = []
            engine.metrics.total_input_tokens = 0
            engine.metrics.total_output_tokens = 0
            engine.iteration = 0
            engine._state = LoopState()
            engine.problem_meta = {}

            engine.orchestrator = MagicMock()
            engine.researcher = MagicMock()
            engine.computationalist = MagicMock()
            engine.critic = MagicMock()
            engine.compressor = MagicMock()
            engine.formatter = MagicMock()
        return engine

    def test_transient_error_continues_loop(self):
        """A transient 504 from dispatch is caught; loop continues to next iteration."""
        engine = self._make_engine()
        engine.config.max_iterations = 10  # enough headroom to avoid budget override

        # Create a 504-like exception
        exc_504 = Exception("Server error")
        exc_504.status_code = 504

        # Orchestrator returns a compute task each time
        task = Task(
            task_id="TASK-001", task_type=TaskType.COMPUTE,
            assigned_to="computationalist", iteration=1,
            body="Verify something.",
        )
        engine.orchestrator.parse_task = MagicMock(return_value=task)

        # First dispatch: transient error; second+third: succeed, then terminate
        task_terminate = Task(
            task_id="TASK-002", task_type=TaskType.TERMINATE,
            assigned_to="orchestrator", iteration=2,
        )
        engine.orchestrator.parse_task = MagicMock(side_effect=[task, task_terminate])
        engine.computationalist.run = MagicMock(side_effect=exc_504)

        engine.run()

        # Alert was logged for the failure
        engine.metrics.alert.assert_any_call(
            1, unittest_any_string_containing("Dispatch failed")
        )
        # Computationalist was called once (failed), then orchestrator terminated
        assert engine.computationalist.run.call_count == 1
        assert engine.iteration == 2

    def test_non_transient_error_propagates(self):
        """A non-transient error (e.g. ValueError) propagates and crashes."""
        engine = self._make_engine()

        task = Task(
            task_id="TASK-001", task_type=TaskType.RESEARCH,
            assigned_to="researcher", iteration=1,
            body="Research something.",
        )
        engine.orchestrator.parse_task = MagicMock(return_value=task)
        engine.researcher.run = MagicMock(side_effect=ValueError("bug in code"))

        import pytest
        with pytest.raises(ValueError, match="bug in code"):
            engine.run()

    def test_dispatch_failure_injects_violation(self):
        """Transient dispatch failure adds a violation for the orchestrator."""
        engine = self._make_engine()
        engine.config.max_iterations = 10

        exc_timeout = Exception("Read timed out")
        exc_timeout.status_code = 504

        task = Task(
            task_id="TASK-001", task_type=TaskType.COMPUTE,
            assigned_to="computationalist", iteration=1,
            body="Verify something.",
        )
        task_terminate = Task(
            task_id="TASK-002", task_type=TaskType.TERMINATE,
            assigned_to="orchestrator", iteration=2,
        )
        engine.orchestrator.parse_task = MagicMock(side_effect=[task, task_terminate])
        engine.computationalist.run = MagicMock(side_effect=exc_timeout)

        engine.run()

        # Violation was consumed by _build_context_prefix on iteration 2, but
        # we can verify it was created by checking the context_prefix set on orchestrator
        prefix = engine.orchestrator.context_prefix
        assert "dispatch_failure" in prefix


class TestAgentFailureRouting:
    """Test _record_agent_failures and its integration with _build_context_prefix."""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 5
            engine._state = LoopState()
        return engine

    def test_max_tokens_recorded(self):
        """max_tokens stop_reason records a truncation failure with token count."""
        engine = self._make_engine()
        result = MagicMock()
        result.stop_reason = "max_tokens"
        result.output_tokens = 8000
        task = Task(task_id="TASK-005", task_type=TaskType.RESEARCH, assigned_to="researcher")

        engine._record_agent_failures(task, "researcher", result)

        assert len(engine._state.agent_failures) == 1
        assert engine._state.agent_failures[0]["event"] == "max_tokens_truncation"
        assert engine._state.agent_failures[0]["task_id"] == "TASK-005"
        assert engine._state.agent_failures[0]["agent"] == "researcher"
        assert "8000 tokens" in engine._state.agent_failures[0]["detail"]
        assert "Decompose" in engine._state.agent_failures[0]["detail"]

    def test_max_rounds_forced_recorded(self):
        """max_rounds_forced stop_reason records an exhaustion failure."""
        from sciralph.llm import AgentResult
        engine = self._make_engine()
        result = AgentResult(text="partial", rounds=10, stop_reason="max_rounds_forced")
        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE, assigned_to="computationalist")

        engine._record_agent_failures(task, "computationalist", result)

        assert len(engine._state.agent_failures) == 1
        assert engine._state.agent_failures[0]["event"] == "max_rounds_exhaustion"
        assert "10 tool-use rounds" in engine._state.agent_failures[0]["detail"]

    def test_normal_end_turn_not_recorded(self):
        """Normal end_turn does NOT record a failure."""
        engine = self._make_engine()
        result = MagicMock()
        result.stop_reason = "end_turn"
        task = Task(task_id="TASK-005", task_type=TaskType.RESEARCH, assigned_to="researcher")

        engine._record_agent_failures(task, "researcher", result)

        assert len(engine._state.agent_failures) == 0

    def test_context_prefix_includes_agent_failures(self):
        """Agent failures appear in context prefix banner."""
        engine = self._make_engine()
        engine._state.agent_failures = [{
            "task_id": "TASK-004",
            "agent": "researcher",
            "event": "max_tokens_truncation",
            "detail": (
                "Output hit token limit (8000 tokens). "
                "Decompose into smaller subtasks, each targeting a single "
                "derivation step or sub-claim."
            ),
            "iteration": 4,
        }]
        prefix = engine._build_context_prefix()

        assert "AGENT FAILURES" in prefix
        assert "TASK-004" in prefix
        assert "max_tokens_truncation" in prefix
        assert "Decompose" in prefix
        assert "token limit" in prefix

    def test_context_prefix_clears_agent_failures(self):
        """Agent failures are cleared after building context prefix."""
        engine = self._make_engine()
        engine._state.agent_failures = [{
            "task_id": "TASK-004",
            "agent": "researcher",
            "event": "max_tokens_truncation",
            "detail": "Task too large.",
            "iteration": 4,
        }]
        engine._build_context_prefix()
        assert len(engine._state.agent_failures) == 0

    def test_compute_verdict_appends_to_agent_failures(self):
        """REFUTED verdict below stall limit appends to _agent_failures."""
        comp_log = (
            "## COMP-001: Check WH-001\n"
            "**CLAIM**: Verify formula X = Y\n"
            "**VERDICT**: REFUTED\n"
            "**NOTES**: Numerical checks fail.\n"
        )
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.read_file = MagicMock(return_value=comp_log)
            ws.write_file = MagicMock()

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(stall_recompute_limit=3)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 5
            engine._state = LoopState()

        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert len(engine._state.agent_failures) == 1
        assert engine._state.agent_failures[0]["event"] == "refuted_verdict"
        assert "Attempt 1/3" in engine._state.agent_failures[0]["detail"]

    def test_compute_verdict_stall_no_agent_failure(self):
        """At stall escalation (count >= limit), no _agent_failures entry (violation used instead)."""
        comp_log = (
            "## COMP-001: Check WH-001\n"
            "**CLAIM**: Verify formula X = Y\n"
            "**VERDICT**: INCONCLUSIVE\n"
            "**NOTES**: Could not determine.\n"
        )
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.read_file = MagicMock(return_value=comp_log)
            ws.write_file = MagicMock()

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(stall_recompute_limit=2)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 5
            engine._state = LoopState(
                claim_failure_count={"verify formula x = y": 1},  # already at limit-1
            )

        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        # Stall escalation uses violations, not _state.agent_failures
        assert len(engine._state.agent_failures) == 0
        assert len(engine._state.pending_violations) == 1
        assert engine._state.pending_violations[0].check == "computation_stall"

    def test_context_prefix_ordering(self):
        """Violations appear before agent failures in context prefix."""
        engine = self._make_engine()
        engine._state.pending_violations = [
            Violation(check="test", severity=ViolationSeverity.WARNING,
                      message="test violation", file="TEST.md"),
        ]
        engine._state.agent_failures = [{
            "task_id": "TASK-003",
            "agent": "computationalist",
            "event": "max_rounds_exhaustion",
            "detail": "Exhausted 10 tool-use rounds without completing.",
            "iteration": 4,
        }]
        prefix = engine._build_context_prefix()

        violations_pos = prefix.index("VIOLATIONS")
        failures_pos = prefix.index("AGENT FAILURES")
        assert violations_pos < failures_pos


class TestP4RecomputeEnrichment:
    """Test P4 recompute wiring through P6 enrichment and verdict flow."""

    COMP_LOG_WITH_METHOD = """\
## COMP-001: Check WH-001
**CLAIM**: Verify WH-001 formula X = Y
**METHOD**:
Used numerical integration with x0=1e-6.
**VERDICT**: REFUTED
**RESULT**:
Got 0.48 instead of 0.50.
**NOTES**:
Grid too coarse at boundary.
"""

    def _make_engine(self, comp_log="", pending_recompute=None,
                     pending_verdict=None):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.metrics.last_critic_iteration = 4
            engine.iteration = 5
            engine._state = LoopState(
                pending_recompute_claim=pending_recompute,
                pending_recompute_verdict=pending_verdict,
                last_content_iteration=5,
            )
        return engine, ws, written

    def test_p4_recompute_gets_enrichment(self):
        """P4 recompute task includes 'Prior Computation Failure Context' from P6."""
        engine, ws, written = self._make_engine(
            pending_recompute="Verify WH-001 formula X = Y",
            pending_verdict="REFUTED",
        )
        # read_file returns comp log for enrichment, then task text for enrichment write
        ws.read_file = MagicMock(side_effect=lambda f: {
            "COMPUTATION_LOG.md": self.COMP_LOG_WITH_METHOD,
            "CURRENT_TASK.md": written.get("CURRENT_TASK.md", ""),
        }.get(f, ""))

        task = Task(
            task_id="TASK-005", task_type=TaskType.RESEARCH,
            assigned_to="researcher", iteration=5,
        )
        result = engine._apply_overrides(task)

        assert result.task_type == TaskType.COMPUTE
        # The CURRENT_TASK.md should have been written twice: once by _make_recompute_task,
        # then again by _enrich with addendum
        assert ws.write_file.call_count >= 2
        # Check that enrichment content was written
        final_content = written["CURRENT_TASK.md"]
        assert "Prior Computation Failure Context" in final_content

    def test_make_recompute_task_inconclusive_verdict(self):
        """_make_recompute_task uses the actual verdict string."""
        engine, _, written = self._make_engine()

        task = engine._make_recompute_task("Test claim", verdict="INCONCLUSIVE")

        assert "Re-verification After INCONCLUSIVE Verdict" in task.body
        assert "returned INCONCLUSIVE" in task.body
        assert "REFUTED" not in task.body
        assert "CURRENT_TASK.md" in written
        assert "INCONCLUSIVE" in written["CURRENT_TASK.md"]

    def test_inconclusive_sets_verdict_field(self):
        """_track_compute_verdict stores verdict in _pending_recompute_verdict."""
        comp_log = """\
## COMP-001: Check WH-001
**CLAIM**: Verify formula X = Y
**VERDICT**: INCONCLUSIVE
"""
        engine, ws, _ = self._make_engine()
        ws.read_file = MagicMock(return_value=comp_log)

        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._track_compute_verdict(task)

        assert engine._state.pending_recompute_claim is not None
        assert engine._state.pending_recompute_verdict == "INCONCLUSIVE"

    def test_p4_recompute_with_inconclusive_verdict(self):
        """End-to-end: INCONCLUSIVE verdict flows through _apply_overrides to recompute task."""
        engine, ws, written = self._make_engine(
            pending_recompute="Verify WH-001 formula X = Y",
            pending_verdict="INCONCLUSIVE",
        )
        ws.read_file = MagicMock(return_value="")  # empty comp log, no enrichment

        task = Task(
            task_id="TASK-005", task_type=TaskType.RESEARCH,
            assigned_to="researcher", iteration=5,
        )
        result = engine._apply_overrides(task)

        assert result.task_type == TaskType.COMPUTE
        assert "INCONCLUSIVE" in result.body
        assert "REFUTED" not in result.body
        assert engine._state.pending_recompute_claim is None
        assert engine._state.pending_recompute_verdict is None


def unittest_any_string_containing(substring):
    """Helper matcher: matches any string containing the given substring."""
    class _Matcher:
        def __eq__(self, other):
            return isinstance(other, str) and substring in other
        def __repr__(self):
            return f"<string containing {substring!r}>"
    return _Matcher()


class TestCallWithRetryNoRetry:
    """_call_with_retry returns immediately on max_tokens — no retry loop."""

    def _make_agent(self, tmp_path):
        """Create a minimal concrete agent for testing _call_with_retry."""
        from sciralph.agents.base import BaseAgent
        from sciralph.llm import LLMResponse

        class _StubAgent(BaseAgent):
            name = "test_agent"
            prompt_file = ""

            def __init__(self, config, workspace, metrics):
                super().__init__(config, workspace, metrics)
                self._system_prompt = "system"

            def build_context(self, task, iteration):
                return "context"

            def process_response(self, response, task, iteration):
                pass

        config = Config(workspace_dir=str(tmp_path))
        ws = MagicMock()
        ws.root = tmp_path
        from sciralph.metrics import MetricsTracker
        metrics = MetricsTracker()
        return _StubAgent(config, ws, metrics), metrics

    def test_no_retry_on_max_tokens(self, tmp_path):
        """call_llm is invoked exactly once even when it returns max_tokens."""
        from sciralph.llm import LLMResponse
        agent, metrics = self._make_agent(tmp_path)

        response = LLMResponse(
            text="partial output...",
            input_tokens=5000, output_tokens=8000,
            stop_reason="max_tokens", duration=1.0,
        )
        with patch("sciralph.agents.base.call_llm", return_value=response) as mock_llm:
            result = agent._call_with_retry("long context", iteration=3)

        mock_llm.assert_called_once()
        assert result.stop_reason == "max_tokens"
        assert result.output_tokens == 8000

    def test_normal_stop_returns_immediately(self, tmp_path):
        """Normal end_turn returns without alert or scaffold event."""
        from sciralph.llm import LLMResponse
        agent, metrics = self._make_agent(tmp_path)

        response = LLMResponse(
            text="full output",
            input_tokens=3000, output_tokens=2000,
            stop_reason="end_turn", duration=0.5,
        )
        with patch("sciralph.agents.base.call_llm", return_value=response) as mock_llm:
            result = agent._call_with_retry("context", iteration=1)

        mock_llm.assert_called_once()
        assert result.stop_reason == "end_turn"
        assert len(metrics.alerts) == 0

    def test_max_tokens_fires_alert_and_scaffold_event(self, tmp_path):
        """max_tokens triggers a metrics alert and scaffold log event."""
        from sciralph.llm import LLMResponse
        agent, metrics = self._make_agent(tmp_path)

        response = LLMResponse(
            text="truncated...",
            input_tokens=5000, output_tokens=8000,
            stop_reason="max_tokens", duration=1.0,
        )
        with patch("sciralph.agents.base.call_llm", return_value=response), \
             patch("sciralph.agents.base.log_scaffold_event") as mock_log:
            agent._call_with_retry("context", iteration=3)

        # Alert fired
        assert len(metrics.alerts) == 1
        assert "max_tokens_reached" in metrics.alerts[0]["message"]

        # Scaffold event emitted
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args[1] == 3  # iteration
        assert kwargs["category"] == "loop_control"
        assert kwargs["event"] == "max_tokens_no_retry"
        assert "test_agent" in kwargs["detail"]


class TestResearchTaskClearsStall:
    """Test _clear_stall_for_research_task mechanism."""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 5
            engine._state = LoopState()
        return engine

    def test_research_task_clears_stall(self):
        """Research task targeting WH-001 clears stall for that claim."""
        engine = self._make_engine()
        from sciralph.markdown import _normalize_claim_key
        key = _normalize_claim_key("Verify WH-001 formula")
        engine._state.stalled_claims = {key}
        engine._state.claim_failure_count[key] = 3

        task = Task(task_id="TASK-005", task_type=TaskType.RESEARCH,
                    assigned_to="researcher", iteration=5,
                    body="Alternative approach for WH-001 formula")
        engine._clear_stall_for_research_task(task)

        assert key not in engine._state.stalled_claims
        assert key not in engine._state.claim_failure_count

    def test_compute_task_does_not_clear_stall(self):
        """Compute tasks do not trigger stall clearing."""
        engine = self._make_engine()
        from sciralph.markdown import _normalize_claim_key
        key = _normalize_claim_key("Verify WH-001 formula")
        engine._state.stalled_claims = {key}

        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", iteration=5,
                    body="Verify WH-001 formula")
        engine._clear_stall_for_research_task(task)

        assert key in engine._state.stalled_claims

    def test_no_matching_ids_no_clear(self):
        """Research task not mentioning stalled claim doesn't clear it."""
        engine = self._make_engine()
        from sciralph.markdown import _normalize_claim_key
        key = _normalize_claim_key("Verify WH-001 formula")
        engine._state.stalled_claims = {key}

        task = Task(task_id="TASK-005", task_type=TaskType.RESEARCH,
                    assigned_to="researcher", iteration=5,
                    body="Research WH-099 something unrelated")
        engine._clear_stall_for_research_task(task)

        assert key in engine._state.stalled_claims


class TestP4PureCondition:
    """Test P4 condition is pure (no side effects)."""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.metrics.last_critic_iteration = 4
            engine.iteration = 5
            engine._state = LoopState(last_content_iteration=5)
        return engine

    def test_p4_condition_is_pure_on_synthesize(self):
        """P4 condition returns False for SYNTHESIZE but does NOT consume pending state."""
        from sciralph.engine import _p4_refuted_recompute_condition
        engine = self._make_engine()
        engine._state.pending_recompute_claim = "Verify WH-001"
        engine._state.pending_recompute_verdict = "REFUTED"

        task = Task(task_id="TASK-005", task_type=TaskType.SYNTHESIZE,
                    assigned_to="researcher", iteration=5)
        result = _p4_refuted_recompute_condition(engine, task)

        assert result is False
        # State NOT consumed by condition
        assert engine._state.pending_recompute_claim == "Verify WH-001"
        assert engine._state.pending_recompute_verdict == "REFUTED"

    def test_p4_suppressed_consumed_in_apply_overrides(self):
        """_apply_overrides consumes pending recompute when P4 condition is False (SYNTHESIZE)."""
        engine = self._make_engine()
        engine._state.pending_recompute_claim = "Verify WH-001"
        engine._state.pending_recompute_verdict = "REFUTED"

        task = Task(task_id="TASK-005", task_type=TaskType.SYNTHESIZE,
                    assigned_to="researcher", iteration=5)
        result = engine._apply_overrides(task)

        # Task passes through unchanged
        assert result.task_type == TaskType.SYNTHESIZE
        # Pending state consumed
        assert engine._state.pending_recompute_claim is None
        assert engine._state.pending_recompute_verdict is None
