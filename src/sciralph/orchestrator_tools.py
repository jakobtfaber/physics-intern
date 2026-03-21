"""Orchestrator state-mutation tools.

The orchestrator uses these tools to mutate the ResearchState object.
Markdown files are rendered from state in process_response().
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar

from rich.console import Console

from .tools import ToolCall
from .categories import CompensationCategory as CC
from .workspace import log_scaffold_event

console = Console()

if TYPE_CHECKING:
    from .research_state import ResearchState
    from .workspace import WorkspaceManager


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
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "IDs of hypotheses this claim depends on "
                            "(e.g. ['ER-001', 'WH-002']). Optional."
                        ),
                    },
                    "from_rq": {
                        "type": "string",
                        "description": (
                            "If this WH is the concrete result of a research "
                            "question, provide the RQ ID (e.g. 'RQ-003'). "
                            "The WH inherits the RQ's number and the RQ is "
                            "auto-resolved."
                        ),
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
                "Requires a VERIFIED verdict from the reviewer. "
                "Call after the reviewer has confirmed the claim."
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
                            "Reference the verification result and supporting evidence."
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
                "Mark a critique as resolved. "
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
                "Replace the content of a top-level section in the research state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["Conventions", "Situation Assessment"],
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
            "name": "append_note",
            "description": (
                "Append a research note. Use for recording intermediate insights, "
                "observations, or decisions. Notes are append-only and visible to "
                "all agents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The note content.",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_research_question",
            "description": (
                "Add an open-ended research question (RQ). "
                "Use for questions that need exploration before a concrete "
                "hypothesis can be formulated. Returns the auto-assigned ID (e.g., RQ-001)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The research question.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Why this question matters for the research.",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_research_question",
            "description": (
                "Close a research question — either it was answered "
                "(e.g. via add_hypothesis with from_rq) or it led nowhere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Research question ID, e.g. RQ-001.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this RQ is being closed (optional).",
                    },
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_dead_end",
            "description": (
                "Record a dead-end approach directly, without creating and "
                "abandoning a hypothesis. Use when you know an approach won't "
                "work but never formulated it as a WH."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Description of the approach that failed or is known to fail.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this approach is a dead end.",
                    },
                },
                "required": ["description", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_next_task",
            "description": (
                "Set the next task for the research loop. "
                "Call this ALONE — not in the same response as any mutation "
                "tools. First emit all mutations, then in your NEXT response "
                "call set_next_task with the actual entity IDs from the "
                "mutation results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "enum": [
                            "research", "compute", "review",
                            "critique", "terminate",
                        ],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "target_claim": {
                        "type": "string",
                        "description": (
                            "The RQ/WH/ER ID this task targets. "
                            "For research/compute: the RQ or WH being investigated. "
                            "For review: the WH being reviewed."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Goal-focused task description. Lead with a single "
                            "sentence stating the deliverable and its scope. "
                            "Add critical constraints or pitfalls if needed. "
                            "Do NOT write step-by-step procedures — put method "
                            "suggestions in method_hints instead."
                        ),
                    },
                    "background": {
                        "type": "string",
                        "description": (
                            "Background context for the task: relevant prior results, "
                            "established conventions, domain knowledge."
                        ),
                    },
                    "method_hints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Suggested methods or approaches for the agent to consider."
                        ),
                    },
                    "assumptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Key assumptions the agent should work under."
                        ),
                    },
                    "relevant_results": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "References to established results or prior evidence "
                            "that are relevant to this task (e.g. 'ER-001', 'WH-003')."
                        ),
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

_BATCH_NUDGE = " Call set_next_task ALONE in your next response to dispatch."


class OrchestratorToolExecutor:
    """Dispatches state-mutation tool calls for the orchestrator."""

    TOOL_DEFINITIONS: ClassVar[list[dict]] = ORCHESTRATOR_TOOL_DEFINITIONS

    exit_tool_name: str = "set_next_task"

    def __init__(self, workspace: WorkspaceManager, iteration: int, research_state: ResearchState | None = None):
        self.workspace = workspace
        self.iteration = iteration
        self.research_state = research_state
        self.mutations_applied: bool = False
        self.mutations_this_round: list[str] = []
        self.dispatch_only: bool = False
        self._reject_mutations: bool = False
        self.task_data: dict | None = None
        self.resolved_critique_ids: set[str] = set()
        self.stop_after_round: bool = False

    def begin_round(self) -> None:
        """Called by the agent loop at the start of each response's tool batch."""
        # Activate mutation rejection if dispatch_only was set in a prior round
        self._reject_mutations = self.dispatch_only
        self.mutations_this_round.clear()

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        start = time.time()

        # Enforce dispatch-only mode: after entity-creating mutations in a
        # prior round, only set_next_task is allowed (activated by begin_round)
        if self._reject_mutations and tool_name != "set_next_task":
            return ToolCall(
                tool_name=tool_name, tool_input=tool_input,
                output=(
                    "Error: only set_next_task is available after creating entities. "
                    "Call set_next_task now with the entity IDs from earlier results."
                ),
                is_error=True,
                duration=time.time() - start,
            )

        handlers = {
            "add_hypothesis": self._add_hypothesis,
            "update_hypothesis": self._update_hypothesis,
            "abandon_hypothesis": self._abandon_hypothesis,
            "promote_hypothesis": self._promote_hypothesis,
            "resolve_critique": self._resolve_critique,
            "update_section": self._update_section,
            "append_note": self._append_note,
            "add_research_question": self._add_research_question,
            "resolve_research_question": self._resolve_research_question,
            "record_dead_end": self._record_dead_end,
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
        from .research_state import Hypothesis, HypothesisStatus, RQStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        statement = args.get("statement", "Untitled")
        derivation = args.get("derivation", "")
        depends_on = args.get("depends_on", [])
        from_rq = args.get("from_rq")

        if from_rq:
            # Inherit RQ's number and auto-resolve it
            if from_rq not in state.research_questions:
                return f"Error: {from_rq} not found in research questions"
            rq = state.research_questions[from_rq]
            if rq.status == RQStatus.RESOLVED:
                return (
                    f"Error: {from_rq} is already resolved"
                    f" (iteration {rq.iteration_resolved})."
                    " Do not create a WH from an already-closed RQ."
                    " Call set_next_task to proceed."
                )
            rq_num = int(from_rq.split("-")[1])
            new_id = f"WH-{rq_num:03d}"
            if new_id in state.hypotheses:
                # Collision: another WH already has this number; use next available
                num = state.next_entity_num()
                new_id = f"WH-{num:03d}"
            else:
                # Auto-resolve the RQ
                rq.status = RQStatus.RESOLVED
                rq.resolved_to.append(new_id)
                rq.iteration_resolved = self.iteration
                rq.resolution_reason = f"Promoted to {new_id}"
        else:
            num = state.next_entity_num()
            new_id = f"WH-{num:03d}"

        # Copy evidence from RQ if available
        evidence = None
        if from_rq:
            rq = state.research_questions[from_rq]
            if rq.evidence is not None:
                from copy import deepcopy
                evidence = deepcopy(rq.evidence)

        state.hypotheses[new_id] = Hypothesis(
            id=new_id,
            statement=statement,
            status=HypothesisStatus.WORKING,
            derivation=derivation,
            depends_on=depends_on,
            iteration_created=self.iteration,
            iteration_modified=self.iteration,
            evidence=evidence,
        )
        self.mutations_applied = True
        self.mutations_this_round.append(f"Added {new_id}")
        self.dispatch_only = True
        detail = f"{new_id}: {statement[:120]}"
        if from_rq:
            detail += f" (from {from_rq})"
        if depends_on:
            detail += f" (depends_on: {', '.join(depends_on)})"
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "add_hypothesis", detail,
        )
        if from_rq:
            console.print(f"  [bold cyan]{from_rq}[/] → [bold yellow]+{new_id}[/] {statement[:80]}")
        else:
            console.print(f"  [bold yellow]+{new_id}[/] {statement[:80]}")
        return f"Added {new_id} — {statement}." + _BATCH_NUDGE

    def _update_hypothesis(self, args: dict) -> str:
        state = self.research_state
        if not state:
            return "Error: no research state available"

        hid = args["id"]
        if hid not in state.hypotheses:
            return f"Error: {hid} not found in research state"

        h = state.hypotheses[hid]
        if "statement" in args:
            h.statement = args["statement"]
        if "derivation" in args:
            h.derivation = args["derivation"]
        h.iteration_modified = self.iteration
        self.mutations_applied = True
        return f"Updated {hid}." + _BATCH_NUDGE

    def _abandon_hypothesis(self, args: dict) -> str:
        from .research_state import FailedApproach, HypothesisStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        hid = args["id"]
        reason = args.get("reason", "No longer viable")

        if hid not in state.hypotheses:
            return f"Error: {hid} not found"

        # Check for dependents — warn but don't block
        dependents = [
            h2.id for h2 in state.hypotheses.values()
            if h2.id != hid
            and h2.status != HypothesisStatus.ABANDONED
            and hid in h2.depends_on
        ]

        h = state.hypotheses[hid]
        title = h.statement or hid

        h.status = HypothesisStatus.ABANDONED
        h.iteration_modified = self.iteration

        state.failed_approaches.append(FailedApproach(
            description=f"Abandoned {hid} — {title}",
            reason=reason,
            related_entities=[hid],
            derivation_excerpt=(h.derivation[:300] if h.derivation else ""),
            iteration=self.iteration,
        ))

        self.mutations_applied = True

        detail = f"{hid}: {reason}"
        if dependents:
            detail += f" (dependents affected: {', '.join(dependents)})"
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "abandon_hypothesis", detail,
        )
        console.print(f"  [bold red]✗ {hid}[/] abandoned: {reason[:80]}")

        msg = f"Abandoned {hid}: {reason}"
        if dependents:
            dep_list = ", ".join(dependents)
            msg += (
                f"\nWarning: {dep_list} depend(s) on {hid}. "
                "Their promotion will be blocked until you remove this "
                "dependency (update or abandon them too)."
            )
        return msg + _BATCH_NUDGE

    def _promote_hypothesis(self, args: dict) -> str:
        from .research_state import HypothesisStatus, CritiqueStatus, Verdict

        state = self.research_state
        if not state:
            return "Error: no research state available"

        wh_id = args["id"]
        justification = args.get("justification", "")

        if not wh_id.startswith("WH-"):
            return f"Error: {wh_id} is not a WH. Only WH-NNN can be promoted."

        if wh_id not in state.hypotheses:
            return f"Error: {wh_id} not found in research state"

        h = state.hypotheses[wh_id]
        num = wh_id.split("-")[1]
        er_id = f"ER-{num}"

        # Guardrail: require VERIFIED review result
        if not h.review or h.review.verdict != Verdict.VERIFIED:
            return (
                f"Error: Cannot promote {wh_id} — no VERIFIED review "
                "result. Schedule a review task first."
            )

        # Guardrail: check for unestablished dependencies
        unestablished = state.unestablished_dependencies(wh_id)
        if unestablished:
            return (
                f"Error: Cannot promote {wh_id} — unestablished dependencies: "
                f"{', '.join(unestablished)}. Promote or resolve them first."
            )

        # Perform promotion in state
        h = state.hypotheses.pop(wh_id)
        h.id = er_id
        h.status = HypothesisStatus.ESTABLISHED
        h.promotion_justification = justification
        h.iteration_modified = self.iteration
        state.hypotheses[er_id] = h
        state.normalize_references()

        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "promote_hypothesis",
            f"Promoted {wh_id} → {er_id}: {justification}",
        )
        console.print(f"  [bold green]{wh_id} → {er_id}[/] promoted")

        self.mutations_applied = True
        msg = f"Promoted {wh_id} → {er_id}. If this is the final result asked by the problem statement, consider closing the remaining RQ and calling set_next_task with task_type 'terminate'."
        if (not state.working_hypotheses()
                and not state.open_research_questions()):
            msg += (
                " No open RQs or working hypotheses remain."
                " If this completes the research, consider set_next_task"
                " with task_type 'terminate'."
            )
        else:
            msg += _BATCH_NUDGE
        return msg

    def _resolve_critique(self, args: dict) -> str:
        from .research_state import CritiqueStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        crit_id = args["critique_id"]
        resolution = args.get("resolution", f"Addressed at iteration {self.iteration}")

        if crit_id not in state.critiques:
            return f"Error: {crit_id} not found in research state"

        c = state.critiques[crit_id]
        if c.status == CritiqueStatus.RESOLVED:
            return f"{crit_id} is already resolved. Call set_next_task now."

        c.status = CritiqueStatus.RESOLVED
        c.resolution = resolution
        c.iteration_resolved = self.iteration

        self.resolved_critique_ids.add(crit_id)
        self.mutations_applied = True
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "resolve_critique", f"{crit_id}: {resolution[:120]}",
        )
        console.print(f"  [dim]{crit_id}[/] resolved")
        return f"Resolved {crit_id}." + _BATCH_NUDGE

    def _update_section(self, args: dict) -> str:
        state = self.research_state
        if not state:
            return "Error: no research state available"

        section_name = args["section"]
        content = args.get("content", "")

        if section_name == "Conventions":
            state.conventions = content.strip()
        elif section_name == "Situation Assessment":
            state.situation_assessment = content.strip()
        else:
            return f"Error: unknown section '{section_name}'"

        self.mutations_applied = True
        return f"Updated # {section_name}." + _BATCH_NUDGE

    def _append_note(self, args: dict) -> str:
        state = self.research_state
        if not state:
            return "Error: no research state available"

        text = args.get("text", "")
        if not text.strip():
            return "Error: note text cannot be empty"

        state.research_notes.append({
            "text": text.strip(),
            "iteration": self.iteration,
        })
        self.mutations_applied = True
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "append_note", text[:120],
        )
        return "Note appended." + _BATCH_NUDGE

    def _record_dead_end(self, args: dict) -> str:
        from .research_state import FailedApproach

        state = self.research_state
        if not state:
            return "Error: no research state available"

        description = args.get("description", "")
        reason = args.get("reason", "")

        state.failed_approaches.append(FailedApproach(
            description=description,
            reason=reason,
            iteration=self.iteration,
        ))
        self.mutations_applied = True
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "record_dead_end", f"{description[:120]}: {reason[:120]}",
        )
        console.print(f"  [bold red]Dead end:[/] {description[:80]}")
        return f"Recorded dead end: {description}." + _BATCH_NUDGE

    def _add_research_question(self, args: dict) -> str:
        from .research_state import ResearchQuestion

        state = self.research_state
        if not state:
            return "Error: no research state available"

        num = state.next_entity_num()
        rq_id = f"RQ-{num:03d}"
        question = args.get("question", "Untitled question")
        context = args.get("context", "")

        state.research_questions[rq_id] = ResearchQuestion(
            id=rq_id,
            question=question,
            context=context,
            iteration_created=self.iteration,
        )
        self.mutations_applied = True
        self.mutations_this_round.append(f"Added {rq_id}")
        self.dispatch_only = True
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "add_research_question", f"{rq_id}: {question[:120]}",
        )
        console.print(f"  [bold cyan]+{rq_id}[/] {question[:80]}")
        return f"Added {rq_id} — {question}." + _BATCH_NUDGE

    def _resolve_research_question(self, args: dict) -> str:
        from .research_state import RQStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        rq_id = args["id"]
        reason = args.get("reason", "")

        if rq_id not in state.research_questions:
            return f"Error: {rq_id} not found in research state"

        rq = state.research_questions[rq_id]
        if rq.status == RQStatus.RESOLVED:
            return (
                f"{rq_id} is already resolved (iteration {rq.iteration_resolved})."
                " No action needed — call set_next_task now."
            )
        rq.status = RQStatus.RESOLVED
        rq.iteration_resolved = self.iteration
        rq.resolution_reason = reason
        self.mutations_applied = True
        detail = f"{rq_id}: {reason}" if reason else f"{rq_id} (closed)"
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "resolve_research_question", detail,
        )
        console.print(f"  [dim]{rq_id}[/] closed" + (f" — {reason[:60]}" if reason else ""))
        return f"Closed {rq_id}." + _BATCH_NUDGE

    def _set_next_task(self, args: dict) -> str:
        # Two-phase gate: reject if mutations occurred in this response batch
        if self.mutations_this_round:
            mutations_list = "; ".join(self.mutations_this_round)
            log_scaffold_event(
                self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                "two_phase_gate_reject",
                f"Rejected set_next_task — mutations this response: {mutations_list}",
            )
            return (
                f"Error: set_next_task cannot be called in the same response "
                f"as mutation tools. Mutations this response: {mutations_list}. "
                "Call set_next_task ALONE in your next response, using the "
                "actual entity IDs from the mutation results above."
            )

        # Validate target_claim when present
        task_type = args.get("task_type", "")
        target_claim = args.get("target_claim")
        skip_validation = task_type in ("critique", "terminate")
        if target_claim and not skip_validation and self.research_state:
            valid = self._validate_target_claim(target_claim)
            if valid is not None:
                return valid

        self.task_data = args
        self.stop_after_round = True
        return f"Task set: {args.get('task_type', '?')}"

    def _validate_target_claim(self, target_claim: str) -> str | None:
        """Validate target_claim exists. Returns error string or None if valid."""
        import re
        state = self.research_state
        assert state is not None

        match = re.match(r"^(RQ|WH|ER)-(\d+)$", target_claim)
        if not match:
            # Unknown prefix — allow through
            return None

        prefix = match.group(1)
        if prefix == "RQ":
            if target_claim in state.research_questions:
                return None
            valid_rqs = sorted(state.research_questions.keys())
            valid_whs = sorted(h for h in state.hypotheses if h.startswith("WH-"))
            valid_ers = sorted(h for h in state.hypotheses if h.startswith("ER-"))
            entity_list = []
            if valid_rqs:
                entity_list.append(f"RQs: {', '.join(valid_rqs)}")
            if valid_whs:
                entity_list.append(f"WHs: {', '.join(valid_whs)}")
            if valid_ers:
                entity_list.append(f"ERs: {', '.join(valid_ers)}")
            listing = "; ".join(entity_list) if entity_list else "none"
        else:
            # WH or ER
            if target_claim in state.hypotheses:
                return None
            valid_rqs = sorted(state.research_questions.keys())
            valid_whs = sorted(h for h in state.hypotheses if h.startswith("WH-"))
            valid_ers = sorted(h for h in state.hypotheses if h.startswith("ER-"))
            entity_list = []
            if valid_rqs:
                entity_list.append(f"RQs: {', '.join(valid_rqs)}")
            if valid_whs:
                entity_list.append(f"WHs: {', '.join(valid_whs)}")
            if valid_ers:
                entity_list.append(f"ERs: {', '.join(valid_ers)}")
            listing = "; ".join(entity_list) if entity_list else "none"

        log_scaffold_event(
            self.workspace.root, self.iteration, CC.LOOP_CONTROL,
            "target_claim_validation_reject",
            f"Invalid target_claim {target_claim}. Valid entities: {listing}",
        )
        return (
            f"Error: target_claim '{target_claim}' not found. "
            f"Valid entities: {listing}. "
            "Use the actual entity ID from mutation results."
        )
