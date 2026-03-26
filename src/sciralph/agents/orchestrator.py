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
            if self.research_state.background_survey:
                from ..renderers import render_survey_sections_xml
                survey_xml = render_survey_sections_xml(self.research_state.background_survey)
                if survey_xml:
                    parts.append(survey_xml + "\n")
        state_text = render_orchestrator_research_state(self.research_state) if self.research_state else ""
        if iteration >= 3 and self.research_state and not self.research_state.conventions:
            parts.append(
                ">>> REMINDER: The '# Conventions' section is still empty. "
                "Consider populating it with the unit system, sign conventions, "
                "and variable definitions being used. <<<\n"
            )
        parts.extend([
            state_text,
            "\n",
            render_orchestrator_critique_log(self.research_state) if self.research_state else "",
        ])
        # Inject critique-handling advice only when there are unresolved critiques
        if self.research_state and self._has_active_critiques():
            parts.append(self._critique_handling_banner())
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

    def _has_active_critiques(self) -> bool:
        """Check if there are any unresolved critiques."""
        from ..research_state import CritiqueStatus
        if not self.research_state:
            return False
        return any(
            c.status == CritiqueStatus.ACTIVE
            for c in self.research_state.critiques.values()
        )

    @staticmethod
    def _critique_handling_banner() -> str:
        return (
            "\n>>> HANDLING CRITIQUES <<<\n"
            "Critiques have two blocking levels based on severity:\n"
            "\n"
            "**HIGH critiques (blocking)** — These BLOCK creation of new WHs and RQs. They "
            "point where the answer might change. You MUST investigate, dismiss, or accept "
            "each HIGH critique before creating new hypotheses or research questions.\n"
            "\n"
            "**MEDIUM/LOW critiques (advisory)** — These do NOT block forward progress. "
            "Address them when convenient. Dismissing with a brief reason is acceptable. "
            "Do not stall research to investigate a MEDIUM or LOW critique.\n"
            "\n"
            "The critic does NOT see detailed evidence or code; a reviewer's VERIFIED verdict "
            "is stronger on specific claims.\n"
            "\n"
            "For each critique, choose one path:\n"
            "- **Investigate** — dispatch research/compute targeting the critique entity or ID "
            "(e.g., target_claim='CRIT-001'). After evidence returns, call dismiss_critique "
            "or accept_critique.\n"
            "- **Dismiss** — call dismiss_critique(critique_id, reason). Explain why the "
            "critique is already addressed or immaterial.\n"
            "- **Accept** — call accept_critique(critique_id, resolution). If the investigation "
            "produced a new finding, pass create_rq and carry_evidence to funnel it into "
            "the research pipeline.\n"
            "\n"
            "Strategy critiques: if the disconnect is real, update_strategy and record the "
            "pivot in Research Notes.\n"
            ">>> END HANDLING CRITIQUES <<<\n"
        )

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
            rq_evidence_cap=self.config.rq_evidence_cap,
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
        """Build a Task from dispatch tool arguments."""
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
            answer_ers=data.get("answer_ers", []),
        )

    def parse_task(self, text: str, iteration: int = 0) -> Task:
        """Parse task from tool executor."""
        if self._tool_executor and self._tool_executor.task_data:
            return self._task_from_tool_data(self._tool_executor.task_data, iteration)
        # Fallback: parse from text frontmatter
        return Task.from_frontmatter(text, fallback_iteration=iteration)
