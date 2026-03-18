"""Computer agent: computational work via code execution."""

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


class ComputerAgent(BaseAgent):
    name = "computer"
    prompt_file = "computer.md"
    tools = ToolExecutor.COMPUTER_TOOLS

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
        ]
        if self.research_state:
            parts.append("\n## Research Context\n")
            if self.research_state.conventions:
                parts.append(f"### Conventions\n{self.research_state.conventions}\n")
            if self.research_state.strategy:
                parts.append(f"### Strategy\n{self.research_state.strategy}\n")
            ers = self.research_state.established_hypotheses()
            if ers:
                parts.append("### Established Results\n")
                for er in ers:
                    parts.append(f"- **{er.id}**: {er.statement}\n")
            open_rqs = self.research_state.open_research_questions()
            if open_rqs:
                parts.append("### Open Questions\n")
                for rq in open_rqs:
                    parts.append(f"- **{rq.id}**: {rq.question}\n")
        return "\n".join(parts)

    def process_response(self, response: AgentResult, task: Task, iteration: int):
        """Build Evidence from document_approach + submit_result and store on target entity."""
        # Extract approach from document_approach tool call
        approach_tc = next(
            (tc for tc in response.tool_calls if tc.tool_name == "document_approach"),
            None,
        )
        approach_text = ""
        if approach_tc and isinstance(approach_tc.tool_input, dict):
            approach_text = approach_tc.tool_input.get("approach", "")

        # Extract result from submit_result tool call
        result_tc = next(
            (tc for tc in response.tool_calls if tc.tool_name == "submit_result"),
            None,
        )

        # Collect script outputs from execute_python calls
        exec_outputs = []
        for tc in response.tool_calls:
            if tc.tool_name == "execute_python" and not tc.is_error:
                exec_outputs.append(tc.output[:500])

        if result_tc and isinstance(result_tc.tool_input, dict):
            params = result_tc.tool_input
            evidence = Evidence(
                type="compute",
                approach=approach_text,
                scripts=list(self._last_script_names),
                output="\n---\n".join(exec_outputs) if exec_outputs else "",
                method=params.get("method", ""),
                result=params.get("result", ""),
                confidence=params.get("confidence", "partial"),
                iteration=iteration,
            )
        else:
            # No exit tool called — build minimal evidence
            evidence = Evidence(
                type="compute",
                approach=approach_text,
                scripts=list(self._last_script_names),
                output="\n---\n".join(exec_outputs) if exec_outputs else "",
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
