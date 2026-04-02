"""Base agent with template method pattern."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, TYPE_CHECKING

from rich.console import Console

from ..config import Config
from ..llm import AgentResult, LLMResponse, call_llm, run_agent_loop
from ..metrics import MetricsTracker
from ..tool_call import ToolCall
from ..tools import ToolExecutor
from ..utils.categories import CompensationCategory as CC
from ..workspace import WorkspaceManager, log_scaffold_event

console = Console()

if TYPE_CHECKING:
    from ..task import Task


PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class BaseAgent(ABC):
    """Abstract base for all SciRalph agents."""

    name: str = "base"
    prompt_file: str = ""
    tools: ClassVar[list[dict]] = []
    max_tool_rounds: ClassVar[int | None] = None
    # parse_retries is now read from self.config.parse_retries (config.default.yaml)

    def __init__(self, config: Config, workspace: WorkspaceManager, metrics: MetricsTracker):
        self.config = config
        self.workspace = workspace
        self.metrics = metrics
        self._system_prompt: str | None = None
        self._last_script_names: list[str] = []

    @property
    def system_prompt(self) -> str:
        if self._system_prompt is None:
            path = PROMPTS_DIR / self.prompt_file
            self._system_prompt = path.read_text()
        return self._system_prompt

    @abstractmethod
    def build_context(self, task: Task, iteration: int) -> str:
        """Build the user message context for the LLM call."""
        ...

    @abstractmethod
    def process_response(self, response: LLMResponse | AgentResult, task: Task, iteration: int):
        """Process the LLM response: write files, execute code, etc."""
        ...

    def run(
        self,
        task: Task,
        iteration: int,
        on_round: Callable[[int, str, list[ToolCall], int, int, int, int, float], None] | None = None,
    ) -> LLMResponse | AgentResult:
        """Template method: build context -> call LLM -> process response."""
        context = self.build_context(task, iteration)
        if self.tools:
            response = self._call_with_tools(context, task, iteration, on_round=on_round)
        else:
            response = self._call_with_retry(context, iteration)
        self.process_response(response, task, iteration)
        return response

    def _call_with_tools(
        self,
        context: str,
        task: Task,
        iteration: int,
        on_round: Callable[[int, str, list[ToolCall], int, int, int, int, float], None] | None = None,
    ) -> AgentResult:
        """Run the tool-use agent loop."""
        tool_executor = ToolExecutor(
            workspace_root=self.workspace.root,
            timeout=self.config.sympy_timeout_seconds,
            output_limit=self.config.tool_output_limit,
            task_type=task.task_type,
        )
        result = run_agent_loop(
            system=self.system_prompt,
            user_content=context,
            config=self.config,
            tool_executor=tool_executor,
            tools=self.tools,
            max_rounds=self.max_tool_rounds or self.config.max_tool_rounds,
            agent_name=self.name,
            iteration=iteration,
            on_round=on_round,
        )
        self._last_script_names = list(getattr(tool_executor, "_script_names", []))

        self.metrics.record_call(
            iteration=iteration,
            agent=self.name,
            input_tokens=result.total_input_tokens,
            output_tokens=result.total_output_tokens,
            duration=result.duration,
            max_tokens_hit=result.truncated,
            rounds=result.rounds,
            tool_calls=len(result.tool_calls),
            truncated=result.truncated,
            reasoning_tokens=result.total_reasoning_tokens,
            answer_tokens=result.total_answer_tokens,
        )

        if result.truncated:
            self.metrics.alert(
                iteration,
                f"tool_loop_truncated on {self.name} "
                f"(rounds={result.rounds}, stop={result.stop_reason})"
            )

        if result.token_alert_fired:
            self.metrics.alert(
                iteration,
                f"computation_token_alert on {self.name} "
                f"(input={result.total_input_tokens})"
            )

        return result

    def _validate_response(self, response: LLMResponse) -> bool:
        """Check whether the one-shot response is parseable.

        Override in subclasses to enable parse-failure retry. Return False
        to trigger a retry (up to ``parse_retries`` additional attempts).
        """
        return True

    def _parse_retry_hint(self) -> str | None:
        """Return a format reminder for the continuation retry, or None.

        Override in subclasses that produce structured JSON output to provide
        the expected JSON schema.  When non-None and ``parse_retries > 0``,
        ``_call_with_retry`` will re-send the original context together with
        the model's previous (incomplete) response and this hint, instead of
        making a blind fresh retry.
        """
        return None

    def _call_with_retry(self, context: str, iteration: int) -> LLMResponse:
        """Call LLM for one-shot agents, with continuation retry on parse failure.

        If the subclass overrides ``_validate_response`` and
        ``config.parse_retries > 0``, a failed parse (or a ``max_tokens``
        truncation that left some text) triggers up to ``parse_retries``
        *continuation* calls: the original context is re-sent with the model's
        accumulated response appended and a JSON format reminder from
        ``_parse_retry_hint()``.  On success the responses are merged so that
        ``process_response`` sees derivation text + JSON in one pass.

        The engine's ``_record_agent_failures()`` still detects
        ``stop_reason='max_tokens'`` on the *returned* response and injects a
        CAPACITY EXCEEDED banner when all continuations fail.
        """
        max_retries = self.config.parse_retries

        response = call_llm(self.system_prompt, context, self.config,
                            agent_name=self.name, iteration=iteration)

        self.metrics.record_call(
            iteration=iteration,
            agent=self.name,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            duration=response.duration,
            max_tokens_hit=(response.stop_reason == "max_tokens"),
            reasoning_tokens=response.reasoning_tokens,
            answer_tokens=response.answer_tokens,
        )

        # Track original stop reason for final logging
        original_stop_reason = response.stop_reason

        # Accumulate across retries for merged response
        accumulated_text = response.text or ""
        total_input = response.input_tokens
        total_output = response.output_tokens
        total_duration = response.duration
        total_reasoning = response.reasoning_tokens
        total_answer = response.answer_tokens

        attempted_continuation = False
        for attempt in range(max_retries):
            # Decide whether a continuation retry is needed
            needs_retry = False
            if response.stop_reason == "max_tokens":
                needs_retry = True
            elif not self._validate_response(response):
                needs_retry = True

            if not needs_retry:
                return response  # valid — done

            hint = self._parse_retry_hint()
            if not hint or not accumulated_text.strip():
                break  # no hint or empty text — can't continue

            attempted_continuation = True

            console.print(
                f"[yellow]{self.name}: structured output missing, "
                f"attempting continuation ({attempt + 1}/{max_retries})...[/yellow]"
            )
            log_scaffold_event(
                self.workspace.root, iteration,
                category=CC.OUTPUT_NORMALIZATION, event="parse_continuation",
                detail=(
                    f"agent={self.name}, trigger={response.stop_reason}, "
                    f"attempt={attempt + 1}/{max_retries}"
                ),
            )

            retry_context = (
                f"{context}\n\n"
                "---\n\n"
                "IMPORTANT: Your previous response to this task is shown below. "
                "It did not contain the required structured JSON output "
                "(the response may have been truncated). "
                "Do not repeat the analysis. Output ONLY the required "
                "fenced JSON block based on your work above.\n\n"
                f"<previous-response>\n{accumulated_text}\n</previous-response>"
                f"\n\n{hint}"
            )

            retry = call_llm(self.system_prompt, retry_context, self.config,
                             agent_name=self.name, iteration=iteration)

            self.metrics.record_call(
                iteration=iteration,
                agent=self.name,
                input_tokens=retry.input_tokens,
                output_tokens=retry.output_tokens,
                duration=retry.duration,
                max_tokens_hit=(retry.stop_reason == "max_tokens"),
                reasoning_tokens=retry.reasoning_tokens,
                answer_tokens=retry.answer_tokens,
            )

            # Accumulate
            accumulated_text = accumulated_text + "\n\n" + (retry.text or "")
            total_input += retry.input_tokens
            total_output += retry.output_tokens
            total_duration += retry.duration
            total_reasoning += retry.reasoning_tokens
            total_answer += retry.answer_tokens

            # Build merged response for validation
            response = LLMResponse(
                text=accumulated_text,
                input_tokens=total_input,
                output_tokens=total_output,
                stop_reason=retry.stop_reason,
                duration=total_duration,
                reasoning_tokens=total_reasoning,
                answer_tokens=total_answer,
            )

            if self._validate_response(response):
                console.print(f"[green]{self.name}: continuation succeeded[/green]")
                log_scaffold_event(
                    self.workspace.root, iteration,
                    category=CC.OUTPUT_NORMALIZATION,
                    event="parse_continuation_success",
                    detail=f"agent={self.name}, attempt={attempt + 1}",
                )
                return response

        # All retries exhausted or no hint available
        if attempted_continuation:
            log_scaffold_event(
                self.workspace.root, iteration,
                category=CC.OUTPUT_NORMALIZATION,
                event="parse_continuation_failed",
                detail=f"agent={self.name}",
            )
        if original_stop_reason == "max_tokens":
            self._log_max_tokens(response, iteration)
        return response

    def _log_max_tokens(self, response: LLMResponse, iteration: int) -> None:
        """Log max_tokens alert and scaffold event."""
        self.metrics.alert(
            iteration,
            f"max_tokens_reached on {self.name} "
            f"(input={response.input_tokens}, output={response.output_tokens})"
        )
        log_scaffold_event(
            self.workspace.root, iteration,
            category=CC.LOOP_CONTROL, event="max_tokens_no_retry",
            detail=f"agent={self.name}, output_tokens={response.output_tokens}",
        )
