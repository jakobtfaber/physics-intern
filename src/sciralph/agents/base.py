"""Base agent with template method pattern."""

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Config
from ..llm import call_llm, LLMResponse
from ..metrics import MetricsTracker
from ..workspace import WorkspaceManager


PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class BaseAgent(ABC):
    """Abstract base for all SciRalph agents."""

    name: str = "base"
    prompt_file: str = ""

    def __init__(self, config: Config, workspace: WorkspaceManager, metrics: MetricsTracker):
        self.config = config
        self.workspace = workspace
        self.metrics = metrics
        self._system_prompt: str | None = None

    @property
    def system_prompt(self) -> str:
        if self._system_prompt is None:
            path = PROMPTS_DIR / self.prompt_file
            self._system_prompt = path.read_text()
        return self._system_prompt

    @abstractmethod
    def build_context(self, task: dict, iteration: int) -> str:
        """Build the user message context for the LLM call."""
        ...

    @abstractmethod
    def process_response(self, response: LLMResponse, task: dict, iteration: int):
        """Process the LLM response: write files, execute code, etc."""
        ...

    def run(self, task: dict, iteration: int) -> LLMResponse:
        """Template method: build context -> call LLM -> process response."""
        context = self.build_context(task, iteration)
        response = self._call_with_retry(context, iteration)
        self.process_response(response, task, iteration)
        return response

    def _call_with_retry(self, context: str, iteration: int) -> LLMResponse:
        """Call LLM with retry on max_tokens."""
        for attempt in range(self.config.max_retries_on_max_tokens + 1):
            response = call_llm(self.system_prompt, context, self.config,
                               agent_name=self.name, iteration=iteration)

            self.metrics.record_call(
                iteration=iteration,
                agent=self.name,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                duration=response.duration,
                max_tokens_hit=(response.stop_reason == "max_tokens"),
            )

            if response.stop_reason != "max_tokens":
                return response

            self.metrics.alert(
                iteration,
                f"max_tokens_reached on {self.name} "
                f"(input={response.input_tokens}, output={response.output_tokens})"
            )

            if attempt < self.config.max_retries_on_max_tokens:
                self.metrics.record_retry()
                # Truncate context for retry: keep first 20% and last 60%
                lines = context.splitlines()
                if len(lines) > 20:
                    cut = len(lines) // 5
                    keep_end = int(len(lines) * 0.6)
                    context = "\n".join(
                        lines[:cut]
                        + ["", "[... context truncated for retry ...]", ""]
                        + lines[-keep_end:]
                    )

        return response
