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
    flatten_unverified_brackets,
    _ER_WH_ID_RE,
    _WH_SECTION_RE,
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
# TASK→COMP mapping helper
# ---------------------------------------------------------------------------

def _build_task_comp_mapping(entries: list[dict]) -> dict[str, set[str]]:
    """Build TASK-NNN → {COMP-NNN, ...} mapping from computation log entries.

    - For TASK-NNN entries: assume COMP-NNN with same number is the product.
    - For COMP-NNN entries: scan body for TASK-NNN references.
    """
    mapping: dict[str, set[str]] = {}
    comp_ids = {e["id"] for e in entries if e["id"].startswith("COMP-")}
    for entry in entries:
        eid = entry["id"]
        if eid.startswith("TASK-"):
            num = eid.split("-")[1]
            comp_equiv = f"COMP-{num}"
            if comp_equiv in comp_ids:
                mapping.setdefault(eid, set()).add(comp_equiv)
        elif eid.startswith("COMP-"):
            # Scan body for TASK-NNN references
            task_refs = re.findall(r'\bTASK-\d+\b', entry.get("body", ""))
            for tref in task_refs:
                mapping.setdefault(tref, set()).add(eid)
    return mapping


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

def check_er_promotion_gate(workspace: WorkspaceManager) -> list[Violation]:
    """Demote ER-NNN sections that lack a VERIFIED computation backing,
    and promote WH-NNN headers when the body already uses ER-NNN with VERIFIED backing."""
    violations: list[Violation] = []
    state = workspace.read_file("RESEARCH_STATE.md")
    comp_log = workspace.read_file("COMPUTATION_LOG.md")
    entries = _parse_comp_entries(comp_log)
    changed = False

    # --- Pass 1: Demote unverified ERs to WHs ---
    er_ids = find_er_section_ids(state)

    for er_id in er_ids:
        num = er_id.split("-")[1]
        wh_id = f"WH-{num}"
        has_verified = any(
            e["verdict"] == "VERIFIED" and (
                er_id in e["claim"] or wh_id in e["claim"]
                or er_id in e.get("body", "") or wh_id in e.get("body", "")
            )
            for e in entries
        )
        if not has_verified:
            state = re.sub(
                rf'^(#{{2,3}} |(?:\*\*)){re.escape(er_id)}',
                rf'\g<1>{wh_id}',
                state,
                count=1,
                flags=re.MULTILINE,
            )
            changed = True
            violations.append(Violation(
                check="er_promotion_gate",
                severity=ViolationSeverity.ERROR,
                message=f"{er_id} has no VERIFIED computation backing — demoted to {wh_id}",
                file="RESEARCH_STATE.md",
                detail=er_id,
            ))

    # --- Pass 2: Promote WH-NNN headers when ER-NNN already in body with VERIFIED backing ---
    wh_ids = _WH_SECTION_RE.findall(state)
    for wh_id in wh_ids:
        num = wh_id.split("-")[1]
        er_id = f"ER-{num}"
        # Only promote if ER-NNN already appears elsewhere in state (sign of intended promotion)
        if er_id not in state:
            continue
        has_verified = any(
            e["verdict"] == "VERIFIED" and (
                er_id in e["claim"] or wh_id in e["claim"]
                or er_id in e.get("body", "") or wh_id in e.get("body", "")
            )
            for e in entries
        )
        if has_verified:
            state = re.sub(
                rf'^(#{{2,3}} ){re.escape(wh_id)}',
                rf'\g<1>{er_id}',
                state,
                count=1,
                flags=re.MULTILINE,
            )
            # Propagate prose references: WH-NNN → ER-NNN throughout state
            state = state.replace(wh_id, er_id)
            changed = True
            violations.append(Violation(
                check="er_promotion_gate",
                severity=ViolationSeverity.WARNING,
                message=f"Promoted header {wh_id} → {er_id} (VERIFIED backing found)",
                file="RESEARCH_STATE.md",
                detail=wh_id,
            ))

    if changed:
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


def check_stale_unverified_labels(workspace: WorkspaceManager) -> list[Violation]:
    """Promote [unverified] labels to VERIFIED when backed by computation."""
    violations: list[Violation] = []
    comp_log = workspace.read_file("COMPUTATION_LOG.md")
    entries = _parse_comp_entries(comp_log)

    # Build set of ER/WH IDs that have VERIFIED computations
    verified_ids: set[str] = set()
    for e in entries:
        if e["verdict"] == "VERIFIED":
            verified_ids.update(_ER_WH_ID_RE.findall(e["claim"]))
            verified_ids.update(_ER_WH_ID_RE.findall(e.get("body", "")))

    if not verified_ids:
        return []

    state = workspace.read_file("RESEARCH_STATE.md")
    if not state or "[unverified]" not in state.lower():
        return []

    # Expand WH-NNN → ER-NNN for promoted hypotheses: if WH-001 is in
    # verified_ids and ER-001 exists as a section header in state, add ER-001.
    er_section_ids = set(find_er_section_ids(state))
    for vid in list(verified_ids):
        if vid.startswith("WH-"):
            promoted_er = "ER-" + vid.split("-")[1]
            if promoted_er in er_section_ids:
                verified_ids.add(promoted_er)

    new_lines = []
    changed = False
    for line in state.splitlines():
        if "[unverified]" in line.lower():
            ids_in_line = _ER_WH_ID_RE.findall(line)
            if ids_in_line and any(id_ in verified_ids for id_ in ids_in_line):
                line = line.replace("[unverified]", "VERIFIED")
                line = line.replace("[Unverified]", "VERIFIED")
                # If any WH-NNN on this line has a promoted ER-NNN header, rename
                for wh in [i for i in ids_in_line if i.startswith("WH-")]:
                    promoted_er = "ER-" + wh.split("-")[1]
                    if promoted_er in er_section_ids:
                        line = line.replace(wh, promoted_er)
                changed = True
                violations.append(Violation(
                    check="stale_unverified_labels",
                    severity=ViolationSeverity.WARNING,
                    message=f"Promoted [unverified] → VERIFIED for {', '.join(ids_in_line)}",
                    file="RESEARCH_STATE.md",
                    detail=", ".join(ids_in_line),
                ))
        new_lines.append(line)

    if changed:
        workspace.write_file("RESEARCH_STATE.md", "\n".join(new_lines))

    return violations


def check_phantom_references(workspace: WorkspaceManager) -> list[Violation]:
    """Replace orphaned COMP/TASK references in RESEARCH_STATE.md."""
    violations: list[Violation] = []
    state = workspace.read_file("RESEARCH_STATE.md")
    comp_log = workspace.read_file("COMPUTATION_LOG.md")

    # Flatten nested bracket markers first
    original_state = state
    state = flatten_unverified_brackets(state)

    entries = _parse_comp_entries(comp_log)
    valid_ids = {e["id"] for e in entries}

    # Expand valid_ids: accept TASK-NNN when a corresponding COMP exists
    task_comp = _build_task_comp_mapping(entries)
    for task_id, comp_set in task_comp.items():
        if comp_set & valid_ids:
            valid_ids.add(task_id)

    # Match bare COMP/TASK refs but exclude those already wrapped in [ID:unverified]
    ref_pattern = re.compile(r'(?<!\[)\b((?:COMP|TASK)-\d+)\b(?!:unverified\])')
    found_refs = set(ref_pattern.findall(state))

    phantoms = sorted(found_refs - valid_ids)
    if not phantoms and state == original_state:
        return []

    for phantom in phantoms:
        # Only replace bare references, not already-wrapped ones
        state = re.sub(
            r'(?<!\[)\b' + re.escape(phantom) + r'\b(?!:unverified\])',
            f'[{phantom}:unverified]',
            state,
        )
        violations.append(Violation(
            check="phantom_references",
            severity=ViolationSeverity.ERROR,
            message=f"Phantom reference {phantom} replaced with [{phantom}:unverified]",
            file="RESEARCH_STATE.md",
            detail=phantom,
        ))

    if state != original_state:
        workspace.write_file("RESEARCH_STATE.md", state)
    return violations


def check_verified_frontmatter_backfill(workspace: WorkspaceManager) -> list[Violation]:
    """Backfill verified_results in RESEARCH_STATE.md frontmatter from computation log."""
    violations: list[Violation] = []
    comp_log = workspace.read_file("COMPUTATION_LOG.md")
    state = workspace.read_file("RESEARCH_STATE.md")
    if not comp_log or not state:
        return []

    entries = _parse_comp_entries(comp_log)
    verified_ids: set[str] = set()
    for e in entries:
        if e["verdict"] == "VERIFIED":
            verified_ids.update(_ER_WH_ID_RE.findall(e["claim"]))
            verified_ids.update(_ER_WH_ID_RE.findall(e.get("body", "")))

    if not verified_ids:
        return []

    meta, body = parse_frontmatter(state)
    existing = set(meta.get("verified_results", []) or [])
    missing = sorted(verified_ids - existing)
    if not missing:
        return []

    meta["verified_results"] = sorted(existing | verified_ids)
    workspace.write_file("RESEARCH_STATE.md", render_frontmatter(meta, body))
    violations.append(Violation(
        check="verified_frontmatter_backfill",
        severity=ViolationSeverity.WARNING,
        message=f"Backfilled verified_results: {', '.join(missing)}",
        file="RESEARCH_STATE.md",
        detail=", ".join(missing),
    ))
    return violations


def check_id_consistency(workspace: WorkspaceManager) -> list[Violation]:
    """Fix total_computations frontmatter counter if it disagrees with actual entries."""
    violations: list[Violation] = []
    comp_log = workspace.read_file("COMPUTATION_LOG.md")
    if not comp_log:
        return []

    meta, body = parse_frontmatter(comp_log)
    entries = _parse_comp_entries(comp_log)
    actual_count = len([e for e in entries if e["id"].startswith("COMP-")])
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
    check_stale_unverified_labels,
    check_verified_frontmatter_backfill,
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
