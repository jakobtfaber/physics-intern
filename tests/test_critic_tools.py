"""Tests for CriticToolExecutor."""

import pytest

from sciralph.critic_tools import CriticToolExecutor


class TestCriticToolExecutor:

    def test_submit_review_with_critiques(self):
        executor = CriticToolExecutor(existing_critique_count=3)
        tc = executor.execute("submit_review", {
            "summary": "Reviewed strategy and 2 ERs.",
            "details": "Full analysis of the research direction...",
            "critiques": [
                {
                    "severity": "HIGH",
                    "target_id": "WH-001",
                    "argument": "Sign error in step 3.",
                },
            ],
        })
        assert not tc.is_error
        assert executor.filed_critiques[0]["id"] == "CRIT-004"
        assert executor.filed_critiques[0]["severity"] == "HIGH"
        assert executor.stop_after_round
        assert executor.review_summary == "Reviewed strategy and 2 ERs."
        assert executor.review_details == "Full analysis of the research direction..."

    def test_submit_review_multiple_critiques(self):
        executor = CriticToolExecutor(existing_critique_count=0)
        tc = executor.execute("submit_review", {
            "summary": "Found 2 issues.",
            "details": "Detailed reasoning here.",
            "critiques": [
                {
                    "severity": "HIGH",
                    "target_id": "WH-001",
                    "argument": "First issue.",
                },
                {
                    "severity": "MEDIUM",
                    "target_id": "ER-002",
                    "argument": "Second issue.",
                },
            ],
        })
        assert not tc.is_error
        assert len(executor.filed_critiques) == 2
        assert executor.filed_critiques[0]["id"] == "CRIT-001"
        assert executor.filed_critiques[1]["id"] == "CRIT-002"
        assert "2 critique(s) filed" in tc.output

    def test_submit_review_clean(self):
        executor = CriticToolExecutor()
        tc = executor.execute("submit_review", {
            "summary": "All clear.",
            "details": "Examined strategy and all ERs, no issues.",
            "critiques": [],
        })
        assert not tc.is_error
        assert executor.stop_after_round
        assert executor.review_summary == "All clear."
        assert executor.review_details == "Examined strategy and all ERs, no issues."
        assert len(executor.filed_critiques) == 0
        assert "No critiques filed" in tc.output

    def test_unknown_tool(self):
        executor = CriticToolExecutor()
        tc = executor.execute("unknown_tool", {})
        assert tc.is_error
        assert "Unknown tool" in tc.output

    def test_numbering_from_zero(self):
        executor = CriticToolExecutor(existing_critique_count=0)
        executor.execute("submit_review", {
            "summary": "Test.",
            "details": "Test details.",
            "critiques": [
                {"severity": "LOW", "target_id": "WH-001", "argument": "Test."},
            ],
        })
        assert executor.filed_critiques[0]["id"] == "CRIT-001"

    def test_numbering_continues(self):
        executor = CriticToolExecutor(existing_critique_count=10)
        executor.execute("submit_review", {
            "summary": "Test.",
            "details": "Test details.",
            "critiques": [
                {"severity": "LOW", "target_id": "WH-001", "argument": "Test."},
            ],
        })
        assert executor.filed_critiques[0]["id"] == "CRIT-011"

    def test_tool_definitions_exist(self):
        defs = CriticToolExecutor.TOOL_DEFINITIONS
        names = {d["function"]["name"] for d in defs}
        assert names == {"submit_review"}

    def test_submit_review_strategy_target(self):
        """submit_review with target_id='STRATEGY' stores correctly."""
        executor = CriticToolExecutor(existing_critique_count=0)
        tc = executor.execute("submit_review", {
            "summary": "Strategy review.",
            "details": "The strategy recommends a refuted approach.",
            "critiques": [
                {
                    "severity": "MEDIUM",
                    "target_id": "STRATEGY",
                    "argument": "Strategy recommends refuted approach.",
                },
            ],
        })
        assert not tc.is_error
        assert len(executor.filed_critiques) == 1
        crit = executor.filed_critiques[0]
        assert crit["target_id"] == "STRATEGY"
        assert crit["severity"] == "MEDIUM"
        assert "1 critique(s) filed" in tc.output

    def test_exit_tool_name(self):
        assert CriticToolExecutor.exit_tool_name == "submit_review"

    def test_details_captured(self):
        """The details field is stored on the executor."""
        executor = CriticToolExecutor()
        executor.execute("submit_review", {
            "summary": "Brief.",
            "details": "This is a very long analysis of the research...",
            "critiques": [],
        })
        assert "very long analysis" in executor.review_details

    def test_empty_critiques_default(self):
        """Missing critiques key defaults to empty list."""
        executor = CriticToolExecutor()
        tc = executor.execute("submit_review", {
            "summary": "Done.",
            "details": "Nothing found.",
        })
        assert not tc.is_error
        assert len(executor.filed_critiques) == 0
        assert executor.stop_after_round
