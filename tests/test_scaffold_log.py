"""Tests for EVENT_LOG.jsonl instrumentation."""

import json

from unittest.mock import MagicMock, patch

from sciralph.config import Config
from sciralph.markdown import render_frontmatter
from sciralph.task import Task, TaskType
from sciralph.workspace import log_llm_call, log_scaffold_event


class TestLogScaffoldEvent:
    """Core function tests for log_scaffold_event."""

    def test_creates_valid_jsonl(self, tmp_path):
        log_scaffold_event(tmp_path, iteration=2, layer=5, event="p1_budget_override",
                           detail="compute -> synthesize")
        logfile = tmp_path / "EVENT_LOG.jsonl"
        assert logfile.exists()
        entry = json.loads(logfile.read_text().strip())
        assert entry["kind"] == "scaffold"
        assert entry["iter"] == 2
        assert entry["layer"] == 5
        assert entry["event"] == "p1_budget_override"
        assert entry["detail"] == "compute -> synthesize"
        assert "ts" in entry

    def test_appends_multiple(self, tmp_path):
        log_scaffold_event(tmp_path, 1, 4, "phantom_references", "first")
        log_scaffold_event(tmp_path, 2, 5, "p3_forced_critic", "second")
        lines = (tmp_path / "EVENT_LOG.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "phantom_references"
        assert json.loads(lines[1])["event"] == "p3_forced_critic"

    def test_no_crash_on_bad_path(self):
        # Writing to a nonexistent directory should not raise
        log_scaffold_event("/nonexistent/dir/xyz", 1, 1, "api_retry", "test")

    def test_empty_detail(self, tmp_path):
        log_scaffold_event(tmp_path, 1, 8, "preamble_stripped")
        entry = json.loads((tmp_path / "EVENT_LOG.jsonl").read_text().strip())
        assert entry["detail"] == ""


class TestValidatePostIntegrationLogs:
    """Integration test: validate_post_integration writes Layer 4 events."""

    def test_violations_are_logged(self, tmp_path):
        """Set up workspace with a phantom reference, verify EVENT_LOG.jsonl gets Layer 4 event."""
        from sciralph.workspace import WorkspaceManager
        from sciralph.validation import validate_post_integration

        config = Config(workspace_dir=str(tmp_path / "ws"))
        ws = WorkspaceManager(config)
        ws.init("Test problem.")

        # Inject a phantom COMP reference into RESEARCH_STATE
        state = ws.read_file("RESEARCH_STATE.md")
        state = state.replace("# Open Questions", "COMP-999 is referenced here.\n\n# Open Questions")
        ws.write_file("RESEARCH_STATE.md", state)

        violations = validate_post_integration(ws, config, iteration=3)
        assert len(violations) > 0

        logfile = ws.root / "EVENT_LOG.jsonl"
        assert logfile.exists()
        entries = [json.loads(line) for line in logfile.read_text().strip().split("\n")]
        layer4 = [e for e in entries if e["layer"] == 4]
        assert len(layer4) > 0
        assert any(e["event"] == "phantom_references" for e in layer4)
        assert all(e["iter"] == 3 for e in layer4)


class TestBudgetOverrideLogs:
    """Integration test: P1 budget override writes Layer 5 event."""

    def test_budget_override_logs_event(self, tmp_path):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = ws_dir
            ws.logs_dir = str(tmp_path / "logs")
            written = {}

            def capture_write(filename, content):
                written[filename] = content
            ws.write_file = MagicMock(side_effect=capture_write)
            ws.read_file = MagicMock(return_value="")

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(max_iterations=10)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 10
            engine._stale_iterations = 0
            engine._pending_recompute_claim = None
            engine._pending_recompute_verdict = None
            engine._stalled_claims = set()
            engine._claim_failure_count = {}
            engine._pending_violations = []
            engine._displaced_tasks = []
            engine._agent_failures = []

            task = Task(
                task_id="TASK-010", task_type=TaskType.COMPUTE,
                assigned_to="computationalist", priority="high",
                iteration=10, body="Verify something.",
            )
            result = engine._apply_overrides(task)

        assert result.task_type == TaskType.SYNTHESIZE

        logfile = ws_dir / "EVENT_LOG.jsonl"
        assert logfile.exists()
        entry = json.loads(logfile.read_text().strip())
        assert entry["layer"] == 5
        assert entry["event"] == "p1_budget_override"
        assert "compute -> synthesize" in entry["detail"]


class TestLogLlmCall:
    """Tests for the log_llm_call function."""

    def test_creates_valid_jsonl(self, tmp_path):
        log_llm_call(
            tmp_path, agent="orchestrator", iteration=1, model="claude-sonnet-4-6",
            input_tokens=1908, output_tokens=802, stop_reason="end_turn",
            duration_s=8.53, system_prompt_chars=5000, user_content_chars=3000,
            response_chars=1500,
        )
        logfile = tmp_path / "EVENT_LOG.jsonl"
        assert logfile.exists()
        entry = json.loads(logfile.read_text().strip())
        assert entry["kind"] == "llm_call"
        assert entry["agent"] == "orchestrator"
        assert entry["iter"] == 1
        assert entry["model"] == "claude-sonnet-4-6"
        assert entry["input_tokens"] == 1908
        assert entry["output_tokens"] == 802
        assert entry["stop_reason"] == "end_turn"
        assert entry["duration_s"] == 8.53
        assert entry["reasoning_tokens"] == 0
        assert entry["round"] == 0
        assert "ts" in entry

    def test_interleaves_with_scaffold_events(self, tmp_path):
        log_scaffold_event(tmp_path, 1, 5, "p1_budget_override", "first")
        log_llm_call(
            tmp_path, agent="researcher", iteration=1, model="m",
            input_tokens=100, output_tokens=50, stop_reason="end_turn",
            duration_s=1.0, system_prompt_chars=100, user_content_chars=200,
            response_chars=50,
        )
        log_scaffold_event(tmp_path, 2, 6, "dispatch_failure", "second")
        lines = (tmp_path / "EVENT_LOG.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3
        assert json.loads(lines[0])["kind"] == "scaffold"
        assert json.loads(lines[1])["kind"] == "llm_call"
        assert json.loads(lines[2])["kind"] == "scaffold"

    def test_no_crash_on_bad_path(self):
        log_llm_call(
            "/nonexistent/dir/xyz", agent="test", iteration=1, model="m",
            input_tokens=0, output_tokens=0, stop_reason="end_turn",
            duration_s=0.0, system_prompt_chars=0, user_content_chars=0,
            response_chars=0,
        )

    def test_reasoning_tokens_and_round(self, tmp_path):
        log_llm_call(
            tmp_path, agent="computationalist", iteration=3, model="m",
            input_tokens=500, output_tokens=200, stop_reason="tool_use",
            duration_s=2.5, system_prompt_chars=1000, user_content_chars=2000,
            response_chars=500, reasoning_tokens=100, answer_tokens=100,
            round=2,
        )
        entry = json.loads((tmp_path / "EVENT_LOG.jsonl").read_text().strip())
        assert entry["reasoning_tokens"] == 100
        assert entry["answer_tokens"] == 100
        assert entry["round"] == 2
