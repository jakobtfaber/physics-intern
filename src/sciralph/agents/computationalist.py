"""Computationalist agent: symbolic/numerical verification via code execution."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..llm import AgentResult
from ..markdown import parse_frontmatter, render_frontmatter
from ..tools import ToolExecutor
from .base import BaseAgent
from ..categories import CompensationCategory as CC
from ..workspace import log_scaffold_event
from ..task import TaskType

if TYPE_CHECKING:
    from ..task import Task

_ER_WH_ID_RE = re.compile(r"(?:ER|WH)-\d+")


class ComputationalistAgent(BaseAgent):
    name = "computationalist"
    prompt_file = "computationalist.md"
    tools = ToolExecutor.TOOL_DEFINITIONS  # default; overridden in build_context

    def build_context(self, task: Task, iteration: int) -> str:
        # Set tools dynamically based on task type
        self.tools = ToolExecutor.tools_for_task_type(task.task_type)

        parts = [
            "## CURRENT_TASK.md\n",
            self.workspace.read_file("CURRENT_TASK.md"),
            "\n## Relevant Research State (excerpts)\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
        ]
        return "\n".join(parts)

    def process_response(self, response: AgentResult, task: Task, iteration: int):
        """Process result from tool-use agent loop.

        Routes to explore or verify handler based on which exit tool was called.
        Writes both COMPUTATION_LOG.md (human-readable) and COMPUTATION_INDEX.jsonl
        (machine-readable).
        """
        # Check which exit tool was called
        verdict_tc = next(
            (tc for tc in response.tool_calls if tc.tool_name == "submit_verdict"),
            None,
        )
        result_tc = next(
            (tc for tc in response.tool_calls if tc.tool_name == "submit_result"),
            None,
        )

        if result_tc and isinstance(result_tc.tool_input, dict):
            md_text, jsonl_entry = self._format_explore_entry(result_tc.tool_input, task, iteration, response)
            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION,
                "submit_result_used",
                f"target={result_tc.tool_input.get('target_id', '?')}",
            )
        elif verdict_tc and isinstance(verdict_tc.tool_input, dict):
            md_text, jsonl_entry = self._format_verify_entry(verdict_tc.tool_input, task, iteration, response)
            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION,
                "submit_verdict_used",
                f"verdict={verdict_tc.tool_input.get('verdict', '?')}",
            )
        else:
            md_text, jsonl_entry = self._format_inconclusive_stub(response, task, iteration)

        # Write Markdown (human-readable, never parsed after JSONL exists)
        self.workspace.append_file("COMPUTATION_LOG.md", "\n" + md_text)
        # Write JSONL (machine-readable, always parsed)
        self.workspace.append_file(
            "COMPUTATION_INDEX.jsonl", json.dumps(jsonl_entry, ensure_ascii=False) + "\n"
        )
        self._update_computation_metadata()

    def _format_explore_entry(
        self, params: dict, task: Task, iteration: int, response: AgentResult
    ) -> tuple[str, dict]:
        """Format an explore entry from submit_result tool parameters."""
        task_id = task.task_id or f"COMP-{iteration:03d}"
        target_id = params.get("target_id", "")
        description = params.get("description", "unknown")
        method = params.get("method", "unknown")
        result = params.get("result", "No results")
        confidence = params.get("confidence", "partial")
        notes = params.get("notes", "")

        md_text = (
            f"## {task_id}: Exploration\n\n"
            f"**TARGET:** {target_id}\n"
            f"**DESCRIPTION:** {description}\n"
            f"**METHOD:** {method}\n"
            f"**RESULT:** {result}\n\n"
            f"**CONFIDENCE:** {confidence}\n"
            f"**NOTES:** {notes}"
            f"\n\n- **Iteration:** {iteration}\n"
            f"- **Tool calls:** {len(response.tool_calls)}\n"
            f"- **Rounds:** {response.rounds}\n"
        )

        jsonl_entry = {
            "id": task_id,
            "kind": "explore",
            "iteration": iteration,
            "target_id": target_id,
            "description": description,
            "method": method,
            "result": result,
            "confidence": confidence,
            "notes": notes,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        return md_text, jsonl_entry

    def _format_verify_entry(
        self, params: dict, task: Task, iteration: int, response: AgentResult
    ) -> tuple[str, dict]:
        """Format a verify entry from submit_verdict tool parameters."""
        task_id = task.task_id or f"COMP-{iteration:03d}"
        target_id = params.get("target_id", "")
        # Backward compat: if target_id not provided, extract from claim or task
        if not target_id:
            ids = _ER_WH_ID_RE.findall(params.get("claim", "") + " " + (task.body or ""))
            target_id = ids[0] if ids else ""
        claim = params.get("claim", "unknown")
        method = params.get("method", "unknown")
        result = params.get("result", "No results")
        verdict = params.get("verdict", "INCONCLUSIVE")
        notes = params.get("notes", "No notes")

        md_text = (
            f"## {task_id}: Computation\n\n"
            f"**CLAIM:** {target_id} — {claim}\n"
            f"**METHOD:** {method}\n"
            f"**RESULT:** {result}\n\n"
            f"**VERDICT:** {verdict}\n"
            f"**NOTES:** {notes}"
            f"\n\n- **Iteration:** {iteration}\n"
            f"- **Tool calls:** {len(response.tool_calls)}\n"
            f"- **Rounds:** {response.rounds}\n"
        )

        jsonl_entry = {
            "id": task_id,
            "kind": "verify",
            "iteration": iteration,
            "target_id": target_id,
            "claim": claim,
            "method": method,
            "result": result,
            "verdict": verdict,
            "notes": notes,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        return md_text, jsonl_entry

    def _format_inconclusive_stub(
        self, response: AgentResult, task: Task, iteration: int
    ) -> tuple[str, dict]:
        """Format an INCONCLUSIVE stub when no exit tool was called."""
        text = response.text.strip()
        task_id = task.task_id or f"TASK-{iteration:03d}"

        # Extract target_id from task
        target_ids = _ER_WH_ID_RE.findall(task.body or "")
        target_id = target_ids[0] if target_ids else ""

        if not text:
            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION, "empty_response_stub", ""
            )
            claim_line = ", ".join(dict.fromkeys(target_ids)) if target_ids else "unknown"
            text = (
                f"**CLAIM:** {claim_line}\n"
                "**VERDICT:** INCONCLUSIVE\n"
                "**NOTES:** Agent produced no text output."
            )

        # Ensure the CLAIM line references the target WH/ER ID
        if target_id:
            claim_match = re.search(r'\*\*CLAIM:\*\*\s*', text)
            if claim_match and target_id not in text[claim_match.start():claim_match.end() + 200]:
                text = text[:claim_match.end()] + f"{target_id} — " + text[claim_match.end():]
                log_scaffold_event(
                    self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION,
                    "claim_id_injected", f"target={target_id}",
                )

        # Ensure ## header
        if not text.startswith("##"):
            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION,
                "header_injected", f"task_id={task_id}",
            )
            text = f"## {task_id}: Computation\n\n" + text

        # Add metadata
        text += f"\n\n- **Iteration:** {iteration}\n"
        text += f"- **Tool calls:** {len(response.tool_calls)}\n"
        text += f"- **Rounds:** {response.rounds}\n"

        # Determine kind based on task type
        kind = "explore" if task.task_type == TaskType.COMPUTE_EXPLORE else "verify"

        jsonl_entry = {
            "id": task_id,
            "kind": kind,
            "iteration": iteration,
            "target_id": target_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if kind == "verify":
            jsonl_entry["claim"] = ", ".join(dict.fromkeys(target_ids)) if target_ids else "unknown"
            jsonl_entry["verdict"] = "INCONCLUSIVE"
            jsonl_entry["notes"] = "Agent produced no exit tool call."
        else:
            jsonl_entry["description"] = "unknown"
            jsonl_entry["confidence"] = "partial"
            jsonl_entry["notes"] = "Agent produced no exit tool call."

        return text, jsonl_entry

    def _update_computation_metadata(self):
        """Update COMPUTATION_LOG.md frontmatter with counts.

        Note: check_id_consistency() in validation.py also fixes this counter.
        Both are kept — this is a best-effort fix at write time; validation is
        the authoritative post-integration check.
        """
        content = self.workspace.read_file("COMPUTATION_LOG.md")
        meta, body = parse_frontmatter(content)
        comp_count = len(re.findall(r'^## COMP-\d+', body, re.MULTILINE))
        meta["total_computations"] = comp_count
        meta["last_computation"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.workspace.write_file("COMPUTATION_LOG.md", render_frontmatter(meta, body))
