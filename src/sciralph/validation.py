"""Post-integration validation and termination gates for SciRalph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .markdown import (
    parse_frontmatter,
    render_frontmatter,
    _parse_comp_entries,
    count_unresolved_critiques,
    count_er_sections,
    find_er_section_ids,
    _ER_WH_ID_RE,
)

if TYPE_CHECKING:
    from .config import Config
    from .metrics import MetricsTracker
    from .workspace import WorkspaceManager


class ViolationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Violation:
    check: str
    severity: ViolationSeverity
    message: str
    file: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Agent routing constants
# ---------------------------------------------------------------------------

_AGENT_ALIASES = {
    "compute": "computationalist",
    "research": "researcher",
    "critique": "deep_critic",
    "review": "deep_critic",
}
_VALID_AGENTS = {"orchestrator", "researcher", "computationalist", "deep_critic", "compressor"}


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

def check_er_promotion_gate(workspace: WorkspaceManager) -> list[Violation]:
    """Demote ER-NNN sections that lack a VERIFIED computation backing."""
    violations: list[Violation] = []
    state = workspace.read_file("RESEARCH_STATE.md")
    comp_log = workspace.read_file("COMPUTATION_LOG.md")
    entries = _parse_comp_entries(comp_log)

    # Find all ER-NNN section entries (H2 headers or **bold** line-start)
    er_ids = find_er_section_ids(state)

    for er_id in er_ids:
        num = er_id.split("-")[1]
        wh_id = f"WH-{num}"
        # Check if any VERIFIED entry mentions ER-NNN or WH-NNN
        # Search both the claim line and the full body (IDs often appear
        # on bullet lines below the claim, not on the claim line itself)
        has_verified = any(
            e["verdict"] == "VERIFIED" and (
                er_id in e["claim"] or wh_id in e["claim"]
                or er_id in e.get("body", "") or wh_id in e.get("body", "")
            )
            for e in entries
        )
        if not has_verified:
            # Demote ER back to WH — handle both ## header and **bold** formats
            state = re.sub(
                rf'^(#{{2,3}} |(?:\*\*)){re.escape(er_id)}',
                rf'\g<1>{wh_id}',
                state,
                count=1,
                flags=re.MULTILINE,
            )
            violations.append(Violation(
                check="er_promotion_gate",
                severity=ViolationSeverity.ERROR,
                message=f"{er_id} has no VERIFIED computation backing — demoted to {wh_id}",
                file="RESEARCH_STATE.md",
                detail=er_id,
            ))

    if violations:
        workspace.write_file("RESEARCH_STATE.md", state)

    return violations


def check_task_agent_routing(workspace: WorkspaceManager) -> list[Violation]:
    """Validate and fix the assigned_to field in CURRENT_TASK.md."""
    violations: list[Violation] = []
    task_text = workspace.read_file("CURRENT_TASK.md")
    if not task_text:
        return []

    meta, body = parse_frontmatter(task_text)
    assigned = meta.get("assigned_to", "")

    if assigned in _VALID_AGENTS:
        return []

    if assigned in _AGENT_ALIASES:
        correct = _AGENT_ALIASES[assigned]
        meta["assigned_to"] = correct
        workspace.write_file("CURRENT_TASK.md", render_frontmatter(meta, body))
        violations.append(Violation(
            check="task_agent_routing",
            severity=ViolationSeverity.WARNING,
            message=f"Alias '{assigned}' resolved to '{correct}'",
            file="CURRENT_TASK.md",
        ))
    else:
        violations.append(Violation(
            check="task_agent_routing",
            severity=ViolationSeverity.ERROR,
            message=f"Unknown assigned_to '{assigned}'",
            file="CURRENT_TASK.md",
        ))

    return violations


def check_phantom_labels(workspace: WorkspaceManager) -> list[Violation]:
    """Strip unsubstantiated VERIFIED labels from state and proposed changes."""
    violations: list[Violation] = []
    comp_log = workspace.read_file("COMPUTATION_LOG.md")
    entries = _parse_comp_entries(comp_log)
    verified_claims = {e["claim"] for e in entries if e["verdict"] == "VERIFIED"}

    for filename in ("RESEARCH_STATE.md", "PROPOSED_CHANGES.md"):
        text = workspace.read_file(filename)
        if not text:
            continue
        # Find lines containing "VERIFIED" that aren't in ## headers
        changed = False
        new_lines = []
        for line in text.splitlines():
            if "VERIFIED" in line and not line.strip().startswith("##"):
                # Check if any verified claim's ER/WH ID is referenced in this line
                ids_in_line = _ER_WH_ID_RE.findall(line)
                if ids_in_line:
                    # Check if any of these IDs appear in verified claims
                    backed = any(
                        any(id_ in claim for claim in verified_claims)
                        for id_ in ids_in_line
                    )
                    if not backed:
                        line = line.replace("VERIFIED", "[unverified]")
                        changed = True
                        violations.append(Violation(
                            check="phantom_labels",
                            severity=ViolationSeverity.ERROR,
                            message="Unsubstantiated VERIFIED label stripped",
                            file=filename,
                            detail=", ".join(ids_in_line),
                        ))
                # If no IDs in the line but VERIFIED appears, leave it (could be prose)
            new_lines.append(line)

        if changed:
            workspace.write_file(filename, "\n".join(new_lines))

    return violations


def check_phantom_references(workspace: WorkspaceManager) -> list[Violation]:
    """Replace orphaned COMP/TASK references in RESEARCH_STATE.md."""
    violations: list[Violation] = []
    state = workspace.read_file("RESEARCH_STATE.md")
    comp_log = workspace.read_file("COMPUTATION_LOG.md")

    entries = _parse_comp_entries(comp_log)
    valid_ids = {e["id"] for e in entries}

    ref_pattern = re.compile(r'\b((?:COMP|TASK)-\d+)\b')
    found_refs = set(ref_pattern.findall(state))

    phantoms = sorted(found_refs - valid_ids)
    if not phantoms:
        return []

    for phantom in phantoms:
        state = state.replace(phantom, f"[{phantom}:unverified]")
        violations.append(Violation(
            check="phantom_references",
            severity=ViolationSeverity.ERROR,
            message=f"Phantom reference {phantom} replaced with [{phantom}:unverified]",
            file="RESEARCH_STATE.md",
            detail=phantom,
        ))

    workspace.write_file("RESEARCH_STATE.md", state)
    return violations


def check_id_consistency(workspace: WorkspaceManager) -> list[Violation]:
    """Fix total_computations frontmatter counter if it disagrees with actual entries."""
    violations: list[Violation] = []
    comp_log = workspace.read_file("COMPUTATION_LOG.md")
    if not comp_log:
        return []

    meta, body = parse_frontmatter(comp_log)
    entries = _parse_comp_entries(comp_log)
    actual_count = len(entries)
    recorded_count = meta.get("total_computations", 0)

    if actual_count != recorded_count:
        meta["total_computations"] = actual_count
        workspace.write_file("COMPUTATION_LOG.md", render_frontmatter(meta, body))
        violations.append(Violation(
            check="id_consistency",
            severity=ViolationSeverity.WARNING,
            message=f"Computation count mismatch: frontmatter={recorded_count}, actual={actual_count}",
            file="COMPUTATION_LOG.md",
        ))

    return violations


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_DEFAULT_CHECKS = [
    check_phantom_references,
    check_er_promotion_gate,
    check_phantom_labels,
    check_task_agent_routing,
    check_id_consistency,
]


def validate_post_integration(workspace: WorkspaceManager, config: Config | None = None) -> list[Violation]:
    """Run all post-integration invariant checks. Returns violations to inject into next orchestrator context."""
    violations: list[Violation] = []
    for check in _DEFAULT_CHECKS:
        violations.extend(check(workspace))
    return violations


# ---------------------------------------------------------------------------
# Termination gate
# ---------------------------------------------------------------------------

def can_terminate(workspace: WorkspaceManager, config: Config, metrics: MetricsTracker, problem_meta: dict | None = None) -> tuple[bool, list[str]]:
    """Check preconditions before allowing the research loop to exit.

    Returns (allowed, blockers) where blockers is a list of human-readable
    reasons the loop must continue.
    """
    blockers: list[str] = []
    state = workspace.read_file("RESEARCH_STATE.md")
    comp_log = workspace.read_file("COMPUTATION_LOG.md")

    er_count = count_er_sections(state)

    # Gate 1: At least one critic pass if ERs exist
    if er_count > 0 and metrics.last_critic_iteration == 0:
        blockers.append(
            "No critic pass has occurred yet. "
            "Emit task_type: critique to run a review before terminating."
        )

    # Gate 2: No unresolved HIGH critiques
    critique_log = workspace.read_file("CRITIQUE_LOG.md")
    crit_counts = count_unresolved_critiques(critique_log)
    if crit_counts.get("HIGH", 0) > 0:
        blockers.append(
            f"{crit_counts['HIGH']} unresolved HIGH critique(s). "
            "Emit task_type: resolve to address them."
        )

    # Gate 3: At least one computation when problem requires it
    meta = problem_meta or {}
    if meta.get("requires_numerical", False):
        entries = _parse_comp_entries(comp_log)
        if len(entries) == 0:
            blockers.append(
                "Problem requires numerical verification but COMPUTATION_LOG has 0 entries. "
                "You MUST emit task_type: compute (assigned_to: computationalist) "
                "to run at least one numerical verification before terminating."
            )

    return (len(blockers) == 0, blockers)
