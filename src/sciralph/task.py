"""Task dataclass for typed task handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .markdown import parse_frontmatter

import yaml


class TaskType(StrEnum):
    RESEARCH_EXPLORE = "research_explore"
    COMPUTE = "compute"
    COMPUTE_EXPLORE = "compute_explore"
    COMPUTE_VERIFY = "compute_verify"
    RESEARCH_VERIFY = "research_verify"
    CRITIQUE = "critique"
    TERMINATE = "terminate"
    FORMAT = "format"


TASK_TYPE_AGENT_MAP: dict[TaskType, str] = {
    TaskType.RESEARCH_EXPLORE: "research_explore",
    TaskType.COMPUTE: "compute_verify",
    TaskType.COMPUTE_EXPLORE: "compute_explore",
    TaskType.COMPUTE_VERIFY: "compute_verify",
    TaskType.RESEARCH_VERIFY: "research_verify",
    TaskType.CRITIQUE: "deep_critic",
    TaskType.TERMINATE: "orchestrator",
    TaskType.FORMAT: "formatter",
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
    target_file: str = ""
    target_claim: str = ""
    body: str = ""

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
        if self.target_file:
            meta["target_file"] = self.target_file
        if self.target_claim:
            meta["target_claim"] = self.target_claim
        yaml_str = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
        return f"---\n{yaml_str}\n---\n\n{self.body}"

    @classmethod
    def from_frontmatter(cls, text: str, fallback_iteration: int = 0) -> Task:
        """Parse from YAML frontmatter text."""
        meta, body = parse_frontmatter(text)
        effective_iter = meta.get("iteration", fallback_iteration) or fallback_iteration
        raw_type = meta.get("task_type", "research_explore")
        try:
            task_type = TaskType(raw_type)
        except ValueError:
            task_type = TaskType.RESEARCH_EXPLORE
        return cls(
            task_id=meta.get("task_id", f"TASK-{effective_iter:03d}"),
            task_type=task_type,
            assigned_to=meta.get("assigned_to", "research_explore") or "research_explore",
            priority=meta.get("priority", "medium"),
            iteration=effective_iter,
            blocking_critiques=meta.get("blocking_critiques", []),
            target_file=meta.get("target_file", ""),
            target_claim=meta.get("target_claim", ""),
            body=body,
        )
