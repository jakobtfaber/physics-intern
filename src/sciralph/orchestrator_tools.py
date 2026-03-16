"""Orchestrator state-mutation tools.

The orchestrator uses these tools to surgically edit RESEARCH_STATE.md
and CRITIQUE_LOG.md instead of rewriting entire files.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar

from .tools import ToolCall
from .categories import CompensationCategory as CC
from .workspace import log_scaffold_event

if TYPE_CHECKING:
    from .workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# Section manipulation helpers
# ---------------------------------------------------------------------------

_ER_WH_HEADER_RE = re.compile(r"^## ((?:ER|WH)-\d+)\s", re.MULTILINE)


def _find_section_range(text: str, section_id: str) -> tuple[int, int] | None:
    """Return (start, end) char range for a ## WH-NNN or ## ER-NNN section."""
    for m in _ER_WH_HEADER_RE.finditer(text):
        if m.group(1) == section_id:
            start = m.start()
            rest = text[m.end():]
            end_match = re.search(r"^(?:##? )", rest, re.MULTILINE)
            end = m.end() + end_match.start() if end_match else len(text)
            return (start, end)
    return None


def _find_h1_content_range(text: str, heading: str) -> tuple[int, int] | None:
    """Return (start, end) of content under a ``# Heading`` (excluding the heading line)."""
    pattern = re.compile(rf"^# {re.escape(heading)}\s*$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    content_start = m.end()
    rest = text[content_start:]
    end_match = re.search(r"^# ", rest, re.MULTILINE)
    content_end = content_start + end_match.start() if end_match else len(text)
    return (content_start, content_end)


def _next_hypothesis_num(text: str) -> int:
    """Determine the next available hypothesis number."""
    nums = [int(m.group(1).split("-")[1]) for m in _ER_WH_HEADER_RE.finditer(text)]
    return max(nums, default=0) + 1


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI canonical format)
# ---------------------------------------------------------------------------

ORCHESTRATOR_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "add_hypothesis",
            "description": (
                "Add a new Working Hypothesis to RESEARCH_STATE.md. "
                "Returns the auto-assigned ID (e.g., WH-003)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "statement": {
                        "type": "string",
                        "description": "One-line title for the hypothesis.",
                    },
                    "derivation": {
                        "type": "string",
                        "description": "Full reasoning or derivation (Markdown).",
                    },
                },
                "required": ["statement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_hypothesis",
            "description": (
                "Update an existing hypothesis (WH-NNN or ER-NNN). "
                "Only provided fields are changed; omitted fields are preserved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Hypothesis ID, e.g. WH-001 or ER-002.",
                    },
                    "statement": {
                        "type": "string",
                        "description": "New title (optional).",
                    },
                    "derivation": {
                        "type": "string",
                        "description": "New body text — replaces the entire section body (optional).",
                    },
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abandon_hypothesis",
            "description": (
                "Mark a hypothesis as a dead end. "
                "Removes it from the WH/ER section and records the reason under Dead Ends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Hypothesis ID to abandon.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this approach was abandoned.",
                    },
                },
                "required": ["id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "promote_hypothesis",
            "description": (
                "Promote a Working Hypothesis to an Established Result. "
                "Call when you judge that the accumulated evidence "
                "(computations, derivations, or both) sufficiently supports the claim."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "WH-NNN to promote to ER-NNN.",
                    },
                    "justification": {
                        "type": "string",
                        "description": (
                            "Why the evidence is sufficient. "
                            "Reference specific COMP entries, derivations, or other evidence."
                        ),
                    },
                },
                "required": ["id", "justification"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_critique",
            "description": (
                "Mark a critique as resolved in CRITIQUE_LOG.md. "
                "The resolution must describe the specific fix — not a generic note."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "critique_id": {
                        "type": "string",
                        "description": "Critique ID, e.g. CRIT-001.",
                    },
                    "resolution": {
                        "type": "string",
                        "description": "Specific description of how the critique was addressed.",
                    },
                },
                "required": ["critique_id", "resolution"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_section",
            "description": (
                "Replace the content of a top-level section in RESEARCH_STATE.md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["Conventions", "Open Questions", "Dead Ends"],
                        "description": "Which section to update.",
                    },
                    "content": {
                        "type": "string",
                        "description": "New section content (replaces existing).",
                    },
                },
                "required": ["section", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_next_task",
            "description": (
                "Set the next task for the research loop. "
                "Call this ONCE as your final action. "
                "This terminates the round — include all mutations in the "
                "SAME batch before calling this tool, as no further rounds "
                "will occur."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "enum": [
                            "research", "derive", "compute",
                            "compute_explore", "compute_verify",
                            "critique", "resolve", "synthesize", "terminate",
                        ],
                    },
                    "assigned_to": {
                        "type": "string",
                        "enum": [
                            "researcher", "computationalist",
                            "deep_critic", "formatter",
                        ],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "target_claim": {
                        "type": "string",
                        "description": "For compute tasks: the WH/ER ID being verified.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed task description (Markdown).",
                    },
                },
                "required": ["task_type", "description"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

class OrchestratorToolExecutor:
    """Dispatches state-mutation tool calls for the orchestrator."""

    TOOL_DEFINITIONS: ClassVar[list[dict]] = ORCHESTRATOR_TOOL_DEFINITIONS

    def __init__(self, workspace: WorkspaceManager, iteration: int, research_state=None):
        self.workspace = workspace
        self.iteration = iteration
        self.research_state = research_state
        self.mutations_applied: bool = False
        self.task_data: dict | None = None
        self.resolved_critique_ids: set[str] = set()
        self.stop_after_round: bool = False

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        start = time.time()
        handlers = {
            "add_hypothesis": self._add_hypothesis,
            "update_hypothesis": self._update_hypothesis,
            "abandon_hypothesis": self._abandon_hypothesis,
            "promote_hypothesis": self._promote_hypothesis,
            "resolve_critique": self._resolve_critique,
            "update_section": self._update_section,
            "set_next_task": self._set_next_task,
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

    # -- Mutation handlers --

    def _add_hypothesis(self, args: dict) -> str:
        state_text = self.workspace.read_file("RESEARCH_STATE.md")
        num = _next_hypothesis_num(state_text)
        new_id = f"WH-{num:03d}"
        statement = args.get("statement", "Untitled")
        derivation = args.get("derivation", "")
        section = f"\n\n## {new_id} — {statement}\n\n{derivation}\n"

        # Insert before "# Dead Ends" if it exists
        dead_match = re.search(r"^# Dead Ends", state_text, re.MULTILINE)
        if dead_match:
            pos = dead_match.start()
            state_text = state_text[:pos].rstrip() + section + "\n\n" + state_text[pos:]
        else:
            state_text = state_text.rstrip() + section
        self.workspace.write_file("RESEARCH_STATE.md", state_text)
        self.mutations_applied = True
        return f"Added {new_id} — {statement}"

    def _update_hypothesis(self, args: dict) -> str:
        hid = args["id"]
        state_text = self.workspace.read_file("RESEARCH_STATE.md")
        range_ = _find_section_range(state_text, hid)
        if range_ is None:
            return f"Error: {hid} not found in RESEARCH_STATE.md"
        start, end = range_

        # Parse existing header and body
        old_section = state_text[start:end]
        first_nl = old_section.find("\n")
        if first_nl == -1:
            old_header_line = old_section
            old_body = ""
        else:
            old_header_line = old_section[:first_nl]
            old_body = old_section[first_nl + 1:].strip()

        # Extract old title from header: ## WH-001 — Title
        title_match = re.search(r"(?:ER|WH)-\d+\s*[-—:]\s*(.*)", old_header_line)
        old_title = title_match.group(1).strip() if title_match else ""

        new_title = args.get("statement", old_title)
        new_body = args.get("derivation", old_body)
        new_section = f"## {hid} — {new_title}\n\n{new_body}\n"

        state_text = state_text[:start] + new_section + state_text[end:]
        self.workspace.write_file("RESEARCH_STATE.md", state_text)
        self.mutations_applied = True
        return f"Updated {hid}"

    def _abandon_hypothesis(self, args: dict) -> str:
        hid = args["id"]
        reason = args.get("reason", "No longer viable")
        state_text = self.workspace.read_file("RESEARCH_STATE.md")
        range_ = _find_section_range(state_text, hid)
        if range_ is None:
            return f"Error: {hid} not found"
        start, end = range_

        # Extract title
        header_line = state_text[start:state_text.index("\n", start)]
        title_match = re.search(r"(?:ER|WH)-\d+\s*[-—:]\s*(.*)", header_line)
        title = title_match.group(1).strip() if title_match else hid

        # Remove section
        state_text = state_text[:start] + state_text[end:]

        # Append to Dead Ends
        dead_entry = f"\n**{hid} — {title}:** {reason}\n"
        dead_range = _find_h1_content_range(state_text, "Dead Ends")
        if dead_range:
            cs, ce = dead_range
            state_text = state_text[:ce].rstrip() + dead_entry + "\n" + state_text[ce:]
        else:
            state_text = state_text.rstrip() + "\n\n# Dead Ends" + dead_entry

        self.workspace.write_file("RESEARCH_STATE.md", state_text)
        self.mutations_applied = True

        # Record in formal state if available
        if self.research_state:
            from .research_state import FailedApproach, HypothesisStatus
            self.research_state.failed_approaches.append(FailedApproach(
                description=f"Abandoned {hid} — {title}",
                reason=reason,
                iteration=self.iteration,
            ))
            # Mark hypothesis as abandoned (preserves it in the graph)
            if hid in self.research_state.hypotheses:
                self.research_state.hypotheses[hid].status = HypothesisStatus.ABANDONED
                self.research_state.hypotheses[hid].iteration_modified = self.iteration

        return f"Abandoned {hid}: {reason}"

    def _promote_hypothesis(self, args: dict) -> str:
        from .markdown import (
            parse_frontmatter,
            render_frontmatter,
            _parse_comp_entries,
            count_unresolved_critiques,
            _ER_WH_ID_RE,
        )

        wh_id = args["id"]
        justification = args.get("justification", "")

        # Validate ID format
        if not wh_id.startswith("WH-"):
            return f"Error: {wh_id} is not a WH. Only WH-NNN can be promoted."

        state_text = self.workspace.read_file("RESEARCH_STATE.md")
        range_ = _find_section_range(state_text, wh_id)
        if range_ is None:
            return f"Error: {wh_id} not found in RESEARCH_STATE.md"

        num = wh_id.split("-")[1]
        er_id = f"ER-{num}"

        # Guardrail: REFUTED block
        has_refuted = False
        has_verified = False
        if self.research_state:
            for c in self.research_state.computations.values():
                if c.target_hypothesis in (wh_id, er_id):
                    if c.verdict.value == "REFUTED":
                        has_refuted = True
                    if c.verdict.value == "VERIFIED":
                        has_verified = True
        if not has_refuted or not has_verified:
            # Fallback: check computation log
            comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
            entries = _parse_comp_entries(comp_log)
            for e in entries:
                refs = e["claim"] + " " + e.get("body", "")
                if wh_id in refs or er_id in refs:
                    if e["verdict"] == "REFUTED":
                        has_refuted = True
                    if e["verdict"] == "VERIFIED":
                        has_verified = True
        if has_refuted and not has_verified:
            return (
                f"Error: Cannot promote {wh_id} — a REFUTED computation exists "
                "with no superseding VERIFIED computation."
            )

        # Guardrail: unresolved HIGH critique block
        critique_log = self.workspace.read_file("CRITIQUE_LOG.md")
        if critique_log:
            # Check if any unresolved HIGH critique mentions this WH
            active_idx = critique_log.find("# Active Critiques")
            resolved_idx = critique_log.find("# Resolved Critiques")
            if active_idx != -1:
                end = resolved_idx if resolved_idx > active_idx else len(critique_log)
                active_section = critique_log[active_idx:end]
                # Find HIGH UNRESOLVED critiques that mention this WH
                _ACTIVE_CRIT_RE = re.compile(
                    r'^##\s+(CRIT(?:IQUE)?-\d+)\s.*?\[HIGH\].*?\[UNRESOLVED\]',
                    re.MULTILINE | re.IGNORECASE,
                )
                headers = list(_ACTIVE_CRIT_RE.finditer(active_section))
                for i, match in enumerate(headers):
                    start = match.end()
                    end_pos = headers[i + 1].start() if i + 1 < len(headers) else len(active_section)
                    body = active_section[start:end_pos]
                    if wh_id in body or er_id in body:
                        crit_id = match.group(1)
                        return (
                            f"Error: Cannot promote {wh_id} — unresolved HIGH "
                            f"critique {crit_id} targets this claim."
                        )

        # Perform promotion: rename header WH-NNN → ER-NNN
        state_text = re.sub(
            rf'^(#{{2,3}} ){re.escape(wh_id)}',
            rf'\g<1>{er_id}',
            state_text,
            count=1,
            flags=re.MULTILINE,
        )
        # Propagate prose references using word-boundary regex
        state_text = re.sub(rf'\b{re.escape(wh_id)}\b', er_id, state_text)

        # Update verified_results frontmatter
        meta, body = parse_frontmatter(state_text)
        vr = meta.get("verified_results", []) or []
        if er_id not in vr:
            vr.append(er_id)
            meta["verified_results"] = sorted(vr)
        # Remove WH form if present
        if wh_id in vr:
            vr.remove(wh_id)
            meta["verified_results"] = sorted(vr)
        state_text = render_frontmatter(meta, body)

        self.workspace.write_file("RESEARCH_STATE.md", state_text)

        # Update formal research state object
        if self.research_state:
            from .research_state import HypothesisStatus
            if wh_id in self.research_state.hypotheses:
                h = self.research_state.hypotheses.pop(wh_id)
                h.id = er_id
                h.status = HypothesisStatus.ESTABLISHED
                h.iteration_modified = self.iteration
                self.research_state.hypotheses[er_id] = h

        # Log scaffold event
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "promote_hypothesis",
            f"Promoted {wh_id} → {er_id}: {justification}",
        )

        self.mutations_applied = True
        return f"Promoted {wh_id} → {er_id}"

    def _resolve_critique(self, args: dict) -> str:
        from .markdown import (
            resolve_critique as _resolve,
            ensure_critique_metadata_consistent,
        )

        crit_id = args["critique_id"]
        resolution = args.get("resolution", f"Addressed at iteration {self.iteration}")
        content = self.workspace.read_file("CRITIQUE_LOG.md")
        content = _resolve(content, crit_id, resolution)
        content = ensure_critique_metadata_consistent(content)
        self.workspace.write_file("CRITIQUE_LOG.md", content)
        self.resolved_critique_ids.add(crit_id)
        self.mutations_applied = True

        # Write RESOLVED entry to CRITIQUE_INDEX.jsonl
        entry = {
            "id": crit_id,
            "status": "RESOLVED",
            "resolution": resolution,
            "iteration": self.iteration,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.workspace.append_file(
            "CRITIQUE_INDEX.jsonl", json.dumps(entry, ensure_ascii=False) + "\n"
        )

        # Update formal state with resolution metadata
        if self.research_state and crit_id in self.research_state.critiques:
            from .research_state import CritiqueStatus
            c = self.research_state.critiques[crit_id]
            c.status = CritiqueStatus.RESOLVED
            c.resolution = resolution
            c.iteration_resolved = self.iteration

        return f"Resolved {crit_id}"

    def _update_section(self, args: dict) -> str:
        section_name = args["section"]
        content = args.get("content", "")
        state_text = self.workspace.read_file("RESEARCH_STATE.md")
        range_ = _find_h1_content_range(state_text, section_name)
        if range_ is None:
            return f"Error: section '# {section_name}' not found"
        cs, ce = range_
        state_text = state_text[:cs] + "\n\n" + content.strip() + "\n\n" + state_text[ce:]
        self.workspace.write_file("RESEARCH_STATE.md", state_text)
        self.mutations_applied = True
        return f"Updated # {section_name}"

    def _set_next_task(self, args: dict) -> str:
        self.task_data = args
        self.stop_after_round = True
        return f"Task set: {args.get('task_type', '?')} → {args.get('assigned_to', '?')}"
