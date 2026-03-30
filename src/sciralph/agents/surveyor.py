"""Surveyor agent: produces background notes mapping the research landscape."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..renderers import render_orchestrator_slim_state
from .base import BaseAgent
from .parsing import JSON_FENCE_RE, try_json_loads

# Text-valued section fields (sanity_checks is list[str], handled separately)
TEXT_SECTION_FIELDS = (
    "background", "key_insights", "known_methods",
    "known_pitfalls", "conventions_and_definitions",
    "problem_summary",
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
        self.parsed_survey: dict | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "<problem-statement>\n",
            self.research_state.problem_statement if self.research_state else "",
            "\n</problem-statement>",
        ]
        if self.research_state and self.research_state.answer_template:
            parts.append(f"\n<answer-template>\n{self.research_state.answer_template}\n</answer-template>")
        # On re-survey (iteration > 0), include current state + previous survey
        if iteration > 0 and self.research_state:
            parts.append("\n<current-research-state>\n")
            parts.append(render_orchestrator_slim_state(self.research_state))
            parts.append("\n</current-research-state>")
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        text = response.text.strip()
        sections: dict[str, str] = {}
        sanity_checks: list[str] = []

        # Try to extract structured JSON block
        fenced = list(JSON_FENCE_RE.finditer(text))
        if fenced:
            try:
                parsed = try_json_loads(fenced[-1].group(1).strip())
                if isinstance(parsed, dict):
                    sections = {
                        k: parsed[k].strip()
                        for k in TEXT_SECTION_FIELDS
                        if k in parsed and isinstance(parsed[k], str) and parsed[k].strip()
                    }
                    # sanity_checks: expect list[str], fallback from str
                    raw_sc = parsed.get("sanity_checks")
                    if isinstance(raw_sc, list):
                        sanity_checks = [str(s).strip() for s in raw_sc if str(s).strip()]
                    elif isinstance(raw_sc, str) and raw_sc.strip():
                        sanity_checks = [line.lstrip("- ").strip() for line in raw_sc.splitlines() if line.strip()]
            except (json.JSONDecodeError, ValueError, AttributeError):
                pass

        self.parsed_survey = {
            "raw_notes": text,
            **sections,
            "sanity_checks": sanity_checks,
        }
