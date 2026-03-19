"""Formatter agent: produces clean ANSWER.md from final research state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..renderers import render_evidence_log_md, render_research_state_md
from .base import BaseAgent

if TYPE_CHECKING:
    from ..config import Config
    from ..metrics import MetricsTracker
    from ..research_state import ResearchState
    from ..task import Task
    from ..workspace import WorkspaceManager


class FormatterAgent(BaseAgent):
    name = "formatter"
    prompt_file = "formatter.md"

    def __init__(self, config: Config, workspace: WorkspaceManager,
                 metrics: MetricsTracker, answer_template: str = ""):
        super().__init__(config, workspace, metrics)
        self.answer_template = answer_template
        self.research_state: ResearchState | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "<research-state>\n",
            render_research_state_md(self.research_state) if self.research_state else "",
            "\n</research-state>",
            "\n<evidence-log>\n",
            render_evidence_log_md(self.research_state) if self.research_state else "",
            "\n</evidence-log>",
        ]
        if self.answer_template:
            parts.append("\n<answer-template>\n")
            parts.append(self.answer_template)
            parts.append("\n</answer-template>")
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Write ANSWER.md from formatter output."""
        self.workspace.write_file("ANSWER.md", response.text)
