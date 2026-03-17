"""Tests for StrategistAgent."""

from unittest.mock import MagicMock

from sciralph.agents.strategist import StrategistAgent
from sciralph.research_state import ResearchState, ResearchStrategy
from sciralph.task import Task, TaskType
from sciralph.llm import LLMResponse


def _make_agent():
    """Create a StrategistAgent with mocked dependencies."""
    config = MagicMock()
    workspace = MagicMock()
    metrics = MagicMock()
    agent = StrategistAgent(config=config, workspace=workspace, metrics=metrics)
    return agent


def _make_task():
    return Task(
        task_id="STRATEGY-000", task_type=TaskType.STRATEGIZE,
        assigned_to="strategist", iteration=0,
    )


# ---------- build_context tests ----------

def test_build_context_initial():
    """iteration=0 includes problem statement only, no 'Current Research State'."""
    agent = _make_agent()
    agent.research_state = ResearchState(problem_statement="Derive the Hawking temperature.")

    context = agent.build_context(_make_task(), iteration=0)

    assert "Derive the Hawking temperature." in context
    assert "Current Research State" not in context


def test_build_context_replan():
    """iteration > 0 includes current research state section."""
    agent = _make_agent()
    agent.research_state = ResearchState(problem_statement="Derive the Hawking temperature.")

    context = agent.build_context(_make_task(), iteration=5)

    assert "Derive the Hawking temperature." in context
    assert "Current Research State" in context


# ---------- process_response tests ----------

STRATEGY_TEXT = """The Hawking temperature derivation proceeds most naturally via the Euclidean
path integral approach. The key insight is that requiring regularity of the
Euclidean section at the horizon fixes the periodicity of imaginary time.

**Promising approaches:**
- Euclidean continuation: Wick rotate, demand regularity at the horizon.
- Surface gravity route: Compute kappa from the Killing vector norm gradient.

**Pitfalls:**
- Don't confuse coordinate-dependent and invariant quantities.
- The naive WKB approximation breaks down near the horizon.
"""


def test_process_response_stores_strategy():
    """process_response stores the raw text as a ResearchStrategy."""
    agent = _make_agent()
    response = LLMResponse(
        text=STRATEGY_TEXT, input_tokens=100, output_tokens=200,
        stop_reason="end_turn", duration=0.5,
    )
    agent.process_response(response, _make_task(), iteration=0)

    assert agent.parsed_strategy is not None
    assert isinstance(agent.parsed_strategy, ResearchStrategy)
    assert "Euclidean" in agent.parsed_strategy.strategy_notes
    assert agent.parsed_strategy.iteration_created == 0


def test_process_response_sets_iteration():
    """iteration parameter is stored in the strategy."""
    agent = _make_agent()
    response = LLMResponse(
        text="Re-plan notes.", input_tokens=50, output_tokens=50,
        stop_reason="end_turn", duration=0.3,
    )
    agent.process_response(response, _make_task(), iteration=7)

    assert agent.parsed_strategy.iteration_created == 7
    assert agent.parsed_strategy.iteration_updated == 7


def test_process_response_empty_text():
    """Empty response still creates a strategy (with empty notes)."""
    agent = _make_agent()
    response = LLMResponse(
        text="", input_tokens=50, output_tokens=0,
        stop_reason="end_turn", duration=0.1,
    )
    agent.process_response(response, _make_task(), iteration=0)

    assert agent.parsed_strategy is not None
    assert agent.parsed_strategy.strategy_notes == ""


def test_process_response_strips_whitespace():
    """Leading/trailing whitespace is stripped from strategy notes."""
    agent = _make_agent()
    response = LLMResponse(
        text="  \n Some notes here. \n  ", input_tokens=50, output_tokens=50,
        stop_reason="end_turn", duration=0.2,
    )
    agent.process_response(response, _make_task(), iteration=0)

    assert agent.parsed_strategy.strategy_notes == "Some notes here."
