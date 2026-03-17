"""Research-verify agent: analytical/structural verification without code execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..renderers import render_research_state_md
from ..tools import ToolExecutor
from .computationalist import ComputationalistAgent

if TYPE_CHECKING:
    from ..task import Task


class ResearchVerifyAgent(ComputationalistAgent):
    name = "research_verify"
    prompt_file = "research_verify.md"
    tools = ToolExecutor.RESEARCH_VERIFY_TOOLS

    def build_context(self, task: Task, iteration: int) -> str:
        self.tools = ToolExecutor.RESEARCH_VERIFY_TOOLS
        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n## Relevant Research State (excerpts)\n",
            render_research_state_md(self.research_state) if self.research_state else "",
        ]
        return "\n".join(parts)
