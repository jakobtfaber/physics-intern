"""Formatter agent: produces clean ANSWER.md from final research state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..llm import LLMResponse
from .base import BaseAgent

if TYPE_CHECKING:
    from ..config import Config
    from ..metrics import MetricsTracker
    from ..task import Task
    from ..workspace import WorkspaceManager


class FormatterAgent(BaseAgent):
    name = "formatter"
    prompt_file = "formatter.md"

    def __init__(self, config: Config, workspace: WorkspaceManager,
                 metrics: MetricsTracker, answer_template: str = ""):
        super().__init__(config, workspace, metrics)
        self.answer_template = answer_template

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "## RESEARCH_STATE.md\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
            "\n## COMPUTATION_LOG.md\n",
            self.workspace.read_file("COMPUTATION_LOG.md"),
        ]
        if self.answer_template:
            parts.append("\n## Answer Template\n")
            parts.append(self.answer_template)
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Write ANSWER.md from formatter output."""
        self.workspace.write_file("ANSWER.md", response.text)
