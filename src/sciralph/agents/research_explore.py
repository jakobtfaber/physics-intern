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
        context = super().build_context(task, iteration)
        # Append critique context for resolve-type tasks
        if task.blocking_critiques and self.research_state:
            parts = ["\n## Relevant Critiques\n"]
            for crit_id in task.blocking_critiques:
                if crit_id in self.research_state.critiques:
                    c = self.research_state.critiques[crit_id]
                    sev_tag = f"[{c.severity}]"
                    parts.append(f"## {c.id} {sev_tag}\n")
                    targets_str = ", ".join(c.targets) if c.targets else "general"
                    parts.append(f"**Target:** {targets_str}\n")
                    if c.argument:
                        parts.append(c.argument)
                    parts.append("")
            context += "\n".join(parts)
        return context
