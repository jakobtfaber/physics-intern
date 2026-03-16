"""Tests for CriticToolExecutor."""

import pytest

from sciralph.critic_tools import CriticToolExecutor


class TestCriticToolExecutor:

    def test_submit_critique_auto_numbers(self):
        executor = CriticToolExecutor(existing_critique_count=3)
        tc = executor.execute("submit_critique", {
            "severity": "HIGH",
            "target_id": "WH-001",
            "argument": "Sign error in step 3.",
        })
        assert not tc.is_error
        assert executor.filed_critiques[0]["id"] == "CRIT-004"
        assert executor.filed_critiques[0]["severity"] == "HIGH"
        assert not executor.stop_after_round

    def test_submit_multiple_critiques(self):
        executor = CriticToolExecutor(existing_critique_count=0)
        executor.execute("submit_critique", {
            "severity": "HIGH",
            "target_id": "WH-001",
            "argument": "First issue.",
        })
        executor.execute("submit_critique", {
            "severity": "MEDIUM",
            "target_id": "ER-002",
            "argument": "Second issue.",
        })
        assert len(executor.filed_critiques) == 2
        assert executor.filed_critiques[0]["id"] == "CRIT-001"
        assert executor.filed_critiques[1]["id"] == "CRIT-002"

    def test_finish_review_sets_stop(self):
        executor = CriticToolExecutor()
        tc = executor.execute("finish_review", {"summary": "Reviewed 3 claims."})
        assert not tc.is_error
        assert executor.stop_after_round
        assert executor.review_summary == "Reviewed 3 claims."

    def test_finish_review_clean_message(self):
        executor = CriticToolExecutor()
        tc = executor.execute("finish_review", {"summary": "All clear."})
        assert "No critiques filed" in tc.output

    def test_finish_review_with_critiques(self):
        executor = CriticToolExecutor()
        executor.execute("submit_critique", {
            "severity": "LOW",
            "target_id": "WH-001",
            "argument": "Minor issue.",
        })
        tc = executor.execute("finish_review", {"summary": "Found 1 issue."})
        assert "1 critique(s) filed" in tc.output

    def test_unknown_tool(self):
        executor = CriticToolExecutor()
        tc = executor.execute("unknown_tool", {})
        assert tc.is_error
        assert "Unknown tool" in tc.output

    def test_suggested_verification_stored(self):
        executor = CriticToolExecutor()
        executor.execute("submit_critique", {
            "severity": "HIGH",
            "target_id": "WH-001",
            "argument": "Boundary issue.",
            "suggested_verification": "Check at x=0.",
        })
        assert executor.filed_critiques[0]["suggested_verification"] == "Check at x=0."

    def test_numbering_from_zero(self):
        executor = CriticToolExecutor(existing_critique_count=0)
        executor.execute("submit_critique", {
            "severity": "LOW",
            "target_id": "WH-001",
            "argument": "Test.",
        })
        assert executor.filed_critiques[0]["id"] == "CRIT-001"

    def test_numbering_continues(self):
        executor = CriticToolExecutor(existing_critique_count=10)
        executor.execute("submit_critique", {
            "severity": "LOW",
            "target_id": "WH-001",
            "argument": "Test.",
        })
        assert executor.filed_critiques[0]["id"] == "CRIT-011"

    def test_no_critiques_before_finish(self):
        executor = CriticToolExecutor()
        executor.execute("finish_review", {"summary": "Clean."})
        assert len(executor.filed_critiques) == 0
        assert executor.stop_after_round

    def test_tool_definitions_exist(self):
        defs = CriticToolExecutor.TOOL_DEFINITIONS
        names = {d["function"]["name"] for d in defs}
        assert names == {"submit_critique", "finish_review"}

    def test_submit_critique_return_value(self):
        executor = CriticToolExecutor(existing_critique_count=0)
        tc = executor.execute("submit_critique", {
            "severity": "HIGH",
            "target_id": "ER-001",
            "argument": "Wrong factor.",
        })
        assert "CRIT-001" in tc.output
        assert "HIGH" in tc.output
        assert "ER-001" in tc.output
