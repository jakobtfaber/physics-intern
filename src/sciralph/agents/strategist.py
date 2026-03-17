"""Strategist agent: decomposes problems into sub-problems with approaches and pitfalls."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..renderers import render_orchestrator_research_state
from ..research_state import ResearchPlan, SubProblem
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
        self.parsed_plan: ResearchPlan | None = None
        self.initial_rqs: list[dict] = []  # Consumed by engine

    def build_context(self, task: Task, iteration: int) -> str:
        parts = []
        parts.append("## Problem Statement\n")
        parts.append(self.research_state.problem_statement if self.research_state else "")
        # On re-plan (iteration > 0), include current state + previous plan
        if iteration > 0 and self.research_state:
            parts.append("\n## Current Research State\n")
            parts.append(render_orchestrator_research_state(self.research_state))
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        plan, rqs = self._parse_plan(response.text, iteration)
        self.parsed_plan = plan
        self.initial_rqs = rqs

    def _parse_plan(self, text: str, iteration: int) -> tuple[ResearchPlan | None, list[dict]]:
        """Extract a ResearchPlan and initial RQs from the LLM response JSON."""
        data = self._extract_json(text)
        if data is None:
            return None, []

        # Build SubProblems
        subs: dict[str, SubProblem] = {}
        for sp_data in data.get("sub_problems", []):
            sp_id = sp_data.get("id", "")
            if not sp_id:
                continue
            subs[sp_id] = SubProblem(
                id=sp_id,
                description=sp_data.get("description", ""),
                approach=sp_data.get("approach", ""),
                alternatives=sp_data.get("alternatives", []),
                depends_on=sp_data.get("depends_on", []),
                notes=sp_data.get("notes", ""),
            )

        plan = ResearchPlan(
            sub_problems=subs,
            strategy_summary=data.get("strategy_summary", ""),
            known_pitfalls=data.get("known_pitfalls", []),
            iteration_created=iteration,
            iteration_updated=iteration,
        )

        rqs = data.get("initial_rqs", [])
        # Normalize: each rq should have question, context, sub_problem keys
        normalized_rqs = []
        for rq in rqs:
            if isinstance(rq, dict) and rq.get("question"):
                normalized_rqs.append({
                    "question": rq["question"],
                    "context": rq.get("context", ""),
                    "sub_problem": rq.get("sub_problem", ""),
                })

        return plan, normalized_rqs

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Extract JSON from markdown fences or raw text."""
        # Try ```json ... ``` fences first
        match = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # Fallback: try the entire text as JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Last resort: try finding a JSON object in the text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None
