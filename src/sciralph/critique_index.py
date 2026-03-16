"""Helpers for reading and writing CRITIQUE_INDEX.jsonl."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import WorkspaceManager


def read_critique_index(workspace: "WorkspaceManager") -> list[dict]:
    """Read all entries from CRITIQUE_INDEX.jsonl."""
    raw = workspace.read_file("CRITIQUE_INDEX.jsonl")
    entries = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def get_critique_status(entries: list[dict]) -> dict[str, dict]:
    """Build a dict of current status per critique ID (last entry wins)."""
    status: dict[str, dict] = {}
    for entry in entries:
        crit_id = entry.get("id", "")
        if crit_id:
            status[crit_id] = entry
    return status


def count_unresolved_by_severity(entries: list[dict]) -> dict[str, int]:
    """Count unresolved critiques by severity from JSONL entries."""
    status = get_critique_status(entries)
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for entry in status.values():
        if entry.get("status") == "UNRESOLVED":
            sev = entry.get("severity", "").upper()
            if sev in counts:
                counts[sev] += 1
    return counts


def has_unresolved_high_for_target(entries: list[dict], target_id: str) -> bool:
    """Check if any unresolved HIGH critique targets the given ID."""
    status = get_critique_status(entries)
    num = target_id.split("-")[1] if "-" in target_id else ""
    wh_form = f"WH-{num}"
    er_form = f"ER-{num}"
    for entry in status.values():
        if (entry.get("status") == "UNRESOLVED"
                and entry.get("severity", "").upper() == "HIGH"
                and entry.get("target_id") in (target_id, wh_form, er_form)):
            return True
    return False
