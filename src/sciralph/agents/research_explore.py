"""Research-explore agent: analytical exploration, derivation, and reasoning."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..tools import ToolExecutor
from .computationalist import ComputationalistAgent

if TYPE_CHECKING:
    from ..task import Task


class ResearchExploreAgent(ComputationalistAgent):
    name = "research_explore"
    prompt_file = "research_explore.md"
    tools = ToolExecutor.RESEARCH_EXPLORE_TOOLS

    def build_context(self, task: Task, iteration: int) -> str:
        self.tools = ToolExecutor.RESEARCH_EXPLORE_TOOLS
        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n## Relevant Research State (excerpts)\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
        ]
        # Include relevant critiques for resolve-type tasks
        if task.blocking_critiques:
            critique_log = self.workspace.read_file("CRITIQUE_LOG.md")
            if critique_log:
                parts.append("\n## Relevant Critiques\n")
                for crit_id in task.blocking_critiques:
                    # Extract the section for this critique ID
                    import re
                    pattern = rf"(## {re.escape(crit_id)}.*?)(?=\n## |\Z)"
                    match = re.search(pattern, critique_log, re.DOTALL)
                    if match:
                        parts.append(match.group(1).strip())
                        parts.append("")
        return "\n".join(parts)
