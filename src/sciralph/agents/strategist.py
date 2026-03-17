"""Strategist agent: produces free-form strategic notes for research guidance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..renderers import render_orchestrator_research_state
from ..research_state import ResearchStrategy
from .base import BaseAgent

if TYPE_CHECKING:
    from ..config import Config
    from ..metrics import MetricsTracker
    from ..research_state import ResearchState
    from ..task import Task
    from ..workspace import WorkspaceManager


class StrategistAgent(BaseAgent):
    name = "strategist"
    prompt_file = "strategist.md"

    def __init__(self, config: Config, workspace: WorkspaceManager, metrics: MetricsTracker):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None
        self.parsed_strategy: ResearchStrategy | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = []
        parts.append("## Problem Statement\n")
        parts.append(self.research_state.problem_statement if self.research_state else "")
        # On re-plan (iteration > 0), include current state + previous strategy
        if iteration > 0 and self.research_state:
            parts.append("\n## Current Research State\n")
            parts.append(render_orchestrator_research_state(self.research_state))
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        text = response.text.strip()
        self.parsed_strategy = ResearchStrategy(
            strategy_notes=text,
            iteration_created=iteration,
            iteration_updated=iteration,
        )
