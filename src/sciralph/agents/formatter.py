"""Formatter agent: produces clean ANSWER.md from final research state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..renderers import render_formatter_context
from .base import BaseAgent

if TYPE_CHECKING:
    from ..config import Config
    from ..metrics import MetricsTracker
    from ..research_state import ResearchState
    from ..task import Task
    from ..workspace import WorkspaceManager

_REJECTION_PREFIX = "FORMATTER_REJECTION:"


class FormatterAgent(BaseAgent):
    name = "formatter"
    prompt_file = "formatter.md"

    def __init__(self, config: Config, workspace: WorkspaceManager,
                 metrics: MetricsTracker, answer_template: str = ""):
        super().__init__(config, workspace, metrics)
        self.answer_template = answer_template
        self.research_state: ResearchState | None = None
        self.rejection_reason: str | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [render_formatter_context(self.research_state, answer_ers=task.answer_ers)]
        template = self.answer_template or (self.research_state.answer_template if self.research_state else "")
        if template:
            parts.append(f"<answer-template>\n{template}\n</answer-template>")
        return "\n\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Write ANSWER.md, checking for LLM-emitted rejection marker."""
        self.rejection_reason = None
        text = response.text or ""

        # Check for LLM-emitted rejection marker
        if text.lstrip().startswith(_REJECTION_PREFIX):
            self.rejection_reason = (
                text.lstrip().split("\n", 1)[0]
                .removeprefix(_REJECTION_PREFIX).strip()
            )

        # Always write — circuit breaker may accept best-effort
        self.workspace.write_file("ANSWER.md", text)
