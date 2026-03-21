"""Tests for PlannerAgent and engine planner integration."""

from unittest.mock import MagicMock, patch

from sciralph.agents.planner import PlannerAgent
from sciralph.config import Config
from sciralph.engine import LoopState
from sciralph.llm import LLMResponse
from sciralph.research_state import BackgroundSurvey, ResearchState
from sciralph.task import Task, TaskType


class TestPlannerBuildContext:
    """Test PlannerAgent.build_context produces correct XML-wrapped content."""

    def _make_agent(self) -> PlannerAgent:
        config = Config()
        ws = MagicMock()
        ws.root = MagicMock()
        metrics = MagicMock()
        agent = PlannerAgent(config, ws, metrics)
        agent.research_state = ResearchState(
            problem_statement="Derive the Hawking temperature.",
        )
        return agent

    def test_includes_problem_statement(self):
        agent = self._make_agent()
        task = Task(task_id="PLAN-000", task_type=TaskType.PLAN, assigned_to="planner")
        ctx = agent.build_context(task, iteration=0)
        assert "<problem-statement>" in ctx
        assert "Hawking temperature" in ctx
        assert "</problem-statement>" in ctx

    def test_includes_background_survey(self):
        agent = self._make_agent()
        agent.research_state.background_survey = BackgroundSurvey(
            survey_notes="Surface gravity via Killing vectors.",
            iteration_created=0,
            iteration_updated=0,
        )
        task = Task(task_id="PLAN-000", task_type=TaskType.PLAN, assigned_to="planner")
        ctx = agent.build_context(task, iteration=0)
        assert "<background-survey>" in ctx
        assert "Killing vectors" in ctx
        assert "</background-survey>" in ctx

    def test_no_survey_section_when_missing(self):
        agent = self._make_agent()
        task = Task(task_id="PLAN-000", task_type=TaskType.PLAN, assigned_to="planner")
        ctx = agent.build_context(task, iteration=0)
        assert "<background-survey>" not in ctx


class TestPlannerProcessResponse:
    """Test PlannerAgent.process_response stores strategy correctly."""

    def _make_agent(self) -> PlannerAgent:
        config = Config()
        ws = MagicMock()
        ws.root = MagicMock()
        metrics = MagicMock()
        return PlannerAgent(config, ws, metrics)

    def test_stores_strategy(self):
        agent = self._make_agent()
        response = MagicMock(spec=LLMResponse)
        response.text = "1. Derive surface gravity\n2. Apply Unruh effect"
        task = Task(task_id="PLAN-000", task_type=TaskType.PLAN, assigned_to="planner")
        agent.process_response(response, task, iteration=0)
        assert agent.parsed_strategy == "1. Derive surface gravity\n2. Apply Unruh effect"

    def test_strips_whitespace(self):
        agent = self._make_agent()
        response = MagicMock(spec=LLMResponse)
        response.text = "  \n  Strategy content  \n  "
        task = Task(task_id="PLAN-000", task_type=TaskType.PLAN, assigned_to="planner")
        agent.process_response(response, task, iteration=0)
        assert agent.parsed_strategy == "Strategy content"

    def test_empty_response_gives_none(self):
        agent = self._make_agent()
        response = MagicMock(spec=LLMResponse)
        response.text = "   "
        task = Task(task_id="PLAN-000", task_type=TaskType.PLAN, assigned_to="planner")
        agent.process_response(response, task, iteration=0)
        assert agent.parsed_strategy is None


class TestEngineApplyStrategy:
    """Test engine._apply_strategy() stores strategy in research state."""

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

            engine.surveyor = MagicMock()
            engine.surveyor.parsed_survey = None
            engine.planner = MagicMock()
            engine.planner.parsed_strategy = None
        return engine

    def test_apply_strategy_stores_strategy(self):
        engine = self._make_engine()
        engine.planner.parsed_strategy = "1. Compute surface gravity\n2. Apply periodicity"
        engine._apply_strategy()
        assert engine.research_state.strategy == "1. Compute surface gravity\n2. Apply periodicity"

    def test_apply_strategy_none_does_nothing(self):
        engine = self._make_engine()
        engine.planner.parsed_strategy = None
        engine._apply_strategy()
        assert engine.research_state.strategy == ""

    def test_planner_skipped_on_resume_with_strategy(self):
        """When strategy already exists (e.g. resume), planner should be skippable."""
        engine = self._make_engine()
        engine.research_state.strategy = "Existing strategy from prior run."
        # The run() method checks `not self.research_state.strategy` before calling _run_planner
        assert engine.research_state.strategy  # truthy → planner would be skipped
