"""Computationalist agent: symbolic/numerical verification via code execution."""

import re
from datetime import datetime, timezone

from ..llm import AgentResult, LLMResponse, call_llm
from ..markdown import parse_frontmatter
from ..sandbox import execute_python
from ..tools import ToolExecutor
from .base import BaseAgent, PROMPTS_DIR


class ComputationalistAgent(BaseAgent):
    name = "computationalist"
    prompt_file = "computationalist.md"
    tools = ToolExecutor.TOOL_DEFINITIONS

    def build_context(self, task: dict, iteration: int) -> str:
        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n## Relevant Research State (excerpts)\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
        ]
        return "\n".join(parts)

    def process_response(self, response: LLMResponse | AgentResult, task: dict, iteration: int):
        """Dispatch to agentic or legacy processing path."""
        if isinstance(response, AgentResult):
            self._process_agentic_response(response, task, iteration)
        else:
            self._process_legacy_response(response, task, iteration)

    # --- Agentic path (tool-use loop) ---

    def _process_agentic_response(self, result: AgentResult, task: dict, iteration: int):
        """Process result from tool-use agent loop.

        The LLM's final text IS the COMPUTATION_LOG entry (with CLAIM, METHOD,
        RESULT, VERDICT, NOTES). Scaffold adds header if missing and metadata.
        """
        text = result.text.strip()
        if not text:
            text = "**VERDICT:** INCONCLUSIVE\n**NOTES:** Agent produced no text output."

        # Ensure ## header
        if not text.startswith("##"):
            task_id = task.get("task_id", f"TASK-{iteration:03d}")
            text = f"## {task_id}: Computation\n\n" + text

        # Add metadata
        text += f"\n\n- **Iteration:** {iteration}\n"
        text += f"- **Tool calls:** {len(result.tool_calls)}\n"
        text += f"- **Rounds:** {result.rounds}\n"

        # Add code file references
        for tc in result.tool_calls:
            if tc.tool_name == "execute_python":
                # Find the script files written by ToolExecutor
                pass  # Files are already saved by ToolExecutor

        self.workspace.append_file("COMPUTATION_LOG.md", "\n" + text)
        self._update_computation_metadata()

    # --- Legacy path (two-pass: generate code + review) ---

    def _process_legacy_response(self, response: LLMResponse, task: dict, iteration: int):
        """Extract code, save, execute, insert results into log entry."""
        text = response.text
        log_entry, code = self._parse_computation_response(text, task, iteration)

        if code:
            task_id = task.get("task_id", f"TASK-{iteration:03d}")
            safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', task_id).lower()
            code_filename = f"computations/comp_{safe_id}.py"
            self.workspace.write_file(code_filename, code)

            # Execute (resolve to absolute so cwd doesn't cause double-pathing)
            script_path = (self.workspace.root / code_filename).resolve()
            result = execute_python(
                script_path,
                timeout=self.config.sympy_timeout_seconds,
                cwd=str(self.workspace.computations_dir),
            )

            exec_output = result.stdout
            if result.returncode != 0:
                exec_output += f"\n\nSTDERR:\n{result.stderr}"
            if result.timed_out:
                exec_output = result.stderr

            failed = result.returncode != 0 or result.timed_out

            # Insert execution output into the RESULT placeholder
            result_block = f"```\n{exec_output.strip()}\n```"
            if failed:
                result_block = (
                    "**EXECUTION FAILED** (returncode={}, timed_out={})\n\n{}"
                ).format(result.returncode, result.timed_out, result_block)
            log_entry = self._fill_result_section(log_entry, result_block)

            # Post-execution review: second LLM call to write VERDICT/NOTES
            review_text = self._review_execution(log_entry, iteration)
            log_entry += f"\n{review_text}\n"

            log_entry += f"\n- **Code:** `{code_filename}`\n"
        else:
            log_entry += "\n- **WARNING:** No Python code block found in response.\n"

        self.workspace.append_file("COMPUTATION_LOG.md", "\n" + log_entry)
        self._update_computation_metadata()

    def _parse_computation_response(self, text: str, task: dict, iteration: int) -> tuple[str, str]:
        """Extract log entry and Python code from LLM response."""
        # Extract last python code block
        code_blocks = re.findall(r'```python\s*\n(.*?)```', text, re.DOTALL)
        code = code_blocks[-1].strip() if code_blocks else ""

        # The rest is the log entry; strip code blocks for the log
        log_text = text
        # Remove code blocks from log text to keep it clean
        log_text = re.sub(r'```python\s*\n.*?```', '[see code file]', log_text, flags=re.DOTALL)

        # Strip any LLM-generated content from RESULT section (will be filled
        # with actual execution output). Match from **RESULT** to the next
        # **VERDICT** or **NOTES** header, or to end of text if those are absent
        # (since the first pass no longer produces VERDICT/NOTES).
        log_text = re.sub(
            r'(\*\*RESULT:?\*\*\s*:?)[\s\S]*?(?=\*\*(VERDICT|NOTES):?\*\*|\n- \*\*Iteration|\n- \*\*Code|\Z)',
            r'\1\n[execution output inserted below]\n\n',
            log_text,
        )

        # Ensure it has a ## header
        if not log_text.strip().startswith("##"):
            task_id = task.get("task_id", f"TASK-{iteration:03d}")
            log_text = f"## {task_id}: Computation\n" + log_text

        log_text += f"\n- **Iteration:** {iteration}\n"
        return log_text, code

    @staticmethod
    def _fill_result_section(log_entry: str, result_block: str) -> str:
        """Replace the RESULT placeholder with actual execution output."""
        placeholder = "[execution output inserted below]"
        if placeholder in log_entry:
            return log_entry.replace(placeholder, result_block)
        # Fallback: if no placeholder found (LLM didn't write RESULT section
        # at all), append at the end
        return log_entry + f"\n**RESULT**:\n{result_block}\n"

    def _review_execution(self, log_entry: str, iteration: int) -> str:
        """Second LLM call: review actual execution output and produce VERDICT/NOTES."""
        review_prompt_path = PROMPTS_DIR / "computationalist_review.md"
        review_system = review_prompt_path.read_text()

        response = call_llm(
            review_system, log_entry, self.config,
            agent_name="computationalist_review", iteration=iteration,
        )

        self.metrics.record_call(
            iteration=iteration,
            agent="computationalist_review",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            duration=response.duration,
            max_tokens_hit=(response.stop_reason == "max_tokens"),
        )

        return response.text.strip()

    # --- Shared ---

    def _update_computation_metadata(self):
        """Update COMPUTATION_LOG.md frontmatter with counts."""
        content = self.workspace.read_file("COMPUTATION_LOG.md")
        meta, body = parse_frontmatter(content)
        comp_count = len(re.findall(r'^## COMP-', body, re.MULTILINE))
        # Also count task-based entries
        comp_count += len(re.findall(r'^## TASK-', body, re.MULTILINE))
        meta["total_computations"] = max(meta.get("total_computations", 0), comp_count)
        meta["last_computation"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        from ..markdown import render_frontmatter
        self.workspace.write_file("COMPUTATION_LOG.md", render_frontmatter(meta, body))
