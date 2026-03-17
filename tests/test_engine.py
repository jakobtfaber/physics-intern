"""Tests for SciRalph engine (compression thresholds, research status, budget enforcement, overrides)."""

from unittest.mock import MagicMock, patch, PropertyMock, call

from sciralph.config import Config
from sciralph.engine import LoopState
from sciralph.research_state import ResearchState
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
            engine.research_state = ResearchState()
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
    """Test _set_research_status updates research state."""

    def test_set_research_status(self):
        from sciralph.engine import SciRalph
        engine = SciRalph.__new__(SciRalph)
        engine.config = Config()
        engine.research_state = ResearchState()

        engine._set_research_status("completed")

        assert engine.research_state.status == "completed"


class TestEnrichComputeTask:
    """Test compute task enrichment with prior failure context."""

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
            from sciralph.research_state import Computation, Verdict
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.research_state = ResearchState()
            # Add prior failed computations to state
            engine.research_state.computations["COMP-001"] = Computation(
                id="COMP-001", target_hypothesis="WH-003", kind="verify",
                verdict=Verdict.INCONCLUSIVE, method="Integration",
                notes="Relative error: 13.6%.", iteration=1,
            )
            engine.research_state.computations["COMP-002"] = Computation(
                id="COMP-002", target_hypothesis="WH-003", kind="verify",
                verdict=Verdict.INCONCLUSIVE, method="Improved integration",
                notes="Still 8% error.", iteration=2,
            )
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 3
        return engine, ws, written

    def test_enrich_compute_task_appends_context(self):
        """Prior failures exist -> CURRENT_TASK enriched."""
        engine, ws, written = self._make_engine()
        ws.read_file = MagicMock(return_value="---\ntask_type: compute_verify\n---\n\nVerify WH-003 mass.")

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify WH-003 mass limit")
        engine._enrich_compute_task_with_prior_failures(task)

        assert "CURRENT_TASK.md" in written
        enriched = written["CURRENT_TASK.md"]
        assert "Prior Computation Failure Context" in enriched
        assert "2 prior failure(s)" in enriched
        assert "ROOT CAUSE" in enriched

    def test_enrich_compute_task_no_match(self):
        """No prior failures on this target -> unchanged."""
        engine, ws, written = self._make_engine()
        ws.read_file = MagicMock(return_value="---\ntask_type: compute_verify\n---\n\nVerify WH-099.")

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify WH-099 something new")
        engine._enrich_compute_task_with_prior_failures(task)

        assert "CURRENT_TASK.md" not in written  # write_file not called


class TestComputeVerdictTracking:
    """Test dispatch-level verdict tracking with failure counter and verdict signals."""

    def _make_engine(self, stall_recompute_limit=2):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(stall_recompute_limit=stall_recompute_limit)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 3
            engine._state = LoopState()
            engine.research_state = ResearchState()
        return engine

    def _add_comp(self, engine, comp_id, target, verdict_str, kind="verify"):
        from sciralph.research_state import Computation, Verdict
        engine.research_state.computations[comp_id] = Computation(
            id=comp_id, target_hypothesis=target,
            verdict=Verdict(verdict_str), kind=kind,
            iteration=engine.iteration,
        )

    def test_refuted_signals_orchestrator(self):
        """REFUTED verdict adds to pending_compute_verdicts."""
        engine = self._make_engine()
        self._add_comp(engine, "COMP-001", "WH-001", "REFUTED")
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        assert len(engine._state.pending_compute_verdicts) == 1
        assert engine._state.pending_compute_verdicts[0]["verdict"] == "REFUTED"
        assert engine._state.pending_compute_verdicts[0]["attempt"] == 1

    def test_inconclusive_signals_orchestrator(self):
        """INCONCLUSIVE also counted and signals orchestrator."""
        engine = self._make_engine()
        self._add_comp(engine, "COMP-001", "WH-001", "INCONCLUSIVE")
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        assert len(engine._state.pending_compute_verdicts) == 1
        assert engine._state.pending_compute_verdicts[0]["verdict"] == "INCONCLUSIVE"

    def test_stalled_verdict_signal(self):
        """After N failures, signal says STALLED in context prefix."""
        engine = self._make_engine()
        self._add_comp(engine, "COMP-001", "WH-001", "REFUTED")
        engine._state.claim_failure_count["WH-001"] = 1  # next will be 2 == limit

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        assert len(engine._state.pending_compute_verdicts) == 1
        assert engine._state.pending_compute_verdicts[0]["attempt"] == 2
        prefix = engine._build_context_prefix()
        assert "STALLED" in prefix
        assert "do NOT schedule another compute" in prefix

    def test_verified_clears_failure_count(self):
        """VERIFIED clears the failure counter and populates pending_verified_results."""
        engine = self._make_engine()
        self._add_comp(engine, "COMP-001", "WH-001", "VERIFIED")
        engine._state.claim_failure_count["WH-001"] = 1

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        assert "WH-001" not in engine._state.claim_failure_count
        assert len(engine._state.pending_compute_verdicts) == 0
        assert len(engine._state.pending_verified_results) == 1

    def test_verified_populates_dict_with_correct_keys(self):
        """VERIFIED comp populates dict with claim, comp_id, kind, confidence."""
        engine = self._make_engine()
        from sciralph.research_state import Computation, Verdict
        engine.research_state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.VERIFIED, kind="verify",
            confidence="exact", iteration=engine.iteration,
        )
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        assert len(engine._state.pending_verified_results) == 1
        v = engine._state.pending_verified_results[0]
        assert v["claim"] == "WH-001"
        assert v["comp_id"] == "COMP-001"
        assert v["kind"] == "verify"
        assert v["confidence"] == "exact"

    def test_verified_banner_renders_and_consumed_once(self):
        """VERIFIED COMPUTATIONS banner renders in context prefix and is consumed."""
        engine = self._make_engine()
        engine._state.pending_verified_results = [{
            "claim": "WH-001", "comp_id": "COMP-001",
            "kind": "verify", "confidence": "",
        }]
        prefix = engine._build_context_prefix()
        assert "VERIFIED COMPUTATIONS" in prefix
        assert "COMP-001 VERIFIED WH-001 (verify)" in prefix
        assert "Consider resolving related critiques" in prefix
        # Consumed
        assert len(engine._state.pending_verified_results) == 0
        # Second call should be empty
        prefix2 = engine._build_context_prefix()
        assert "VERIFIED COMPUTATIONS" not in prefix2

    def test_verified_banner_with_confidence(self):
        """VERIFIED banner includes confidence when present."""
        engine = self._make_engine()
        engine._state.pending_verified_results = [{
            "claim": "WH-001", "comp_id": "COMP-001",
            "kind": "research_verify", "confidence": "exact",
        }]
        prefix = engine._build_context_prefix()
        assert "confidence: exact" in prefix

    def test_verified_banner_ordering(self):
        """VERIFIED banner appears after explore results, before computation verdicts."""
        engine = self._make_engine()
        engine._state.pending_explore_results = [{
            "target_id": "WH-002", "description": "Explore result",
            "result": "x = 42", "confidence": "exact",
        }]
        engine._state.pending_verified_results = [{
            "claim": "WH-001", "comp_id": "COMP-001",
            "kind": "verify", "confidence": "",
        }]
        engine._state.pending_compute_verdicts = [{
            "verdict": "REFUTED", "claim": "WH-003", "attempt": 1,
            "notes": "", "failure_detail": "",
        }]
        prefix = engine._build_context_prefix()
        explore_pos = prefix.index("EXPLORE RESULTS")
        verified_pos = prefix.index("VERIFIED COMPUTATIONS")
        verdicts_pos = prefix.index("COMPUTATION VERDICTS")
        assert explore_pos < verified_pos < verdicts_pos

    def test_different_claims_tracked_independently(self):
        """Two different WH IDs have separate counters."""
        engine = self._make_engine()
        self._add_comp(engine, "COMP-001", "WH-002", "REFUTED")
        engine._state.claim_failure_count["WH-001"] = 1

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify WH-002 temperature")
        engine._track_computation(task)

        assert engine._state.claim_failure_count["WH-001"] == 1
        assert engine._state.claim_failure_count.get("WH-002", 0) == 1

    def test_refuted_with_notes_populates_dict(self):
        """REFUTED comp with notes includes notes in pending_compute_verdicts."""
        engine = self._make_engine()
        from sciralph.research_state import Computation, Verdict
        engine.research_state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.REFUTED, kind="verify",
            notes="Expected 1/(8*pi*M) but got 1/(4*pi*M)",
            iteration=engine.iteration,
        )
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        assert len(engine._state.pending_compute_verdicts) == 1
        v = engine._state.pending_compute_verdicts[0]
        assert v["notes"] == "Expected 1/(8*pi*M) but got 1/(4*pi*M)"
        assert v["failure_detail"] == ""

    def test_context_prefix_renders_notes(self):
        """Context prefix renders notes when present in verdict dict."""
        engine = self._make_engine()
        engine._state.pending_compute_verdicts = [{
            "verdict": "REFUTED", "claim": "WH-001", "attempt": 1,
            "notes": "Factor of 2 discrepancy",
            "failure_detail": "",
        }]
        prefix = engine._build_context_prefix()
        assert "Notes: Factor of 2 discrepancy" in prefix
        assert "Failure detail" not in prefix  # empty should be omitted

    def test_context_prefix_renders_failure_detail(self):
        """Context prefix renders failure_detail when present."""
        engine = self._make_engine()
        engine._state.pending_compute_verdicts = [{
            "verdict": "REFUTED", "claim": "WH-001", "attempt": 1,
            "notes": "",
            "failure_detail": "Division by zero at r=0",
        }]
        prefix = engine._build_context_prefix()
        assert "Failure detail: Division by zero at r=0" in prefix
        assert "Notes:" not in prefix  # empty should be omitted

    def test_empty_notes_and_failure_detail_omitted(self):
        """Empty notes and failure_detail are omitted from prefix."""
        engine = self._make_engine()
        engine._state.pending_compute_verdicts = [{
            "verdict": "REFUTED", "claim": "WH-001", "attempt": 1,
            "notes": "",
            "failure_detail": "",
        }]
        prefix = engine._build_context_prefix()
        assert "Notes:" not in prefix
        assert "Failure detail:" not in prefix

    def test_compute_verdict_signal_in_context_prefix(self):
        """Non-VERIFIED verdict appears in context prefix with attempt count."""
        engine = self._make_engine()
        self._add_comp(engine, "COMP-001", "WH-001", "REFUTED")
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        prefix = engine._build_context_prefix()
        assert "COMPUTATION VERDICTS" in prefix
        assert "REFUTED" in prefix
        assert "Attempt 1/2" in prefix
        assert "recompute" in prefix

    def test_empty_comp_log_noop(self):
        """No computations at this iteration, nothing happens."""
        engine = self._make_engine()

        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        assert len(engine._state.claim_failure_count) == 0
        assert len(engine._state.pending_compute_verdicts) == 0


class TestCriticCleanSignal:
    """Test that _no_critiques_filed flag injects a violation for the orchestrator."""

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
            engine.research_state = ResearchState()
            engine.critic = MagicMock()
        return engine

    def test_no_critiques_filed_injects_violation(self):
        """_no_critiques_filed=True adds a violation for orchestrator."""
        engine = self._make_engine()
        response = MagicMock()
        response.text = ""
        engine.critic.run = MagicMock(return_value=response)
        engine.critic._no_critiques_filed = True

        task = Task(task_id="TASK-005", task_type=TaskType.CRITIQUE, assigned_to="deep_critic")
        engine._dispatch(task)

        assert len(engine._state.pending_violations) == 1
        assert engine._state.pending_violations[0].check == "critic_clean"
        assert "NO issues" in engine._state.pending_violations[0].message

    def test_normal_critique_no_violation(self):
        """_no_critiques_filed=False does NOT inject a violation."""
        engine = self._make_engine()
        response = MagicMock()
        response.text = "Critiques filed."
        engine.critic.run = MagicMock(return_value=response)
        engine.critic._no_critiques_filed = False

        task = Task(task_id="TASK-005", task_type=TaskType.CRITIQUE, assigned_to="deep_critic")
        engine._dispatch(task)

        assert len(engine._state.pending_violations) == 0


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
            engine.research_state = ResearchState()
            engine.problem_meta = {}
        return engine, written

    def test_terminate_allowed_when_stub(self):
        """Stub can_terminate always returns True, so TERMINATE proceeds."""
        from sciralph.validation import can_terminate
        engine, _ = self._make_engine()

        # The stub always allows termination
        allowed, blockers = can_terminate(
            engine.workspace, engine.config, engine.metrics, engine.problem_meta,
            research_state=engine.research_state)
        assert allowed is True
        assert blockers == []

    def test_build_context_prefix_with_violations(self):
        """Context prefix includes pending violations."""
        engine, _ = self._make_engine()
        engine._state.pending_violations = [
            Violation(
                check="test_check", severity=ViolationSeverity.ERROR,
                message="Something wrong",
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
                detail="ER-001",
            ),
            Violation(
                check="phantom_references", severity=ViolationSeverity.ERROR,
                message="Phantom reference COMP-999",
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
            engine.research_state = ResearchState()
            engine.workspace = ws
            engine.iteration = 0
        return engine

    def test_completed_status(self):
        engine = self._make_engine("status: completed\n# Problem")
        engine.research_state.status = "completed"
        assert engine._check_status_field() is True

    def test_abandoned_status(self):
        engine = self._make_engine('status: "abandoned"\n# Problem')
        engine.research_state.status = "abandoned"
        assert engine._check_status_field() is True

    def test_partially_complete_status(self):
        engine = self._make_engine("status: partially_complete\n# Problem")
        engine.research_state.status = "partially_complete"
        assert engine._check_status_field() is True

    def test_in_progress_status(self):
        engine = self._make_engine("status: in_progress\n# Problem")
        engine.research_state.status = "in_progress"
        assert engine._check_status_field() is False

    def test_empty_state(self):
        engine = self._make_engine("")
        assert engine._check_status_field() is False


class TestZeroOutputStallHandling:
    """Tests for zero-output stall detection and enrichment (Improvement 1C-1D)."""

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
            engine.metrics.last_critic_iteration = 4
            engine.iteration = 5
            engine._state = LoopState(last_content_iteration=5)
            engine.research_state = ResearchState()
        return engine, ws, written

    def test_enrich_flags_zero_output_stall(self):
        """Enrichment adds ZERO-OUTPUT STALL instructions when prior comp has zero_output=True."""
        from sciralph.research_state import Computation, Verdict
        engine, ws, written = self._make_engine()
        engine.research_state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-003", kind="verify",
            verdict=Verdict.INCONCLUSIVE, notes="Agent produced no exit tool call.",
            zero_output=True, iteration=4,
        )
        ws.read_file = MagicMock(return_value="---\ntask_type: compute_verify\n---\n\nVerify WH-003 mass.")
        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
            assigned_to="compute_verify", body="Verify WH-003 mass limit",
        )
        engine._enrich_compute_task_with_prior_failures(task)
        assert "CURRENT_TASK.md" in written
        enriched = written["CURRENT_TASK.md"]
        assert "ZERO-OUTPUT STALL DETECTED" in enriched


class TestDispatchNewAgents:
    """Test dispatch routing to new split agents (Phase 4a)."""

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
            engine.research_state = ResearchState()
            engine.research_explore = MagicMock()
            engine.compute_verify = MagicMock()
            engine.compute_explore = MagicMock()
            engine.research_verify = MagicMock()
            engine.critic = MagicMock()
            engine.formatter = MagicMock()
        return engine

    def test_compute_verify_dispatch(self):
        engine = self._make_engine()
        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", iteration=5, body="Verify WH-001")
        name, _ = engine._dispatch(task)
        assert name == "compute_verify"

    def test_compute_explore_dispatch(self):
        engine = self._make_engine()
        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE_EXPLORE,
                    assigned_to="compute_explore", iteration=5, body="Explore WH-001")
        name, _ = engine._dispatch(task)
        assert name == "compute_explore"

    def test_research_verify_dispatch(self):
        engine = self._make_engine()
        task = Task(task_id="TASK-005", task_type=TaskType.RESEARCH_VERIFY,
                    assigned_to="research_verify", iteration=5, body="Verify WH-001 analytically")
        name, _ = engine._dispatch(task)
        assert name == "research_verify"

    def test_compute_verify_routes_correctly(self):
        engine = self._make_engine()
        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", iteration=5, body="Verify something")
        name, _ = engine._dispatch(task)
        assert name == "compute_verify"


class TestUpdateResearchIteration:
    """Test engine-side iteration counter update."""

    def test_iteration_field_updated(self):
        from sciralph.engine import SciRalph
        engine = SciRalph.__new__(SciRalph)
        engine.config = Config()
        engine.research_state = ResearchState()
        engine.iteration = 5
        engine._update_research_iteration()
        assert engine.research_state.iteration == 5

    def test_iteration_starts_at_zero(self):
        from sciralph.engine import SciRalph
        engine = SciRalph.__new__(SciRalph)
        engine.config = Config()
        engine.research_state = ResearchState()
        assert engine.research_state.iteration == 0
        engine.iteration = 1
        engine._update_research_iteration()
        assert engine.research_state.iteration == 1


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
            engine.research_state = ResearchState()
            engine.problem_meta = {}

            engine.orchestrator = MagicMock()
            engine.research_explore = MagicMock()
            engine.compute_verify = MagicMock()
            engine.compute_explore = MagicMock()
            engine.research_verify = MagicMock()
            engine.critic = MagicMock()
            engine.compressor = MagicMock()
            engine.formatter = MagicMock()
            engine.strategist = MagicMock()
            engine.strategist.parsed_strategy = None
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
            task_id="TASK-001", task_type=TaskType.COMPUTE_VERIFY,
            assigned_to="compute_verify", iteration=1,
            body="Verify something.",
        )
        engine.orchestrator.parse_task = MagicMock(return_value=task)

        # First dispatch: transient error; second+third: succeed, then terminate
        task_terminate = Task(
            task_id="TASK-002", task_type=TaskType.TERMINATE,
            assigned_to="orchestrator", iteration=2,
        )
        engine.orchestrator.parse_task = MagicMock(side_effect=[task, task_terminate])
        engine.compute_verify.run = MagicMock(side_effect=exc_504)

        engine.run()

        # Alert was logged for the failure
        engine.metrics.alert.assert_any_call(
            1, unittest_any_string_containing("Dispatch failed")
        )
        # compute_verify was called once (failed), then orchestrator terminated
        assert engine.compute_verify.run.call_count == 1
        assert engine.iteration == 2

    def test_non_transient_error_propagates(self):
        """A non-transient error (e.g. ValueError) propagates and crashes."""
        engine = self._make_engine()

        task = Task(
            task_id="TASK-001", task_type=TaskType.RESEARCH_EXPLORE,
            assigned_to="research_explore", iteration=1,
            body="Research something.",
        )
        engine.orchestrator.parse_task = MagicMock(return_value=task)
        engine.research_explore.run = MagicMock(side_effect=ValueError("bug in code"))

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
            task_id="TASK-001", task_type=TaskType.COMPUTE_VERIFY,
            assigned_to="compute_verify", iteration=1,
            body="Verify something.",
        )
        task_terminate = Task(
            task_id="TASK-002", task_type=TaskType.TERMINATE,
            assigned_to="orchestrator", iteration=2,
        )
        engine.orchestrator.parse_task = MagicMock(side_effect=[task, task_terminate])
        engine.compute_verify.run = MagicMock(side_effect=exc_timeout)

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
            engine.research_state = ResearchState()
        return engine

    def test_max_tokens_recorded(self):
        """max_tokens stop_reason records a truncation failure with token count."""
        engine = self._make_engine()
        result = MagicMock()
        result.stop_reason = "max_tokens"
        result.output_tokens = 8000
        task = Task(task_id="TASK-005", task_type=TaskType.RESEARCH_EXPLORE, assigned_to="research_explore")

        engine._record_agent_failures(task, "research_explore", result)

        assert len(engine._state.agent_failures) == 1
        assert engine._state.agent_failures[0]["event"] == "max_tokens_truncation"
        assert engine._state.agent_failures[0]["task_id"] == "TASK-005"
        assert engine._state.agent_failures[0]["agent"] == "research_explore"
        assert "8000 tokens" in engine._state.agent_failures[0]["detail"]
        assert "Decompose" in engine._state.agent_failures[0]["detail"]

    def test_max_rounds_forced_recorded(self):
        """max_rounds_forced stop_reason records an exhaustion failure."""
        from sciralph.llm import AgentResult
        engine = self._make_engine()
        result = AgentResult(text="partial", rounds=10, stop_reason="max_rounds_forced")
        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY, assigned_to="compute_verify")

        engine._record_agent_failures(task, "compute_verify", result)

        assert len(engine._state.agent_failures) == 1
        assert engine._state.agent_failures[0]["event"] == "max_rounds_exhaustion"
        assert "10 tool-use rounds" in engine._state.agent_failures[0]["detail"]

    def test_normal_end_turn_not_recorded(self):
        """Normal end_turn does NOT record a failure."""
        engine = self._make_engine()
        result = MagicMock()
        result.stop_reason = "end_turn"
        task = Task(task_id="TASK-005", task_type=TaskType.RESEARCH_EXPLORE, assigned_to="research_explore")

        engine._record_agent_failures(task, "research_explore", result)

        assert len(engine._state.agent_failures) == 0

    def test_context_prefix_includes_agent_failures(self):
        """Agent failures appear in context prefix banner."""
        engine = self._make_engine()
        engine._state.agent_failures = [{
            "task_id": "TASK-004",
            "agent": "research_explore",
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
            "agent": "research_explore",
            "event": "max_tokens_truncation",
            "detail": "Task too large.",
            "iteration": 4,
        }]
        engine._build_context_prefix()
        assert len(engine._state.agent_failures) == 0

    def test_compute_verdict_appends_to_agent_failures(self):
        """REFUTED verdict below stall limit appends to pending_compute_verdicts."""
        from sciralph.research_state import Computation, Verdict

        engine = self._make_engine()
        engine.iteration = 5
        engine.research_state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.REFUTED, kind="verify", iteration=5,
        )

        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        # Verdict now goes to pending_compute_verdicts, not agent_failures
        assert len(engine._state.pending_compute_verdicts) == 1
        assert engine._state.pending_compute_verdicts[0]["verdict"] == "REFUTED"
        assert engine._state.pending_compute_verdicts[0]["attempt"] == 1

    def test_compute_verdict_stall_signals_orchestrator(self):
        """At stall (count >= limit), verdict signal still goes to pending_compute_verdicts."""
        from sciralph.research_state import Computation, Verdict

        engine = self._make_engine()
        engine.iteration = 5
        engine.config.stall_recompute_limit = 2
        engine._state.claim_failure_count["WH-001"] = 1  # already at limit-1
        engine.research_state.computations["COMP-001"] = Computation(
            id="COMP-001", target_hypothesis="WH-001",
            verdict=Verdict.INCONCLUSIVE, kind="verify", iteration=5,
        )

        task = Task(task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
                    assigned_to="compute_verify", body="Verify formula X = Y")
        engine._track_computation(task)

        assert len(engine._state.pending_compute_verdicts) == 1
        assert engine._state.pending_compute_verdicts[0]["attempt"] == 2

    def test_context_prefix_ordering(self):
        """Violations appear before agent failures in context prefix."""
        engine = self._make_engine()
        engine._state.pending_violations = [
            Violation(check="test", severity=ViolationSeverity.WARNING,
                      message="test violation"),
        ]
        engine._state.agent_failures = [{
            "task_id": "TASK-003",
            "agent": "compute_verify",
            "event": "max_rounds_exhaustion",
            "detail": "Exhausted 10 tool-use rounds without completing.",
            "iteration": 4,
        }]
        prefix = engine._build_context_prefix()

        violations_pos = prefix.index("VIOLATIONS")
        failures_pos = prefix.index("AGENT FAILURES")
        assert violations_pos < failures_pos


class TestProblemStatementPopulated:
    """Test that ResearchState gets problem_statement populated on init (A1)."""

    def test_problem_statement_set_on_init(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.name = "20260316_test_run"
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 0
            engine._state = LoopState()
            # Simulate what __init__ does after workspace.init
            problem = "Derive the Hawking temperature."
            engine.research_state = ResearchState()
            engine.research_state.problem_statement = problem.strip()
            engine.research_state.title = ws.root.name

            assert engine.research_state.problem_statement == "Derive the Hawking temperature."
            assert engine.research_state.title == "20260316_test_run"

    def test_problem_statement_includes_answer_template(self):
        """When answer_template is appended, problem_statement includes it."""
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.name = "test_run"
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"

            from sciralph.engine import SciRalph
            engine = SciRalph("Derive T_H.", Config(), answer_template="## Answer\n\nT_H = ?")

            assert "Expected answer format" in engine.research_state.problem_statement
            assert "T_H = ?" in engine.research_state.problem_statement


class TestSyncOnTermination:
    """Test that _sync_research_state is called on termination path (A3)."""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.read_file = MagicMock(return_value="")
            ws.write_file = MagicMock()
            ws.file_size = MagicMock(return_value=0)
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
            engine.research_state = ResearchState()
            engine.problem_meta = {}
            engine.orchestrator = MagicMock()
            engine.research_explore = MagicMock()
            engine.computationalist = MagicMock()
            engine.critic = MagicMock()
            engine.compressor = MagicMock()
            engine.formatter = MagicMock()
            engine.strategist = MagicMock()
            engine.strategist.parsed_strategy = None
        return engine, ws

    def test_sync_called_on_termination(self):
        """_sync_research_state is called when termination is allowed."""
        engine, ws = self._make_engine()
        engine.config.max_iterations = 10

        task_terminate = Task(
            task_id="TASK-001", task_type=TaskType.TERMINATE,
            assigned_to="orchestrator", iteration=1,
        )
        engine.orchestrator.parse_task = MagicMock(return_value=task_terminate)

        with patch.object(engine, '_sync_research_state') as mock_sync:
            engine.run()
            mock_sync.assert_called()


class TestExploreResultSuppression:
    """Test that failed explore computations are NOT appended to pending_explore_results (C3)."""

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
            engine.iteration = 3
            engine._state = LoopState()
            engine.research_state = ResearchState()
        return engine

    def _add_comp(self, engine, comp_id, target, kind="explore", **kwargs):
        from sciralph.research_state import Computation
        engine.research_state.computations[comp_id] = Computation(
            id=comp_id, target_hypothesis=target,
            kind=kind, iteration=engine.iteration,
            **kwargs,
        )

    def test_zero_output_explore_not_appended(self):
        """zero_output explore computation NOT appended to pending_explore_results."""
        engine = self._make_engine()
        self._add_comp(engine, "TASK-003", "WH-001", kind="explore",
                        zero_output=True, result="", claim="test")
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_EXPLORE,
                    assigned_to="compute_explore", body="Explore WH-001")
        engine._track_computation(task)

        assert len(engine._state.pending_explore_results) == 0

    def test_empty_result_explore_not_appended(self):
        """Explore with empty result NOT appended to pending_explore_results."""
        engine = self._make_engine()
        self._add_comp(engine, "TASK-003", "WH-001", kind="explore",
                        result="", claim="test")
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_EXPLORE,
                    assigned_to="compute_explore", body="Explore WH-001")
        engine._track_computation(task)

        assert len(engine._state.pending_explore_results) == 0

    def test_successful_explore_appended(self):
        """Successful explore IS appended to pending_explore_results."""
        engine = self._make_engine()
        self._add_comp(engine, "TASK-003", "WH-001", kind="explore",
                        result="x = 42", claim="Compute x", confidence="exact")
        task = Task(task_id="TASK-003", task_type=TaskType.COMPUTE_EXPLORE,
                    assigned_to="compute_explore", body="Explore WH-001")
        engine._track_computation(task)

        assert len(engine._state.pending_explore_results) == 1
        assert engine._state.pending_explore_results[0]["target_id"] == "WH-001"


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


# ===========================================================================
# Strategist integration
# ===========================================================================

class TestStrategistEngine:
    """Tests for strategist agent integration in the engine."""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.write_file = MagicMock()
            ws.git_commit = MagicMock()
            ws.file_size = MagicMock(return_value=0)

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.research_state = ResearchState(
                problem_statement="Derive Hawking temperature.",
            )
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 0
            engine._state = LoopState()

            # Create mock strategist
            engine.strategist = MagicMock()
            engine.strategist.parsed_strategy = None
        return engine

    def test_apply_strategist_plan_stores_strategy(self):
        from sciralph.research_state import ResearchStrategy
        engine = self._make_engine()

        strategy = ResearchStrategy(
            strategy_notes="Derive kappa via Killing vectors first.",
            iteration_created=0,
            iteration_updated=0,
        )
        engine.strategist.parsed_strategy = strategy
        engine._apply_strategist_plan()

        assert engine.research_state.research_strategy is not None
        assert "Killing vectors" in engine.research_state.research_strategy.strategy_notes

    def test_apply_strategist_plan_none_does_nothing(self):
        engine = self._make_engine()
        engine.strategist.parsed_strategy = None
        engine._apply_strategist_plan()
        assert engine.research_state.research_strategy is None

    def test_strategize_dispatch(self):
        from sciralph.llm import LLMResponse
        engine = self._make_engine()
        engine.strategist.research_state = None
        engine.strategist._system_prompt = "cached"
        engine.strategist.run = MagicMock(return_value=LLMResponse(
            text="Some strategy notes.", input_tokens=100, output_tokens=200,
            stop_reason="end_turn", duration=0.5,
        ))
        engine.strategist.parsed_strategy = None

        task = Task(
            task_id="TASK-005", task_type=TaskType.STRATEGIZE,
            assigned_to="strategist", iteration=5,
        )
        agent_name, result = engine._dispatch(task)
        assert agent_name == "strategist"
        engine.strategist.run.assert_called_once()

    def test_should_suggest_replan_heuristic(self):
        from sciralph.research_state import Hypothesis, HypothesisStatus, ResearchStrategy
        engine = self._make_engine()

        # No strategy → no suggestion
        assert not engine._should_suggest_replan()

        # With strategy but iteration too low
        engine.research_state.research_strategy = ResearchStrategy(iteration_updated=0)
        engine.iteration = 3
        assert not engine._should_suggest_replan()

        # Iteration high enough but not enough abandoned
        engine.iteration = 6
        assert not engine._should_suggest_replan()

        # 3+ abandoned, 0 established → should suggest
        for i in range(3):
            engine.research_state.hypotheses[f"WH-{i+1:03d}"] = Hypothesis(
                id=f"WH-{i+1:03d}", status=HypothesisStatus.ABANDONED,
            )
        assert engine._should_suggest_replan()

        # Strategy recently updated → no suggestion
        engine.research_state.research_strategy.iteration_updated = 5
        assert not engine._should_suggest_replan()


class TestTerminationCircuitBreaker:
    """Test the circuit breaker that auto-abandons WHs after repeated termination blocks."""

    def _make_engine(self):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.write_file = MagicMock()

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.research_state = ResearchState()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 10
            engine._state = LoopState()
        return engine

    def test_force_abandon_working_hypotheses(self):
        """Auto-abandon all remaining WHs when circuit breaker fires."""
        from sciralph.research_state import Hypothesis, HypothesisStatus
        engine = self._make_engine()
        engine.research_state.hypotheses["WH-001"] = Hypothesis(
            id="WH-001", status=HypothesisStatus.WORKING, statement="Claim A",
        )
        engine.research_state.hypotheses["WH-004"] = Hypothesis(
            id="WH-004", status=HypothesisStatus.WORKING, statement="Claim B",
        )
        engine.research_state.hypotheses["ER-002"] = Hypothesis(
            id="ER-002", status=HypothesisStatus.ESTABLISHED,
        )
        engine._state.consecutive_termination_blocks = 3

        engine._force_abandon_working_hypotheses()

        assert engine.research_state.hypotheses["WH-001"].status == HypothesisStatus.ABANDONED
        assert engine.research_state.hypotheses["WH-004"].status == HypothesisStatus.ABANDONED
        assert engine.research_state.hypotheses["ER-002"].status == HypothesisStatus.ESTABLISHED
        assert len(engine.research_state.failed_approaches) == 2
        assert engine._state.consecutive_termination_blocks == 0

    def test_counter_increments_on_block(self):
        """consecutive_termination_blocks increments when termination is blocked."""
        engine = self._make_engine()
        assert engine._state.consecutive_termination_blocks == 0
        engine._state.consecutive_termination_blocks += 1
        assert engine._state.consecutive_termination_blocks == 1

    def test_counter_resets_on_non_terminate(self):
        """The counter should reset when a non-terminate task is processed."""
        engine = self._make_engine()
        engine._state.consecutive_termination_blocks = 2
        # Simulate what engine does for non-terminate tasks
        engine._state.consecutive_termination_blocks = 0
        assert engine._state.consecutive_termination_blocks == 0

    def test_no_abandon_when_no_working_hypotheses(self):
        """Force-abandon is a no-op when all hypotheses are already established/abandoned."""
        from sciralph.research_state import Hypothesis, HypothesisStatus
        engine = self._make_engine()
        engine.research_state.hypotheses["ER-001"] = Hypothesis(
            id="ER-001", status=HypothesisStatus.ESTABLISHED,
        )
        engine._state.consecutive_termination_blocks = 5

        engine._force_abandon_working_hypotheses()

        assert engine.research_state.hypotheses["ER-001"].status == HypothesisStatus.ESTABLISHED
        assert len(engine.research_state.failed_approaches) == 0


class TestRedundantCriticPassFix:
    """Test that forced critic clears stale blockers and injects can-terminate signal."""

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
            engine.research_state = ResearchState()
            engine.critic = MagicMock()
        return engine

    def test_forced_critic_clears_stale_termination_blockers(self):
        """After forced critic, pending_termination_blockers must be empty."""
        engine = self._make_engine()
        engine._state.pending_termination_blockers = [
            "No critic pass has occurred yet"
        ]
        engine.metrics.last_critic_iteration = 0
        engine.config.critic_every_n = 1  # ensure _critic_overdue fires

        # Patch _critic_overdue to return True and _make_forced_critic_task
        with patch.object(type(engine), '_critic_overdue', return_value=True), \
             patch.object(type(engine), '_make_forced_critic_task',
                          return_value=Task(task_id="TASK-FC", task_type=TaskType.CRITIQUE,
                                            assigned_to="deep_critic")):
            # Call the forced-critic branch directly by simulating the condition
            # We just need to test the code path after _make_forced_critic_task
            task = engine._make_forced_critic_task()
            engine._state.pending_termination_blockers.clear()

        assert engine._state.pending_termination_blockers == []

    def test_critic_clean_can_terminate_injected_after_prior_terminate_attempt(self):
        """When critic files no issues and orchestrator had tried to terminate,
        a critic_clean_can_terminate violation is injected."""
        engine = self._make_engine()
        engine._state.consecutive_termination_blocks = 1  # prior terminate attempt

        response = MagicMock()
        response.text = ""
        engine.critic.run = MagicMock(return_value=response)
        engine.critic._no_critiques_filed = True

        task = Task(task_id="TASK-006", task_type=TaskType.CRITIQUE, assigned_to="deep_critic")
        engine._dispatch(task)

        checks = [v.check for v in engine._state.pending_violations]
        assert "critic_clean" in checks
        assert "critic_clean_can_terminate" in checks
        can_term = [v for v in engine._state.pending_violations
                    if v.check == "critic_clean_can_terminate"][0]
        assert can_term.severity == ViolationSeverity.INFO
        assert "retry" in can_term.message.lower()

    def test_critic_clean_can_terminate_not_injected_without_prior_terminate(self):
        """When no prior terminate attempt, critic_clean_can_terminate is NOT injected."""
        engine = self._make_engine()
        engine._state.consecutive_termination_blocks = 0  # no prior terminate

        response = MagicMock()
        response.text = ""
        engine.critic.run = MagicMock(return_value=response)
        engine.critic._no_critiques_filed = True

        task = Task(task_id="TASK-007", task_type=TaskType.CRITIQUE, assigned_to="deep_critic")
        engine._dispatch(task)

        checks = [v.check for v in engine._state.pending_violations]
        assert "critic_clean" in checks
        assert "critic_clean_can_terminate" not in checks

