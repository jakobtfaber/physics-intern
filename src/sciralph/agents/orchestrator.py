"""Orchestrator agent: plans and coordinates research."""

from __future__ import annotations

import re

from ..llm import LLMResponse
from ..markdown import (
    parse_frontmatter,
    render_frontmatter,
    count_unresolved_critiques,
    resolve_critique,
    extract_resolved_critique_ids,
    recount_critique_metadata,
    detect_computation_stalls,
    count_er_sections,
    count_wh_sections,
    normalize_er_wh_headers,
    flatten_unverified_brackets,
    CRIT_ID_RE,
)
from ..task import Task, TaskType
from .base import BaseAgent

DELIM_RESEARCH = "=== RESEARCH_STATE.md ==="
DELIM_TASK = "=== CURRENT_TASK.md ==="


class OrchestratorAgent(BaseAgent):
    name = "orchestrator"
    prompt_file = "orchestrator.md"

    def __init__(self, config, workspace, metrics):
        super().__init__(config, workspace, metrics)
        self.context_prefix: str = ""

    def _completion_analysis(self, iteration: int = 0) -> str | None:
        """Check if research appears complete; return banner if so.

        When budget is low (≤3 iterations remaining), returns a synthesis
        banner even if Working Hypotheses or critiques remain unresolved.
        """
        state = self.workspace.read_file("RESEARCH_STATE.md")
        critique = self.workspace.read_file("CRITIQUE_LOG.md")
        crit_meta, _ = parse_frontmatter(critique)

        er_count = count_er_sections(state)
        wh_count = count_wh_sections(state)
        high = crit_meta.get("unresolved_high", 0) or 0
        medium = crit_meta.get("unresolved_medium", 0) or 0

        if er_count >= self.config.min_er_for_completion and wh_count == 0 and high == 0 and medium == 0:
            return (
                ">>> COMPLETION CHECK: "
                f"{er_count} Established Results, "
                f"{wh_count} Working Hypotheses remaining, "
                f"{high} HIGH / {medium} MEDIUM unresolved critiques. "
                "ALL PROBLEM STEPS APPEAR TO BE ESTABLISHED. "
                "Write a brief '## Synthesis' section at the end of "
                "RESEARCH_STATE.md summarizing the key results and their "
                "connections, then emit task_type: terminate. <<<"
            )

        # Budget-aware: force synthesis when ≤3 iterations remain
        budget_remaining = self.config.max_iterations - iteration
        if budget_remaining <= 3 and er_count >= 1:
            return (
                f">>> BUDGET SYNTHESIS REQUIRED: Only {budget_remaining} "
                f"iteration(s) remaining (iteration {iteration} of "
                f"{self.config.max_iterations}). "
                f"{er_count} Established Results, "
                f"{wh_count} Working Hypotheses still pending, "
                f"{high} HIGH / {medium} MEDIUM unresolved critiques. "
                "You MUST synthesize ALL current Established Results into a "
                "final answer NOW using task_type: synthesize. "
                "Unresolved Working Hypotheses and critiques should be noted "
                "as limitations, not pursued further. Set the research status "
                "to 'partially_complete' if any items remain unresolved. <<<"
            )
        return None

    def build_context(self, task: Task, iteration: int) -> str:
        parts = []
        if self.context_prefix:
            parts.append(self.context_prefix)
            self.context_prefix = ""  # consume after use
        banner = self._completion_analysis(iteration)
        if banner:
            parts.append(f"{banner}\n")
        state = self.workspace.read_file("RESEARCH_STATE.md")
        # Clean phantom markers to prevent LLM from copying bracketed form
        state = flatten_unverified_brackets(state)
        state = re.sub(r'\[((COMP|TASK)-\d+):unverified\]', r'\1 (unverified)', state)
        if iteration >= 3 and "To be populated by the orchestrator" in state:
            parts.append(
                ">>> REMINDER: The '# Conventions' section in RESEARCH_STATE.md "
                "is still empty. Consider populating it with the unit system, "
                "sign conventions, and variable definitions being used. <<<\n"
            )
        # Computation stall detection
        comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
        stalls = detect_computation_stalls(comp_log, threshold=self.config.stall_threshold)
        for stall in stalls:
            parts.append(
                f">>> COMPUTATION STALL: {stall['count']} consecutive failures "
                f"on claim: {stall['claim'][:100]}. "
                f"Verdicts: {', '.join(stall['verdicts'])}. "
                f"Do NOT retry the same approach. Consider: (a) alternative derivation, "
                f"(b) skip and advance, or (c) critic review of the claim. <<<\n"
            )
        budget_remaining = self.config.max_iterations - iteration
        parts.extend([
            f"# Current Iteration: {iteration} of {self.config.max_iterations} "
            f"({budget_remaining} remaining)\n",
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

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Write CURRENT_TASK.md (and optionally RESEARCH_STATE.md) from orchestrator output."""
        research_state, task_text = _split_response(response.text)
        if research_state is not None:
            research_state = self._enforce_problem_statement(research_state)
            research_state = normalize_er_wh_headers(research_state)
            self.workspace.write_file("RESEARCH_STATE.md", research_state)
            self.workspace.delete_file("PROPOSED_CHANGES.md")
            # Resolve critiques mentioned in the orchestrator's output
            self._resolve_critiques(response.text, iteration)
        self.workspace.write_file("CURRENT_TASK.md", task_text)

    def _enforce_problem_statement(self, research_state: str) -> str:
        """Replace the Problem Statement section with the original from the problem YAML."""
        original = getattr(self.workspace, "problem_statement", None)
        if not original:
            return research_state
        # Replace everything between "# Problem Statement" and the next "# " heading
        return re.sub(
            r"(# Problem Statement\s*\n).*?(?=\n# )",
            rf"\g<1>\n{original}\n",
            research_state,
            count=1,
            flags=re.DOTALL,
        )

    @staticmethod
    def _validate_resolution_note(note: str, crit_id: str, iteration: int) -> str:
        """Validate and clean resolution note quality."""
        _SYSTEM_MARKERS = ("[error]", "phantom", ":unverified]", ">>> ", "<<<")
        if len(note) < 20:
            return f"Resolved via computation/analysis at iteration {iteration}."
        if any(marker in note.lower() for marker in _SYSTEM_MARKERS):
            return f"Resolved via computation/analysis at iteration {iteration}."
        return note

    def _resolve_critiques(self, response_text: str, iteration: int):
        """Scan orchestrator output for resolved critique IDs and update CRITIQUE_LOG.md."""
        resolved_ids = extract_resolved_critique_ids(response_text)

        # Defense-in-depth: also extract from RESEARCH_STATE.md frontmatter
        state_text = self.workspace.read_file("RESEARCH_STATE.md")
        if state_text:
            state_meta, _ = parse_frontmatter(state_text)
            rc = state_meta.get("resolved_critiques")
            if isinstance(rc, dict):
                for key in rc:
                    for m in CRIT_ID_RE.finditer(str(key)):
                        resolved_ids.add(m.group())
            elif isinstance(rc, list):
                for item in rc:
                    for m in CRIT_ID_RE.finditer(str(item)):
                        resolved_ids.add(m.group())

        if not resolved_ids:
            return

        # Try to extract per-critique resolution notes from prose
        resolution_notes = {}
        for crit_id in resolved_ids:
            note_match = re.search(
                rf'{re.escape(crit_id)}[\s:—\-]+(.+?)(?=\n\n|\nCRIT(?:IQUE)?-\d|$)',
                response_text,
                re.DOTALL,
            )
            if note_match:
                note = note_match.group(1).strip()
                # Collapse whitespace, cap at 300 chars at sentence boundary
                note = " ".join(note.split())
                if len(note) > 300:
                    cut = note[:300].rfind('.')
                    note = note[:cut + 1] if cut > 50 else note[:300] + "..."
                resolution_notes[crit_id] = note

        content = self.workspace.read_file("CRITIQUE_LOG.md")
        for crit_id in sorted(resolved_ids):
            note = resolution_notes.get(
                crit_id,
                f"Addressed by orchestrator integration at iteration {iteration}.",
            )
            note = self._validate_resolution_note(note, crit_id, iteration)
            content = resolve_critique(content, crit_id, note)

        # Update frontmatter counts
        meta, body = parse_frontmatter(content)
        recounted = recount_critique_metadata(content)
        meta["unresolved_high"] = recounted["unresolved_high"]
        meta["unresolved_medium"] = recounted["unresolved_medium"]
        meta["unresolved_low"] = recounted["unresolved_low"]
        self.workspace.write_file("CRITIQUE_LOG.md", render_frontmatter(meta, body))

    def parse_task(self, text: str, iteration: int = 0) -> Task:
        """Parse CURRENT_TASK.md content into a Task."""
        _, task_text = _split_response(text)
        return Task.from_frontmatter(task_text, fallback_iteration=iteration)


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
