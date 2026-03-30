"""Planner agent: produces and revises research strategy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..renderers import render_planner_revise_context, _render_survey_context
from ..task import TaskType
from .base import BaseAgent
from .parsing import JSON_FENCE_RE, try_json_loads

if TYPE_CHECKING:
    from ..config import Config
    from ..metrics import MetricsTracker
    from ..research_state import ResearchState
    from ..task import Task
    from ..workspace import WorkspaceManager


def _parse_planner_json(text: str) -> dict | None:
    """Extract the last JSON block from planner output."""
    fenced = list(JSON_FENCE_RE.finditer(text))
    if fenced:
        try:
            parsed = try_json_loads(fenced[-1].group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


class PlannerAgent(BaseAgent):
    name = "planner"
    prompt_file = "planner.md"
    tools = []

    def __init__(self, config: Config, workspace: WorkspaceManager, metrics: MetricsTracker):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None
        self.parsed_strategy: str | None = None
        # Revise mode outputs
        self.parsed_entity_actions: list[dict] | None = None
        self.parsed_sanity_checks: list[str] | None = None
        self.parsed_revision_rationale: str | None = None

    def _is_revise_mode(self, task: Task) -> bool:
        return task.task_type == TaskType.PLAN_REVISE

    def run(self, task, iteration, **kwargs):
        """Swap prompt file for revise mode, then delegate to BaseAgent.run()."""
        if self._is_revise_mode(task):
            original_prompt = self.prompt_file
            # Clear cached system prompt so the new prompt_file is loaded
            self._system_prompt = None
            self.prompt_file = "planner_revise.md"
            try:
                return super().run(task, iteration, **kwargs)
            finally:
                self.prompt_file = original_prompt
                # Clear cache again so next initial-mode call reloads planner.md
                self._system_prompt = None
        return super().run(task, iteration, **kwargs)

    def build_context(self, task: Task, iteration: int) -> str:
        if self._is_revise_mode(task):
            if self.research_state:
                return render_planner_revise_context(
                    self.research_state, task.body or ""
                )
            return ""

        # Initial mode
        parts = [
            "<problem-statement>\n",
            self.research_state.problem_statement if self.research_state else "",
            "\n</problem-statement>",
        ]
        if self.research_state:
            if self.research_state.answer_template:
                parts.append(f"\n<answer-template>\n{self.research_state.answer_template}\n</answer-template>")
            survey_ctx = _render_survey_context(self.research_state)
            if survey_ctx:
                parts.append(f"\n<background-survey>\n{survey_ctx}\n</background-survey>")
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        text = response.text or ""

        if self._is_revise_mode(task):
            self._process_revise_response(text)
        else:
            # Initial mode: store raw text as strategy
            stripped = text.strip()
            self.parsed_strategy = stripped if stripped else None

    def _process_revise_response(self, text: str):
        """Parse structured JSON from revise mode response."""
        parsed = _parse_planner_json(text)
        if parsed:
            self.parsed_strategy = parsed.get("revised_strategy")
            self.parsed_entity_actions = parsed.get("entity_actions")
            raw_sc = parsed.get("sanity_checks")
            if isinstance(raw_sc, list):
                # Accept list[str] or legacy list[dict] (extract "check" field)
                self.parsed_sanity_checks = [
                    c.get("check", str(c)) if isinstance(c, dict) else str(c)
                    for c in raw_sc if c
                ]
            else:
                self.parsed_sanity_checks = None
            self.parsed_revision_rationale = parsed.get("revision_rationale")
        else:
            # Fallback: treat entire response as strategy text
            stripped = text.strip()
            self.parsed_strategy = stripped if stripped else None
            self.parsed_entity_actions = None
            self.parsed_sanity_checks = None
            self.parsed_revision_rationale = stripped[:200] if stripped else None
