"""Deep Critic agent: adversarial review of research state."""

from datetime import datetime, timezone

from ..llm import LLMResponse
from ..markdown import parse_frontmatter, count_unresolved_critiques, render_frontmatter
from .base import BaseAgent


class CriticAgent(BaseAgent):
    name = "deep_critic"
    prompt_file = "deep_critic.md"

    def build_context(self, task: dict, iteration: int) -> str:
        parts = [
            "## RESEARCH_STATE.md\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
            "\n## COMPUTATION_LOG.md\n",
            self.workspace.read_file("COMPUTATION_LOG.md"),
            "\n## Your Previous Critiques (do not repeat)\n",
            self.workspace.read_file("CRITIQUE_LOG.md"),
        ]
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: dict, iteration: int):
        """Append new critiques and update frontmatter counts."""
        self.workspace.append_file("CRITIQUE_LOG.md", "\n" + response.text)
        self._update_critique_metadata()

    def _update_critique_metadata(self):
        """Recount unresolved critiques and update frontmatter."""
        content = self.workspace.read_file("CRITIQUE_LOG.md")
        meta, body = parse_frontmatter(content)
        counts = count_unresolved_critiques(content)
        meta["unresolved_high"] = counts["HIGH"]
        meta["unresolved_medium"] = counts["MEDIUM"]
        meta["unresolved_low"] = counts["LOW"]
        meta["last_critic_pass"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Count total critiques
        import re
        meta["total_critiques"] = len(re.findall(r'^#{2,3} CRIT(?:IQUE)?-', content, re.MULTILINE))
        self.workspace.write_file("CRITIQUE_LOG.md", render_frontmatter(meta, body))
