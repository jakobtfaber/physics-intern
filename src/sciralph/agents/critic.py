"""Deep Critic agent: single-round strategic review via submit_review tool."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.console import Console

from ..critic_tools import CriticToolExecutor
from ..llm import AgentResult, LLMResponse, run_agent_loop
from ..renderers import (
    _critique_log_body,
    _research_state_body,
    render_background_survey,
)
from ..research_state import Critique, CritiqueStatus, Severity
from ..tools import ToolCall
from .base import BaseAgent
from ..categories import CompensationCategory as CC
from ..workspace import log_scaffold_event

console = Console()

if TYPE_CHECKING:
    from ..research_state import ResearchState
    from ..task import Task


class CriticAgent(BaseAgent):
    name = "deep_critic"
    prompt_file = "deep_critic.md"
    tools = CriticToolExecutor.TOOL_DEFINITIONS

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self._tool_executor: CriticToolExecutor | None = None
        self._no_critiques_filed: bool = False
        self.research_state: ResearchState | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "<research-state>\n",
            _research_state_body(self.research_state) if self.research_state else "",
            "\n</research-state>",
            "\n<background-survey>\n",
            render_background_survey(self.research_state) if self.research_state else "",
            "\n</background-survey>",
            "\n<previous-critiques>\n",
            _critique_log_body(self.research_state) if self.research_state else "",
            "\n</previous-critiques>",
        ]
        return "\n".join(parts)

    def _call_with_tools(
        self,
        context: str,
        task: Task,
        iteration: int,
        on_round: Callable[[int, str, list[ToolCall], int, int, int, int, float], None] | None = None,
    ) -> AgentResult:
        """Run the critic with submit_review tool (single round)."""
        # Count existing critiques for CRIT-NNN auto-numbering
        if self.research_state:
            existing_count = self.research_state.next_critique_num() - 1
        else:
            existing_count = 0
        self._tool_executor = CriticToolExecutor(existing_critique_count=existing_count)

        result = run_agent_loop(
            system=self.system_prompt,
            user_content=context,
            config=self.config,
            tool_executor=self._tool_executor,
            tools=self.tools,
            max_rounds=1,
            agent_name=self.name,
            iteration=iteration,
            on_round=on_round,
        )

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
        return result

    def process_response(self, response: LLMResponse | AgentResult, task: Task, iteration: int):
        """Process review output from submit_review tool call."""
        if not self._tool_executor:
            return

        filed = self._tool_executor.filed_critiques
        self._no_critiques_filed = len(filed) == 0

        if self._no_critiques_filed:
            summary = self._tool_executor.review_summary
            log_scaffold_event(
                self.workspace.root, iteration, CC.LOOP_CONTROL,
                "no_critiques_filed", f"summary={summary}",
            )
            if self.research_state:
                self.research_state.critic_clean_reviews.append({
                    "iteration": iteration,
                    "summary": summary,
                })
        elif self.research_state:
            # Write Critique objects to research state
            for critique_data in filed:
                sev = Severity(critique_data["severity"])
                crit = Critique(
                    id=critique_data["id"],
                    targets=[critique_data["target_id"]] if critique_data["target_id"] else [],
                    severity=sev,
                    argument=critique_data["argument"],
                    status=CritiqueStatus.ACTIVE,
                    iteration_filed=iteration,
                )
                self.research_state.critiques[crit.id] = crit
                # Console output + scaffold event
                sev_label = sev.value
                target_str = critique_data["target_id"] or "general"
                arg_short = critique_data["argument"][:80]
                if sev == Severity.HIGH:
                    style = "bold red"
                elif sev == Severity.MEDIUM:
                    style = "bold yellow"
                else:
                    style = "dim"
                console.print(f"  [{style}]{crit.id}[/] [{sev_label}] targeting {target_str}: {arg_short}")
                log_scaffold_event(
                    self.workspace.root, iteration, CC.STATE_INVARIANTS,
                    "file_critique",
                    f"{crit.id} [{sev_label}] → {target_str}: {critique_data['argument'][:120]}",
                )
                # Link to hypothesis
                for t in crit.targets:
                    if t in self.research_state.hypotheses:
                        h = self.research_state.hypotheses[t]
                        if crit.id not in h.critiques:
                            h.critiques.append(crit.id)
