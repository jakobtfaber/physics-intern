"""Post-integration validation and termination gates for SciRalph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .computation_index import read_computation_index, find_verified_target_ids, find_refuted_target_ids
from .markdown import (
    parse_frontmatter,
    render_frontmatter,
    _parse_comp_entries,
    count_unresolved_critiques,
    find_er_section_ids,
    flatten_unverified_brackets,
    _ER_WH_ID_RE,
    _WH_SECTION_RE,
)
from .categories import CompensationCategory as CC
from .workspace import log_scaffold_event

if TYPE_CHECKING:
    from .config import Config
    from .metrics import MetricsTracker
    from .research_state import ResearchState
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

def check_er_demotion_safety(workspace: WorkspaceManager, research_state: ResearchState | None = None) -> list[Violation]:
    """Demote ER-NNN sections only when an explicit REFUTED computation exists
    with no superseding VERIFIED computation for that claim.

    Promotion is now handled by the orchestrator's promote_hypothesis tool.
    """
    violations: list[Violation] = []
    state = workspace.read_file("RESEARCH_STATE.md")

    # Prefer JSONL index; fall back to markdown parsing
    index_entries = read_computation_index(workspace)
    verified_targets = find_verified_target_ids(index_entries) if index_entries else set()
    refuted_targets = find_refuted_target_ids(index_entries) if index_entries else set()

    # Fallback for workspaces without JSONL
    if not index_entries:
        comp_log = workspace.read_file("COMPUTATION_LOG.md")
        md_entries = _parse_comp_entries(comp_log)
    else:
        md_entries = []

    changed = False
    er_ids = find_er_section_ids(state)

    for er_id in er_ids:
        num = er_id.split("-")[1]
        wh_id = f"WH-{num}"

        # Check for REFUTED and VERIFIED computations targeting this claim
        has_refuted = er_id in refuted_targets or wh_id in refuted_targets
        has_verified = er_id in verified_targets or wh_id in verified_targets

        if research_state and (not has_refuted or not has_verified):
            for c in research_state.computations.values():
                if c.target_hypothesis in (er_id, wh_id):
                    if c.verdict.value == "REFUTED":
                        has_refuted = True
                    if c.verdict.value == "VERIFIED":
                        has_verified = True

        if not has_refuted or not has_verified:
            # Fallback: substring matching on markdown entries
            for e in md_entries:
                refs = e["claim"] + " " + e.get("body", "")
                if er_id in refs or wh_id in refs:
                    if e["verdict"] == "REFUTED":
                        has_refuted = True
                    if e["verdict"] == "VERIFIED":
                        has_verified = True

        # Only demote when REFUTED exists and no VERIFIED supersedes it
        if has_refuted and not has_verified:
            state = re.sub(
                rf'^(#{{2,3}} |(?:\*\*)){re.escape(er_id)}',
                rf'\g<1>{wh_id}',
                state,
                count=1,
                flags=re.MULTILINE,
            )
            # Propagate prose references: ER-NNN → WH-NNN throughout state
            state = re.sub(rf'\b{re.escape(er_id)}\b', wh_id, state)
            changed = True
            violations.append(Violation(
                check="er_demotion_safety",
                severity=ViolationSeverity.WARNING,
                message=f"{er_id} has REFUTED computation with no VERIFIED — demoted to {wh_id}",
                file="RESEARCH_STATE.md",
                detail=er_id,
            ))

    # --- Normalize frontmatter verified_results to match current section headers ---
    if changed:
        meta, body = parse_frontmatter(state)
        vr_list = meta.get("verified_results")
        if vr_list:
            current_er_ids = set(find_er_section_ids(body))
            current_wh_ids = set(_WH_SECTION_RE.findall(body))
            normalized = []
            for vid in vr_list:
                num = vid.split("-")[1]
                er_form = f"ER-{num}"
                wh_form = f"WH-{num}"
                if vid.startswith("WH-") and er_form in current_er_ids:
                    normalized.append(er_form)
                elif vid.startswith("ER-") and er_form not in current_er_ids and wh_form in current_wh_ids:
                    normalized.append(wh_form)
                else:
                    normalized.append(vid)
            meta["verified_results"] = sorted(set(normalized))
            state = render_frontmatter(meta, body)
        workspace.write_file("RESEARCH_STATE.md", state)

    return violations


def check_phantom_labels(workspace: WorkspaceManager) -> list[Violation]:
    """Strip unsubstantiated VERIFIED labels from state and proposed changes."""
    violations: list[Violation] = []

    # Prefer JSONL for verified target lookup
    index_entries = read_computation_index(workspace)
    verified_ids = find_verified_target_ids(index_entries) if index_entries else set()

    # Fallback: parse markdown
    if not index_entries:
        comp_log = workspace.read_file("COMPUTATION_LOG.md")
        md_entries = _parse_comp_entries(comp_log)
        verified_claims = {e["claim"] for e in md_entries if e["verdict"] == "VERIFIED"}
    else:
        verified_claims = set()

    for filename in ("RESEARCH_STATE.md",):
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
                    # Check if any of these IDs are JSONL-verified or match markdown claims
                    backed = any(id_ in verified_ids for id_ in ids_in_line) or any(
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

    # Build set of ER/WH IDs that have VERIFIED computations
    index_entries = read_computation_index(workspace)
    verified_ids = find_verified_target_ids(index_entries) if index_entries else set()

    # Fallback: parse markdown
    if not index_entries:
        comp_log = workspace.read_file("COMPUTATION_LOG.md")
        md_entries = _parse_comp_entries(comp_log)
        for e in md_entries:
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
    """Sync verified_results in RESEARCH_STATE.md frontmatter with current ER section headers.

    Promotion is now explicit via the orchestrator's promote_hypothesis tool,
    so verified_results should simply reflect actual ER sections.
    """
    violations: list[Violation] = []
    state = workspace.read_file("RESEARCH_STATE.md")
    if not state:
        return []

    meta, body = parse_frontmatter(state)
    current_er_ids = sorted(find_er_section_ids(body))
    existing = sorted(meta.get("verified_results", []) or [])

    if current_er_ids == existing:
        return []

    added = sorted(set(current_er_ids) - set(existing))
    removed = sorted(set(existing) - set(current_er_ids))

    meta["verified_results"] = current_er_ids
    workspace.write_file("RESEARCH_STATE.md", render_frontmatter(meta, body))

    parts = []
    if added:
        parts.append(f"added {', '.join(added)}")
    if removed:
        parts.append(f"removed {', '.join(removed)}")
    detail = "; ".join(parts)

    violations.append(Violation(
        check="verified_frontmatter_backfill",
        severity=ViolationSeverity.WARNING,
        message=f"Synced verified_results with ER sections: {detail}",
        file="RESEARCH_STATE.md",
        detail=detail,
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


def check_critique_resolution_consistency(workspace: WorkspaceManager) -> list[Violation]:
    """Check that resolved critiques are actually consistent with current state.

    For label-related critiques: flag if both WH-NNN and ER-NNN still co-exist.
    For any resolved critique: flag if the target ID has vanished entirely.
    Returns WARNING-level violations only (advisory for orchestrator).
    """
    violations: list[Violation] = []
    critique_log = workspace.read_file("CRITIQUE_LOG.md")
    if not critique_log:
        return []

    # Find the resolved critiques section
    resolved_idx = critique_log.find("# Resolved Critiques")
    if resolved_idx == -1:
        return []
    resolved_section = critique_log[resolved_idx:]

    state = workspace.read_file("RESEARCH_STATE.md")
    if not state:
        return []

    # Parse individual resolved critique blocks
    _RESOLVED_CRIT_RE = re.compile(
        r'^##\s+(CRIT(?:IQUE)?-\d+)\s.*?\[RESOLVED\]',
        re.MULTILINE | re.IGNORECASE,
    )
    headers = list(_RESOLVED_CRIT_RE.finditer(resolved_section))
    if not headers:
        return []

    _LABEL_KEYWORDS = re.compile(
        r'label|inconsisten|rename|mislabel|header', re.IGNORECASE
    )

    for i, match in enumerate(headers):
        crit_id = match.group(1)
        # Extract critique body (until next header or end)
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(resolved_section)
        body = resolved_section[start:end]

        # Extract target IDs from the critique body
        target_ids = _ER_WH_ID_RE.findall(body)
        if not target_ids:
            continue

        is_label_critique = bool(_LABEL_KEYWORDS.search(body))

        for tid in set(target_ids):
            num = tid.split("-")[1]
            wh_form = f"WH-{num}"
            er_form = f"ER-{num}"

            # Check if target vanished entirely
            if tid not in state and wh_form not in state and er_form not in state:
                violations.append(Violation(
                    check="critique_resolution_consistency",
                    severity=ViolationSeverity.WARNING,
                    message=(
                        f"Resolved {crit_id} targets {tid} which no longer "
                        f"appears in RESEARCH_STATE.md"
                    ),
                    file="CRITIQUE_LOG.md",
                    detail=f"{crit_id}:{tid}",
                ))

            # For label critiques: check WH/ER co-existence
            if is_label_critique and wh_form in state and er_form in state:
                violations.append(Violation(
                    check="critique_resolution_consistency",
                    severity=ViolationSeverity.WARNING,
                    message=(
                        f"Resolved {crit_id} (label critique) but {wh_form} and "
                        f"{er_form} still co-exist in RESEARCH_STATE.md"
                    ),
                    file="CRITIQUE_LOG.md",
                    detail=f"{crit_id}:{wh_form}+{er_form}",
                ))

    return violations


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_DEFAULT_CHECKS = [
    check_phantom_references,
    check_er_demotion_safety,
    check_phantom_labels,
    check_stale_unverified_labels,
    check_verified_frontmatter_backfill,
    check_id_consistency,
    check_critique_resolution_consistency,
]


def validate_post_integration(
    workspace: WorkspaceManager,
    config: Config | None = None,
    iteration: int = 0,
    research_state: ResearchState | None = None,
) -> list[Violation]:
    """Run all post-integration invariant checks. Returns violations to inject into next orchestrator context."""
    violations: list[Violation] = []
    for check in _DEFAULT_CHECKS:
        # Pass research_state to checks that accept it
        if check is check_er_demotion_safety:
            check_violations = check(workspace, research_state=research_state)
        else:
            check_violations = check(workspace)
        if check_violations and hasattr(workspace, 'root') and workspace.root:
            for v in check_violations:
                log_scaffold_event(workspace.root, iteration, CC.STATE_INVARIANTS, v.check, v.message)
        violations.extend(check_violations)
    return violations


# ---------------------------------------------------------------------------
# Termination gate
# ---------------------------------------------------------------------------

def can_terminate(
    workspace: WorkspaceManager,
    config: Config,
    metrics: MetricsTracker,
    problem_meta: dict | None = None,
    *,
    research_state: ResearchState,
) -> tuple[bool, list[str]]:
    """Check preconditions before allowing the research loop to exit.

    Returns (allowed, blockers) where blockers is a list of human-readable
    reasons the loop must continue.
    """
    from .research_state import HypothesisStatus, RQStatus, Verdict

    blockers: list[str] = []

    # Prefer JSONL; fall back to markdown parsing
    index_entries = read_computation_index(workspace)
    verified_targets = find_verified_target_ids(index_entries) if index_entries else set()

    if not index_entries:
        comp_log = workspace.read_file("COMPUTATION_LOG.md")
        md_entries = _parse_comp_entries(comp_log)
    else:
        md_entries = []

    # Gate 1: At least one critic pass if verified results exist
    has_verified = bool(verified_targets) or any(e["verdict"] == "VERIFIED" for e in md_entries)
    if has_verified and metrics.last_critic_iteration == 0:
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
    all_entries = index_entries or md_entries
    if meta.get("requires_numerical", False):
        if len(all_entries) == 0:
            blockers.append(
                "Problem requires numerical verification but no computations found. "
                "You MUST emit task_type: compute_verify "
                "to run at least one numerical verification before terminating."
            )

    # Gate 4: All RQs and WHs must be resolved before termination
    for rq in research_state.research_questions.values():
        if rq.status == RQStatus.OPEN:
            blockers.append(
                f"{rq.id} is still OPEN. "
                "Call resolve_research_question or abandon it before terminating."
            )

    # Build set of WHs with VERIFIED backing for a more specific message
    verified_wh_ids: set[str] = set()
    for comp in research_state.computations.values():
        if comp.verdict == Verdict.VERIFIED and comp.target_hypothesis:
            verified_wh_ids.add(comp.target_hypothesis)

    for h in research_state.hypotheses.values():
        if h.status == HypothesisStatus.WORKING:
            if h.id in verified_wh_ids:
                blockers.append(
                    f"{h.id} has VERIFIED computation backing but was not promoted. "
                    "Call promote_hypothesis or abandon_hypothesis before terminating."
                )
            else:
                blockers.append(
                    f"{h.id} is still a working hypothesis. "
                    "Promote, verify, or abandon it before terminating."
                )

    return (len(blockers) == 0, blockers)
