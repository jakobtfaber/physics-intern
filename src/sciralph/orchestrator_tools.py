"""Orchestrator state-mutation tools.

The orchestrator uses these tools to mutate the ResearchState object.
Markdown files are rendered from state in process_response().
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar

from .tools import ToolCall
from .categories import CompensationCategory as CC
from .workspace import log_scaffold_event

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
                "Mark a research question as resolved. "
                "Provide the WH IDs that the question resolved into."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Research question ID, e.g. RQ-001.",
                    },
                    "resolved_to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "WH/ER IDs this question resolved into.",
                    },
                },
                "required": ["id", "resolved_to"],
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
                            "research_explore",
                            "compute_explore", "compute_verify",
                            "research_verify",
                            "critique", "terminate",
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

    exit_tool_name: str = "set_next_task"

    def __init__(self, workspace: WorkspaceManager, iteration: int, research_state: ResearchState | None = None):
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
            "add_research_question": self._add_research_question,
            "resolve_research_question": self._resolve_research_question,
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
        else:
            num = state.next_entity_num()
            new_id = f"WH-{num:03d}"

        state.hypotheses[new_id] = Hypothesis(
            id=new_id,
            statement=statement,
            status=HypothesisStatus.WORKING,
            derivation=derivation,
            depends_on=depends_on,
            iteration_created=self.iteration,
            iteration_modified=self.iteration,
        )
        self.mutations_applied = True
        return f"Added {new_id} — {statement}"

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
        return f"Updated {hid}"

    def _abandon_hypothesis(self, args: dict) -> str:
        from .research_state import FailedApproach, HypothesisStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        hid = args["id"]
        reason = args.get("reason", "No longer viable")

        if hid not in state.hypotheses:
            return f"Error: {hid} not found"

        h = state.hypotheses[hid]
        title = h.statement or hid

        h.status = HypothesisStatus.ABANDONED
        h.iteration_modified = self.iteration

        state.failed_approaches.append(FailedApproach(
            description=f"Abandoned {hid} — {title}",
            reason=reason,
            iteration=self.iteration,
        ))

        self.mutations_applied = True
        return f"Abandoned {hid}: {reason}"

    def _promote_hypothesis(self, args: dict) -> str:
        from .research_state import HypothesisStatus, Severity, CritiqueStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        wh_id = args["id"]
        justification = args.get("justification", "")

        if not wh_id.startswith("WH-"):
            return f"Error: {wh_id} is not a WH. Only WH-NNN can be promoted."

        if wh_id not in state.hypotheses:
            return f"Error: {wh_id} not found in research state"

        num = wh_id.split("-")[1]
        er_id = f"ER-{num}"

        # Guardrail: check for REFUTED without VERIFIED
        refuted = state.refuted_targets()
        has_refuted = wh_id in refuted or er_id in refuted
        has_verified = state.has_verified_backing(wh_id) or state.has_verified_backing(er_id)

        if has_refuted and not has_verified:
            return (
                f"Error: Cannot promote {wh_id} — a REFUTED computation exists "
                "with no superseding VERIFIED computation."
            )

        # Guardrail: check for unresolved HIGH critiques
        for c in state.critiques.values():
            if (c.severity == Severity.HIGH
                    and c.status == CritiqueStatus.ACTIVE
                    and (wh_id in c.targets or er_id in c.targets)):
                return (
                    f"Error: Cannot promote {wh_id} — unresolved HIGH "
                    f"critique {c.id} targets this claim."
                )

        # Guardrail: check for unestablished dependencies
        unestablished = state.unestablished_dependencies(wh_id)
        if unestablished:
            return (
                f"Error: Cannot promote {wh_id} — unestablished dependencies: "
                f"{', '.join(unestablished)}. Promote or resolve them first."
            )

        # Guardrail: require verification evidence
        verify_kinds = {"verify", "research_verify"}
        from .research_state import Verdict as _V
        has_verification = any(
            c.target_hypothesis in (wh_id, er_id)
            and c.kind in verify_kinds
            and c.verdict == _V.VERIFIED
            for c in state.computations.values()
        )
        if not has_verification:
            return (
                f"Error: Cannot promote {wh_id} — no VERIFIED computation "
                "with kind 'verify' or 'research_verify' exists. "
                "Schedule a compute_verify or research_verify task first."
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

        self.mutations_applied = True
        return f"Promoted {wh_id} → {er_id}"

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
        c.status = CritiqueStatus.RESOLVED
        c.resolution = resolution
        c.iteration_resolved = self.iteration

        self.resolved_critique_ids.add(crit_id)
        self.mutations_applied = True

        return f"Resolved {crit_id}"

    def _update_section(self, args: dict) -> str:
        state = self.research_state
        if not state:
            return "Error: no research state available"

        section_name = args["section"]
        content = args.get("content", "")

        if section_name == "Conventions":
            state.conventions = content.strip()
        elif section_name == "Open Questions":
            state.open_questions = content.strip()
        elif section_name == "Dead Ends":
            # Dead Ends is a special case — content is free-form text
            # We don't parse it into FailedApproach objects here
            pass
        else:
            return f"Error: unknown section '{section_name}'"

        self.mutations_applied = True
        return f"Updated # {section_name}"

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
        return f"Added {rq_id} — {question}"

    def _resolve_research_question(self, args: dict) -> str:
        from .research_state import RQStatus

        state = self.research_state
        if not state:
            return "Error: no research state available"

        rq_id = args["id"]
        resolved_to = args.get("resolved_to", [])

        if rq_id not in state.research_questions:
            return f"Error: {rq_id} not found in research state"

        rq = state.research_questions[rq_id]
        rq.status = RQStatus.RESOLVED
        rq.resolved_to = resolved_to
        rq.iteration_resolved = self.iteration
        self.mutations_applied = True
        return f"Resolved {rq_id} → {', '.join(resolved_to)}"

    def _set_next_task(self, args: dict) -> str:
        self.task_data = args
        self.stop_after_round = True
        return f"Task set: {args.get('task_type', '?')}"
