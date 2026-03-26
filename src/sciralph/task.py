"""Task dataclass for typed task handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .markdown import parse_frontmatter

import yaml


class TaskType(StrEnum):
    RESEARCH = "research"
    COMPUTE = "compute"
    REVIEW = "review"
    CRITIQUE = "critique"
    TERMINATE = "terminate"
    FORMAT = "format"
    SURVEY = "survey"
    PLAN = "plan"


TASK_TYPE_AGENT_MAP: dict[TaskType, str] = {
    TaskType.RESEARCH: "researcher",
    TaskType.COMPUTE: "computer",
    TaskType.REVIEW: "reviewer",
    TaskType.CRITIQUE: "deep_critic",
    TaskType.TERMINATE: "orchestrator",
    TaskType.FORMAT: "formatter",
    TaskType.SURVEY: "surveyor",
    TaskType.PLAN: "planner",
}


@dataclass
class Task:
    """Typed representation of a CURRENT_TASK.md task."""

    task_id: str
    task_type: TaskType
    assigned_to: str
    priority: str = "medium"
    iteration: int = 0
    blocking_critiques: list[str] = field(default_factory=list)
    target_claim: str = ""
    body: str = ""
    # Structured dispatch context (populated by orchestrator dispatch tools)
    background: str = ""
    method_hints: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    relevant_results: list[str] = field(default_factory=list)
    # Termination context (populated by request_termination)
    answer_ers: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Render as YAML frontmatter + body Markdown."""
        meta = {
            "task_id": self.task_id,
            "task_type": self.task_type.value,
            "assigned_to": self.assigned_to,
            "priority": self.priority,
            "iteration": self.iteration,
        }
        if self.blocking_critiques:
            meta["blocking_critiques"] = self.blocking_critiques
        if self.target_claim:
            meta["target_claim"] = self.target_claim
        if self.background:
            meta["background"] = self.background
        if self.method_hints:
            meta["method_hints"] = self.method_hints
        if self.assumptions:
            meta["assumptions"] = self.assumptions
        if self.relevant_results:
            meta["relevant_results"] = self.relevant_results
        yaml_str = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
        return f"---\n{yaml_str}\n---\n\n{self.body}"

    def render_agent_context(self, include_structured: bool = True) -> str:
        """Render task for agent context (no YAML metadata, no file read-back).

        Args:
            include_structured: If True, include background/method_hints/assumptions/
                relevant_results.  Verifier passes False to avoid biasing.
        """
        parts: list[str] = []
        if self.body:
            parts.append(self.body)
        if include_structured:
            if self.background:
                parts.append(f"<background>\n{self.background}\n</background>")
            if self.method_hints:
                hints = "\n".join(f"- {h}" for h in self.method_hints)
                parts.append(f"<method-hints>\n{hints}\n</method-hints>")
            if self.assumptions:
                items = "\n".join(f"- {a}" for a in self.assumptions)
                parts.append(f"<assumptions>\n{items}\n</assumptions>")
            if self.relevant_results:
                items = "\n".join(f"- {r}" for r in self.relevant_results)
                parts.append(f"<relevant-results>\n{items}\n</relevant-results>")
        else:
            # Reviewer: include background only (orchestrator doubt context)
            if self.background:
                parts.append(f"<background>\n{self.background}\n</background>")
        return "\n\n".join(parts)

    @classmethod
    def from_frontmatter(cls, text: str, fallback_iteration: int = 0) -> Task:
        """Parse from YAML frontmatter text."""
        meta, body = parse_frontmatter(text)
        effective_iter = meta.get("iteration", fallback_iteration) or fallback_iteration
        raw_type = meta.get("task_type", "research")
        try:
            task_type = TaskType(raw_type)
        except ValueError:
            task_type = TaskType.RESEARCH
        return cls(
            task_id=meta.get("task_id", f"TASK-{effective_iter:03d}"),
            task_type=task_type,
            assigned_to=meta.get("assigned_to", "researcher") or "researcher",
            priority=meta.get("priority", "medium"),
            iteration=effective_iter,
            blocking_critiques=meta.get("blocking_critiques", []),
            target_claim=meta.get("target_claim", ""),
            body=body,
            background=meta.get("background", ""),
            method_hints=meta.get("method_hints", []),
            assumptions=meta.get("assumptions", []),
            relevant_results=meta.get("relevant_results", []),
        )
