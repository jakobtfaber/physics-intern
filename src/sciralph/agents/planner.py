"""Planner agent: produces an initial research strategy from problem + background survey."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..llm import LLMResponse
from .base import BaseAgent

if TYPE_CHECKING:
    from ..config import Config
    from ..metrics import MetricsTracker
    from ..research_state import ResearchState
    from ..task import Task
    from ..workspace import WorkspaceManager


class PlannerAgent(BaseAgent):
    name = "planner"
    prompt_file = "planner.md"

    def __init__(self, config: Config, workspace: WorkspaceManager, metrics: MetricsTracker):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None
        self.parsed_strategy: str | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "<problem-statement>\n",
            self.research_state.problem_statement if self.research_state else "",
            "\n</problem-statement>",
        ]
        if self.research_state and self.research_state.background_survey:
            from ..renderers import render_survey_sections_text
            survey_text = render_survey_sections_text(self.research_state.background_survey)
            if survey_text:
                parts.append(f"\n<background-survey>\n{survey_text}\n</background-survey>")
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        text = response.text.strip()
        self.parsed_strategy = text if text else None
