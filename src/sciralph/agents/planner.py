"""Planner agent: produces and revises research strategy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..rendering import _problem_guidelines, render_planner_revise_context, render_background_survey_xml
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
        self.parsed_critique_assessments: list[dict] | None = None
        self._in_revise_mode: bool = False

    def _is_revise_mode(self, task: Task) -> bool:
        return task.task_type == TaskType.PLAN_REVISE

    def _validate_response(self, response: LLMResponse) -> bool:
        if not self._in_revise_mode:
            return True  # initial mode: raw text, nothing to validate
        return _parse_planner_json(response.text or "") is not None

    def _parse_retry_hint(self) -> str | None:
        if not self._in_revise_mode:
            return None  # initial mode: no structured format
        return (
            "Recall the required output format and provide it now:\n\n"
            "```json\n"
            "{\n"
            '  "revised_strategy": "...",\n'
            '  "revision_rationale": "...",\n'
            '  "entity_actions": [{"id": "...", "action": "keep|abandon", "reason": "..."}],\n'
            '  "sanity_checks": ["...", "..."],\n'
            '  "critique_assessments": [{"id": "CRIT-NNN", "verdict": "accepted|dismissed", "reason": "..."}]\n'
            "}\n"
            "```"
        )

    def run(self, task, iteration, **kwargs):
        """Swap prompt file for revise mode, then delegate to BaseAgent.run()."""
        if self._is_revise_mode(task):
            self._in_revise_mode = True
            original_prompt = self.prompt_file
            # Clear cached system prompt so the new prompt_file is loaded
            self._system_prompt = None
            self.prompt_file = "planner_revise.md"
            try:
                return super().run(task, iteration, **kwargs)
            finally:
                self._in_revise_mode = False
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
            parts.append(f"\n{_problem_guidelines()}")
            survey_ctx = render_background_survey_xml(self.research_state)
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
            raw_ca = parsed.get("critique_assessments")
            if isinstance(raw_ca, list):
                self.parsed_critique_assessments = [
                    a for a in raw_ca
                    if isinstance(a, dict) and "id" in a and "verdict" in a
                ]
            else:
                self.parsed_critique_assessments = None
        else:
            # Fallback: treat entire response as strategy text
            stripped = text.strip()
            self.parsed_strategy = stripped if stripped else None
            self.parsed_entity_actions = None
            self.parsed_sanity_checks = None
            self.parsed_revision_rationale = stripped[:200] if stripped else None
            self.parsed_critique_assessments = None
