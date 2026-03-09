"""Orchestrator agent: plans and coordinates research."""

import re

from ..llm import LLMResponse
from ..markdown import (
    parse_frontmatter,
    render_frontmatter,
    count_unresolved_critiques,
    resolve_critique,
)
from .base import BaseAgent

DELIM_RESEARCH = "=== RESEARCH_STATE.md ==="
DELIM_TASK = "=== CURRENT_TASK.md ==="


class OrchestratorAgent(BaseAgent):
    name = "orchestrator"
    prompt_file = "orchestrator.md"

    def _completion_analysis(self) -> str | None:
        """Check if research appears complete; return banner if so."""
        state = self.workspace.read_file("RESEARCH_STATE.md")
        critique = self.workspace.read_file("CRITIQUE_LOG.md")
        crit_meta, _ = parse_frontmatter(critique)

        er_count = len(re.findall(r'^## ER-\d+', state, re.MULTILINE))
        wh_count = len(re.findall(r'^## WH-\d+', state, re.MULTILINE))
        high = crit_meta.get("unresolved_high", 0) or 0
        medium = crit_meta.get("unresolved_medium", 0) or 0

        if er_count >= 3 and wh_count == 0 and high == 0 and medium == 0:
            return (
                ">>> COMPLETION CHECK: "
                f"{er_count} Established Results, "
                f"{wh_count} Working Hypotheses remaining, "
                f"{high} HIGH / {medium} MEDIUM unresolved critiques. "
                "ALL PROBLEM STEPS APPEAR TO BE ESTABLISHED. "
                "You SHOULD emit task_type: synthesize or task_type: terminate "
                "unless you can identify a specific gap. <<<"
            )
        return None

    def build_context(self, task: dict, iteration: int) -> str:
        banner = self._completion_analysis()
        parts = []
        if banner:
            parts.append(f"{banner}\n")
        state = self.workspace.read_file("RESEARCH_STATE.md")
        if iteration >= 3 and "To be populated by the orchestrator" in state:
            parts.append(
                ">>> REMINDER: The '# Conventions' section in RESEARCH_STATE.md "
                "is still empty. Consider populating it with the unit system, "
                "sign conventions, and variable definitions being used. <<<\n"
            )
        parts.extend([
            f"# Current Iteration: {iteration}\n",
            "## RESEARCH_STATE.md\n",
            state,
            "\n## CRITIQUE_LOG.md\n",
            self.workspace.read_file("CRITIQUE_LOG.md"),
            "\n## COMPUTATION_LOG.md (last 5 entries)\n",
            self.workspace.read_file_tail("COMPUTATION_LOG.md", n_entries=5),
            "\n## METRICS.md (summary)\n",
            self.workspace.read_file("METRICS.md"),
        ])
        if self.workspace.file_exists("PROPOSED_CHANGES.md"):
            parts.append("\n## PROPOSED_CHANGES.md (pending review)\n")
            parts.append(self.workspace.read_file("PROPOSED_CHANGES.md"))
        return "\n".join(parts)

    def process_response(self, response: LLMResponse, task: dict, iteration: int):
        """Write CURRENT_TASK.md (and optionally RESEARCH_STATE.md) from orchestrator output."""
        research_state, task_text = _split_response(response.text)
        if research_state is not None:
            self.workspace.write_file("RESEARCH_STATE.md", research_state)
            self.workspace.delete_file("PROPOSED_CHANGES.md")
            # Resolve critiques mentioned in the orchestrator's output
            self._resolve_critiques(response.text)
        self.workspace.write_file("CURRENT_TASK.md", task_text)

    def _resolve_critiques(self, response_text: str):
        """Scan orchestrator output for resolved critique IDs and update CRITIQUE_LOG.md."""
        # Look for resolved_critiques list in YAML or inline references like "CRIT-001 resolved"
        resolved_ids = set()

        # Pattern 1: resolved_critiques: ["CRIT-001", "CRIT-002"] or [CRIT-001, CRIT-002]
        list_match = re.search(
            r'resolved_critiques:\s*\[([^\]]+)\]', response_text
        )
        if list_match:
            for crit in re.findall(r'CRIT(?:IQUE)?-\d+', list_match.group(1)):
                resolved_ids.add(crit)

        # Pattern 2: "CRIT-NNN" near "resolved"/"addressed"/"incorporated" in prose
        for match in re.finditer(
            r'(CRIT(?:IQUE)?-\d+)\b[^.\n]{0,80}\b(?:resolved|addressed|incorporated|verified)',
            response_text, re.IGNORECASE,
        ):
            resolved_ids.add(match.group(1))
        # Also match reverse: "resolved ... CRIT-NNN"
        for match in re.finditer(
            r'(?:resolved|addressed|incorporated|verified)\b[^.\n]{0,80}\b(CRIT(?:IQUE)?-\d+)',
            response_text, re.IGNORECASE,
        ):
            resolved_ids.add(match.group(1))

        if not resolved_ids:
            return

        # Get current iteration from RESEARCH_STATE frontmatter
        state_content = self.workspace.read_file("RESEARCH_STATE.md")
        state_meta, _ = parse_frontmatter(state_content)
        iteration = state_meta.get("iteration", "?")

        content = self.workspace.read_file("CRITIQUE_LOG.md")
        for crit_id in sorted(resolved_ids):
            content = resolve_critique(
                content, crit_id,
                f"Addressed by orchestrator integration at iteration {iteration}.",
            )

        # Update frontmatter counts
        meta, body = parse_frontmatter(content)
        counts = count_unresolved_critiques(content)
        meta["unresolved_high"] = counts["HIGH"]
        meta["unresolved_medium"] = counts["MEDIUM"]
        meta["unresolved_low"] = counts["LOW"]
        self.workspace.write_file("CRITIQUE_LOG.md", render_frontmatter(meta, body))

    def parse_task(self, text: str, iteration: int = 0) -> dict:
        """Parse CURRENT_TASK.md content into a task dict."""
        _, task_text = _split_response(text)
        meta, body = parse_frontmatter(task_text)
        effective_iter = meta.get("iteration", iteration) or iteration
        return {
            "task_id": meta.get("task_id", f"TASK-{effective_iter:03d}"),
            "task_type": meta.get("task_type", "research"),
            "assigned_to": meta.get("assigned_to", "researcher"),
            "priority": meta.get("priority", "medium"),
            "iteration": effective_iter,
            "blocking_critiques": meta.get("blocking_critiques", []),
            "target_file": meta.get("target_file", ""),
            "body": body,
        }


def _split_response(text: str) -> tuple[str | None, str]:
    """Split orchestrator response into (research_state, task_text).

    Returns (None, text) if no section delimiters are found (backward compat).
    """
    if DELIM_TASK not in text:
        return None, text.strip()

    if DELIM_RESEARCH in text:
        after_research = text.split(DELIM_RESEARCH, 1)[1]
        research_state, rest = after_research.split(DELIM_TASK, 1)
        return research_state.strip(), rest.strip()

    task_text = text.split(DELIM_TASK, 1)[1]
    return None, task_text.strip()
