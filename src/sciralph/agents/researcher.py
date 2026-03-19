"""Researcher agent: analytical reasoning and derivation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..llm import AgentResult
from ..research_state import Evidence
from ..tools import ToolExecutor
from .base import BaseAgent

if TYPE_CHECKING:
    from ..research_state import ResearchState
    from ..task import Task

_ENTITY_ID_RE = re.compile(r"(?:ER|WH|RQ)-\d+")


class ResearcherAgent(BaseAgent):
    name = "researcher"
    prompt_file = "researcher.md"
    tools = ToolExecutor.RESEARCHER_TOOLS

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "<task>\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n</task>",
        ]
        if self.research_state:
            rc_parts: list[str] = []
            if self.research_state.conventions:
                rc_parts.append(f"<conventions>\n{self.research_state.conventions}\n</conventions>")
            if self.research_state.strategy:
                rc_parts.append(f"<strategy>\n{self.research_state.strategy}\n</strategy>")
            ers = self.research_state.established_hypotheses()
            if ers:
                er_lines = [f"- **{er.id}**: {er.statement}" for er in ers]
                rc_parts.append("<established-results>\n" + "\n".join(er_lines) + "\n</established-results>")
            open_rqs = self.research_state.open_research_questions()
            if open_rqs:
                rq_lines = [f"- **{rq.id}**: {rq.question}" for rq in open_rqs]
                rc_parts.append("<open-questions>\n" + "\n".join(rq_lines) + "\n</open-questions>")
            parts.append("\n<research-context>\n" + "\n".join(rc_parts) + "\n</research-context>")
        return "\n".join(parts)

    def process_response(self, response: AgentResult, task: Task, iteration: int):
        """Build Evidence from submit_result and store on target entity."""
        result_tc = next(
            (tc for tc in response.tool_calls if tc.tool_name == "submit_result"),
            None,
        )

        if result_tc and isinstance(result_tc.tool_input, dict):
            params = result_tc.tool_input
            evidence = Evidence(
                type="research",
                reasoning=params.get("result", ""),
                method=params.get("method", ""),
                result=params.get("result", ""),
                confidence=params.get("confidence", "partial"),
                iteration=iteration,
            )
        else:
            # No exit tool called — build minimal evidence from text
            evidence = Evidence(
                type="research",
                reasoning=response.text[:2000] if response.text else "",
                result="Agent produced no exit tool call.",
                confidence="partial",
                iteration=iteration,
            )

        # Store on target entity
        if self.research_state:
            target_id = ""
            if result_tc and isinstance(result_tc.tool_input, dict):
                target_id = result_tc.tool_input.get("target_id", "")
            if not target_id:
                ids = _ENTITY_ID_RE.findall(task.body or "")
                target_id = ids[0] if ids else task.target_claim

            self._store_evidence(target_id, evidence)

    def _store_evidence(self, target_id: str, evidence: Evidence):
        """Store evidence on the target entity (RQ or WH)."""
        state = self.research_state
        if not state:
            return
        if target_id in state.research_questions:
            state.research_questions[target_id].evidence = evidence
        elif target_id in state.hypotheses:
            state.hypotheses[target_id].evidence = evidence
