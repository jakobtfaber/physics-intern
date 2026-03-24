"""Shared base for evidence-producing agents (researcher, computer).

Provides common context building, target resolution, evidence storage,
and relevant-results rendering. Subclasses only need to define
`name`, `prompt_file`, `tools`, and `process_response`.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from typing import TYPE_CHECKING

from ..research_state import Evidence
from .base import BaseAgent

if TYPE_CHECKING:
    from ..llm import AgentResult, LLMResponse
    from ..research_state import ResearchState
    from ..task import Task

ENTITY_ID_RE = re.compile(r"(?:ER|WH|RQ)-\d+")
_RELEVANT_ID_RE = re.compile(r"^(?:ER|WH|RQ)-\d+$")


def render_relevant_results(
    refs: list[str], research_state: "ResearchState | None",
) -> str:
    """Resolve relevant_results IDs to their content for agent context."""
    lines: list[str] = []
    for ref in refs:
        ref = ref.strip()
        if not research_state or not _RELEVANT_ID_RE.match(ref):
            lines.append(f"- {ref}")
            continue

        # Resolve entity
        entity_line = f"- **{ref}**"
        ev_summary = ""
        if ref in research_state.hypotheses:
            h = research_state.hypotheses[ref]
            entity_line += f": {h.statement}" if h.statement else ""
            if h.evidence:
                parts = []
                if h.evidence.summary:
                    parts.append(h.evidence.summary)
                elif h.evidence.result:
                    parts.append(h.evidence.result[:200])
                if h.evidence.confidence:
                    parts.append(h.evidence.confidence)
                if parts:
                    ev_summary = f"  Evidence: {' — '.join(parts)}"
        elif ref in research_state.research_questions:
            rq = research_state.research_questions[ref]
            entity_line += f": {rq.question}" if rq.question else ""
            if rq.evidence:
                parts = []
                if rq.evidence.summary:
                    parts.append(rq.evidence.summary)
                elif rq.evidence.result:
                    parts.append(rq.evidence.result[:200])
                if rq.evidence.confidence:
                    parts.append(rq.evidence.confidence)
                if parts:
                    ev_summary = f"  Evidence: {' — '.join(parts)}"
        else:
            entity_line += " (not found in current state)"

        lines.append(entity_line)
        if ev_summary:
            lines.append(ev_summary)
    return "\n".join(lines)


class EvidenceAgent(BaseAgent):
    """Base class for researcher and computer agents.

    Provides shared context building (background → target → task → research context),
    target resolution, and evidence storage. Subclasses implement process_response.
    """

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None

    def build_context(self, task: Task, iteration: int) -> str:
        parts: list[str] = []

        # 1. Background — domain context first
        if task.background:
            parts.append(f"<background>\n{task.background}\n</background>")

        # 2. Target question/statement — what the agent is answering
        target_text = self._resolve_target_text(task.target_claim)
        if target_text:
            parts.append(f"<target>\n{task.target_claim}: {target_text}\n</target>")

        # 3. Task description + method hints + assumptions
        task_parts: list[str] = []
        if task.body:
            task_parts.append(task.body)
        if task.method_hints:
            hints = "\n".join(f"- {h}" for h in task.method_hints)
            task_parts.append(f"<method-hints>\n{hints}\n</method-hints>")
        if task.assumptions:
            items = "\n".join(f"- {a}" for a in task.assumptions)
            task_parts.append(f"<assumptions>\n{items}\n</assumptions>")
        if task.relevant_results:
            items = render_relevant_results(task.relevant_results, self.research_state)
            task_parts.append(f"<relevant-results>\n{items}\n</relevant-results>")
        if task_parts:
            parts.append("<task>\n" + "\n\n".join(task_parts) + "\n</task>")

        # 4. Research context — conventions and established results only
        #    (no strategy, no open questions — those are orchestrator concerns)
        if self.research_state:
            rc_parts: list[str] = []
            if self.research_state.conventions:
                rc_parts.append(f"<conventions>\n{self.research_state.conventions}\n</conventions>")
            ers = self.research_state.established_hypotheses()
            if ers:
                er_lines = [f"- **{er.id}**: {er.statement}" for er in ers]
                rc_parts.append("<established-results>\n" + "\n".join(er_lines) + "\n</established-results>")
            if rc_parts:
                parts.append("<research-context>\n" + "\n".join(rc_parts) + "\n</research-context>")

        return "\n\n".join(parts)

    def _resolve_target_text(self, target_claim: str) -> str | None:
        """Resolve a target_claim ID to its question or statement text."""
        if not self.research_state or not target_claim:
            return None
        if target_claim in self.research_state.research_questions:
            rq = self.research_state.research_questions[target_claim]
            if rq.context:
                return f"{rq.question}\nContext: {rq.context}"
            return rq.question
        if target_claim in self.research_state.hypotheses:
            return self.research_state.hypotheses[target_claim].statement
        if target_claim in self.research_state.critiques:
            c = self.research_state.critiques[target_claim]
            text = f"[{c.severity.value}] {c.argument}"
            if c.targets:
                text += f"\nTargets: {', '.join(c.targets)}"
            return text
        return None

    def _store_evidence(self, target_id: str, evidence: Evidence):
        """Store evidence on the target entity (RQ, WH, or critique)."""
        state = self.research_state
        if not state:
            return
        if target_id in state.research_questions:
            state.research_questions[target_id].evidence = evidence
        elif target_id in state.hypotheses:
            state.hypotheses[target_id].evidence = evidence
        elif target_id in state.critiques:
            state.critiques[target_id].evidence = evidence

    @abstractmethod
    def process_response(self, response: "LLMResponse | AgentResult", task: "Task", iteration: int):
        ...
