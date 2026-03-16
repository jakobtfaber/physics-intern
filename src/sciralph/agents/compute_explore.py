"""Compute-explore agent: exploratory computation via code execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..tools import ToolExecutor
from .computationalist import ComputationalistAgent

if TYPE_CHECKING:
    from ..task import Task


class ComputeExploreAgent(ComputationalistAgent):
    name = "compute_explore"
    prompt_file = "compute_explore.md"
    tools = ToolExecutor.EXPLORE_TOOLS

    def build_context(self, task: Task, iteration: int) -> str:
        # Explore agent always uses EXPLORE_TOOLS (no dynamic override needed)
        self.tools = ToolExecutor.EXPLORE_TOOLS
        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n## Relevant Research State (excerpts)\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
        ]
        return "\n".join(parts)
