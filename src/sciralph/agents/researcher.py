"""Researcher agent: one-shot analytical reasoning with structured JSON output."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..research_state import Evidence
from .evidence_base import ENTITY_ID_RE, EvidenceAgent

if TYPE_CHECKING:
    from ..task import Task

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


class ResearcherAgent(EvidenceAgent):
    name = "researcher"
    prompt_file = "researcher.md"
    tools = []  # one-shot: no tools

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
                ids = ENTITY_ID_RE.findall(task.body or "")
                target_id = ids[0] if ids else ""

            self._store_evidence(target_id, evidence)
