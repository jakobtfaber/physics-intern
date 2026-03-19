"""Reviewer tool executor.

The reviewer uses a single tool to submit a verdict on a hypothesis.
No critique filing — the reviewer simply reports its conclusion.
"""

from __future__ import annotations

import time
from typing import ClassVar

from .tools import ToolCall


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI canonical format)
# ---------------------------------------------------------------------------

REVIEWER_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": (
                "Submit your review of the hypothesis. Call this ONCE when you "
                "have examined all evidence and reached a conclusion. This "
                "immediately ends your session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["VERIFIED", "REFUTED", "INCONCLUSIVE"],
                        "description": (
                            "VERIFIED: evidence is sound and supports the claim. "
                            "REFUTED: clear errors found that invalidate the claim. "
                            "INCONCLUSIVE: cannot determine — more evidence needed."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Brief summary of the review outcome (1-3 sentences)."
                        ),
                    },
                    "details": {
                        "type": "string",
                        "description": (
                            "Detailed reasoning for your verdict. Explain what you "
                            "checked, what you found, and why you reached this "
                            "conclusion."
                        ),
                    },
                },
                "required": ["verdict", "summary", "details"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

class ReviewerToolExecutor:
    """Dispatches tool calls for the reviewer agent."""

    TOOL_DEFINITIONS: ClassVar[list[dict]] = REVIEWER_TOOL_DEFINITIONS
    exit_tool_name: str = "submit_review"

    def __init__(self):
        self.review_data: dict | None = None
        self.stop_after_round: bool = False

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        start = time.time()
        handlers = {
            "submit_review": self._submit_review,
        }
        handler = handlers.get(tool_name)
        if not handler:
            return ToolCall(
                tool_name=tool_name, tool_input=tool_input,
                output=f"Unknown tool: {tool_name}", is_error=True,
                duration=time.time() - start,
            )
        try:
            output = handler(tool_input)
            is_error = False
        except Exception as e:
            output = f"Error: {type(e).__name__}: {e}"
            is_error = True
        return ToolCall(
            tool_name=tool_name, tool_input=tool_input,
            output=output, is_error=is_error,
            duration=time.time() - start,
        )

    def _submit_review(self, args: dict) -> str:
        self.review_data = args
        self.stop_after_round = True
        verdict = args.get("verdict", "UNKNOWN")
        return f"Review recorded: {verdict}"
