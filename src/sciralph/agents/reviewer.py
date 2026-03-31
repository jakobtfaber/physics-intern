"""Reviewer agent: one-shot structured review of hypotheses."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..research_state import ReviewResult
from .base import BaseAgent
from .parsing import JSON_FENCE_RE, try_json_loads

if TYPE_CHECKING:
    from ..research_state import ResearchState
    from ..task import Task


# Match a bare top-level { ... } object containing "verdict"
_BARE_JSON_RE = re.compile(r"\{[^{}]*\"verdict\"[^{}]*\}", re.DOTALL)


def _parse_review_json(text: str) -> dict | None:
    """Extract the last JSON block containing a verdict from model output."""
    # Prefer fenced ```json blocks — take the last one
    fenced = list(JSON_FENCE_RE.finditer(text))
    if fenced:
        try:
            return try_json_loads(fenced[-1].group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass
    # Fall back to bare JSON objects containing "verdict"
    bare = list(_BARE_JSON_RE.finditer(text))
    if bare:
        try:
            return try_json_loads(bare[-1].group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


class ReviewerAgent(BaseAgent):
    name = "reviewer"
    prompt_file = "reviewer.md"
    tools = []  # one-shot: no tools

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None

    def _auto_review_description(self, target_id: str) -> str:
        """Generate a review task description from the WH and its evidence."""
        h = self.research_state.hypotheses.get(target_id) if self.research_state else None
        if not h:
            return f"Review {target_id}."

        ev_types = sorted(set(ev.type for ev in h.evidence)) if h.evidence else ["research"]
        ev_type = "+".join(ev_types) if len(ev_types) > 1 else ev_types[0]

        # Find originating RQ
        rq_context = ""
        if self.research_state:
            for rq in self.research_state.research_questions.values():
                if target_id in rq.resolved_to:
                    rq_context = f"\nOriginating question ({rq.id}): {rq.question}"
                    if rq.context:
                        rq_context += f"\nContext: {rq.context}"
                    break

        if ev_type == "compute":
            desc = (
                f"Review {target_id}: audit the computational approach and code, checking "
                "implementation correctness, result interpretation, and physical consistency "
                "with expected qualitative behavior."
            )
        else:
            desc = (
                f"Review {target_id}: verify the analytical derivation, checking mathematical "
                "correctness, convention consistency with the problem's symbol definitions, "
                "and physical sanity (expected scaling, limiting cases, dimensional analysis)."
            )

        if rq_context:
            desc += rq_context

        return desc

    def build_context(self, task: Task, iteration: int) -> str:
        """Build focused verification context: WH + evidence + light state."""
        from ..renderers import render_research_context_xml

        parts: list[str] = []

        # 1. Research context — problem statement + answer template
        if self.research_state:
            parts.append(render_research_context_xml(self.research_state))

        # 2. Background survey — known pitfalls only
        if self.research_state and self.research_state.known_pitfalls:
            parts.append(
                f"<background-survey>\n"
                f"<known-pitfalls>\n{self.research_state.known_pitfalls}\n</known-pitfalls>\n"
                f"</background-survey>"
            )

        # 3. Research state — conventions, established results, sanity checks
        if self.research_state:
            rs_parts: list[str] = []
            if self.research_state.conventions:
                rs_parts.append(f"<conventions>\n{self.research_state.conventions}\n</conventions>")
            ers = self.research_state.established_hypotheses()
            if ers:
                er_lines = [f"- **{er.id}**: {er.statement}" for er in ers]
                rs_parts.append("<established-results>\n" + "\n".join(er_lines) + "\n</established-results>")
            if self.research_state.sanity_checks:
                checks_text = "\n".join(f"- {c}" for c in self.research_state.sanity_checks)
                rs_parts.append(f"<sanity-checks>\n{checks_text}\n</sanity-checks>")
            if rs_parts:
                parts.append("<research-state>\n" + "\n".join(rs_parts) + "\n</research-state>")

        # 4. Original question (before claim, for context)
        if self.research_state and task.target_claim:
            target_id = task.target_claim
            for rq in self.research_state.research_questions.values():
                if target_id in rq.resolved_to:
                    rq_content = f"{rq.id}: {rq.question}"
                    parts.append(f'<original-question id="{rq.id}">\n{rq_content}\n</original-question>')
                    break

        # 5. Claim + evidence
        if self.research_state and task.target_claim:
            target_id = task.target_claim
            h = self.research_state.hypotheses.get(target_id)
            if h:
                claim_parts: list[str] = [f"Statement: {h.statement}"]
                if h.derivation:
                    claim_parts.append(f"<derivation>\n{h.derivation}\n</derivation>")
                parts.append(f'<claim id="{target_id}">\n' + "\n".join(claim_parts) + "\n</claim>")

                # Evidence (iterate over all items)
                if h.evidence:
                    multi = len(h.evidence) > 1
                    for ev_idx, ev in enumerate(h.evidence, 1):
                        ev_parts: list[str] = []
                        if ev.approach:
                            ev_parts.append(f"<approach>\n{ev.approach}\n</approach>")
                        if ev.method:
                            ev_parts.append(f"<method>{ev.method}</method>")
                        if ev.result:
                            ev_parts.append(f"<result>{ev.result}</result>")
                        if ev.scripts:
                            for script_name in ev.scripts:
                                purpose = ev.script_purposes.get(script_name, "")
                                try:
                                    code = self.workspace.read_file(f"computations/{script_name}")
                                except Exception:
                                    code = "[not found]"
                                stem = Path(script_name).stem
                                try:
                                    output = self.workspace.read_file(f"computations/{stem}.output")
                                except Exception:
                                    output = "[not found]"
                                comp_parts = []
                                if purpose:
                                    comp_parts.append(f"  <purpose>{purpose}</purpose>")
                                comp_parts.append(f'  <code language="python">\n{code}\n  </code>')
                                comp_parts.append(f"  <output>\n{output}\n  </output>")
                                ev_parts.append(
                                    f'<computation name="{script_name}">\n'
                                    + "\n".join(comp_parts)
                                    + "\n</computation>"
                                )
                        if ev.derivation_file:
                            try:
                                content = self.workspace.read_file(f"derivations/{ev.derivation_file}")
                            except Exception:
                                content = ""
                            ev_parts.append(
                                f'<derivation file="{ev.derivation_file}">\n'
                                f"{content or ev.reasoning}\n</derivation>"
                            )
                        elif ev.reasoning:
                            ev_parts.append(f"<reasoning>\n{ev.reasoning}\n</reasoning>")
                        if ev.confidence:
                            ev_parts.append(f"<confidence>{ev.confidence}</confidence>")
                        label = f' n="{ev_idx}/{len(h.evidence)}"' if multi else ""
                        parts.append(f'<evidence type="{ev.type}"{label}>\n' + "\n".join(ev_parts) + "\n</evidence>")

        # 6. Instructions (task description, at the end)
        if self.research_state and task.target_claim:
            auto_desc = self._auto_review_description(task.target_claim)
        else:
            auto_desc = task.render_agent_context(include_structured=False)
        parts.append(f"<instructions>\n{auto_desc}\n</instructions>")

        return "\n\n".join(parts)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Parse structured JSON verdict from one-shot response text."""
        text = response.text or ""
        parsed = _parse_review_json(text)

        if parsed and "verdict" in parsed:
            verdict = parsed["verdict"]
            if verdict not in ("VERIFIED", "REFUTED", "INCONCLUSIVE"):
                verdict = "INCONCLUSIVE"
            review = ReviewResult(
                verdict=verdict,
                summary=parsed.get("summary", ""),
                details=parsed.get("details", ""),
                iteration=iteration,
            )
        else:
            review = ReviewResult(
                verdict="INCONCLUSIVE",
                summary="Failed to parse structured review output.",
                details=text[:2000] if text else "",
                iteration=iteration,
            )

        # Store on target hypothesis
        if self.research_state:
            target_id = task.target_claim
            if target_id and target_id in self.research_state.hypotheses:
                self.research_state.hypotheses[target_id].review = review
