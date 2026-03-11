"""Computationalist agent: symbolic/numerical verification via code execution."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..llm import AgentResult
from ..markdown import parse_frontmatter, render_frontmatter
from ..tools import ToolExecutor
from .base import BaseAgent

if TYPE_CHECKING:
    from ..task import Task


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
        RESULT, VERDICT, NOTES). Scaffold adds header if missing and metadata.
        """
        text = response.text.strip()
        if not text:
            text = "**VERDICT:** INCONCLUSIVE\n**NOTES:** Agent produced no text output."

        # Ensure ## header
        if not text.startswith("##"):
            task_id = task.task_id or f"TASK-{iteration:03d}"
            text = f"## {task_id}: Computation\n\n" + text

        # Add metadata
        text += f"\n\n- **Iteration:** {iteration}\n"
        text += f"- **Tool calls:** {len(response.tool_calls)}\n"
        text += f"- **Rounds:** {response.rounds}\n"

        self.workspace.append_file("COMPUTATION_LOG.md", "\n" + text)
        self._update_computation_metadata()

    def _update_computation_metadata(self):
        """Update COMPUTATION_LOG.md frontmatter with counts."""
        content = self.workspace.read_file("COMPUTATION_LOG.md")
        meta, body = parse_frontmatter(content)
        comp_count = len(re.findall(r'^## COMP-\d+', body, re.MULTILINE))
        meta["total_computations"] = comp_count
        meta["last_computation"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.workspace.write_file("COMPUTATION_LOG.md", render_frontmatter(meta, body))
