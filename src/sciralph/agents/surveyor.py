"""Surveyor agent: produces background notes mapping the research landscape."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..renderers import render_orchestrator_research_state
from ..research_state import BackgroundSurvey
from .base import BaseAgent
from .parsing import JSON_FENCE_RE, try_json_loads

SECTION_FIELDS = (
    "background", "key_insights", "known_methods",
    "known_pitfalls", "conventions_and_definitions", "sanity_checks",
)

if TYPE_CHECKING:
    from ..config import Config
    from ..metrics import MetricsTracker
    from ..research_state import ResearchState
    from ..task import Task
    from ..workspace import WorkspaceManager


class SurveyorAgent(BaseAgent):
    name = "surveyor"
    prompt_file = "surveyor.md"

    def __init__(self, config: Config, workspace: WorkspaceManager, metrics: MetricsTracker):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None
        self.parsed_survey: BackgroundSurvey | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "<problem-statement>\n",
            self.research_state.problem_statement if self.research_state else "",
            "\n</problem-statement>",
        ]
        # On re-survey (iteration > 0), include current state + previous survey
        if iteration > 0 and self.research_state:
            parts.append("\n<current-research-state>\n")
            parts.append(render_orchestrator_research_state(self.research_state))
            parts.append("\n</current-research-state>")
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        text = response.text.strip()
        sections: dict[str, str] = {}

        # Try to extract structured JSON block
        fenced = list(JSON_FENCE_RE.finditer(text))
        if fenced:
            try:
                parsed = try_json_loads(fenced[-1].group(1).strip())
                if isinstance(parsed, dict):
                    sections = {
                        k: parsed[k].strip()
                        for k in SECTION_FIELDS
                        if k in parsed and isinstance(parsed[k], str) and parsed[k].strip()
                    }
            except (json.JSONDecodeError, ValueError, AttributeError):
                pass

        self.parsed_survey = BackgroundSurvey(
            raw_notes=text,
            **sections,
            iteration_created=iteration,
            iteration_updated=iteration,
        )
