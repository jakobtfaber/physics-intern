"""Researcher agent: one-shot analytical reasoning with structured JSON output."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..research_state import Evidence
from .base import BaseAgent

if TYPE_CHECKING:
    from ..research_state import ResearchState
    from ..task import Task

_ENTITY_ID_RE = re.compile(r"(?:ER|WH|RQ)-\d+")

# ---------------------------------------------------------------------------
# JSON parsing (mirrors reviewer/critic pattern)
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

_VALID_CONFIDENCE = {"exact", "approximate", "partial"}


def _parse_researcher_json(text: str) -> dict | None:
    """Extract the last JSON block containing a 'result' key from model output.

    Tries fenced ```json blocks first (last match), then falls back to
    brace-counting for bare JSON (needed because the reasoning field may
    contain braces).
    """
    # Prefer fenced ```json blocks — take the last one
    fenced = list(_JSON_FENCE_RE.finditer(text))
    if fenced:
        try:
            parsed = json.loads(fenced[-1].group(1).strip())
            if isinstance(parsed, dict) and "result" in parsed:
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # Bare JSON fallback: brace-counting (like critic)
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start : i + 1]
                if '"result"' in candidate:
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except (json.JSONDecodeError, ValueError):
                        pass
                start = None

    return None


class ResearcherAgent(BaseAgent):
    name = "researcher"
    prompt_file = "researcher.md"
    tools = []  # one-shot: no tools

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
            items = "\n".join(f"- {r}" for r in task.relevant_results)
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
            return self.research_state.research_questions[target_claim].question
        if target_claim in self.research_state.hypotheses:
            return self.research_state.hypotheses[target_claim].statement
        return None

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Parse structured JSON from one-shot response text and build Evidence."""
        text = response.text or ""
        parsed = _parse_researcher_json(text)

        if parsed and "result" in parsed:
            confidence = parsed.get("confidence", "partial")
            if confidence not in _VALID_CONFIDENCE:
                confidence = "partial"
            evidence = Evidence(
                type="research",
                reasoning=text,  # full derivation text
                result=parsed.get("result", ""),
                method=parsed.get("method", ""),
                confidence=confidence,
                summary=parsed.get("summary", ""),
                iteration=iteration,
            )
        else:
            # Parse failure — build minimal evidence from text
            evidence = Evidence(
                type="research",
                reasoning=text[:2000] if text else "",
                result="Failed to parse structured research output.",
                confidence="partial",
                iteration=iteration,
            )

        # Store on target entity — use task.target_claim, not tool params
        if self.research_state:
            target_id = task.target_claim
            if not target_id:
                ids = _ENTITY_ID_RE.findall(task.body or "")
                target_id = ids[0] if ids else ""

            self._store_evidence(target_id, evidence)

    def _store_evidence(self, target_id: str, evidence: Evidence):
        """Store evidence on the target entity (RQ or WH)."""
        state = self.research_state
        if not state:
            return
        if target_id in state.research_questions:
            state.research_questions[target_id].evidence = evidence
        elif target_id in state.hypotheses:
            state.hypotheses[target_id].evidence = evidence
