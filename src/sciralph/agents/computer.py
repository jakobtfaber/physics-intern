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
                output = tc.output or ""
                # Strip structured header (=== ... ===\nPurpose: ...\nExit: ...\n\n)
                header_end = output.find("\n\n")
                if header_end != -1 and output.startswith("==="):
                    output = output[header_end + 2:]
                exec_outputs.append(output[:2000])

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
