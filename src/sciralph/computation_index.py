"""Helpers for reading COMPUTATION_INDEX.jsonl."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import WorkspaceManager


def read_computation_index(workspace: "WorkspaceManager") -> list[dict]:
    """Read all entries from COMPUTATION_INDEX.jsonl."""
    raw = workspace.read_file("COMPUTATION_INDEX.jsonl")
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


def find_verified_target_ids(entries: list[dict]) -> set[str]:
    """Return target_ids that have at least one VERIFIED verify entry."""
    return {
        e["target_id"]
        for e in entries
        if e.get("kind") == "verify" and e.get("verdict") == "VERIFIED" and e.get("target_id")
    }


def find_refuted_target_ids(entries: list[dict]) -> set[str]:
    """Return target_ids that have at least one REFUTED verify entry."""
    return {
        e["target_id"]
        for e in entries
        if e.get("kind") == "verify" and e.get("verdict") == "REFUTED" and e.get("target_id")
    }


def detect_computation_stalls(entries: list[dict], threshold: int = 3) -> list[dict]:
    """Find targets with >= threshold consecutive non-VERIFIED verdicts.

    Only considers verify-kind entries. A VERIFIED verdict resets the streak.
    Returns [{"claim": target_id, "count": int, "verdicts": list[str]}].
    """
    streaks: dict[str, list[str]] = {}
    for entry in entries:
        if entry.get("kind") != "verify":
            continue
        key = entry.get("target_id", "")
        if not key:
            continue
        verdict = entry.get("verdict", "UNKNOWN")
        if verdict == "VERIFIED":
            streaks[key] = []
        else:
            streaks.setdefault(key, []).append(verdict)

    stalls = []
    for key, verdicts in streaks.items():
        if len(verdicts) >= threshold:
            stalls.append({"claim": key, "count": len(verdicts), "verdicts": verdicts})
    return stalls
