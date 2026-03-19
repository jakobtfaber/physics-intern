"""Deep Critic tool executor.

The critic uses a single submit_review tool to file its complete review
(summary + detailed reasoning + structured critiques) in one call.
"""

from __future__ import annotations

import time
from typing import ClassVar

from .tools import ToolCall


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI canonical format)
# ---------------------------------------------------------------------------

CRITIC_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": (
                "Submit your complete review. Call this ONCE as your only action. "
                "Include your full reasoning in 'details', a concise audit trail "
                "in 'summary', and any critiques in the 'critiques' array "
                "(leave empty if no issues found)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "Concise audit summary: for each area reviewed, one line "
                            "noting what you checked and your conclusion."
                        ),
                    },
                    "details": {
                        "type": "string",
                        "description": (
                            "Extensive reasoning: your full analysis of the research "
                            "strategy, result coherence, and any issues found. "
                            "This is the main body of your review."
                        ),
                    },
                    "critiques": {
                        "type": "array",
                        "description": (
                            "Structured critiques. Empty array if no issues found."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "severity": {
                                    "type": "string",
                                    "enum": ["HIGH", "MEDIUM", "LOW"],
                                    "description": (
                                        "HIGH: strategy actively wasting iterations or "
                                        "systematic issue threatens multiple results. "
                                        "MEDIUM: misalignment or coherence concern, "
                                        "not causing immediate harm. "
                                        "LOW: minor strategic suggestion."
                                    ),
                                },
                                "target_id": {
                                    "type": "string",
                                    "description": (
                                        "'STRATEGY' for strategy issues, or a specific "
                                        "WH/ER ID for coherence issues."
                                    ),
                                },
                                "argument": {
                                    "type": "string",
                                    "description": (
                                        "The critique argument. Include: "
                                        "(1) what is wrong, (2) why it matters, "
                                        "(3) how to test whether the objection is valid."
                                    ),
                                },
                            },
                            "required": ["severity", "target_id", "argument"],
                        },
                    },
                },
                "required": ["summary", "details", "critiques"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

class CriticToolExecutor:
    """Dispatches tool calls for the deep critic agent."""

    TOOL_DEFINITIONS: ClassVar[list[dict]] = CRITIC_TOOL_DEFINITIONS
    exit_tool_name: str = "submit_review"

    def __init__(self, existing_critique_count: int = 0):
        self.filed_critiques: list[dict] = []
        self.review_summary: str = ""
        self.review_details: str = ""
        self.stop_after_round: bool = False
        self._next_crit_num = existing_critique_count + 1

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        start = time.time()
        if tool_name != "submit_review":
            return ToolCall(
                tool_name=tool_name, tool_input=tool_input,
                output=f"Unknown tool: {tool_name}", is_error=True,
                duration=time.time() - start,
            )
        try:
            output = self._submit_review(tool_input)
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
        self.review_summary = args.get("summary", "")
        self.review_details = args.get("details", "")
        critiques = args.get("critiques", [])

        for crit_data in critiques:
            crit_id = f"CRIT-{self._next_crit_num:03d}"
            self._next_crit_num += 1
            self.filed_critiques.append({
                "id": crit_id,
                "severity": crit_data.get("severity", "MEDIUM"),
                "target_id": crit_data.get("target_id", ""),
                "argument": crit_data.get("argument", ""),
            })

        self.stop_after_round = True
        count = len(self.filed_critiques)
        if count:
            return f"Review complete. {count} critique(s) filed."
        return "Review complete. No critiques filed — clean review."
