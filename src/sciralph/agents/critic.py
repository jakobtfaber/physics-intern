"""Deep Critic agent: adversarial review of research state via tool-use."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..critic_tools import CriticToolExecutor
from ..llm import AgentResult, LLMResponse, run_agent_loop
from ..markdown import (
    parse_frontmatter,
    render_frontmatter,
    insert_into_active_critiques,
    ensure_critique_metadata_consistent,
)
from ..renderers import render_critique_log_md
from ..research_state import Critique, CritiqueStatus, Severity
from ..tools import ToolCall
from .base import BaseAgent
from ..categories import CompensationCategory as CC
from ..workspace import log_scaffold_event

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
            "## RESEARCH_STATE.md\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
            "\n## COMPUTATION_LOG.md\n",
            self.workspace.read_file("COMPUTATION_LOG.md"),
            "\n## Your Previous Critiques (do not repeat)\n",
            self.workspace.read_file("CRITIQUE_LOG.md"),
        ]
        return "\n".join(parts)

    def _call_with_tools(
        self,
        context: str,
        task: Task,
        iteration: int,
        on_round: Callable[[int, str, list[ToolCall], int, int], None] | None = None,
    ) -> AgentResult:
        """Run the critic with submit_critique/finish_review tools."""
        # Count existing critiques for CRIT-NNN auto-numbering
        if self.research_state:
            existing_count = self.research_state.next_critique_num() - 1
        else:
            existing_count = self._count_existing_critiques()
        self._tool_executor = CriticToolExecutor(existing_critique_count=existing_count)

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
        """Process critique output from tool-based agent loop."""
        if not self._tool_executor:
            return

        filed = self._tool_executor.filed_critiques
        self._no_critiques_filed = len(filed) == 0

        if self._no_critiques_filed:
            log_scaffold_event(
                self.workspace.root, iteration, CC.LOOP_CONTROL,
                "no_critiques_filed", f"summary={self._tool_executor.review_summary[:100]}",
            )
        else:
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
                if self.research_state:
                    self.research_state.critiques[crit.id] = crit
                    # Link to hypothesis
                    for t in crit.targets:
                        if t in self.research_state.hypotheses:
                            h = self.research_state.hypotheses[t]
                            if crit.id not in h.critiques:
                                h.critiques.append(crit.id)

            if self.research_state:
                # Render CRITIQUE_LOG.md from state
                self.workspace.write_file("CRITIQUE_LOG.md", render_critique_log_md(self.research_state))
            else:
                # Fallback: insert markdown directly
                md_blocks = []
                for critique_data in filed:
                    block = (
                        f"## {critique_data['id']} [{critique_data['severity']}] [UNRESOLVED]\n"
                        f"- **Target:** {critique_data['target_id']}\n"
                        f"- **Filed:** iteration {iteration}\n\n"
                        f"{critique_data['argument']}\n"
                    )
                    if critique_data.get("suggested_verification"):
                        block += f"\n- **Suggested verification:** {critique_data['suggested_verification']}\n"
                    md_blocks.append(block)
                critique_text = "\n".join(md_blocks)
                content = self.workspace.read_file("CRITIQUE_LOG.md")
                content = insert_into_active_critiques(content, critique_text)
                self.workspace.write_file("CRITIQUE_LOG.md", content)
                self._update_critique_metadata()

            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION,
                "critiques_filed_via_tools", f"count={len(filed)}",
            )

        # Update last_critic_pass timestamp
        if not self.research_state:
            self._update_critique_metadata()

    def _count_existing_critiques(self) -> int:
        """Count existing CRIT-NNN entries for auto-numbering (fallback)."""
        import re
        content = self.workspace.read_file("CRITIQUE_LOG.md")
        return len(re.findall(r'CRIT-\d+', content))

    def _update_critique_metadata(self):
        """Recount unresolved critiques and update frontmatter (fallback)."""
        content = self.workspace.read_file("CRITIQUE_LOG.md")
        updated = ensure_critique_metadata_consistent(content)
        meta, body = parse_frontmatter(updated)
        meta["last_critic_pass"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.workspace.write_file("CRITIQUE_LOG.md", render_frontmatter(meta, body))
