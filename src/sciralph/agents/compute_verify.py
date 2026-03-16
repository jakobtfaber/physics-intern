"""Compute-verify agent: numerical verification via code execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..tools import ToolExecutor
from .computationalist import ComputationalistAgent

if TYPE_CHECKING:
    from ..task import Task


class ComputeVerifyAgent(ComputationalistAgent):
    name = "compute_verify"
    prompt_file = "compute_verify.md"
    tools = ToolExecutor.VERIFY_TOOLS

    def build_context(self, task: Task, iteration: int) -> str:
        # Verify agent always uses VERIFY_TOOLS (no dynamic override needed)
        self.tools = ToolExecutor.VERIFY_TOOLS
        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n## Relevant Research State (excerpts)\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
        ]
        return "\n".join(parts)
