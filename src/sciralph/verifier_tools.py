"""Verifier tool executor.

The verifier uses these tools to submit a verdict on a hypothesis and
optionally file structured critiques during the verification process.
"""

from __future__ import annotations

import time
from typing import ClassVar

from .tools import ToolCall


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI canonical format)
# ---------------------------------------------------------------------------

VERIFIER_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "submit_verdict",
            "description": (
                "Submit your final verification verdict. Call this ONCE when you "
                "have examined all evidence and reached a conclusion. This "
                "immediately ends your session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The WH ID being verified (e.g. 'WH-002').",
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["VERIFIED", "REFUTED", "INCONCLUSIVE"],
                        "description": (
                            "VERIFIED: evidence is sound and supports the claim. "
                            "REFUTED: clear errors found that invalidate the claim. "
                            "INCONCLUSIVE: cannot determine — more evidence needed."
                        ),
                    },
                    "reasoning": {
                        "type": "string",
                        "description": (
                            "Detailed reasoning for your verdict. Explain what you "
                            "checked, what you found, and why you reached this conclusion."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": "Summary notes (1-3 sentences).",
                    },
                },
                "required": ["target_id", "verdict", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_critique",
            "description": (
                "File a specific critique about the evidence or methodology. "
                "Call this for each genuine issue you find during verification. "
                "Does NOT end the review — continue examining evidence and "
                "call submit_verdict when done."
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
                            "LOW: minor issue or suggestion."
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
                "required": ["severity", "argument"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_progress",
            "description": (
                "Report your verification progress so far. Summarize what "
                "you have examined and whether you have enough evidence "
                "to reach a verdict."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "findings_so_far": {
                        "type": "string",
                        "description": "Summary of what you have examined so far.",
                    },
                    "remaining_questions": {
                        "type": "string",
                        "description": "What you still need to examine, if anything.",
                    },
                    "ready_to_conclude": {
                        "type": "boolean",
                        "description": "True if you have enough evidence to call submit_verdict.",
                    },
                },
                "required": ["findings_so_far", "remaining_questions", "ready_to_conclude"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

class VerifierToolExecutor:
    """Dispatches tool calls for the verifier agent."""

    TOOL_DEFINITIONS: ClassVar[list[dict]] = VERIFIER_TOOL_DEFINITIONS
    exit_tool_name: str = "submit_verdict"

    def __init__(self, existing_critique_count: int = 0):
        self.verdict_data: dict | None = None
        self.filed_critiques: list[dict] = []
        self.stop_after_round: bool = False
        self.ready_to_conclude_signaled: bool = False
        self._next_crit_num = existing_critique_count + 1

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        start = time.time()
        handlers = {
            "submit_verdict": self._submit_verdict,
            "submit_critique": self._submit_critique,
            "report_progress": self._report_progress,
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

    def _submit_verdict(self, args: dict) -> str:
        self.verdict_data = args
        self.stop_after_round = True
        verdict = args.get("verdict", "UNKNOWN")
        target = args.get("target_id", "?")
        count = len(self.filed_critiques)
        suffix = f" ({count} critique(s) filed)" if count else ""
        return f"Verdict recorded: {verdict} for {target}{suffix}"

    def _submit_critique(self, args: dict) -> str:
        crit_id = f"VCRIT-{self._next_crit_num:03d}"
        self._next_crit_num += 1

        severity = args.get("severity", "MEDIUM")
        argument = args.get("argument", "")

        critique = {
            "id": crit_id,
            "severity": severity,
            "argument": argument,
        }
        self.filed_critiques.append(critique)
        return f"Filed {crit_id} [{severity}]. Continue examining evidence, then call submit_verdict."

    def _report_progress(self, args: dict) -> str:
        ready = args.get("ready_to_conclude", False)
        if ready:
            self.ready_to_conclude_signaled = True
            return (
                "Acknowledged. You have indicated you are ready to conclude. "
                "Call submit_verdict now with your verdict."
            )
        remaining = args.get("remaining_questions", "")
        return (
            f"Acknowledged. Remaining: {remaining}\n"
            "Continue examining evidence, then call submit_verdict when ready."
        )
