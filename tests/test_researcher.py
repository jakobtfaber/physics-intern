"""Tests for ResearcherAgent: JSON parsing, process_response, context building."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from sciralph.agents.researcher import ResearcherAgent, _parse_researcher_json
from sciralph.llm import LLMResponse
from sciralph.research_state import (
    Evidence,
    Hypothesis,
    HypothesisStatus,
    ResearchQuestion,
    ResearchState,
)
from sciralph.task import Task, TaskType


# ---------------------------------------------------------------------------
# _parse_researcher_json
# ---------------------------------------------------------------------------


class TestParseResearcherJson:
    def test_fenced_json(self):
        text = (
            'Some derivation...\n'
            '```json\n'
            '{"result": "T_H = 1/(8*pi*M)", "method": "Euclidean", '
            '"confidence": "exact", "summary": "Hawking temperature derived"}\n'
            '```'
        )
        result = _parse_researcher_json(text)
        assert result is not None
        assert result["result"] == "T_H = 1/(8*pi*M)"
        assert result["method"] == "Euclidean"

    def test_bare_json_with_brace_counting(self):
        text = (
            'The derivation shows {that intermediate step} leads to:\n'
            '{"result": "S = k ln(W)", "method": "statistical mechanics", '
            '"confidence": "exact", "summary": "Entropy formula"}'
        )
        result = _parse_researcher_json(text)
        assert result is not None
        assert result["result"] == "S = k ln(W)"

    def test_last_fenced_wins(self):
        text = (
            '```json\n{"result": "wrong", "method": "a", "confidence": "partial", "summary": "x"}\n```\n'
            'More analysis...\n'
            '```json\n{"result": "correct", "method": "b", "confidence": "exact", "summary": "y"}\n```'
        )
        result = _parse_researcher_json(text)
        assert result is not None
        assert result["result"] == "correct"

    def test_no_json_returns_none(self):
        text = "Just plain text with no JSON."
        assert _parse_researcher_json(text) is None

    def test_invalid_json_returns_none(self):
        text = '```json\n{invalid json}\n```'
        assert _parse_researcher_json(text) is None

    def test_fenced_preferred_over_bare(self):
        text = (
            '{"result": "bare", "method": "a", "confidence": "exact", "summary": "x"}\n'
            '```json\n{"result": "fenced", "method": "b", "confidence": "exact", "summary": "y"}\n```'
        )
        result = _parse_researcher_json(text)
        assert result["result"] == "fenced"

    def test_json_without_result_key_skipped(self):
        """JSON blocks that don't contain 'result' key are ignored."""
        text = '```json\n{"verdict": "VERIFIED", "summary": "ok"}\n```'
        assert _parse_researcher_json(text) is None

    def test_nested_braces_in_reasoning(self):
        """Brace-counting handles nested braces in surrounding text."""
        text = (
            'The set {a, b, c} has 3 elements. '
            'The function f(x) = x^{2} gives:\n'
            '{"result": "f(2) = 4", "method": "direct computation", '
            '"confidence": "exact", "summary": "Squared"}'
        )
        result = _parse_researcher_json(text)
        assert result is not None
        assert result["result"] == "f(2) = 4"


# ---------------------------------------------------------------------------
# ResearcherAgent.process_response
# ---------------------------------------------------------------------------


def _make_researcher() -> ResearcherAgent:
    config = MagicMock()
    workspace = MagicMock()
    workspace.root = Path(tempfile.mkdtemp())
    metrics = MagicMock()
    agent = ResearcherAgent(config, workspace, metrics)
    state = ResearchState(problem_statement="test")
    # Add an RQ and a WH for target tests
    state.research_questions["RQ-001"] = ResearchQuestion(
        id="RQ-001", question="What is the Hawking temperature?"
    )
    state.hypotheses["WH-002"] = Hypothesis(
        id="WH-002",
        statement="T_H = 1/(8*pi*M)",
        status=HypothesisStatus.WORKING,
    )
    agent.research_state = state
    return agent


class TestResearcherProcessResponse:
    def test_parses_json_and_builds_evidence(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive T_H", target_claim="RQ-001")
        text = (
            'Starting from the Schwarzschild metric...\n\n'
            '```json\n'
            '{"result": "T_H = 1/(8*pi*M)", "method": "Euclidean path integral", '
            '"confidence": "exact", "summary": "Hawking temperature via Euclidean method"}\n'
            '```'
        )
        response = LLMResponse(text=text, input_tokens=500, output_tokens=200,
                               stop_reason="end_turn", duration=1.0)
        agent.process_response(response, task, iteration=1)
        ev = agent.research_state.research_questions["RQ-001"].evidence
        assert ev is not None
        assert ev.type == "research"
        assert ev.result == "T_H = 1/(8*pi*M)"
        assert ev.method == "Euclidean path integral"
        assert ev.confidence == "exact"
        assert ev.summary == "Hawking temperature via Euclidean method"
        # reasoning is the full response text
        assert "Schwarzschild metric" in ev.reasoning
        assert ev.iteration == 1

    def test_stores_on_wh_target(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Refine WH-002", target_claim="WH-002")
        text = '```json\n{"result": "confirmed", "method": "direct", "confidence": "exact", "summary": "ok"}\n```'
        response = LLMResponse(text=text, input_tokens=100, output_tokens=50,
                               stop_reason="end_turn", duration=0.1)
        agent.process_response(response, task, iteration=2)
        ev = agent.research_state.hypotheses["WH-002"].evidence
        assert ev is not None
        assert ev.result == "confirmed"
        assert ev.iteration == 2

    def test_fallback_on_parse_failure(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive something", target_claim="RQ-001")
        response = LLMResponse(text="Long derivation without any JSON block at the end.",
                               input_tokens=100, output_tokens=50,
                               stop_reason="end_turn", duration=0.1)
        agent.process_response(response, task, iteration=3)
        ev = agent.research_state.research_questions["RQ-001"].evidence
        assert ev is not None
        assert ev.confidence == "partial"
        assert "Failed to parse" in ev.result
        assert "Long derivation" in ev.reasoning

    def test_fallback_reasoning_truncated(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive something", target_claim="RQ-001")
        long_text = "x" * 5000
        response = LLMResponse(text=long_text, input_tokens=100, output_tokens=50,
                               stop_reason="end_turn", duration=0.1)
        agent.process_response(response, task, iteration=1)
        ev = agent.research_state.research_questions["RQ-001"].evidence
        assert len(ev.reasoning) == 2000

    def test_invalid_confidence_normalized(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive", target_claim="RQ-001")
        text = '```json\n{"result": "ok", "method": "m", "confidence": "VERY_HIGH", "summary": "s"}\n```'
        response = LLMResponse(text=text, input_tokens=100, output_tokens=50,
                               stop_reason="end_turn", duration=0.1)
        agent.process_response(response, task, iteration=1)
        ev = agent.research_state.research_questions["RQ-001"].evidence
        assert ev.confidence == "partial"

    def test_target_extracted_from_body_when_no_target_claim(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Investigate RQ-001 further", target_claim="")
        text = '```json\n{"result": "done", "method": "m", "confidence": "exact", "summary": "s"}\n```'
        response = LLMResponse(text=text, input_tokens=100, output_tokens=50,
                               stop_reason="end_turn", duration=0.1)
        agent.process_response(response, task, iteration=1)
        ev = agent.research_state.research_questions["RQ-001"].evidence
        assert ev is not None
        assert ev.result == "done"

    def test_empty_response_text(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive", target_claim="RQ-001")
        response = LLMResponse(text="", input_tokens=100, output_tokens=0,
                               stop_reason="end_turn", duration=0.1)
        agent.process_response(response, task, iteration=1)
        ev = agent.research_state.research_questions["RQ-001"].evidence
        assert ev is not None
        assert ev.confidence == "partial"


# ---------------------------------------------------------------------------
# ResearcherAgent.build_context
# ---------------------------------------------------------------------------


class TestResearcherBuildContext:
    def test_includes_background(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive T_H", target_claim="RQ-001",
                    background="Hawking radiation is thermal emission from black holes.")
        ctx = agent.build_context(task, iteration=1)
        assert "<background>" in ctx
        assert "Hawking radiation" in ctx

    def test_includes_target(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive T_H", target_claim="RQ-001")
        ctx = agent.build_context(task, iteration=1)
        assert "<target>" in ctx
        assert "Hawking temperature" in ctx

    def test_includes_method_hints(self):
        agent = _make_researcher()
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive", target_claim="RQ-001",
                    method_hints=["Use Euclidean path integral"])
        ctx = agent.build_context(task, iteration=1)
        assert "Euclidean path integral" in ctx

    def test_includes_conventions(self):
        agent = _make_researcher()
        agent.research_state.conventions = "Natural units: ħ = c = k_B = 1"
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive", target_claim="RQ-001")
        ctx = agent.build_context(task, iteration=1)
        assert "<conventions>" in ctx
        assert "Natural units" in ctx

    def test_includes_established_results(self):
        agent = _make_researcher()
        agent.research_state.hypotheses["ER-003"] = Hypothesis(
            id="ER-003",
            statement="Area law holds",
            status=HypothesisStatus.ESTABLISHED,
        )
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive", target_claim="RQ-001")
        ctx = agent.build_context(task, iteration=1)
        assert "ER-003" in ctx
        assert "Area law holds" in ctx

    def test_no_research_state(self):
        agent = _make_researcher()
        agent.research_state = None
        task = Task(task_id="T1", task_type=TaskType.RESEARCH, assigned_to="researcher",
                    body="Derive something")
        ctx = agent.build_context(task, iteration=1)
        assert "<task>" in ctx


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


class TestResearcherConfig:
    def test_is_one_shot(self):
        assert ResearcherAgent.tools == []

    def test_name(self):
        assert ResearcherAgent.name == "researcher"

    def test_prompt_file(self):
        assert ResearcherAgent.prompt_file == "researcher.md"
