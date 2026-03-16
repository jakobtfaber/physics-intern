"""Computationalist agent: symbolic/numerical verification via code execution."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..llm import AgentResult
from ..markdown import parse_frontmatter, render_frontmatter
from ..renderers import render_computation_log_md
from ..research_state import Computation, Verdict
from ..tools import ToolExecutor
from .base import BaseAgent
from ..categories import CompensationCategory as CC
from ..workspace import log_scaffold_event
from ..task import TaskType

if TYPE_CHECKING:
    from ..research_state import ResearchState
    from ..task import Task

_ER_WH_ID_RE = re.compile(r"(?:ER|WH)-\d+")


class ComputationalistAgent(BaseAgent):
    name = "computationalist"
    prompt_file = "computationalist.md"
    tools = ToolExecutor.TOOL_DEFINITIONS  # default; overridden in build_context

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self.research_state: ResearchState | None = None

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
        Creates Computation objects in research_state, renders COMPUTATION_LOG.md,
        and writes COMPUTATION_INDEX.jsonl (dual-write during transition).
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
            comp, jsonl_entry = self._build_explore_computation(result_tc.tool_input, task, iteration, response)
            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION,
                "submit_result_used",
                f"target={result_tc.tool_input.get('target_id', '?')}",
            )
        elif verdict_tc and isinstance(verdict_tc.tool_input, dict):
            comp, jsonl_entry = self._build_verify_computation(verdict_tc.tool_input, task, iteration, response)
            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION,
                "submit_verdict_used",
                f"verdict={verdict_tc.tool_input.get('verdict', '?')}",
            )
        else:
            comp, jsonl_entry = self._build_inconclusive_computation(response, task, iteration)

        # Write to research state
        if self.research_state:
            self.research_state.computations[comp.id] = comp
            # Render full COMPUTATION_LOG.md from state
            self.workspace.write_file("COMPUTATION_LOG.md", render_computation_log_md(self.research_state))
        else:
            # Fallback: append markdown directly
            md_text = self._render_comp_md(comp)
            self.workspace.append_file("COMPUTATION_LOG.md", "\n" + md_text)
            self._update_computation_metadata()

        pass  # JSONL dual-write removed — state is authoritative

    def _build_explore_computation(
        self, params: dict, task: Task, iteration: int, response: AgentResult
    ) -> tuple[Computation, dict]:
        """Build a Computation from submit_result tool parameters."""
        task_id = task.task_id or f"COMP-{iteration:03d}"
        target_id = params.get("target_id", "")
        description = params.get("description", "unknown")
        method = params.get("method", "unknown")
        result = params.get("result", "No results")
        confidence = params.get("confidence", "partial")
        notes = params.get("notes", "")

        comp = Computation(
            id=task_id,
            target_hypothesis=target_id,
            verdict=Verdict.INCONCLUSIVE,
            claim=description,
            method=method,
            result=result,
            kind="explore",
            confidence=confidence,
            notes=notes,
            iteration=iteration,
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
        return comp, jsonl_entry

    def _build_verify_computation(
        self, params: dict, task: Task, iteration: int, response: AgentResult
    ) -> tuple[Computation, dict]:
        """Build a Computation from submit_verdict tool parameters."""
        task_id = task.task_id or f"COMP-{iteration:03d}"
        target_id = params.get("target_id", "")
        if not target_id:
            ids = _ER_WH_ID_RE.findall(params.get("claim", "") + " " + (task.body or ""))
            target_id = ids[0] if ids else ""
        claim = params.get("claim", "unknown")
        method = params.get("method", "unknown")
        result = params.get("result", "No results")
        verdict_str = params.get("verdict", "INCONCLUSIVE")
        notes = params.get("notes", "No notes")

        try:
            verdict = Verdict(verdict_str)
        except ValueError:
            verdict = Verdict.INCONCLUSIVE

        comp = Computation(
            id=task_id,
            target_hypothesis=target_id,
            verdict=verdict,
            claim=claim,
            method=method,
            result=result,
            kind="verify",
            notes=notes,
            failure_detail=notes if verdict != Verdict.VERIFIED else "",
            iteration=iteration,
        )

        jsonl_entry = {
            "id": task_id,
            "kind": "verify",
            "iteration": iteration,
            "target_id": target_id,
            "claim": claim,
            "method": method,
            "result": result,
            "verdict": verdict_str,
            "notes": notes,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        return comp, jsonl_entry

    def _build_inconclusive_computation(
        self, response: AgentResult, task: Task, iteration: int
    ) -> tuple[Computation, dict]:
        """Build an INCONCLUSIVE Computation when no exit tool was called."""
        task_id = task.task_id or f"TASK-{iteration:03d}"
        target_ids = _ER_WH_ID_RE.findall(task.body or "")
        target_id = target_ids[0] if target_ids else ""
        kind = "explore" if task.task_type == TaskType.COMPUTE_EXPLORE else "verify"
        zero_output = not response.text.strip()

        if zero_output:
            log_scaffold_event(
                self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION, "empty_response_stub", ""
            )

        comp = Computation(
            id=task_id,
            target_hypothesis=target_id,
            verdict=Verdict.INCONCLUSIVE,
            claim=", ".join(dict.fromkeys(target_ids)) if target_ids else "unknown",
            kind=kind,
            notes="Agent produced no exit tool call.",
            zero_output=zero_output,
            iteration=iteration,
        )

        jsonl_entry = {
            "id": task_id,
            "kind": kind,
            "iteration": iteration,
            "target_id": target_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if kind == "verify":
            jsonl_entry["claim"] = comp.claim
            jsonl_entry["verdict"] = "INCONCLUSIVE"
            jsonl_entry["notes"] = "Agent produced no exit tool call."
        else:
            jsonl_entry["description"] = "unknown"
            jsonl_entry["confidence"] = "partial"
            jsonl_entry["notes"] = "Agent produced no exit tool call."

        return comp, jsonl_entry

    @staticmethod
    def _render_comp_md(comp: Computation) -> str:
        """Render a single computation entry as markdown (fallback path)."""
        if comp.kind == "explore":
            return (
                f"## {comp.id}: Exploration\n\n"
                f"**TARGET:** {comp.target_hypothesis}\n"
                f"**DESCRIPTION:** {comp.claim}\n"
                f"**METHOD:** {comp.method}\n"
                f"**RESULT:** {comp.result}\n\n"
                f"**CONFIDENCE:** {comp.confidence}\n"
                f"**NOTES:** {comp.notes}"
                f"\n\n- **Iteration:** {comp.iteration}\n"
            )
        return (
            f"## {comp.id}: Computation\n\n"
            f"**CLAIM:** {comp.target_hypothesis} — {comp.claim}\n"
            f"**METHOD:** {comp.method}\n"
            f"**RESULT:** {comp.result}\n\n"
            f"**VERDICT:** {comp.verdict}\n"
            f"**NOTES:** {comp.notes}"
            f"\n\n- **Iteration:** {comp.iteration}\n"
        )

    def _update_computation_metadata(self):
        """Update COMPUTATION_LOG.md frontmatter with counts (fallback path)."""
        content = self.workspace.read_file("COMPUTATION_LOG.md")
        meta, body = parse_frontmatter(content)
        comp_count = len(re.findall(r'^## COMP-\d+', body, re.MULTILINE))
        meta["total_computations"] = comp_count
        meta["last_computation"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.workspace.write_file("COMPUTATION_LOG.md", render_frontmatter(meta, body))
