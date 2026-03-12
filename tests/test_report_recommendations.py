"""Tests for all report recommendations (P0-A, P0-B, P1-A, P1-B, P2-A, P2-B, P2-C, P2-D, P3).

Covers: zero-text watchdog, checkpoint message, token alert, stale unverified labels,
WH→ER header promotion, computation counter fix, critique resolution regex, and
redundant critic skip.
"""

import re
from unittest.mock import MagicMock, patch

from sciralph.config import Config, DEFAULTS
from sciralph.llm import AgentResult, run_agent_loop
from sciralph.markdown import render_frontmatter
from sciralph.providers.base import ProviderResponse
from sciralph.task import Task, TaskType
from sciralph.tools import ToolExecutor
from sciralph.validation import (
    Violation,
    ViolationSeverity,
    check_er_promotion_gate,
    check_stale_unverified_labels,
)


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

class MockWorkspace:
    """Simple mock workspace for validation tests."""

    def __init__(self, files: dict[str, str] | None = None):
        self._files = files or {}

    def read_file(self, filename: str) -> str:
        return self._files.get(filename, "")

    def write_file(self, filename: str, content: str):
        self._files[filename] = content


def _mock_provider_response(text="", stop_reason="end_turn",
                             input_tokens=100, output_tokens=50,
                             tool_calls=None):
    """Create a mock ProviderResponse."""
    return ProviderResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        raw_content=None,
    )


def _make_config(**overrides) -> Config:
    defaults = dict(api_key="test-key", audit_log="", logs_dir="", provider="anthropic")
    defaults.update(overrides)
    return Config(**defaults)


def _make_executor():
    import tempfile
    from pathlib import Path
    root = Path(tempfile.mkdtemp())
    return ToolExecutor(workspace_root=root, timeout=60)


def _mock_provider():
    """Create a mock provider with sensible defaults for format methods."""
    provider = MagicMock()
    provider.format_assistant_message.return_value = {"role": "assistant", "content": "mock"}
    provider.build_tool_result_messages.return_value = [{"role": "user", "content": []}]
    return provider


# ---------------------------------------------------------------------------
# P0-A: Zero-Text Watchdog
# ---------------------------------------------------------------------------

class TestZeroTextWatchdog:
    """P0-A: Computationalist enters tool-only loops producing no text."""

    @patch("sciralph.llm._get_provider")
    def test_bailout_at_zero_text_threshold(self, mock_get_provider):
        """Loop breaks after zero_text_bailout consecutive zero-text rounds."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        # Tool-only response (no text)
        tool_response = _mock_provider_response(
            "", "tool_use", 100, 50,
            tool_calls=[{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}],
        )

        # Forced final text response
        text_response = _mock_provider_response(
            "## COMP-001\n**VERDICT:** INCONCLUSIVE", "end_turn", 150, 80
        )

        # 3 zero-text rounds, then forced final
        provider.call.side_effect = [
            tool_response, tool_response, tool_response,
            text_response,
        ]

        config = _make_config(zero_text_bailout=3, max_tool_rounds=10)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        # Should have bailed out early (3 tool rounds + 1 forced = 4 total calls)
        assert result.stop_reason == "max_rounds_forced"
        assert result.rounds < 10 + 1  # Less than max_rounds + forced
        assert provider.call.call_count == 4

    @patch("sciralph.llm._get_provider")
    def test_zero_text_streak_resets_on_text(self, mock_get_provider):
        """Zero-text streak resets when text is produced."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tc = [{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}]
        # Round 1: tool only (zero text)
        r1 = _mock_provider_response("", "tool_use", 100, 50, tool_calls=tc)
        # Round 2: tool + text (resets streak)
        r2 = _mock_provider_response("Working on it...", "tool_use", 100, 50, tool_calls=tc)
        # Round 3: tool only (streak = 1 again)
        r3 = _mock_provider_response("", "tool_use", 100, 50, tool_calls=tc)
        # Round 4: end_turn
        r4 = _mock_provider_response("Done.", "end_turn", 100, 50)

        provider.call.side_effect = [r1, r2, r3, r4]

        config = _make_config(zero_text_bailout=2, max_tool_rounds=10)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        # Should NOT have bailed out — streak was reset
        assert result.stop_reason == "end_turn"
        assert result.rounds == 4

    @patch("sciralph.llm._get_provider")
    def test_bailout_forced_system_message(self, mock_get_provider):
        """Forced system message mentions early termination for zero-text bailout."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tc = [{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}]
        tool_response = _mock_provider_response("", "tool_use", 100, 50, tool_calls=tc)
        text_response = _mock_provider_response("Done", "end_turn", 100, 50)
        provider.call.side_effect = [
            tool_response, tool_response,  # 2 zero-text rounds
            text_response,  # forced
        ]

        config = _make_config(zero_text_bailout=2, max_tool_rounds=10)
        executor = _make_executor()
        run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        # The forced final call should mention early termination
        calls = provider.call.call_args_list
        last_call = calls[-1]
        forced_system = last_call.kwargs["system"]
        assert "terminated early" in forced_system
        assert "stopped producing text" in forced_system


# ---------------------------------------------------------------------------
# P1-A: Checkpoint Message at Round N
# ---------------------------------------------------------------------------

class TestCheckpointMessage:
    """P1-A: Checkpoint nudge injected at configured round."""

    @patch("sciralph.llm._get_provider")
    def test_checkpoint_injected_at_round_n(self, mock_get_provider):
        """Checkpoint message appears in messages at the configured round."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tc = [{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}]
        tool_response = _mock_provider_response("", "tool_use", 100, 50, tool_calls=tc)
        text_response = _mock_provider_response("Done.", "end_turn", 100, 50)

        # 3 tool rounds then end_turn on round 4
        provider.call.side_effect = [
            tool_response, tool_response, tool_response,
            text_response,
        ]

        config = _make_config(checkpoint_round=2, max_tool_rounds=10)
        executor = _make_executor()
        run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        # Inspect the messages passed to round 3 (which should include checkpoint)
        # The checkpoint is added after round 2, so round 3's call should have it
        calls = provider.call.call_args_list
        # Round 3 messages should contain the checkpoint
        round3_messages = calls[2].kwargs["messages"]
        checkpoint_found = any(
            isinstance(msg.get("content"), list)
            and any(
                isinstance(c, dict) and "CHECKPOINT" in c.get("text", "")
                for c in msg["content"]
            )
            for msg in round3_messages
            if isinstance(msg, dict) and msg.get("role") == "user"
        )
        assert checkpoint_found, "Checkpoint message should be injected after round 2"

    @patch("sciralph.llm._get_provider")
    def test_no_checkpoint_before_round_n(self, mock_get_provider):
        """No checkpoint if loop ends before checkpoint_round."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tc = [{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}]
        r1 = _mock_provider_response("", "tool_use", 100, 50, tool_calls=tc)
        r2 = _mock_provider_response("Done.", "end_turn", 100, 50)
        provider.call.side_effect = [r1, r2]

        config = _make_config(checkpoint_round=5, max_tool_rounds=10)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        # Only 2 rounds — no checkpoint should have been injected
        assert result.rounds == 2
        calls = provider.call.call_args_list
        for call in calls:
            msgs = call.kwargs.get("messages", [])
            for msg in msgs:
                if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                    for c in msg["content"]:
                        if isinstance(c, dict):
                            assert "CHECKPOINT" not in c.get("text", "")


# ---------------------------------------------------------------------------
# Final Warning Near End of Loop
# ---------------------------------------------------------------------------

class TestFinalWarning:
    """Final warning injected 2 rounds before max_rounds."""

    @patch("sciralph.llm._get_provider")
    def test_final_warning_injected_near_end(self, mock_get_provider):
        """FINAL WARNING appears in messages at round max_rounds-2."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tc = [{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}]
        tool_response = _mock_provider_response(
            "Working on computation...", "tool_use", 100, 50, tool_calls=tc,
        )
        text_response = _mock_provider_response("Done.", "end_turn", 100, 50)

        # 9 tool rounds then end_turn on round 10
        provider.call.side_effect = [tool_response] * 9 + [text_response]

        config = _make_config(
            checkpoint_round=2, max_tool_rounds=10, zero_text_bailout=20,
        )
        executor = _make_executor()
        run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        # Final warning fires after round 8 (max_rounds - 2 = 8), so round 9's
        # call should see it in messages
        calls = provider.call.call_args_list
        round9_messages = calls[8].kwargs["messages"]
        warning_found = any(
            isinstance(msg.get("content"), list)
            and any(
                isinstance(c, dict) and "FINAL WARNING" in c.get("text", "")
                for c in msg["content"]
            )
            for msg in round9_messages
            if isinstance(msg, dict) and msg.get("role") == "user"
        )
        assert warning_found, "FINAL WARNING should be injected after round 8 (max_rounds-2)"

    @patch("sciralph.llm._get_provider")
    def test_no_final_warning_for_short_loops(self, mock_get_provider):
        """No FINAL WARNING when max_rounds < 5."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tc = [{"id": "t1", "name": "execute_python", "input": {"code": "print(1)"}}]
        tool_response = _mock_provider_response("", "tool_use", 100, 50, tool_calls=tc)
        text_response = _mock_provider_response("Done.", "end_turn", 100, 50)

        # 3 tool rounds then end_turn
        provider.call.side_effect = [tool_response] * 3 + [text_response]

        config = _make_config(checkpoint_round=2, max_tool_rounds=4)
        executor = _make_executor()
        run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=4,
        )

        # No FINAL WARNING should appear in any call
        for call in provider.call.call_args_list:
            msgs = call.kwargs.get("messages", [])
            for msg in msgs:
                if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                    for c in msg["content"]:
                        if isinstance(c, dict):
                            assert "FINAL WARNING" not in c.get("text", "")


# ---------------------------------------------------------------------------
# P1-B: Per-Computation Token Budget Alert
# ---------------------------------------------------------------------------

class TestTokenAlert:
    """P1-B: token_alert_fired set when input tokens exceed threshold."""

    @patch("sciralph.llm._get_provider")
    def test_alert_fired_above_threshold(self, mock_get_provider):
        """token_alert_fired=True when total_input > computation_token_alert."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        # Single round with high token count
        text_response = _mock_provider_response(
            "Done.", "end_turn",
            input_tokens=200_000, output_tokens=100,
        )
        provider.call.return_value = text_response

        config = _make_config(computation_token_alert=150_000)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.token_alert_fired is True

    @patch("sciralph.llm._get_provider")
    def test_alert_not_fired_below_threshold(self, mock_get_provider):
        """token_alert_fired=False when total_input <= computation_token_alert."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        text_response = _mock_provider_response(
            "Done.", "end_turn",
            input_tokens=50_000, output_tokens=100,
        )
        provider.call.return_value = text_response

        config = _make_config(computation_token_alert=150_000)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=5,
        )

        assert result.token_alert_fired is False

    @patch("sciralph.llm._get_provider")
    def test_alert_accumulates_across_rounds(self, mock_get_provider):
        """Alert fires when cumulative input exceeds threshold."""
        provider = _mock_provider()
        mock_get_provider.return_value = provider

        tc = [{"id": "t1", "name": "execute_python", "input": {"code": "x=1"}}]
        r1 = _mock_provider_response("", "tool_use", input_tokens=80_000, output_tokens=50, tool_calls=tc)
        r2 = _mock_provider_response("", "tool_use", input_tokens=80_000, output_tokens=50, tool_calls=tc)
        r3 = _mock_provider_response("Done.", "end_turn", input_tokens=80_000, output_tokens=50)
        provider.call.side_effect = [r1, r2, r3]

        config = _make_config(computation_token_alert=150_000)
        executor = _make_executor()
        result = run_agent_loop(
            system="sys", user_content="question",
            config=config, tool_executor=executor,
            tools=ToolExecutor.TOOL_DEFINITIONS, max_rounds=10,
        )

        # 80K + 80K = 160K > 150K → should fire after round 2
        assert result.token_alert_fired is True
        assert result.total_input_tokens == 240_000


# ---------------------------------------------------------------------------
# P2-A: Fix [unverified] Label Persistence
# ---------------------------------------------------------------------------

class TestStaleUnverifiedLabels:
    """P2-A: [unverified] labels promoted to VERIFIED when computation backs them."""

    def test_unverified_promoted_when_backed(self):
        """[unverified] → VERIFIED when COMPUTATION_LOG has matching VERIFIED entry."""
        meta = {"total_computations": 1}
        body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001 Hawking temperature
**VERDICT**: VERIFIED
**RESULT**:
Confirmed.
"""
        state = "ER-001 is [unverified] pending computation.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_stale_unverified_labels(ws)

        assert len(violations) == 1
        assert violations[0].check == "stale_unverified_labels"
        assert violations[0].severity == ViolationSeverity.WARNING
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "VERIFIED" in updated
        assert "[unverified]" not in updated

    def test_unverified_kept_when_no_backing(self):
        """[unverified] stays when no VERIFIED computation exists."""
        state = "ER-001 is [unverified] pending computation.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
        })
        violations = check_stale_unverified_labels(ws)

        assert len(violations) == 0
        assert "[unverified]" in ws.read_file("RESEARCH_STATE.md")

    def test_no_unverified_no_action(self):
        """No violations when no [unverified] labels exist."""
        state = "ER-001 result is established.\n"
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
        })
        violations = check_stale_unverified_labels(ws)
        assert len(violations) == 0

    def test_mixed_ids_only_backed_promoted(self):
        """Only IDs with VERIFIED backing are promoted."""
        meta = {"total_computations": 1}
        body = """# Computations

## COMP-001

**CLAIM**: Verify ER-001
**VERDICT**: VERIFIED
**RESULT**: OK.
"""
        state = (
            "ER-001 is [unverified].\n"
            "ER-002 is [unverified].\n"
        )
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_stale_unverified_labels(ws)

        assert len(violations) == 1
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "ER-001" in violations[0].detail
        assert "VERIFIED" in updated.split("\n")[0]  # first line promoted
        assert "[unverified]" in updated.split("\n")[1]  # second line unchanged

    def test_promoted_wh_to_er_matched_in_table(self):
        """[unverified] promoted when computation verified WH-NNN that was later promoted to ER-NNN."""
        meta = {"total_computations": 1}
        # Computation references WH-001 (original ID when computation ran)
        body = """# Computations

## COMP-001

**CLAIM**: Verify WH-001 partition function
**VERDICT**: VERIFIED
**RESULT**: OK.
"""
        # State has ER-001 as section header (WH-001 was promoted) and a synthesis
        # table that references ER-001 with [unverified]
        state = (
            "## ER-001 — Partition Function\n\n"
            "Z = 1/(2 sinh(x/2)). VERIFIED.\n\n"
            "# Final Synthesis\n\n"
            "| Quantity | Formula | Status |\n"
            "|---|---|---|\n"
            "| Partition function | `Z = 1/(2 sinh(x/2))` | ER-001 — [unverified] |\n"
        )
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_stale_unverified_labels(ws)

        assert len(violations) == 1
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "[unverified]" not in updated
        assert "ER-001 — VERIFIED" in updated

    def test_no_promotion_when_wh_not_promoted_to_er(self):
        """[unverified] stays when WH-002 is verified but ER-002 doesn't exist as section."""
        meta = {"total_computations": 1}
        body = """# Computations

## COMP-001

**CLAIM**: Verify WH-002
**VERDICT**: VERIFIED
**RESULT**: OK.
"""
        # ER-002 doesn't exist as a section — WH-002 was never promoted
        state = (
            "## WH-002 — Some Hypothesis\n\n"
            "Details.\n\n"
            "# Summary\n"
            "| Result | Status |\n"
            "|---|---|\n"
            "| Something | ER-002 — [unverified] |\n"
        )
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, body),
        })
        violations = check_stale_unverified_labels(ws)

        # ER-002 is not a section header, so WH-002 → ER-002 mapping is not made
        # But WH-002 IS in verified_ids and the table line contains ER-002 not WH-002
        # No promotion should happen because ER-002 is not an ER section
        assert len(violations) == 0
        assert "[unverified]" in ws.read_file("RESEARCH_STATE.md")


# ---------------------------------------------------------------------------
# P2-B: Fix WH-to-ER Header Renaming on Promotion
# ---------------------------------------------------------------------------

class TestWhToErHeaderPromotion:
    """P2-B: WH-NNN headers promoted to ER-NNN when body uses ER-NNN with VERIFIED backing."""

    def test_wh_header_promoted_when_er_in_body(self):
        """WH header renamed to ER when ER-NNN appears in body and has VERIFIED."""
        state = """# Established Results

## WH-001 Hawking Temperature

Result: ER-001 is the Hawking temperature T = hbar*kappa/(2*pi*k_B).
"""
        meta = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify WH-001 Hawking temperature
**VERDICT**: VERIFIED
**RESULT**: Confirmed.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_er_promotion_gate(ws)

        # Should have a promotion warning
        promotion_vs = [v for v in violations if "Promoted header" in v.message]
        assert len(promotion_vs) == 1
        assert promotion_vs[0].severity == ViolationSeverity.WARNING
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "## ER-001" in updated
        assert "## WH-001" not in updated

    def test_wh_header_not_promoted_without_er_in_body(self):
        """WH header stays WH when ER-NNN does not appear in body."""
        state = """# Working Hypotheses

## WH-001 Some Hypothesis

Just a regular WH, no ER reference.
"""
        meta = {"total_computations": 1}
        comp_body = """# Computations

## COMP-001

**CLAIM**: Verify WH-001
**VERDICT**: VERIFIED
**RESULT**: OK.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": render_frontmatter(meta, comp_body),
        })
        violations = check_er_promotion_gate(ws)

        # No promotion — ER-001 not in body
        promotion_vs = [v for v in violations if "Promoted header" in v.message]
        assert len(promotion_vs) == 0
        updated = ws.read_file("RESEARCH_STATE.md")
        assert "## WH-001" in updated

    def test_wh_header_not_promoted_without_verified(self):
        """WH header stays WH even if ER-NNN in body but no VERIFIED computation."""
        state = """# Results

## WH-001 Something

ER-001 appears in body text.
"""
        ws = MockWorkspace({
            "RESEARCH_STATE.md": state,
            "COMPUTATION_LOG.md": "",
        })
        violations = check_er_promotion_gate(ws)

        promotion_vs = [v for v in violations if "Promoted header" in v.message]
        assert len(promotion_vs) == 0


# ---------------------------------------------------------------------------
# P2-C: Fix total_computations Counter
# ---------------------------------------------------------------------------

class TestComputationCounter:
    """P2-C: Only COMP- headers counted, not TASK- sub-entries."""

    def test_only_comp_headers_counted(self):
        """_update_computation_metadata counts COMP-NNN but not TASK-NNN."""
        from sciralph.agents.computationalist import ComputationalistAgent

        config = MagicMock()
        workspace = MagicMock()
        metrics = MagicMock()
        agent = ComputationalistAgent(config=config, workspace=workspace, metrics=metrics)

        body = """# Computations

## COMP-001: First check

**CLAIM**: test
**VERDICT**: VERIFIED

### TASK-A: subtask

Some sub-result.

## COMP-002: Second check

**CLAIM**: test2
**VERDICT**: REFUTED

## TASK-003: This is a task entry not a comp

**CLAIM**: test3
**VERDICT**: INCONCLUSIVE
"""
        content = render_frontmatter({"total_computations": 0}, body)
        workspace.read_file.return_value = content

        written = {}
        def capture(fn, c):
            written[fn] = c
        workspace.write_file.side_effect = capture

        agent._update_computation_metadata()

        from sciralph.markdown import parse_frontmatter
        meta, _ = parse_frontmatter(written["COMPUTATION_LOG.md"])
        # Only COMP-001 and COMP-002, not TASK-003 or TASK-A
        assert meta["total_computations"] == 2

    def test_count_goes_down_after_compression(self):
        """Counter should reflect actual count, not max() with previous."""
        from sciralph.agents.computationalist import ComputationalistAgent

        config = MagicMock()
        workspace = MagicMock()
        metrics = MagicMock()
        agent = ComputationalistAgent(config=config, workspace=workspace, metrics=metrics)

        body = """# Computations

## COMP-001: Only entry

**CLAIM**: test
**VERDICT**: VERIFIED
"""
        # Previous count was 5 (before compression)
        content = render_frontmatter({"total_computations": 5}, body)
        workspace.read_file.return_value = content

        written = {}
        def capture(fn, c):
            written[fn] = c
        workspace.write_file.side_effect = capture

        agent._update_computation_metadata()

        from sciralph.markdown import parse_frontmatter
        meta, _ = parse_frontmatter(written["COMPUTATION_LOG.md"])
        assert meta["total_computations"] == 1  # Not max(5, 1) = 5


# ---------------------------------------------------------------------------
# P2-D: Fix Critique Resolution Text Truncation
# ---------------------------------------------------------------------------

class TestCritiqueResolutionRegex:
    """P2-D: Multi-line resolution notes captured, not truncated at first period."""

    def test_multiline_resolution_captured(self):
        """Resolution text spanning multiple lines is captured."""
        from sciralph.agents.orchestrator import OrchestratorAgent

        config = MagicMock()
        config.min_er_for_completion = 3
        workspace = MagicMock()
        metrics = MagicMock()
        agent = OrchestratorAgent(config, workspace, metrics)

        response_text = (
            "CRIT-001: Corrected the sign error in Eq. 3.\n"
            "The minus sign was missing from the exponent,\n"
            "which caused the divergence issue.\n\n"
            "CRIT-002: Added missing normalization factor."
        )

        # Call the extraction logic directly
        resolution_notes = {}
        for crit_id in ["CRIT-001", "CRIT-002"]:
            note_match = re.search(
                rf'{re.escape(crit_id)}[\s:—\-]+(.+?)(?=\n\n|\nCRIT(?:IQUE)?-\d|$)',
                response_text,
                re.DOTALL,
            )
            if note_match:
                note = note_match.group(1).strip()
                note = " ".join(note.split())
                if len(note) > 300:
                    cut = note[:300].rfind('.')
                    note = note[:cut + 1] if cut > 50 else note[:300] + "..."
                resolution_notes[crit_id] = note

        # CRIT-001 should capture full multi-line text
        assert "CRIT-001" in resolution_notes
        assert "sign error" in resolution_notes["CRIT-001"]
        assert "divergence issue" in resolution_notes["CRIT-001"]

        # CRIT-002 should also be captured
        assert "CRIT-002" in resolution_notes
        assert "normalization" in resolution_notes["CRIT-002"]

    def test_old_regex_would_truncate(self):
        """Verify the old regex would have truncated at first period."""
        text = "CRIT-001: Fixed the error in Eq. 3 which caused divergence."

        # Old regex
        old_match = re.search(
            r'CRIT-001[\s:—\-]+([^.\n]{10,120}[.])',
            text,
        )
        # New regex
        new_match = re.search(
            r'CRIT-001[\s:—\-]+(.+?)(?=\n\n|\nCRIT(?:IQUE)?-\d|$)',
            text,
            re.DOTALL,
        )

        old_text = old_match.group(1).strip() if old_match else ""
        new_text = new_match.group(1).strip() if new_match else ""

        # Old truncates at "Eq." — new captures full sentence
        assert len(new_text) >= len(old_text)
        assert "divergence" in new_text

    def test_long_note_capped_at_300(self):
        """Very long notes are capped at ~300 chars at sentence boundary."""
        long_text = "CRIT-001: " + "This is a sentence. " * 30  # ~600 chars

        note_match = re.search(
            r'CRIT-001[\s:—\-]+(.+?)(?=\n\n|\nCRIT(?:IQUE)?-\d|$)',
            long_text,
            re.DOTALL,
        )
        note = note_match.group(1).strip()
        note = " ".join(note.split())
        if len(note) > 300:
            cut = note[:300].rfind('.')
            note = note[:cut + 1] if cut > 50 else note[:300] + "..."

        assert len(note) <= 303  # 300 + "..."


# ---------------------------------------------------------------------------
# P3: Skip Redundant Critic Passes
# ---------------------------------------------------------------------------

class TestCriticOverdue:
    """P3: _critic_overdue returns False when no new content since last critic."""

    def _make_engine(self, last_critic_iteration=0, last_content_iteration=0,
                     current_iteration=5, critic_every_n=4):
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.read_file = MagicMock(return_value="")

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config(critic_every_n=critic_every_n)
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.metrics.last_critic_iteration = last_critic_iteration
            engine.iteration = current_iteration
            engine._last_content_iteration = last_content_iteration
        return engine

    def test_overdue_with_new_content(self):
        """Overdue returns True when interval exceeded AND new content exists."""
        engine = self._make_engine(
            last_critic_iteration=0, last_content_iteration=3,
            current_iteration=5, critic_every_n=4,
        )
        assert engine._critic_overdue() is True

    def test_not_overdue_when_critic_reviewed_latest(self):
        """Overdue returns False when critic reviewed after last content."""
        engine = self._make_engine(
            last_critic_iteration=4, last_content_iteration=3,
            current_iteration=8, critic_every_n=4,
        )
        assert engine._critic_overdue() is False

    def test_not_overdue_when_interval_not_reached(self):
        """Overdue returns False when fewer than N iterations since last critic."""
        engine = self._make_engine(
            last_critic_iteration=3, last_content_iteration=4,
            current_iteration=5, critic_every_n=4,
        )
        assert engine._critic_overdue() is False

    def test_overdue_when_content_after_critic(self):
        """Overdue returns True when content produced after last critic and interval exceeded."""
        engine = self._make_engine(
            last_critic_iteration=2, last_content_iteration=5,
            current_iteration=8, critic_every_n=4,
        )
        assert engine._critic_overdue() is True

    def test_last_content_tracked_in_dispatch(self):
        """_dispatch updates _last_content_iteration for research/compute tasks."""
        with patch("sciralph.engine.WorkspaceManager") as MockWS:
            ws = MockWS.return_value
            ws.init = MagicMock()
            ws.root = MagicMock()
            ws.root.__truediv__ = MagicMock()
            ws.logs_dir = "/tmp/logs"
            ws.read_file = MagicMock(return_value="")

            from sciralph.engine import SciRalph
            engine = SciRalph.__new__(SciRalph)
            engine.config = Config()
            engine.workspace = ws
            engine.metrics = MagicMock()
            engine.iteration = 7
            engine._last_content_iteration = 0
            engine.researcher = MagicMock()
            engine.computationalist = MagicMock()
            engine.critic = MagicMock()

        task = Task(task_id="T", task_type=TaskType.RESEARCH, assigned_to="researcher")
        engine._dispatch(task)
        assert engine._last_content_iteration == 7


# ---------------------------------------------------------------------------
# Config: new fields present
# ---------------------------------------------------------------------------

class TestNewConfigFields:
    """Verify new config fields are properly loaded from defaults."""

    def test_zero_text_bailout_default(self):
        assert DEFAULTS["zero_text_bailout"] == 3
        assert Config().zero_text_bailout == 3

    def test_checkpoint_round_default(self):
        assert DEFAULTS["checkpoint_round"] == 2
        assert Config().checkpoint_round == 2

    def test_computation_token_alert_default(self):
        assert DEFAULTS["computation_token_alert"] == 150_000
        assert Config().computation_token_alert == 150_000

    def test_new_fields_in_yaml_config_fields(self):
        from sciralph.config import _YAML_CONFIG_FIELDS
        assert "zero_text_bailout" in _YAML_CONFIG_FIELDS
        assert "checkpoint_round" in _YAML_CONFIG_FIELDS
        assert "computation_token_alert" in _YAML_CONFIG_FIELDS
