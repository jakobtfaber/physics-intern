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
                "Add a new Working Hypothesis from a Research Question and "
                "auto-dispatch the reviewer. "
                "Requires from_rq: every WH must originate from an RQ "
                "that has gathered evidence. "
                "ENDS YOUR TURN — the reviewer is auto-dispatched to check "
                "the new WH. Complete all other mutations before calling this. "
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
                "required": ["statement", "from_rq"],
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
                        "enum": ["Conventions", "Strategy"],
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
            "name": "dispatch_researcher",
            "description": (
                "Dispatch the researcher agent for pure reasoning, derivation, "
                "or analysis. No code execution. Can be called alongside "
                "mutation tools — all mutations are applied before dispatch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_claim": {
                        "type": "string",
                        "description": (
                            "The RQ, WH, or CRIT ID this task targets "
                            "(e.g. 'RQ-001', 'WH-003', 'CRIT-001')."
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
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "background": {
                        "type": "string",
                        "description": (
                            "Background context for the task: relevant prior results, "
                            "established conventions, domain knowledge. CRITICAL: the "
                            "researcher has NO access to background survey, research "
                            "notes, or strategy — provide all needed context here."
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
                "required": ["target_claim", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_computer",
            "description": (
                "Dispatch the computer agent for numerical, symbolic, or "
                "simulation work via Python/SymPy/NumPy/SciPy. Can be called "
                "alongside mutation tools — all mutations are applied before dispatch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_claim": {
                        "type": "string",
                        "description": (
                            "The RQ, WH, or CRIT ID this task targets "
                            "(e.g. 'RQ-001', 'WH-003', 'CRIT-001')."
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
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "background": {
                        "type": "string",
                        "description": (
                            "Background context for the task: relevant prior results, "
                            "established conventions, domain knowledge. CRITICAL: the "
                            "computer has NO access to background survey, research "
                            "notes, or strategy — provide all needed context here."
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
                "required": ["target_claim", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_termination",
            "description": (
                "Request termination of the research loop. Use when all RQs "
                "are resolved or abandoned and all WHs are promoted or abandoned. "
                "The system enforces completion gates and reports blockers if not met."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the research is complete.",
                    },
                    "answer_ers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Ordered list of ER IDs that constitute the answer "
                            "(e.g. ['ER-001', 'ER-003', 'ER-005']). The formatter "
                            "uses this to structure the final answer."
                        ),
                    },
                },
                "required": ["answer_ers"],
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

    exit_tool_names: ClassVar[frozenset[str]] = frozenset({
        "add_hypothesis", "dispatch_researcher", "dispatch_computer",
        "request_termination",
    })

    def __init__(
        self, workspace: WorkspaceManager, iteration: int,
        research_state: ResearchState | None = None,
        *,
        min_er_for_completion: int = 3,
        max_iterations: int = 20,
        budget_synthesis_margin: int = 3,
    ):
        self.workspace = workspace
        self.iteration = iteration
        self.research_state = research_state
        self.mutations_applied: bool = False
        self._calls_this_round: int = 0
        self._round_mutations: list[str] = []
        self.task_data: dict | None = None
        self.resolved_critique_ids: set[str] = set()
        self.stop_after_round: bool = False
        self._min_er_for_completion = min_er_for_completion
        self._max_iterations = max_iterations
        self._budget_synthesis_margin = budget_synthesis_margin

    def begin_round(self) -> None:
        """Called at the start of each response's tool batch.

        Clears per-round tracking so the dispatch gate only sees
        tool calls from the current response.
        """
        self._calls_this_round = 0
        self._round_mutations.clear()

    def end_round(self) -> str | None:
        """Called after tool results are appended.

        Returns a state injection string if mutations occurred this round,
        or None otherwise.  The injection is placed after tool results so
        the model sees it at the top of the next generation window.
        """
        if not self._round_mutations:
            return None
        return self._render_state_injection()

    def _render_state_injection(self) -> str:
        """Produce a compact state summary after mutations."""
        state = self.research_state
        lines: list[str] = ["── State after your mutations ──"]

        # Applied mutations
        applied = ", ".join(f"✓ {m}" for m in self._round_mutations)
        lines.append(f"Applied: {applied}")

        if state:
            # State snapshot
            lines.append("")
            lines.append("State snapshot:")
            ers = state.established_hypotheses()
            if ers:
                er_items = ", ".join(f"{h.id} ({h.statement[:50]})" for h in ers)
                lines.append(f"  ER: {er_items}")
            whs = state.working_hypotheses()
            if whs:
                wh_items = []
                for h in whs:
                    parts = [h.id]
                    if h.evidence:
                        if h.review:
                            parts.append(h.review.verdict.upper())
                        else:
                            parts.append(f"has {len(h.evidence)} evidence, PENDING REVIEW")
                    else:
                        parts.append("no evidence")
                    wh_items.append(f"{parts[0]} ({', '.join(parts[1:])})")
                lines.append(f"  WH: {', '.join(wh_items)}")
            open_rqs = state.open_research_questions()
            if open_rqs:
                rq_items = []
                for rq in open_rqs:
                    rq_items.append(f"{rq.id} ({f'{len(rq.evidence)} evidence' if rq.evidence else 'no evidence'})")
                lines.append(f"  Open RQs: {', '.join(rq_items)}")
            from .research_state import CritiqueStatus
            unresolved = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]
            if unresolved:
                crit_ids = ", ".join(c.id for c in unresolved)
                lines.append(f"  Unresolved critiques: {crit_ids}")

            # Conditional guidance
            lines.append("")
            lines.append("Pending:")
            guidance = self._build_guidance(state, ers, whs, open_rqs, unresolved)
            for g in guidance:
                lines.append(f"- {g}")

        lines.append("──")
        return "\n".join(lines)

    def _build_guidance(
        self, state, ers: list, whs: list, open_rqs: list, unresolved_critiques: list,
    ) -> list[str]:
        """Build conditional guidance lines for the state injection."""
        from .research_state import CritiqueStatus, HypothesisStatus, Verdict
        guidance: list[str] = []

        er_count = len(ers)
        wh_count = len(whs)
        active_critiques = len(unresolved_critiques)

        # Budget pressure
        budget_remaining = self._max_iterations - self.iteration
        if budget_remaining <= self._budget_synthesis_margin and er_count >= 1:
            guidance.append(
                f"BUDGET: Only {budget_remaining} iteration(s) remaining "
                f"(iteration {self.iteration} of {self._max_iterations}). "
                "Synthesize results now; note unresolved items as limitations."
            )

        # All resolved → may terminate
        if er_count >= self._min_er_for_completion and wh_count == 0 and active_critiques == 0:
            open_rq_count = len(open_rqs)
            if open_rq_count == 0:
                guidance.append("All entities resolved. You may terminate.")
            else:
                guidance.append(
                    f"All WHs promoted and critiques resolved. {open_rq_count} open RQ(s) remain — "
                    "resolve or abandon them before terminating."
                )

        # Per-WH guidance
        for h in whs:
            if h.review and h.review.verdict == Verdict.VERIFIED:
                guidance.append(f"{h.id} is VERIFIED but not yet promoted — check depends_on.")
            elif h.review and h.review.verdict == Verdict.REFUTED:
                guidance.append(
                    f"{h.id} was REFUTED — dispatch researcher/computer with "
                    "new evidence (auto-review will follow), or abandon."
                )
            elif h.evidence and not h.review:
                guidance.append(f"{h.id} awaiting auto-review.")

        # Per-RQ with evidence
        for rq in open_rqs:
            if rq.evidence:
                guidance.append(f"{rq.id} has evidence — formulate a WH (add_hypothesis with from_rq).")

        # Unresolved critiques
        if active_critiques:
            guidance.append(f"{active_critiques} unresolved critique(s) to address.")

        # Default closing
        guidance.append("When ready, call a dispatch tool (dispatch_researcher, dispatch_computer, or request_termination) — or add_hypothesis to formulate a WH (auto-triggers review).")
        return guidance

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        start = time.time()
        self._calls_this_round += 1

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
            "dispatch_researcher": self._dispatch_researcher,
            "dispatch_computer": self._dispatch_computer,
            "request_termination": self._request_termination,
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
        from .research_state import CritiqueStatus, Hypothesis, HypothesisStatus, RQStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        # Cap: block if too many working WHs or unresolved critiques
        whs = state.working_hypotheses()
        if len(whs) >= 2:
            ids = ", ".join(h.id for h in whs)
            return (
                f"Error: already {len(whs)} working hypotheses ({ids}). "
                "Review, promote, or abandon existing WHs before creating new ones."
            )
        unresolved = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]
        if unresolved:
            crit_ids = ", ".join(c.id for c in unresolved)
            return (
                f"Error: {len(unresolved)} unresolved critique(s) ({crit_ids}). "
                "Address critiques before creating new WHs."
            )

        statement = args.get("statement", "Untitled")
        derivation = args.get("derivation", "")
        depends_on = args.get("depends_on", [])
        from_rq = args.get("from_rq")

        if not from_rq:
            return (
                "Error: from_rq is required. Every Working Hypothesis must "
                "originate from a Research Question with gathered evidence. "
                "Create an RQ first with add_research_question, dispatch a "
                "research or compute task to gather evidence on it, then "
                "call add_hypothesis with from_rq set to that RQ ID."
            )

        # Inherit RQ's number and auto-resolve it
        if from_rq not in state.research_questions:
            return f"Error: {from_rq} not found in research questions"
        rq = state.research_questions[from_rq]
        if rq.status == RQStatus.RESOLVED:
            return (
                f"Error: {from_rq} is already resolved"
                f" (iteration {rq.iteration_resolved})."
                " Do not create a WH from an already-closed RQ."
                " Call a dispatch tool to proceed."
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

        # Copy evidence from RQ
        from copy import deepcopy
        rq = state.research_questions[from_rq]
        evidence = deepcopy(rq.evidence) if rq.evidence else []

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
        self._round_mutations.append(f"Added {new_id}")
        detail = f"{new_id}: {statement[:120]} (from {from_rq})"
        if depends_on:
            detail += f" (depends_on: {', '.join(depends_on)})"
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "add_hypothesis", detail,
        )
        console.print(f"  [bold cyan]{from_rq}[/] → [bold yellow]+{new_id}[/] {statement[:80]}")

        # Auto-dispatch reviewer for the new WH
        self.task_data = {"task_type": "review", "target_claim": new_id}
        self.stop_after_round = True

        msg = f"Added {new_id} — {statement}."
        if evidence:
            msg += f" {len(evidence)} evidence item(s) copied."
        msg += " Review will be dispatched automatically."
        return msg

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
        self._round_mutations.append(f"Updated {hid}")
        console.print(f"  [dim]Updated {hid}[/]")
        return f"Updated {hid}."

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

        self._round_mutations.append(f"Abandoned {hid}")
        msg = f"Abandoned {hid}: {reason}"
        if dependents:
            dep_list = ", ".join(dependents)
            msg += (
                f"\nWarning: {dep_list} depend(s) on {hid}. "
                "Their promotion will be blocked until you remove this "
                "dependency (update or abandon them too)."
            )
        return msg

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
        self._round_mutations.append(f"Promoted {wh_id} → {er_id}")
        return f"Promoted {wh_id} → {er_id}."

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
            return f"{crit_id} is already resolved."

        c.status = CritiqueStatus.RESOLVED
        c.resolution = resolution
        c.iteration_resolved = self.iteration

        self.resolved_critique_ids.add(crit_id)
        self.mutations_applied = True
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "resolve_critique", f"{crit_id}: {resolution[:120]}",
        )
        self._round_mutations.append(f"Resolved {crit_id}")
        console.print(f"  [dim]{crit_id}[/] resolved — {resolution[:60]}")
        return f"Resolved {crit_id}."

    def _update_section(self, args: dict) -> str:
        state = self.research_state
        if not state:
            return "Error: no research state available"

        section_name = args["section"]
        content = args.get("content", "")

        if section_name == "Conventions":
            new = content.strip()
            state.conventions = (state.conventions.rstrip() + "\n\n" + new) if state.conventions else new
        elif section_name == "Strategy":
            state.strategy = content.strip()
        else:
            return f"Error: unknown section '{section_name}'"

        self.mutations_applied = True
        self._round_mutations.append(f"Updated {section_name}")
        console.print(f"  [dim]Updated {section_name}[/]")
        return f"Updated {section_name}."

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
        self._round_mutations.append("Appended research note")
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "append_note", text[:120],
        )
        console.print(f"  [dim]Note:[/] {text[:80]}")
        return "Note appended."

    def _add_research_question(self, args: dict) -> str:
        from .research_state import CritiqueStatus, ResearchQuestion

        state = self.research_state
        if not state:
            return "Error: no research state available"

        # Cap: block if too many open RQs or unresolved critiques
        open_rqs = state.open_research_questions()
        unresolved = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]
        if len(open_rqs) >= 3:
            ids = ", ".join(rq.id for rq in open_rqs)
            return (
                f"Error: already {len(open_rqs)} open RQs ({ids}). "
                "Resolve or abandon existing RQs before creating new ones. "
                "Dispatch research/compute tasks on existing RQs first."
            )
        if unresolved:
            crit_ids = ", ".join(c.id for c in unresolved)
            return (
                f"Error: {len(unresolved)} unresolved critique(s) ({crit_ids}). "
                "Address critiques before creating new RQs."
            )

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
        self._round_mutations.append(f"Added {rq_id}")
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "add_research_question", f"{rq_id}: {question[:120]}",
        )
        console.print(f"  [bold cyan]+{rq_id}[/] {question[:80]}")
        return f"Added {rq_id} — {question}."

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
            return f"{rq_id} is already resolved (iteration {rq.iteration_resolved})."
        rq.status = RQStatus.RESOLVED
        rq.iteration_resolved = self.iteration
        rq.resolution_reason = reason
        self.mutations_applied = True
        self._round_mutations.append(f"Closed {rq_id}")
        detail = f"{rq_id}: {reason}" if reason else f"{rq_id} (closed)"
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "resolve_research_question", detail,
        )
        console.print(f"  [dim]{rq_id}[/] closed" + (f" — {reason[:60]}" if reason else ""))
        return f"Closed {rq_id}."

    def _dispatch_researcher(self, args: dict) -> str:
        target = args["target_claim"]
        if self.research_state:
            err = self._validate_target_claim(target)
            if err is not None:
                return err
        self.task_data = {"task_type": "research", **args}
        self.stop_after_round = True
        return f"Dispatched: researcher → {target}"

    def _dispatch_computer(self, args: dict) -> str:
        target = args["target_claim"]
        if self.research_state:
            err = self._validate_target_claim(target)
            if err is not None:
                return err
        self.task_data = {"task_type": "compute", **args}
        self.stop_after_round = True
        return f"Dispatched: computer → {target}"

    def _request_termination(self, args: dict) -> str:
        reason = args.get("reason", "Research complete.")
        answer_ers = args.get("answer_ers", [])
        self.task_data = {
            "task_type": "terminate",
            "description": reason,
            "answer_ers": answer_ers,
        }
        self.stop_after_round = True
        return "Termination requested."

    def _validate_target_claim(self, target_claim: str) -> str | None:
        """Validate target_claim exists. Returns error string or None if valid."""
        import re
        state = self.research_state
        assert state is not None

        match = re.match(r"^(RQ|WH|ER|CRIT)-(\d+)$", target_claim)
        if not match:
            # Unknown prefix — allow through
            return None

        prefix = match.group(1)
        if prefix == "RQ":
            if target_claim in state.research_questions:
                return None
        elif prefix == "CRIT":
            if target_claim in state.critiques:
                return None
        else:
            # WH or ER
            if target_claim in state.hypotheses:
                return None

        # Build entity listing for error message
        valid_rqs = sorted(state.research_questions.keys())
        valid_whs = sorted(h for h in state.hypotheses if h.startswith("WH-"))
        valid_ers = sorted(h for h in state.hypotheses if h.startswith("ER-"))
        valid_crits = sorted(state.critiques.keys())
        entity_list = []
        if valid_rqs:
            entity_list.append(f"RQs: {', '.join(valid_rqs)}")
        if valid_whs:
            entity_list.append(f"WHs: {', '.join(valid_whs)}")
        if valid_ers:
            entity_list.append(f"ERs: {', '.join(valid_ers)}")
        if valid_crits:
            entity_list.append(f"CRITs: {', '.join(valid_crits)}")
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
