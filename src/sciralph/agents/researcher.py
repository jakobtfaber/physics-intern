"""Researcher agent: derivations, proofs, hypothesis generation."""

from ..llm import LLMResponse
from ..markdown import extract_section_by_id
from .base import BaseAgent


class ResearcherAgent(BaseAgent):
    name = "researcher"
    prompt_file = "researcher.md"

    def build_context(self, task: dict, iteration: int) -> str:
        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n## RESEARCH_STATE.md\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
        ]
        # Include relevant critiques for resolve tasks
        if task.get("task_type") == "resolve":
            parts.append("\n## Relevant Critiques\n")
            critique_log = self.workspace.read_file("CRITIQUE_LOG.md")
            for crit_id in task.get("blocking_critiques", []):
                section = extract_section_by_id(critique_log, crit_id)
                if section:
                    parts.append(section)
                    parts.append("")
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: dict, iteration: int):
        """Write PROPOSED_CHANGES.md from researcher output."""
        self.workspace.write_file("PROPOSED_CHANGES.md", response.text)
