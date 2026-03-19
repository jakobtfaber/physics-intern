"""Tool execution for agentic agents (execute_python via sandbox)."""

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .sandbox import execute_python
from .task import TaskType


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

    _EXECUTE_PYTHON_DEF: ClassVar[dict] = {
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
                    "filename": {
                        "type": "string",
                        "description": (
                            "A short, descriptive filename for this script "
                            "(e.g. 'verify_enumeration.py'). Each script runs "
                            "as an independent .py file."
                        ),
                    },
                },
                "required": ["purpose", "code"],
            },
        },
    }

    _SUBMIT_VERDICT_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "submit_verdict",
            "description": (
                "Submit your final verification verdict. Call this ONCE when you "
                "have enough evidence to conclude. This immediately ends your "
                "session. Do NOT call execute_python in the same response as "
                "submit_verdict."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The WH/ER ID being verified (e.g. 'WH-002').",
                    },
                    "claim": {
                        "type": "string",
                        "description": "The claim description (e.g. 'fidelity is 1 to first order').",
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
                    "evidence_scripts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of script filenames providing key evidence "
                            "(e.g. ['001_verify_enumeration.py'])."
                        ),
                    },
                },
                "required": ["target_id", "claim", "method", "result", "verdict", "notes"],
            },
        },
    }

    _SUBMIT_RESULT_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "submit_result",
            "description": (
                "Submit the result of an exploratory computation. Call this ONCE "
                "when you have a concrete result. This immediately ends your session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The WH/ER ID being explored (e.g. 'WH-001').",
                    },
                    "description": {
                        "type": "string",
                        "description": "What was computed.",
                    },
                    "method": {
                        "type": "string",
                        "description": "Approach used.",
                    },
                    "result": {
                        "type": "string",
                        "description": "The actual result.",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["exact", "approximate", "partial"],
                        "description": "Confidence level of the result.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes.",
                    },
                    "evidence_scripts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of script filenames providing key evidence "
                            "(e.g. ['001_compute_partition.py'])."
                        ),
                    },
                },
                "required": ["target_id", "description", "method", "result", "confidence", "notes"],
            },
        },
    }

    _REPORT_PROGRESS_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "report_progress",
            "description": (
                "Report your progress so far. You MUST call this when prompted by "
                "the system before making more execute_python calls. Summarize what "
                "your computations have shown and whether you have enough evidence "
                "to reach a conclusion."
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
                        "description": "True if you have enough evidence to call submit_verdict/submit_result.",
                    },
                },
                "required": ["findings_so_far", "remaining_questions", "ready_to_conclude"],
            },
        },
    }

    _DOCUMENT_APPROACH_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "document_approach",
            "description": (
                "Document your computational approach BEFORE writing code. "
                "You MUST call this before your first execute_python call. "
                "Records your plan, assumptions, and expected outcome so "
                "the verifier can later assess your methodology."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "approach": {
                        "type": "string",
                        "description": (
                            "Detailed description of the computational approach: "
                            "what you will compute, how, and why this method is appropriate."
                        ),
                    },
                    "assumptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of assumptions underlying the computation.",
                    },
                    "expected_outcome": {
                        "type": "string",
                        "description": "What form the result should take and how to judge success.",
                    },
                },
                "required": ["approach"],
            },
        },
    }

    # Tool sets by agent type
    RESEARCHER_TOOLS: ClassVar[list[dict]] = [
        _SUBMIT_RESULT_DEF,
        _REPORT_PROGRESS_DEF,
    ]
    COMPUTER_TOOLS: ClassVar[list[dict]] = [
        _DOCUMENT_APPROACH_DEF,
        _EXECUTE_PYTHON_DEF,
        _SUBMIT_RESULT_DEF,
        _REPORT_PROGRESS_DEF,
    ]

    # Default tool set (computer tools)
    TOOL_DEFINITIONS: ClassVar[list[dict]] = COMPUTER_TOOLS

    @classmethod
    def tools_for_task_type(cls, task_type: "TaskType") -> list[dict]:
        """Return the appropriate tool set for a task type."""
        if task_type == TaskType.RESEARCH:
            return cls.RESEARCHER_TOOLS
        if task_type == TaskType.COMPUTE:
            return cls.COMPUTER_TOOLS
        return cls.TOOL_DEFINITIONS  # default fallback

    # Tool sets available after document_approach has been called
    COMPUTER_TOOLS_POST_APPROACH: ClassVar[list[dict]] = [
        _EXECUTE_PYTHON_DEF,
        _SUBMIT_RESULT_DEF,
        _REPORT_PROGRESS_DEF,
    ]

    def __init__(self, workspace_root: Path, timeout: int = 60, output_limit: int = 10_000,
                 task_type: "TaskType | None" = None):
        self.workspace_root = workspace_root
        self.timeout = timeout
        self._output_limit = output_limit
        self._counter = 0
        self._computations_dir = workspace_root / "computations"
        self._task_type = task_type
        self.ready_to_conclude_signaled = False
        self._script_names: list[str] = []
        self._approach_documented: bool = False

    @staticmethod
    def _sanitize_filename(raw: str, max_len: int = 60) -> str:
        """Sanitize a model-provided filename for safe filesystem use."""
        # Strip path separators and parent references
        cleaned = raw.replace("/", "_").replace("\\", "_").replace("..", "_")
        # Remove non-alphanumeric except _, -, .
        cleaned = re.sub(r"[^a-zA-Z0-9_.\-]", "", cleaned)
        # Ensure .py extension
        if not cleaned.endswith(".py"):
            cleaned = re.sub(r"\.[^.]*$", "", cleaned)  # strip wrong extension
            cleaned += ".py"
        # Truncate (keep .py suffix)
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len - 3] + ".py"
        return cleaned or "script.py"

    @property
    def exit_tool_name(self) -> str:
        """Return the context-appropriate exit tool name."""
        return "submit_result"

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        """Dispatch a tool call by name."""
        start = time.time()

        if tool_name == "execute_python":
            output, is_error = self._execute_python(
                tool_input["code"],
                purpose=tool_input.get("purpose", ""),
                filename=tool_input.get("filename", ""),
            )
        elif tool_name == "submit_result":
            output, is_error = self._submit_result(tool_input)
        elif tool_name == "document_approach":
            output, is_error = self._document_approach(tool_input)
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

    def _document_approach(self, params: dict) -> tuple[str, bool]:
        """Record the computational approach before coding. Only callable once."""
        if self._approach_documented:
            return (
                "Error: approach already documented. "
                "Call execute_python to run your code, or submit_result to finish."
            ), True
        approach = params.get("approach", "")
        assumptions = params.get("assumptions", [])
        expected_outcome = params.get("expected_outcome", "")
        self._documented_approach = {
            "approach": approach,
            "assumptions": assumptions,
            "expected_outcome": expected_outcome,
        }
        self._approach_documented = True
        return "Approach documented. Now call execute_python to run your code.", False

    @property
    def active_tools(self) -> list[dict] | None:
        """Return dynamic tool set, or None to keep the original tools.

        After document_approach is called, remove it from the tool set
        so the model cannot call it again.
        """
        if self._approach_documented:
            return self.COMPUTER_TOOLS_POST_APPROACH
        return None

    def _report_progress(self, params: dict) -> tuple[str, bool]:
        """Acknowledge progress report and guide next action."""
        exit_tool = self.exit_tool_name
        ready = params.get("ready_to_conclude", False)
        if ready:
            self.ready_to_conclude_signaled = True
            return (
                "Acknowledged. You have indicated you are ready to conclude. "
                f"Call `{exit_tool}` now with your findings."
            ), False
        remaining = params.get("remaining_questions", "")
        return (
            f"Acknowledged. Remaining questions: {remaining}\n"
            "Continue with your next execute_python call, then call "
            f"{exit_tool} when you have enough evidence."
        ), False

    def _submit_result(self, params: dict) -> tuple[str, bool]:
        """Record exploratory result and signal loop to stop."""
        self.stop_after_round = True
        self._last_result = params
        return f"Result recorded for {params.get('target_id', '?')}: {params.get('confidence', '?')}", False

    def _execute_python(self, code: str, purpose: str = "", filename: str = "") -> tuple[str, bool]:
        """Write code to file, execute via sandbox, return (output, is_error)."""
        self._counter += 1
        self._computations_dir.mkdir(parents=True, exist_ok=True)

        # Build script name
        if filename:
            sanitized = self._sanitize_filename(filename)
            # Strip leading counter if agent already included one
            sanitized = re.sub(r"^\d+_", "", sanitized)
            if not sanitized or sanitized == ".py":
                sanitized = "script.py"
            script_name = f"{self._counter:03d}_{sanitized}"
        else:
            script_name = f"tool_exec_{self._counter:03d}.py"
        self._script_names.append(script_name)

        script_path = self._computations_dir / script_name
        script_path.write_text(code)

        result = execute_python(
            script_path,
            timeout=self.timeout,
            cwd=str(self._computations_dir),
        )

        # Determine exit status label
        if result.timed_out:
            exit_label = "timeout"
        elif result.returncode != 0:
            exit_label = f"error (rc={result.returncode})"
        else:
            exit_label = "success"

        # Build structured header
        header = f"=== {script_name} ===\n"
        if purpose:
            header += f"Purpose: {purpose}\n"
        header += f"Exit: {exit_label}\n\n"

        if result.timed_out:
            body = (
                f"TIMEOUT: Script exceeded {self.timeout}s limit.\n\n"
                "Suggestions:\n"
                "- Reduce grid/array sizes\n"
                "- Use fewer iterations or lower precision\n"
                "- Switch to analytical approaches where possible\n"
                "- Break the computation into smaller steps"
            )
            self._save_output_file(script_name, body)
            return header + body, True

        # Combine stdout/stderr for error cases
        if result.returncode != 0:
            raw_output = result.stdout + "\n\nSTDERR:\n" + result.stderr if result.stdout else result.stderr
        else:
            raw_output = result.stdout

        # Save full output before truncation
        self._save_output_file(script_name, raw_output)

        # Truncate the body portion only
        body = self._truncate_output(raw_output, self._output_limit)

        if result.returncode != 0:
            if "NameError" in result.stderr:
                body += (
                    "\n\n--- REMINDER ---\n"
                    "Each execute_python call runs in a FRESH Python process. No variables,\n"
                    "functions, or imports carry over from previous calls. You must include\n"
                    "ALL imports and function definitions in every script."
                )
            return header + body, True

        return header + body, False

    def _save_output_file(self, script_name: str, content: str) -> None:
        """Save script output to a companion .output file."""
        stem = Path(script_name).stem
        output_path = self._computations_dir / f"{stem}.output"
        output_path.write_text(content)

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
