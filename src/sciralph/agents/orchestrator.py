"""Orchestrator agent: plans and coordinates research via state-mutation tools."""

from __future__ import annotations

from collections.abc import Callable

from ..llm import AgentResult, LLMResponse, run_agent_loop
from ..orchestrator_tools import OrchestratorToolExecutor
from ..renderers import (
    render_orchestrator_critique_log,
    render_orchestrator_research_state,
)
from ..task import Task, TaskType, TASK_TYPE_AGENT_MAP
from ..tools import ToolCall
from .base import PROMPTS_DIR, BaseAgent
from ..categories import CompensationCategory as CC
from ..workspace import log_scaffold_event


class OrchestratorAgent(BaseAgent):
    name = "orchestrator"
    prompt_file = "orchestrator.md"
    tools = OrchestratorToolExecutor.TOOL_DEFINITIONS

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self.context_suffix: str = ""
        self._tool_executor: OrchestratorToolExecutor | None = None
        self.research_state: ResearchState | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = []
        # Problem statement and background survey at the top of user message
        if self.research_state:
            if self.research_state.problem_statement:
                parts.append(f"<problem-statement>\n{self.research_state.problem_statement}\n</problem-statement>\n")
            if self.research_state.background_survey and self.research_state.background_survey.survey_notes:
                parts.append(f"<background-survey>\n{self.research_state.background_survey.survey_notes}\n</background-survey>\n")
        state_text = render_orchestrator_research_state(self.research_state) if self.research_state else ""
        if iteration >= 3 and self.research_state and not self.research_state.conventions:
            parts.append(
                ">>> REMINDER: The '# Conventions' section is still empty. "
                "Consider populating it with the unit system, sign conventions, "
                "and variable definitions being used. <<<\n"
            )
        parts.extend([
            f"# Current Iteration: {iteration}\n",
            state_text,
            "\n",
            render_orchestrator_critique_log(self.research_state) if self.research_state else "",
        ])
        # Situation assessment
        if self.research_state and self.research_state.situation_assessment:
            parts.append(f"\n<situation-assessment>\n{self.research_state.situation_assessment}\n</situation-assessment>\n")
        # Research notes
        if self.research_state and self.research_state.research_notes:
            note_lines = []
            for note in self.research_state.research_notes[-10:]:  # show last 10
                note_lines.append(f"- [iter {note.get('iteration', '?')}] {note.get('text', '')}")
            parts.append("\n<research-notes>\n" + "\n".join(note_lines) + "\n</research-notes>\n")
        # Inter-iteration banners (evidence results, verified hypotheses, etc.) at the end
        if self.context_suffix:
            parts.append(self.context_suffix)
            self.context_suffix = ""  # consume after use
        return "\n".join(parts)

    def _call_with_tools(
        self,
        context: str,
        task: Task,
        iteration: int,
        on_round: Callable[[int, str, list[ToolCall], int, int, int, int, float], None] | None = None,
    ) -> AgentResult:
        """Run the orchestrator with state-mutation tools."""
        self._tool_executor = OrchestratorToolExecutor(
            workspace=self.workspace, iteration=iteration,
            research_state=self.research_state,
            min_er_for_completion=self.config.min_er_for_completion,
            max_iterations=self.config.max_iterations,
            budget_synthesis_margin=self.config.budget_synthesis_margin,
        )
        result = run_agent_loop(
            system=self.system_prompt,
            user_content=context,
            config=self.config,
            tool_executor=self._tool_executor,
            tools=self.tools,
            max_rounds=self.config.max_tool_rounds,
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
        """Process orchestrator output — state already mutated by tool executor."""
        if not self._tool_executor:
            return

        # Always write CURRENT_TASK.md when a task was dispatched, even if no
        # state mutations occurred.  Without this, verify agents read a stale
        # file and target the wrong hypothesis.
        if self._tool_executor.task_data:
            task_obj = self._task_from_tool_data(self._tool_executor.task_data, iteration)
            self.workspace.write_file("CURRENT_TASK.md", task_obj.to_markdown())

    def _task_from_tool_data(self, data: dict, iteration: int) -> Task:
        """Build a Task from set_next_task tool arguments."""
        raw_type = data.get("task_type", "research")
        try:
            task_type = TaskType(raw_type)
        except ValueError:
            task_type = TaskType.RESEARCH
        assigned_to = TASK_TYPE_AGENT_MAP.get(task_type, "researcher")
        return Task(
            task_id=f"TASK-{iteration:03d}",
            task_type=task_type,
            assigned_to=assigned_to,
            priority=data.get("priority", "medium"),
            iteration=iteration,
            target_claim=data.get("target_claim", ""),
            body=data.get("description", ""),
            background=data.get("background", ""),
            method_hints=data.get("method_hints", []),
            assumptions=data.get("assumptions", []),
            relevant_results=data.get("relevant_results", []),
        )

    def parse_task(self, text: str, iteration: int = 0) -> Task:
        """Parse task from tool executor."""
        if self._tool_executor and self._tool_executor.task_data:
            return self._task_from_tool_data(self._tool_executor.task_data, iteration)
        # Fallback: parse from text frontmatter
        return Task.from_frontmatter(text, fallback_iteration=iteration)
