"""Orchestrator agent: plans and coordinates research via state-mutation tools."""

from __future__ import annotations

import re
from collections.abc import Callable

from ..llm import AgentResult, LLMResponse, run_agent_loop
from ..markdown import (
    parse_frontmatter,
    render_frontmatter,
    count_er_sections,
    count_wh_sections,
    flatten_unverified_brackets,
)
from ..orchestrator_tools import OrchestratorToolExecutor
from ..renderers import render_research_state_md, render_critique_log_md
from ..task import Task, TaskType
from ..tools import ToolCall
from .base import BaseAgent
from ..categories import CompensationCategory as CC
from ..workspace import log_scaffold_event

# Agent routing for set_next_task fallback
_TASK_TYPE_AGENT_DEFAULTS = {
    "research": "researcher",
    "derive": "researcher",
    "compute": "computationalist",
    "compute_explore": "computationalist",
    "compute_verify": "computationalist",
    "critique": "deep_critic",
    "resolve": "researcher",
    "synthesize": "researcher",
    "terminate": "orchestrator",
}


class OrchestratorAgent(BaseAgent):
    name = "orchestrator"
    prompt_file = "orchestrator.md"
    tools = OrchestratorToolExecutor.TOOL_DEFINITIONS

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self.context_prefix: str = ""
        self._tool_executor: OrchestratorToolExecutor | None = None

    def _completion_analysis(self, iteration: int = 0) -> str | None:
        """Check if research appears complete; return banner if so."""
        state = self.workspace.read_file("RESEARCH_STATE.md")
        critique = self.workspace.read_file("CRITIQUE_LOG.md")
        crit_meta, _ = parse_frontmatter(critique)

        er_count = count_er_sections(state)
        wh_count = count_wh_sections(state)
        high = crit_meta.get("unresolved_high", 0) or 0
        medium = crit_meta.get("unresolved_medium", 0) or 0

        if er_count >= self.config.min_er_for_completion and wh_count == 0 and high == 0 and medium == 0:
            return (
                ">>> COMPLETION CHECK: "
                f"{er_count} Established Results, "
                f"{wh_count} Working Hypotheses remaining, "
                f"{high} HIGH / {medium} MEDIUM unresolved critiques. "
                "ALL PROBLEM STEPS APPEAR TO BE ESTABLISHED. "
                "Write a brief '## Synthesis' section using update_hypothesis or "
                "add_hypothesis, then call set_next_task with task_type: terminate. <<<"
            )

        budget_remaining = self.config.max_iterations - iteration
        if budget_remaining <= self.config.budget_synthesis_margin and er_count >= 1:
            return (
                f">>> BUDGET SYNTHESIS REQUIRED: Only {budget_remaining} "
                f"iteration(s) remaining (iteration {iteration} of "
                f"{self.config.max_iterations}). "
                f"{er_count} Established Results, "
                f"{wh_count} Working Hypotheses still pending, "
                f"{high} HIGH / {medium} MEDIUM unresolved critiques. "
                "You MUST call set_next_task with task_type: synthesize NOW. "
                "Unresolved items should be noted as limitations. <<<"
            )
        return None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = []
        if self.context_prefix:
            parts.append(self.context_prefix)
            self.context_prefix = ""  # consume after use
        banner = self._completion_analysis(iteration)
        if banner:
            parts.append(f"{banner}\n")
        state = self.workspace.read_file("RESEARCH_STATE.md")
        # Clean phantom markers to prevent LLM from copying bracketed form
        before = state
        state = flatten_unverified_brackets(state)
        if state != before:
            log_scaffold_event(self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION, "bracket_flattened", "")
        state = re.sub(r'\[((COMP|TASK)-\d+):unverified\]', r'\1 (unverified)', state)
        if iteration >= 3 and "To be populated by the orchestrator" in state:
            parts.append(
                ">>> REMINDER: The '# Conventions' section in RESEARCH_STATE.md "
                "is still empty. Consider populating it with the unit system, "
                "sign conventions, and variable definitions being used. <<<\n"
            )
        # Computation stall detection from research state
        research_state = getattr(self, "_research_state_ref", None)
        stalls = research_state.detect_computation_stalls(threshold=self.config.stall_threshold) if research_state else []
        for stall in stalls:
            parts.append(
                f">>> COMPUTATION STALL: {stall['count']} consecutive failures "
                f"on claim: {stall['claim'][:100]}. "
                f"Verdicts: {', '.join(stall['verdicts'])}. "
                f"Do NOT retry the same approach. Consider: (a) alternative derivation, "
                f"(b) skip and advance, or (c) critic review of the claim. <<<\n"
            )
        budget_remaining = self.config.max_iterations - iteration
        parts.extend([
            f"# Current Iteration: {iteration} of {self.config.max_iterations} "
            f"({budget_remaining} remaining)\n",
            "## RESEARCH_STATE.md\n",
            state,
            "\n## CRITIQUE_LOG.md\n",
            self.workspace.read_file("CRITIQUE_LOG.md"),
            f"\n## COMPUTATION_LOG.md (last {self.config.orchestrator_comp_log_tail} entries)\n",
            self.workspace.read_file_tail("COMPUTATION_LOG.md", n_entries=self.config.orchestrator_comp_log_tail),
            "\n## METRICS.md (summary)\n",
            self.workspace.read_file("METRICS.md"),
        ])
        if self.workspace.file_exists("PROPOSED_CHANGES.md"):
            parts.append("\n## PROPOSED_CHANGES.md (pending review)\n")
            parts.append(self.workspace.read_file("PROPOSED_CHANGES.md"))
        return "\n".join(parts)

    def _call_with_tools(
        self,
        context: str,
        task: Task,
        iteration: int,
        on_round: Callable[[int, str, list[ToolCall], int, int], None] | None = None,
    ) -> AgentResult:
        """Run the orchestrator with state-mutation tools."""
        # Pass research_state if the engine set it on us
        research_state = getattr(self, "_research_state_ref", None)
        self._tool_executor = OrchestratorToolExecutor(
            workspace=self.workspace, iteration=iteration,
            research_state=research_state,
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
        """Process orchestrator output — render state to markdown after tool mutations."""
        if not (self._tool_executor and self._tool_executor.mutations_applied):
            return

        research_state = self._tool_executor.research_state
        if research_state:
            # Render state to markdown files
            self.workspace.write_file("RESEARCH_STATE.md", render_research_state_md(research_state))
            self.workspace.write_file("CRITIQUE_LOG.md", render_critique_log_md(research_state))
        self.workspace.delete_file("PROPOSED_CHANGES.md")

        # Write CURRENT_TASK.md from set_next_task tool call
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
        assigned_to = data.get("assigned_to") or _TASK_TYPE_AGENT_DEFAULTS.get(raw_type, "researcher")
        return Task(
            task_id=f"TASK-{iteration:03d}",
            task_type=task_type,
            assigned_to=assigned_to,
            priority=data.get("priority", "medium"),
            iteration=iteration,
            target_claim=data.get("target_claim", ""),
            body=data.get("description", ""),
        )

    def parse_task(self, text: str, iteration: int = 0) -> Task:
        """Parse task from tool executor."""
        if self._tool_executor and self._tool_executor.task_data:
            return self._task_from_tool_data(self._tool_executor.task_data, iteration)
        # Fallback: parse from text frontmatter
        return Task.from_frontmatter(text, fallback_iteration=iteration)
