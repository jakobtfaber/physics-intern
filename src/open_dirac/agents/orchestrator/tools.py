"""Orchestrator state-mutation tools.

The orchestrator uses these tools to mutate the ResearchState object.
Markdown files are rendered from state in process_response().
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar

from open_dirac.console import console
from open_dirac.tool_call import ToolCall
from open_dirac.utils.categories import CompensationCategory as CC
from open_dirac.workspace import log_scaffold_event

if TYPE_CHECKING:
    from open_dirac.research_state import ResearchState
    from open_dirac.workspace import WorkspaceManager


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
            "name": "append_convention",
            "description": (
                "Add new convention entries to the Conventions section. "
                "Only include NEW items — existing conventions are preserved automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "New convention entries to append. "
                            "Do NOT repeat existing conventions."
                        ),
                    },
                },
                "required": ["content"],
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
            "name": "abandon_research_question",
            "description": (
                "Mark a research question as a dead end. "
                "Use when the gathered evidence is inconclusive or the "
                "question turned out to be ill-posed. "
                "(RQs answered via add_hypothesis are auto-resolved.)"
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
                        "description": "Why this RQ is being abandoned.",
                    },
                },
                "required": ["id", "reason"],
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
                            "The RQ or WH ID this task targets "
                            "(e.g. 'RQ-001', 'WH-003')."
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
                            "Task-specific context the agent cannot see on its own. "
                            "The agent already receives: problem summary, conventions, "
                            "and all established results. "
                            "Do NOT repeat those. Instead provide: (1) relevant parts of "
                            "the survey background or known methods, (2) strategic context — "
                            "why this task matters and how it fits the plan, (3) relevant "
                            "research notes or observations from prior iterations, "
                            "(4) inter-entity connections the agent should be aware of. "
                            "The agent has NO access to strategy, research notes, or "
                            "survey — relay the relevant parts here."
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
                    "recommended_sanity_checks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Sanity checks the agent should verify its result against. "
                            "Pick from the global sanity-checks list or write task-specific "
                            "ones (e.g. 'result must reduce to X in the Y limit')."
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
                            "The RQ or WH ID this task targets "
                            "(e.g. 'RQ-001', 'WH-003')."
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
                            "Task-specific context the agent cannot see on its own. "
                            "The agent already receives: problem summary, conventions, "
                            "and all established results. "
                            "Do NOT repeat those. Instead provide: (1) relevant parts of "
                            "the survey background or known methods, (2) strategic context — "
                            "why this task matters and how it fits the plan, (3) relevant "
                            "research notes or observations from prior iterations, "
                            "(4) inter-entity connections the agent should be aware of. "
                            "The agent has NO access to strategy, research notes, or "
                            "survey — relay the relevant parts here."
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
                    "recommended_sanity_checks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Sanity checks the agent should verify its result against. "
                            "Pick from the global sanity-checks list or write task-specific "
                            "ones (e.g. 'result must reduce to X in the Y limit')."
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
        max_open_rqs: int = 1,
        rq_evidence_cap: int = 3,
        max_refuted_retries: int = 2,
    ):
        self.workspace = workspace
        self.iteration = iteration
        self.research_state = research_state
        self.mutations_applied: bool = False
        self._calls_this_round: int = 0
        self._round_mutations: list[str] = []
        self.task_data: dict | None = None
        self.stop_after_round: bool = False
        self._min_er_for_completion = min_er_for_completion
        self._max_iterations = max_iterations
        self._budget_synthesis_margin = budget_synthesis_margin
        self._max_open_rqs = max_open_rqs
        self._rq_evidence_cap = rq_evidence_cap
        self._max_refuted_retries = max_refuted_retries

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

            # Conditional guidance
            lines.append("")
            lines.append("Pending:")
            guidance = self._build_guidance(state, ers, whs, open_rqs)
            for g in guidance:
                lines.append(f"- {g}")

        lines.append("──")
        return "\n".join(lines)

    def _build_guidance(
        self, state, ers: list, whs: list, open_rqs: list,
    ) -> list[str]:
        """Build conditional guidance lines for the state injection."""
        from open_dirac.research_state import HypothesisStatus, Verdict
        guidance: list[str] = []

        er_count = len(ers)
        wh_count = len(whs)

        # Budget pressure
        budget_remaining = self._max_iterations - self.iteration
        if budget_remaining <= self._budget_synthesis_margin and er_count >= 1:
            guidance.append(
                f"BUDGET: Only {budget_remaining} iteration(s) remaining "
                f"(iteration {self.iteration} of {self._max_iterations}). "
                "Synthesize results now; note unresolved items as limitations."
            )

        # All resolved → may terminate
        if er_count >= self._min_er_for_completion and wh_count == 0:
            open_rq_count = len(open_rqs)
            if open_rq_count == 0:
                guidance.append("All entities resolved. You may terminate.")
            else:
                guidance.append(
                    f"All WHs promoted. {open_rq_count} open RQ(s) remain — "
                    "resolve or abandon them before terminating."
                )

        # Per-WH guidance
        for h in whs:
            if h.review and h.review.verdict == Verdict.VERIFIED:
                unest = state.unestablished_dependencies(h.id)
                if unest:
                    guidance.append(
                        f"{h.id} is VERIFIED, pending auto-promotion "
                        f"(unestablished deps: {', '.join(unest)})."
                    )
                else:
                    guidance.append(f"{h.id} is VERIFIED, pending auto-promotion.")
            elif h.review and h.review.verdict == Verdict.REFUTED:
                rc = h.refuted_count
                cap = self._max_refuted_retries
                if rc >= cap:
                    guidance.append(
                        f"{h.id} REFUTED {rc} time(s) (limit reached) — "
                        "you MUST abandon it (abandon_hypothesis)."
                    )
                else:
                    guidance.append(
                        f"{h.id} was REFUTED (attempt {rc}/{cap}) — "
                        "dispatch new evidence or abandon."
                    )
            elif h.evidence and not h.review:
                guidance.append(f"{h.id} awaiting auto-review.")

        # Per-RQ with evidence
        for rq in open_rqs:
            if rq.evidence:
                guidance.append(f"{rq.id} has evidence — formulate a WH (add_hypothesis with from_rq).")

        # Default closing
        guidance.append("When ready, call a dispatch tool (dispatch_researcher, dispatch_computer, or request_termination) — or add_hypothesis to formulate a WH (auto-triggers review).")
        return guidance

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        start = time.time()
        self._calls_this_round += 1

        handlers = {
            "add_hypothesis": self._add_hypothesis,
            "abandon_hypothesis": self._abandon_hypothesis,
            "append_convention": self._append_convention,
            "append_note": self._append_note,
            "add_research_question": self._add_research_question,
            "abandon_research_question": self._abandon_research_question,
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
        # Block a second exit tool from overwriting the first dispatch
        if tool_name in self.exit_tool_names and self.stop_after_round:
            return ToolCall(
                tool_name=tool_name, tool_input=tool_input,
                output=(
                    f"Error: an exit tool has already been called this round "
                    f"(task_data={self.task_data}). Only one exit tool per "
                    f"turn is allowed. This call was ignored."
                ),
                is_error=True,
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
        from open_dirac.research_state import CritiqueStatus, Hypothesis, HypothesisStatus, RQStatus, Severity

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
        blocking = [c for c in state.critiques.values()
                    if c.status == CritiqueStatus.ACTIVE and c.severity == Severity.HIGH]
        if blocking:
            return (
                f"Error: blocked — {len(blocking)} pending strategic review(s). "
                "Wait for the system to resolve them before creating new WHs."
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

    def _abandon_hypothesis(self, args: dict) -> str:
        from open_dirac.research_state import FailedApproach, HypothesisStatus

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

    def _append_convention(self, args: dict) -> str:
        state = self.research_state
        if not state:
            return "Error: no research state available"

        new = args.get("content", "").strip()
        if not new:
            return "Error: convention content cannot be empty"

        state.conventions = (state.conventions.rstrip() + "\n\n" + new) if state.conventions else new

        self.mutations_applied = True
        self._round_mutations.append("Appended conventions")
        console.print("  [dim]Appended conventions[/]")
        return "Appended to Conventions."

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
        from open_dirac.research_state import CritiqueStatus, ResearchQuestion, Severity

        state = self.research_state
        if not state:
            return "Error: no research state available"

        # Cap: block if too many open RQs or unresolved HIGH critiques
        open_rqs = state.open_research_questions()
        blocking = [c for c in state.critiques.values()
                    if c.status == CritiqueStatus.ACTIVE and c.severity == Severity.HIGH]
        if len(open_rqs) >= self._max_open_rqs:
            ids = ", ".join(rq.id for rq in open_rqs)
            return (
                f"Error: already {len(open_rqs)} open RQ(s) ({ids}), limit is {self._max_open_rqs}. "
                "You should first take care of them. "
                "You can either: "
                "- Turn an existing RQ into a Working Hypothesis using `add_hypothesis`, provided the RQ has at least 1 evidence. "
                "- Abandon an RQ using `abandon_research_question`. "
                "- Keep working on an existing RQ by using `dispatch_researcher` or `dispatch_computer` to gather more evidence on it."
            )
        if blocking:
            return (
                f"Error: blocked — {len(blocking)} pending strategic review(s). "
                "Wait for the system to resolve them before creating new RQs."
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

    def _abandon_research_question(self, args: dict) -> str:
        from open_dirac.research_state import RQStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        rq_id = args["id"]
        reason = args["reason"]

        if rq_id not in state.research_questions:
            return f"Error: {rq_id} not found in research state"

        rq = state.research_questions[rq_id]
        if rq.status == RQStatus.RESOLVED:
            return f"Error: {rq_id} is already resolved (promoted to WH). Cannot abandon."
        if rq.status == RQStatus.ABANDONED:
            return f"{rq_id} is already abandoned (iteration {rq.iteration_resolved})."
        rq.status = RQStatus.ABANDONED
        rq.iteration_resolved = self.iteration
        rq.resolution_reason = reason
        self.mutations_applied = True
        self._round_mutations.append(f"Abandoned {rq_id}")
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "abandon_research_question", f"{rq_id}: {reason}",
        )
        console.print(f"  [dim]{rq_id}[/] abandoned — {reason[:60]}")
        return f"Abandoned {rq_id}."

    def _dispatch_researcher(self, args: dict) -> str:
        target = args["target_claim"]
        if self.research_state:
            err = self._validate_target_claim(target)
            if err is not None:
                return err
            err = self._check_focus(target)
            if err is not None:
                return err
            err = self._check_saturated_rqs()
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
            err = self._check_focus(target)
            if err is not None:
                return err
            err = self._check_saturated_rqs()
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
        """Validate target_claim exists and is a valid dispatch target.

        Valid targets: open RQs, working WHs (including refuted).
        Blocked: ERs (immutable), resolved/abandoned RQs.
        Returns error string or None if valid.
        """
        import re
        state = self.research_state
        assert state is not None

        match = re.match(r"^(RQ|WH|ER)-(\d+)$", target_claim)
        if not match:
            # Unknown prefix — allow through
            return None

        prefix = match.group(1)
        if prefix == "ER":
            return (
                f"Error: cannot dispatch work on {target_claim} — Established Results "
                "are immutable. If you suspect an ER is wrong, note your concern in "
                "research notes; the strategic auditor will evaluate it."
            )
        if prefix == "RQ":
            rq = state.research_questions.get(target_claim)
            if rq:
                from open_dirac.research_state import RQStatus
                if rq.status != RQStatus.OPEN:
                    return (
                        f"Error: {target_claim} is {rq.status.value} and cannot receive "
                        "new evidence. Create a new RQ with add_research_question if "
                        "further investigation is needed."
                    )
                return None
        else:
            # WH — always valid if it exists (including refuted WHs awaiting new evidence)
            if target_claim in state.hypotheses:
                return None

        # Build entity listing for error message
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

    def _check_saturated_rqs(self) -> str | None:
        """Block dispatch if any open RQ has >= rq_evidence_cap non-refuted evidence.

        Returns an error string listing the saturated RQs, or None if all clear.
        """
        state = self.research_state
        if state is None:
            return None
        cap = self._rq_evidence_cap
        saturated: list[tuple[str, int]] = []
        for rq in state.open_research_questions():
            active = sum(1 for e in rq.evidence if not e.refuted)
            if active >= cap:
                saturated.append((rq.id, active))
        if not saturated:
            return None
        listing = ", ".join(f"{rid} ({n} evidence)" for rid, n in saturated)
        return (
            f"Error: dispatch blocked — saturated RQ(s): {listing}. "
            f"RQs with >= {cap} evidence items must be resolved before dispatching new work. "
            "Either promote to a Working Hypothesis (add_hypothesis) for adversarial review, "
            "or abandon (abandon_research_question) if the evidence is inconclusive."
        )

    def _check_focus(self, target: str) -> str | None:
        """Enforce serial RQ focus and dangling WH resolution.

        Rule 1: Block dispatch to a WH that exceeded the refuted retry cap.
        Rule 2: Dangling WHs (REFUTED/INCONCLUSIVE) block dispatch to any RQ.
        Rule 3: Serial RQ focus — only one RQ may have evidence at a time.

        Returns error string or None if valid.
        """
        from open_dirac.research_state import Verdict

        state = self.research_state
        if state is None:
            return None

        # Rule 1: refuted retry cap
        if target.startswith("WH-"):
            h = state.hypotheses.get(target)
            if h and h.refuted_count >= self._max_refuted_retries:
                return (
                    f"Error: dispatch blocked — {target} has been refuted "
                    f"{h.refuted_count} time(s) (limit={self._max_refuted_retries}). "
                    f"You must abandon it: call abandon_hypothesis(id=\"{target}\", reason=\"...\")."
                )

        # Rules 2 & 3 apply only to RQ targets
        if target.startswith("RQ-"):
            # Rule 2: dangling WHs block RQ dispatch
            dangling = []
            for h in state.working_hypotheses():
                if h.review and h.review.verdict in (Verdict.REFUTED, Verdict.INCONCLUSIVE):
                    dangling.append(f"{h.id} ({h.review.verdict})")
            if dangling:
                listing = ", ".join(dangling)
                return (
                    f"Error: dispatch to {target} blocked — unresolved WH(s) need "
                    f"attention first: {listing}. "
                    "Dispatch evidence to them or abandon them before working on RQs."
                )

            # Rule 3: serial RQ focus
            other_with_evidence = []
            for rq in state.open_research_questions():
                if rq.id != target and rq.evidence:
                    other_with_evidence.append(
                        f"{rq.id} ({len(rq.evidence)} evidence)"
                    )
            if other_with_evidence:
                listing = ", ".join(other_with_evidence)
                return (
                    f"Error: dispatch to {target} blocked — serial RQ focus: "
                    f"finish {listing} first "
                    "(promote to WH or abandon) before dispatching to another RQ."
                )

        return None
