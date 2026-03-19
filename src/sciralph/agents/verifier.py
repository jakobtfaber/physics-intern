"""Verifier agent: adversarial verification of hypotheses."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..llm import AgentResult, run_agent_loop
from ..research_state import VerificationResult
from ..verifier_tools import VerifierToolExecutor
from ..tools import ToolCall
from .base import BaseAgent

if TYPE_CHECKING:
    from ..research_state import ResearchState
    from ..task import Task


class VerifierAgent(BaseAgent):
    name = "verifier"
    prompt_file = "verify_agent.md"
    tools = VerifierToolExecutor.TOOL_DEFINITIONS

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self._tool_executor: VerifierToolExecutor | None = None
        self.research_state: ResearchState | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        """Build focused verification context: WH + evidence + light state."""
        parts = [
            "<task>\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n</task>",
        ]

        if self.research_state and task.target_claim:
            target_id = task.target_claim
            h = self.research_state.hypotheses.get(target_id)
            if h:
                claim_parts: list[str] = [f"Statement: {h.statement}"]
                if h.derivation:
                    claim_parts.append(f"<derivation>\n{h.derivation}\n</derivation>")
                parts.append(f'\n<claim id="{target_id}">\n' + "\n".join(claim_parts) + "\n</claim>")

                # Evidence
                if h.evidence:
                    ev = h.evidence
                    ev_parts: list[str] = []
                    if ev.approach:
                        ev_parts.append(f"<approach>\n{ev.approach}\n</approach>")
                    if ev.method:
                        ev_parts.append(f"<method>{ev.method}</method>")
                    if ev.result:
                        ev_parts.append(f"<result>{ev.result}</result>")
                    if ev.scripts:
                        ev_parts.append(f"<scripts>{', '.join(ev.scripts)}</scripts>")
                    if ev.output:
                        ev_parts.append(f"<output>\n{ev.output[:3000]}\n</output>")
                    if ev.reasoning:
                        ev_parts.append(f"<reasoning>\n{ev.reasoning[:3000]}\n</reasoning>")
                    if ev.confidence:
                        ev_parts.append(f"<confidence>{ev.confidence}</confidence>")
                    parts.append(f'\n<evidence type="{ev.type}">\n' + "\n".join(ev_parts) + "\n</evidence>")

                # Find originating RQ
                for rq in self.research_state.research_questions.values():
                    if target_id in rq.resolved_to:
                        rq_content = f"{rq.id}: {rq.question}"
                        if rq.context:
                            rq_content += f"\nContext: {rq.context}"
                        parts.append(f'\n<original-question id="{rq.id}">\n{rq_content}\n</original-question>')
                        break

            # Light established context
            ers = self.research_state.established_hypotheses()
            if ers:
                er_lines = [f"- **{er.id}**: {er.statement}" for er in ers]
                parts.append("\n<established-context>\n" + "\n".join(er_lines) + "\n</established-context>")
            if self.research_state.conventions:
                parts.append(f"\n<conventions>\n{self.research_state.conventions}\n</conventions>")

        return "\n".join(parts)

    def _call_with_tools(
        self,
        context: str,
        task: Task,
        iteration: int,
        on_round: Callable[[int, str, list[ToolCall], int, int, int, int, float], None] | None = None,
    ) -> AgentResult:
        """Run the verifier with verdict + critique tools."""
        existing_count = 0
        if self.research_state:
            existing_count = self.research_state.next_critique_num() - 1
        self._tool_executor = VerifierToolExecutor(existing_critique_count=existing_count)

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

    def process_response(self, response: AgentResult, task: Task, iteration: int):
        """Store VerificationResult on the target hypothesis."""
        if not self._tool_executor:
            return

        verdict_data = self._tool_executor.verdict_data
        filed_critiques = self._tool_executor.filed_critiques

        if verdict_data:
            verification = VerificationResult(
                verdict=verdict_data.get("verdict", "INCONCLUSIVE"),
                reasoning=verdict_data.get("reasoning", ""),
                critiques=filed_critiques,
                iteration=iteration,
            )
        else:
            verification = VerificationResult(
                verdict="INCONCLUSIVE",
                reasoning="Agent produced no submit_verdict call.",
                critiques=filed_critiques,
                iteration=iteration,
            )

        # Store on target hypothesis
        if self.research_state:
            target_id = ""
            if verdict_data:
                target_id = verdict_data.get("target_id", "")
            if not target_id:
                target_id = task.target_claim

            if target_id and target_id in self.research_state.hypotheses:
                self.research_state.hypotheses[target_id].verification = verification
