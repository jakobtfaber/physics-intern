"""Computationalist agent: symbolic/numerical verification via code execution."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..llm import AgentResult
from ..markdown import parse_frontmatter, render_frontmatter
from ..tools import ToolExecutor
from .base import BaseAgent
from ..categories import CompensationCategory as CC
from ..workspace import log_scaffold_event

if TYPE_CHECKING:
    from ..task import Task

_ER_WH_ID_RE = re.compile(r"(?:ER|WH)-\d+")


class ComputationalistAgent(BaseAgent):
    name = "computationalist"
    prompt_file = "computationalist.md"
    tools = ToolExecutor.TOOL_DEFINITIONS

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n## Relevant Research State (excerpts)\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
        ]
        return "\n".join(parts)

    def process_response(self, response: AgentResult, task: Task, iteration: int):
        """Process result from tool-use agent loop.

        The LLM's final text IS the COMPUTATION_LOG entry (with CLAIM, METHOD,
        RESULT, VERDICT, NOTES). If submit_verdict was called, its structured
        data takes priority. Scaffold adds header if missing and metadata.
        """
        text = response.text.strip()

        # Check if submit_verdict was called — use its structured data
        verdict_tc = next(
            (tc for tc in response.tool_calls if tc.tool_name == "submit_verdict"),
            None,
        )
        if verdict_tc and isinstance(verdict_tc.tool_input, dict):
            text = self._format_verdict(verdict_tc.tool_input, task)
            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION,
                "submit_verdict_used",
                f"verdict={verdict_tc.tool_input.get('verdict', '?')}",
            )
        elif not text:
            log_scaffold_event(self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION, "empty_response_stub", "")
            # Extract claim IDs from task body so stall detection can match
            claim_ids = _ER_WH_ID_RE.findall(task.body or "")
            claim_line = ", ".join(dict.fromkeys(claim_ids)) if claim_ids else "unknown"
            text = (
                f"**CLAIM:** {claim_line}\n"
                "**VERDICT:** INCONCLUSIVE\n"
                "**NOTES:** Agent produced no text output."
            )

        # Ensure the CLAIM line references the target WH/ER ID (needed by
        # demotion safety check and promote_hypothesis guardrails).
        target_ids = _ER_WH_ID_RE.findall(task.body or "")
        if target_ids:
            target_id = target_ids[0]
            claim_match = re.search(r'\*\*CLAIM:\*\*\s*', text)
            if claim_match and target_id not in text[claim_match.start():claim_match.end() + 200]:
                text = text[:claim_match.end()] + f"{target_id} — " + text[claim_match.end():]
                log_scaffold_event(self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION, "claim_id_injected",
                                   f"target={target_id}")

        # Ensure ## header
        if not text.startswith("##"):
            log_scaffold_event(self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION, "header_injected",
                               f"task_id={task.task_id or ''}")
            task_id = task.task_id or f"TASK-{iteration:03d}"
            text = f"## {task_id}: Computation\n\n" + text

        # Add metadata
        text += f"\n\n- **Iteration:** {iteration}\n"
        text += f"- **Tool calls:** {len(response.tool_calls)}\n"
        text += f"- **Rounds:** {response.rounds}\n"

        self.workspace.append_file("COMPUTATION_LOG.md", "\n" + text)
        self._update_computation_metadata()

    @staticmethod
    def _format_verdict(params: dict, task: Task) -> str:
        """Format a COMP entry from submit_verdict tool parameters."""
        task_id = task.task_id or "COMP-000"
        claim = params.get("claim", "unknown")
        method = params.get("method", "unknown")
        result = params.get("result", "No results")
        verdict = params.get("verdict", "INCONCLUSIVE")
        notes = params.get("notes", "No notes")
        return (
            f"## {task_id}: Computation\n\n"
            f"**CLAIM:** {claim}\n"
            f"**METHOD:** {method}\n"
            f"**RESULT:** {result}\n\n"
            f"**VERDICT:** {verdict}\n"
            f"**NOTES:** {notes}"
        )

    def _update_computation_metadata(self):
        """Update COMPUTATION_LOG.md frontmatter with counts.

        Note: check_id_consistency() in validation.py also fixes this counter.
        Both are kept — this is a best-effort fix at write time; validation is
        the authoritative post-integration check.
        """
        content = self.workspace.read_file("COMPUTATION_LOG.md")
        meta, body = parse_frontmatter(content)
        comp_count = len(re.findall(r'^## COMP-\d+', body, re.MULTILINE))
        meta["total_computations"] = comp_count
        meta["last_computation"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.workspace.write_file("COMPUTATION_LOG.md", render_frontmatter(meta, body))
