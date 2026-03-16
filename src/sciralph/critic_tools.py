"""Deep Critic tool executor.

The critic uses these tools to file structured critiques and complete its
review.  Modeled on OrchestratorToolExecutor.
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
            "name": "submit_critique",
            "description": (
                "File a critique against a specific claim. "
                "Call once per genuine finding. "
                "Does NOT end the review — keep examining remaining claims."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["HIGH", "MEDIUM", "LOW"],
                        "description": (
                            "HIGH: could invalidate the result. "
                            "MEDIUM: gap or concern, likely doesn't invalidate. "
                            "LOW: stylistic or minor clarity issue."
                        ),
                    },
                    "target_id": {
                        "type": "string",
                        "description": "Claim ID being critiqued, e.g. WH-002 or ER-001.",
                    },
                    "argument": {
                        "type": "string",
                        "description": (
                            "The critique argument. Include: "
                            "(1) what is wrong, (2) why it matters, "
                            "(3) how to test whether the objection is valid."
                        ),
                    },
                    "suggested_verification": {
                        "type": "string",
                        "description": "Optional: specific computation or check to verify the objection.",
                    },
                },
                "required": ["severity", "target_id", "argument"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_review",
            "description": (
                "Complete the review. Call this ONCE as your final action. "
                "If you filed no submit_critique calls, this signals a clean review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "Audit summary: for each claim reviewed, one line "
                            "noting what you checked and your conclusion."
                        ),
                    },
                },
                "required": ["summary"],
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
    exit_tool_name: str = "finish_review"

    def __init__(self, existing_critique_count: int = 0):
        self.filed_critiques: list[dict] = []
        self.review_summary: str = ""
        self.stop_after_round: bool = False
        self._next_crit_num = existing_critique_count + 1

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        start = time.time()
        handlers = {
            "submit_critique": self._submit_critique,
            "finish_review": self._finish_review,
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

    def _submit_critique(self, args: dict) -> str:
        crit_id = f"CRIT-{self._next_crit_num:03d}"
        self._next_crit_num += 1

        severity = args.get("severity", "MEDIUM")
        target_id = args.get("target_id", "")
        argument = args.get("argument", "")
        suggested_verification = args.get("suggested_verification", "")

        critique = {
            "id": crit_id,
            "severity": severity,
            "target_id": target_id,
            "argument": argument,
            "suggested_verification": suggested_verification,
        }
        self.filed_critiques.append(critique)
        return f"Filed {crit_id} [{severity}] targeting {target_id}"

    def _finish_review(self, args: dict) -> str:
        self.review_summary = args.get("summary", "")
        self.stop_after_round = True
        count = len(self.filed_critiques)
        if count:
            return f"Review complete. {count} critique(s) filed."
        return "Review complete. No critiques filed — clean review."
