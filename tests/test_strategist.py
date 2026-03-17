"""Tests for StrategistAgent."""

from unittest.mock import MagicMock

from sciralph.agents.strategist import StrategistAgent
from sciralph.research_state import ResearchState, ResearchPlan, SubProblem
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


# ---------- _parse_plan tests ----------

VALID_PLAN_JSON = """{
    "strategy_summary": "Derive Hawking temperature from first principles.",
    "sub_problems": [
        {
            "id": "SP-001",
            "description": "Establish the Schwarzschild metric",
            "approach": "Start from Einstein field equations",
            "alternatives": ["Use Kerr metric as generalization"],
            "depends_on": [],
            "notes": ""
        },
        {
            "id": "SP-002",
            "description": "Compute surface gravity",
            "approach": "Use Killing vector formalism",
            "alternatives": [],
            "depends_on": ["SP-001"],
            "notes": "Standard approach"
        }
    ],
    "initial_rqs": [
        {
            "question": "What is the surface gravity of a Schwarzschild black hole?",
            "context": "Needed for temperature derivation",
            "sub_problem": "SP-001"
        },
        {
            "question": "How does the Unruh effect relate to Hawking radiation?",
            "context": "Provides physical intuition",
            "sub_problem": "SP-002"
        }
    ],
    "known_pitfalls": [
        "Do not confuse coordinate and invariant quantities."
    ]
}"""

FENCED_PLAN_JSON = f"""Here is my research plan:

```json
{VALID_PLAN_JSON}
```

This plan addresses the core derivation."""


def test_parse_plan_from_json_block():
    """Valid JSON without fences is parsed correctly."""
    agent = _make_agent()
    plan, rqs = agent._parse_plan(VALID_PLAN_JSON, iteration=0)

    assert plan is not None
    assert isinstance(plan, ResearchPlan)
    assert plan.strategy_summary == "Derive Hawking temperature from first principles."


def test_parse_plan_from_fenced_json():
    """JSON inside ```json fences is extracted and parsed."""
    agent = _make_agent()
    plan, rqs = agent._parse_plan(FENCED_PLAN_JSON, iteration=0)

    assert plan is not None
    assert isinstance(plan, ResearchPlan)
    assert len(plan.sub_problems) == 2


def test_parse_plan_malformed_json_returns_empty():
    """Malformed JSON gracefully returns None plan and empty rqs."""
    agent = _make_agent()
    plan, rqs = agent._parse_plan("This is not valid JSON at all {{{", iteration=0)

    assert plan is None
    assert rqs == []


def test_parsed_plan_has_sub_problems():
    """Verify SubProblem objects are created from the plan JSON."""
    agent = _make_agent()
    plan, _ = agent._parse_plan(VALID_PLAN_JSON, iteration=0)

    assert plan is not None
    assert len(plan.sub_problems) == 2
    assert all(isinstance(sp, SubProblem) for sp in plan.sub_problems.values())
    assert "SP-001" in plan.sub_problems
    assert "SP-002" in plan.sub_problems
    assert plan.sub_problems["SP-001"].description == "Establish the Schwarzschild metric"
    assert plan.sub_problems["SP-002"].depends_on == ["SP-001"]


def test_parsed_plan_has_initial_rqs():
    """Verify initial_rqs list is populated from the plan JSON."""
    agent = _make_agent()
    _, rqs = agent._parse_plan(VALID_PLAN_JSON, iteration=0)

    assert len(rqs) == 2
    assert rqs[0]["question"] == "What is the surface gravity of a Schwarzschild black hole?"
    assert rqs[0]["sub_problem"] == "SP-001"
    assert rqs[1]["question"] == "How does the Unruh effect relate to Hawking radiation?"


def test_parsed_plan_has_known_pitfalls():
    """Verify known_pitfalls are extracted."""
    agent = _make_agent()
    plan, _ = agent._parse_plan(VALID_PLAN_JSON, iteration=0)

    assert plan is not None
    assert len(plan.known_pitfalls) == 1
    assert "coordinate" in plan.known_pitfalls[0]
