"""Deep Critic agent: adversarial review of research state."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..markdown import (
    parse_frontmatter,
    insert_into_active_critiques,
    filter_self_retracted_critiques,
    render_frontmatter,
    recount_critique_metadata,
)
from .base import BaseAgent

if TYPE_CHECKING:
    from ..task import Task


class CriticAgent(BaseAgent):
    name = "deep_critic"
    prompt_file = "deep_critic.md"

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "## RESEARCH_STATE.md\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
            "\n## COMPUTATION_LOG.md\n",
            self.workspace.read_file("COMPUTATION_LOG.md"),
            "\n## Your Previous Critiques (do not repeat)\n",
            self.workspace.read_file("CRITIQUE_LOG.md"),
        ]
        return "\n".join(parts)

    def _strip_preamble(self, text: str) -> str:
        """Remove text before the first ## CRIT- heading."""
        match = re.search(r'^## CRIT', text, re.MULTILINE)
        if match:
            return text[match.start():]
        return text  # no CRIT header found — keep all (could be NO_CRITIQUES_FILED)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Insert new critiques into Active section and update frontmatter counts."""
        stripped = self._strip_preamble(response.text)
        filtered_text, retracted = filter_self_retracted_critiques(stripped)

        if filtered_text.strip():
            content = self.workspace.read_file("CRITIQUE_LOG.md")
            content = insert_into_active_critiques(content, filtered_text)
            self.workspace.write_file("CRITIQUE_LOG.md", content)

        if retracted:
            self._log_retractions(retracted, iteration)

        self._update_critique_metadata()

    def _log_retractions(self, retracted: list[str], iteration: int):
        """Log retracted critiques as HTML comments (invisible to agents) and alert."""
        lines = [f"<!-- Self-retracted critiques (iteration {iteration}):"]
        for summary in retracted:
            lines.append(f"  - {summary}")
        lines.append("-->")
        self.workspace.append_file("CRITIQUE_LOG.md", "\n" + "\n".join(lines) + "\n")
        self.metrics.alert(
            iteration,
            f"Critic self-retraction: {len(retracted)} critique(s) filtered",
        )

    def _update_critique_metadata(self):
        """Recount unresolved critiques and update frontmatter."""
        content = self.workspace.read_file("CRITIQUE_LOG.md")
        meta, body = parse_frontmatter(content)
        recounted = recount_critique_metadata(content)
        meta.update(recounted)
        meta["last_critic_pass"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.workspace.write_file("CRITIQUE_LOG.md", render_frontmatter(meta, body))
