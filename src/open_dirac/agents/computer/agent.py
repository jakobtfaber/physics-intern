"""Computer agent: computational work via code execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from open_dirac.llm import AgentResult, ParseFailureError
from open_dirac.research_state import Evidence

from ..evidence_base import ENTITY_ID_RE, EvidenceAgent
from .tools import ToolExecutor

if TYPE_CHECKING:
    from open_dirac.task import Task


class ComputerAgent(EvidenceAgent):
    name = "computer"
    prompt_file = "prompt.md"
    tools = ToolExecutor.COMPUTER_TOOLS
    raise_on_parse_failure = True

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
            assumptions = approach_tc.tool_input.get("assumptions", "")
            expected_outcome = approach_tc.tool_input.get("expected_outcome", "")
            if assumptions:
                approach_text += f"\n\nAssumptions: {assumptions}"
            if expected_outcome:
                approach_text += f"\n\nExpected outcome: {expected_outcome}"

        # Extract result from submit_result tool call
        result_tc = next(
            (tc for tc in response.tool_calls if tc.tool_name == "submit_result"),
            None,
        )

        # Collect per-script purposes from execute_python calls
        purposes: dict[str, str] = {}
        exec_idx = 0
        for tc in response.tool_calls:
            if tc.tool_name == "execute_python":
                if exec_idx < len(self._last_script_names):
                    purpose = ""
                    if isinstance(tc.tool_input, dict):
                        purpose = tc.tool_input.get("purpose", "")
                    purposes[self._last_script_names[exec_idx]] = purpose
                exec_idx += 1

        # Filter scripts to evidence_scripts if provided by submit_result
        all_scripts = list(self._last_script_names)
        evidence_scripts_param: list[str] | None = None
        if result_tc and isinstance(result_tc.tool_input, dict):
            evidence_scripts_param = result_tc.tool_input.get("evidence_scripts")
        if evidence_scripts_param:
            # Validate against known scripts, keep only valid ones
            valid = [s for s in evidence_scripts_param if s in all_scripts]
            filtered_scripts = valid if valid else all_scripts
        else:
            filtered_scripts = all_scripts

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
                scripts=filtered_scripts,
                script_purposes=purposes,
                output="\n---\n".join(exec_outputs) if exec_outputs else "",
                method=params.get("method", ""),
                result=params.get("result", ""),
                description=params.get("description", ""),
                notes=params.get("notes", ""),
                confidence=params.get("confidence", "partial"),
                iteration=iteration,
            )
        else:
            # No exit tool called — agent failed to produce structured output
            raise ParseFailureError(
                agent_name=self.name,
                detail=f"Agent produced no submit_result tool call"
                       f" (rounds={getattr(response, 'rounds', '?')},"
                       f" tool_calls={len(response.tool_calls)})",
            )

        # Store on target entity
        if self.research_state:
            target_id = ""
            if result_tc and isinstance(result_tc.tool_input, dict):
                target_id = result_tc.tool_input.get("target_id", "")
            if not target_id:
                ids = ENTITY_ID_RE.findall(task.body or "")
                target_id = ids[0] if ids else task.target_claim

            self._store_evidence(target_id, evidence)
