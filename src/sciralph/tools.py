"""Tool execution for agentic agents (execute_python via sandbox)."""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .sandbox import execute_python


@dataclass
class ToolCall:
    """Record of a single tool invocation."""
    tool_name: str
    tool_input: dict
    output: str
    is_error: bool
    duration: float


class ToolExecutor:
    """Dispatches tool calls for agentic agents.

    The LLM never chooses file paths — it passes code as a string, and
    ToolExecutor writes it to computations/tool_exec_NNN.py.
    """

    TOOL_DEFINITIONS: ClassVar[list[dict]] = [
        {
            "type": "function",
            "function": {
                "name": "execute_python",
                "description": (
                    "Execute a Python script and return its stdout/stderr. "
                    "Available packages: Python 3.12+, NumPy >= 2.0, SciPy >= 1.14, "
                    "SymPy >= 1.13, matplotlib >= 3.9, standard library.\n\n"
                    "BANNED APIs (will crash):\n"
                    "- scipy.misc.derivative -> manual finite differences\n"
                    "- numpy.trapz -> numpy.trapezoid\n"
                    "- numpy.math -> math (stdlib)\n"
                    "- scipy.integrate.simps -> scipy.integrate.simpson\n\n"
                    "The script must be self-contained. Never call plt.show() "
                    "(use plt.savefig() then plt.close()). "
                    "Timeout: scripts are killed after the configured timeout "
                    "(default 60s). If you hit a timeout, simplify your approach: "
                    "reduce grid sizes, use fewer iterations, or switch to "
                    "analytical methods."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "purpose": {
                            "type": "string",
                            "description": (
                                "Brief explanation of what this computation will determine "
                                "and why it is needed beyond previous results."
                            ),
                        },
                        "code": {
                            "type": "string",
                            "description": "The complete Python script to execute.",
                        },
                    },
                    "required": ["purpose", "code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_verdict",
                "description": (
                    "Submit your final COMP entry verdict. Call this ONCE when you "
                    "have enough evidence to conclude. This immediately ends your "
                    "session. Do NOT call execute_python in the same response as "
                    "submit_verdict."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "claim": {
                            "type": "string",
                            "description": "The claim ID and description (e.g. 'WH-002 — fidelity is 1 to first order').",
                        },
                        "method": {
                            "type": "string",
                            "description": "Computation method used.",
                        },
                        "result": {
                            "type": "string",
                            "description": "Key numerical results and observations.",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["VERIFIED", "REFUTED", "INCONCLUSIVE"],
                            "description": "Your verdict.",
                        },
                        "notes": {
                            "type": "string",
                            "description": "Summary notes (1-3 sentences).",
                        },
                    },
                    "required": ["claim", "method", "result", "verdict", "notes"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "report_progress",
                "description": (
                    "Report your progress so far. You MUST call this when prompted by "
                    "the system before making more execute_python calls. Summarize what "
                    "your computations have shown and whether you have enough evidence "
                    "to reach a verdict."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "findings_so_far": {
                            "type": "string",
                            "description": "Summary of what your computations have shown so far.",
                        },
                        "remaining_questions": {
                            "type": "string",
                            "description": "What specific new information you still need, if any.",
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

    def __init__(self, workspace_root: Path, timeout: int = 60, output_limit: int = 10_000):
        self.workspace_root = workspace_root
        self.timeout = timeout
        self._output_limit = output_limit
        self._counter = 0
        self._computations_dir = workspace_root / "computations"

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        """Dispatch a tool call by name."""
        start = time.time()

        if tool_name == "execute_python":
            output, is_error = self._execute_python(tool_input["code"])
        elif tool_name == "submit_verdict":
            output, is_error = self._submit_verdict(tool_input)
        elif tool_name == "report_progress":
            output, is_error = self._report_progress(tool_input)
        else:
            raise ValueError(f"Unknown tool: {tool_name}")

        duration = time.time() - start
        return ToolCall(
            tool_name=tool_name,
            tool_input=tool_input,
            output=output,
            is_error=is_error,
            duration=duration,
        )

    def _report_progress(self, params: dict) -> tuple[str, bool]:
        """Acknowledge progress report and guide next action."""
        ready = params.get("ready_to_conclude", False)
        if ready:
            return (
                "Acknowledged. You have indicated you are ready to conclude. "
                "Call `submit_verdict` now with your findings."
            ), False
        remaining = params.get("remaining_questions", "")
        return (
            f"Acknowledged. Remaining questions: {remaining}\n"
            "Continue with your next execute_python call, then call "
            "submit_verdict when you have enough evidence."
        ), False

    def _submit_verdict(self, params: dict) -> tuple[str, bool]:
        """Record verdict and signal loop to stop."""
        self.stop_after_round = True
        self._last_verdict = params
        return f"Verdict recorded: {params.get('verdict', 'UNKNOWN')}", False

    def _execute_python(self, code: str) -> tuple[str, bool]:
        """Write code to file, execute via sandbox, return (output, is_error)."""
        self._counter += 1
        self._computations_dir.mkdir(parents=True, exist_ok=True)
        script_path = self._computations_dir / f"tool_exec_{self._counter:03d}.py"
        script_path.write_text(code)

        result = execute_python(
            script_path,
            timeout=self.timeout,
            cwd=str(self._computations_dir),
        )

        if result.timed_out:
            output = (
                f"TIMEOUT: Script exceeded {self.timeout}s limit.\n\n"
                "Suggestions:\n"
                "- Reduce grid/array sizes\n"
                "- Use fewer iterations or lower precision\n"
                "- Switch to analytical approaches where possible\n"
                "- Break the computation into smaller steps"
            )
            return output, True

        output = result.stdout
        if result.returncode != 0:
            output = result.stdout + "\n\nSTDERR:\n" + result.stderr if result.stdout else result.stderr
            return self._truncate_output(output, self._output_limit), True

        return self._truncate_output(output, self._output_limit), False

    @staticmethod
    def _truncate_output(text: str, limit: int = 10_000) -> str:
        """Truncate output to limit chars, preserving head and tail."""
        if len(text) <= limit:
            return text
        half = limit // 2
        return (
            text[:half]
            + f"\n\n[... truncated {len(text) - limit} chars ...]\n\n"
            + text[-half:]
        )
