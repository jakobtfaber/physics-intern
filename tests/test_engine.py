"""Tests for SciRalph engine (compression thresholds, research status, budget enforcement)."""

from unittest.mock import MagicMock, patch, PropertyMock

from sciralph.config import Config
from sciralph.markdown import parse_frontmatter, render_frontmatter


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
        engine.compressor.run.assert_called_once_with({"target_file": "TEST.md"}, 1)

    def test_force_compression_at_2x(self):
        engine = self._make_engine({"TEST.md": 25_000})  # 2.5x
        engine._check_compression()
        engine.metrics.alert.assert_called_once()
        engine.compressor.run.assert_called_once_with({"target_file": "TEST.md"}, 1)


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
        task = {
            "task_id": "TASK-010",
            "task_type": "compute",
            "assigned_to": "computationalist",
            "priority": "high",
            "iteration": 10,
            "blocking_critiques": [],
            "target_file": "",
            "body": "Verify something.",
        }
        budget_remaining = engine.config.max_iterations - engine.iteration
        assert budget_remaining <= 1

        # Simulate budget enforcement logic
        if budget_remaining <= 1 and task["task_type"] not in ("synthesize", "terminate"):
            task = engine._make_budget_synthesize_task()

        assert task["task_type"] == "synthesize"
        assert task["assigned_to"] == "researcher"
        assert "CURRENT_TASK.md" in written
        assert "Budget-Enforced Synthesis" in written["CURRENT_TASK.md"]

    def test_budget_enforcement_allows_terminal(self):
        """synthesize/terminate are not overridden even at budget limit."""
        engine, _ = self._make_engine(max_iterations=10, current_iteration=10)
        for task_type in ("synthesize", "terminate"):
            task = {"task_type": task_type, "task_id": "TASK-010"}
            budget_remaining = engine.config.max_iterations - engine.iteration
            if budget_remaining <= 1 and task["task_type"] not in ("synthesize", "terminate"):
                task = engine._make_budget_synthesize_task()
            assert task["task_type"] == task_type  # unchanged

    def test_budget_enforcement_not_triggered_with_budget(self):
        """Plenty of budget -> no override."""
        engine, _ = self._make_engine(max_iterations=20, current_iteration=5)
        task = {"task_type": "compute", "task_id": "TASK-005"}
        budget_remaining = engine.config.max_iterations - engine.iteration
        assert budget_remaining > 1
        # No override
        if budget_remaining <= 1 and task["task_type"] not in ("synthesize", "terminate"):
            task = engine._make_budget_synthesize_task()
        assert task["task_type"] == "compute"  # unchanged


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

        task = {"task_type": "compute", "body": "Verify WH-003 mass limit"}
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

        task = {"task_type": "compute", "body": "Verify WH-099 something new"}
        engine._enrich_compute_task_with_prior_failures(task)

        assert "CURRENT_TASK.md" not in written  # write_file not called
