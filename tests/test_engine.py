"""Tests for SciRalph engine (compression thresholds, research status, budget enforcement, overrides)."""

from unittest.mock import MagicMock, patch, PropertyMock, call

from sciralph.config import Config
from sciralph.llm import LLMResponse
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

    def test_force_compression_at_2x(self):
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


class TestBudgetEnforcement:
    """Test scaffold-level budget enforcement (item 6)."""

    def _make_engine(self, max_iterations: int, current_iteration: int):
        """Create a SciRalph instance with mocked workspace."""
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
            engine.config = Config(max_iterations=max_iterations)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = current_iteration
        return engine, written

    def test_budget_enforcement_overrides(self):
        """When <=1 iteration remaining, compute task -> overridden to synthesize."""
        engine, written = self._make_engine(max_iterations=10, current_iteration=10)
        task = Task(
            task_id="TASK-010", task_type=TaskType.COMPUTE,
            assigned_to="computationalist", priority="high",
            iteration=10, body="Verify something.",
        )
        budget_remaining = engine.config.max_iterations - engine.iteration
        assert budget_remaining <= 1

        # Simulate budget enforcement logic
        if budget_remaining <= 1 and task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
            task = engine._make_budget_synthesize_task()

        assert task.task_type == TaskType.SYNTHESIZE
        assert task.assigned_to == "researcher"
        assert "CURRENT_TASK.md" in written
        assert "Budget-Enforced Synthesis" in written["CURRENT_TASK.md"]

    def test_budget_enforcement_allows_terminal(self):
        """synthesize/terminate are not overridden even at budget limit."""
        engine, _ = self._make_engine(max_iterations=10, current_iteration=10)
        for tt in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
            task = Task(task_id="TASK-010", task_type=tt, assigned_to="researcher")
            budget_remaining = engine.config.max_iterations - engine.iteration
            if budget_remaining <= 1 and task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
                task = engine._make_budget_synthesize_task()
            assert task.task_type == tt  # unchanged

    def test_budget_enforcement_not_triggered_with_budget(self):
        """Plenty of budget -> no override."""
        engine, _ = self._make_engine(max_iterations=20, current_iteration=5)
        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE, assigned_to="computationalist")
        budget_remaining = engine.config.max_iterations - engine.iteration
        assert budget_remaining > 1
        # No override
        if budget_remaining <= 1 and task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
            task = engine._make_budget_synthesize_task()
        assert task.task_type == TaskType.COMPUTE  # unchanged


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
            engine._pending_recompute_claim = None
        return engine, ws, written

    def test_refuted_sets_pending_recompute(self):
        """REFUTED verdict sets _pending_recompute_claim."""
        engine, ws, _ = self._make_engine()
        ws.read_file = MagicMock(return_value=self.COMP_LOG_REFUTED)

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._check_for_refuted_verdict(task)

        assert engine._pending_recompute_claim is not None
        assert "formula X = Y" in engine._pending_recompute_claim

    def test_verified_no_pending(self):
        """VERIFIED verdict does not set _pending_recompute_claim."""
        engine, ws, _ = self._make_engine()
        ws.read_file = MagicMock(return_value=self.COMP_LOG_VERIFIED)

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE,
                    assigned_to="computationalist", body="Verify formula X = Y")
        engine._check_for_refuted_verdict(task)

        assert engine._pending_recompute_claim is None

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
        engine._pending_recompute_claim = "Verify formula X = Y"

        # Simulate what happens at top of loop
        claim = engine._pending_recompute_claim
        engine._pending_recompute_claim = None
        task = Task(task_id="TASK-003", task_type=TaskType.RESEARCH, assigned_to="researcher")
        if task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
            task = engine._make_recompute_task(claim)

        assert task.task_type == TaskType.COMPUTE
        assert engine._pending_recompute_claim is None

    def test_pending_recompute_skipped_on_synthesize(self):
        """Pending recompute is NOT forced during synthesize/terminate."""
        engine, _, _ = self._make_engine()
        engine._pending_recompute_claim = "Verify formula X = Y"

        task = Task(task_id="TASK-003", task_type=TaskType.SYNTHESIZE, assigned_to="researcher")
        claim = engine._pending_recompute_claim
        engine._pending_recompute_claim = None
        if task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
            task = engine._make_recompute_task(claim)

        assert task.task_type == TaskType.SYNTHESIZE


class TestCriticRetry:
    """Test critic retry on underflow (silent failure)."""

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
            engine.critic = MagicMock()
        return engine

    def test_critic_retry_on_low_tokens(self):
        """Critic retried when output_tokens < 200."""
        engine = self._make_engine()
        low_response = LLMResponse(
            text="OK", input_tokens=5000, output_tokens=23,
            stop_reason="end_turn", duration=1.0,
        )
        normal_response = LLMResponse(
            text="## CRIT-001\nReal critique.", input_tokens=5000,
            output_tokens=800, stop_reason="end_turn", duration=2.0,
        )
        engine.critic.run = MagicMock(side_effect=[low_response, normal_response])

        task = Task(task_id="TASK-005", task_type=TaskType.CRITIQUE, assigned_to="deep_critic")
        result = engine._dispatch(task)

        assert result == "deep_critic"
        assert engine.critic.run.call_count == 2
        engine.metrics.alert.assert_called_once()

    def test_critic_no_retry_on_normal_tokens(self):
        """Critic NOT retried when output_tokens >= 200."""
        engine = self._make_engine()
        normal_response = LLMResponse(
            text="## CRIT-001\nReal critique.", input_tokens=5000,
            output_tokens=800, stop_reason="end_turn", duration=2.0,
        )
        engine.critic.run = MagicMock(return_value=normal_response)

        task = Task(task_id="TASK-005", task_type=TaskType.CRITIQUE, assigned_to="deep_critic")
        result = engine._dispatch(task)

        assert result == "deep_critic"
        assert engine.critic.run.call_count == 1
        engine.metrics.alert.assert_not_called()


class TestApplyOverrides:
    """Test the consolidated _apply_overrides method."""

    def _make_engine(self, max_iterations=20, current_iteration=5,
                     last_critic_iteration=0, critic_every_n=4,
                     pending_recompute=None, stale_iterations=0,
                     research_state=""):
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
            ws.read_file = MagicMock(return_value=research_state)

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
            engine._stale_iterations = stale_iterations
            engine._stalled_claims = set()
            engine._pending_recompute_claim = pending_recompute
        return engine, written

    def test_budget_override_highest_priority(self):
        """Budget enforcement (P1) overrides everything when <=1 iteration left."""
        engine, written = self._make_engine(
            max_iterations=10, current_iteration=10,
            last_critic_iteration=0, critic_every_n=4,  # critic overdue too
            pending_recompute="some claim",  # recompute pending too
        )
        task = Task(
            task_id="TASK-010", task_type=TaskType.COMPUTE,
            assigned_to="computationalist", iteration=10,
        )
        result = engine._apply_overrides(task)

        assert result.task_type == TaskType.SYNTHESIZE
        assert "CURRENT_TASK.md" in written
        engine.metrics.alert.assert_called_once()

    def test_stale_loop_forces_synthesize_not_break(self):
        """Stale loop (P2) forces SYNTHESIZE instead of breaking."""
        # Need ER count >= min_er_for_completion and WH count == 0
        state_text = "## ER-001\nSome result\n\n## ER-002\nAnother result\n"
        engine, written = self._make_engine(
            max_iterations=20, current_iteration=5,
            stale_iterations=1,  # already 1 stale, this will be 2nd
            research_state=state_text,
        )
        engine.config.min_er_for_completion = 2

        task = Task(
            task_id="TASK-005", task_type=TaskType.RESEARCH,
            assigned_to="researcher", iteration=5,
        )
        result = engine._apply_overrides(task)

        assert result.task_type == TaskType.SYNTHESIZE
        assert engine._stale_iterations == 2

    def test_forced_critic_overrides_task(self):
        """Forced critic (P3) replaces non-critique task when overdue."""
        engine, written = self._make_engine(
            max_iterations=20, current_iteration=8,
            last_critic_iteration=0, critic_every_n=4,
        )
        task = Task(
            task_id="TASK-008", task_type=TaskType.RESEARCH,
            assigned_to="researcher", iteration=8,
        )
        result = engine._apply_overrides(task)

        assert result.task_type == TaskType.CRITIQUE
        assert result.assigned_to == "deep_critic"

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
        assert engine._pending_recompute_claim is None  # consumed

    def test_enrichment_non_overriding(self):
        """P5 enrichment mutates task body without changing task type."""
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

        # P1 doesn't trigger (budget OK), P2 resets stale, P3 skips critique tasks,
        # P4 skips synthesize/terminate
        assert result.task_type == TaskType.SYNTHESIZE

    def test_terminate_never_overridden_by_budget_or_stale(self):
        """TERMINATE tasks are not overridden by budget or stale checks."""
        engine, written = self._make_engine(
            max_iterations=10, current_iteration=10,  # budget exhausted
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
            engine._stale_iterations = 0
            engine._pending_recompute_claim = None
            engine.problem_meta = {}
            engine._pending_violations = []
            engine._pending_termination_blockers = []
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
        engine._pending_violations = [
            Violation(
                check="test_check", severity=ViolationSeverity.ERROR,
                message="Something wrong", file="TEST.md",
            ),
        ]
        prefix = engine._build_context_prefix()

        assert "POST-INTEGRATION VIOLATIONS" in prefix
        assert "test_check" in prefix
        assert "Something wrong" in prefix
        assert len(engine._pending_violations) == 0  # consumed

    def test_build_context_prefix_with_blockers(self):
        """Context prefix includes termination blockers."""
        engine, _ = self._make_engine()
        engine._pending_termination_blockers = [
            "Missing numerical verification",
            "Unresolved critiques remain",
        ]
        prefix = engine._build_context_prefix()

        assert "TERMINATION BLOCKED" in prefix
        assert "Missing numerical verification" in prefix
        assert "Unresolved critiques remain" in prefix
        assert len(engine._pending_termination_blockers) == 0  # consumed

    def test_build_context_prefix_empty_when_no_issues(self):
        """Context prefix is empty when no violations or blockers."""
        engine, _ = self._make_engine()
        prefix = engine._build_context_prefix()
        assert prefix == ""


class TestIsStaleLoop:
    """Test the _is_stale_loop detection."""

    def _make_engine(self, stale_iterations=0, research_state=""):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.read_file = MagicMock(return_value=research_state)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(min_er_for_completion=2)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 5
            engine._stale_iterations = stale_iterations
        return engine

    def test_synthesize_resets_stale_counter(self):
        """SYNTHESIZE resets _stale_iterations to 0."""
        engine = self._make_engine(stale_iterations=3)
        task = Task(task_id="T", task_type=TaskType.SYNTHESIZE, assigned_to="researcher")

        result = engine._is_stale_loop(task)

        assert result is False
        assert engine._stale_iterations == 0

    def test_terminate_resets_stale_counter(self):
        """TERMINATE resets _stale_iterations to 0."""
        engine = self._make_engine(stale_iterations=3)
        task = Task(task_id="T", task_type=TaskType.TERMINATE, assigned_to="orchestrator")

        result = engine._is_stale_loop(task)

        assert result is False
        assert engine._stale_iterations == 0

    def test_stale_detected_after_two_iterations(self):
        """Stale loop detected when 2+ iterations with complete-looking state."""
        state = "## ER-001\nResult 1\n\n## ER-002\nResult 2\n"
        engine = self._make_engine(stale_iterations=1, research_state=state)

        task = Task(task_id="T", task_type=TaskType.RESEARCH, assigned_to="researcher")
        result = engine._is_stale_loop(task)

        assert result is True
        assert engine._stale_iterations == 2

    def test_not_stale_on_first_iteration(self):
        """Not stale after just one iteration of complete-looking state."""
        state = "## ER-001\nResult 1\n\n## ER-002\nResult 2\n"
        engine = self._make_engine(stale_iterations=0, research_state=state)

        task = Task(task_id="T", task_type=TaskType.RESEARCH, assigned_to="researcher")
        result = engine._is_stale_loop(task)

        assert result is False
        assert engine._stale_iterations == 1

    def test_not_stale_when_wh_present(self):
        """Not stale when Working Hypotheses remain."""
        state = "## ER-001\nResult 1\n\n## ER-002\nResult 2\n\n## WH-001\nHypothesis\n"
        engine = self._make_engine(stale_iterations=5, research_state=state)

        task = Task(task_id="T", task_type=TaskType.RESEARCH, assigned_to="researcher")
        result = engine._is_stale_loop(task)

        assert result is False
        assert engine._stale_iterations == 0  # reset

    def test_not_stale_when_insufficient_er(self):
        """Not stale when fewer ERs than min_er_for_completion."""
        state = "## ER-001\nResult 1\n"
        engine = self._make_engine(stale_iterations=5, research_state=state)

        task = Task(task_id="T", task_type=TaskType.RESEARCH, assigned_to="researcher")
        result = engine._is_stale_loop(task)

        assert result is False
        assert engine._stale_iterations == 0  # reset


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


class TestStallDetection:
    """Test stall detection and blocking in _apply_overrides."""

    COMP_LOG_STALLED = """\
---
total_computations: 3
---

# Computations

## COMP-001: Check WH-001
**CLAIM**: Verify WH-001 formula
**VERDICT**: INCONCLUSIVE

## COMP-002: Check WH-001 retry
**CLAIM**: Verify WH-001 formula
**VERDICT**: INCONCLUSIVE

## COMP-003: Check WH-001 third try
**CLAIM**: Verify WH-001 formula
**VERDICT**: REFUTED
"""

    def _make_engine(self, comp_log: str = ""):
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
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.metrics.last_critic_iteration = 4
            engine.iteration = 5
            engine._stale_iterations = 0
            engine._stalled_claims = set()
            engine._pending_recompute_claim = None
            engine._pending_violations = []
            engine._pending_termination_blockers = []
            engine.problem_meta = {}
        return engine

    def test_stall_detected_after_threshold(self):
        engine = self._make_engine(self.COMP_LOG_STALLED)
        engine._update_stall_tracking()
        assert len(engine._stalled_claims) > 0
        assert any("WH-001" in c for c in engine._stalled_claims)

    def test_stalled_claim_blocked_in_overrides(self):
        engine = self._make_engine()
        engine._stalled_claims = {"WH-001"}
        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE,
            assigned_to="computationalist", iteration=5,
            body="Verify WH-001 formula",
        )
        result = engine._apply_overrides(task)
        assert result.task_type == TaskType.RESEARCH
        assert "Alternative Approach" in result.body

    def test_non_stalled_compute_passes_through(self):
        engine = self._make_engine()
        engine._stalled_claims = {"WH-001"}
        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE,
            assigned_to="computationalist", iteration=5,
            body="Verify WH-099 different formula",
        )
        result = engine._apply_overrides(task)
        assert result.task_type == TaskType.COMPUTE
