"""Post-integration validation and termination gates for SciRalph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .research_state import CritiqueStatus, HypothesisStatus, Severity, Verdict
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
# ER/WH ID pattern (used by multiple checks)
# ---------------------------------------------------------------------------
_ER_WH_ID_RE = re.compile(r"\b(?:ER|WH)-\d+\b")


# ---------------------------------------------------------------------------
# Individual check functions — all operate on ResearchState
# ---------------------------------------------------------------------------

def check_er_demotion_safety(research_state: ResearchState) -> list[Violation]:
    """Demote ER-NNN hypotheses when an explicit REFUTED computation exists
    with no superseding VERIFIED computation for that claim.
    """
    violations: list[Violation] = []

    for hid, h in list(research_state.hypotheses.items()):
        if not hid.startswith("ER-"):
            continue
        num = hid.split("-")[1]
        wh_id = f"WH-{num}"

        # Check for REFUTED and VERIFIED computations targeting this claim
        has_refuted = False
        has_verified = False
        for c in research_state.computations.values():
            if c.target_hypothesis in (hid, wh_id):
                if c.verdict == Verdict.REFUTED:
                    has_refuted = True
                if c.verdict == Verdict.VERIFIED:
                    has_verified = True

        # Only demote when REFUTED exists and no VERIFIED supersedes it
        if has_refuted and not has_verified:
            new_id = research_state.demote_hypothesis(hid)
            if new_id:
                violations.append(Violation(
                    check="er_demotion_safety",
                    severity=ViolationSeverity.WARNING,
                    message=f"{hid} has REFUTED computation with no VERIFIED — demoted to {new_id}",
                    file="RESEARCH_STATE.md",
                    detail=hid,
                ))

    return violations


def check_phantom_labels(research_state: ResearchState) -> list[Violation]:
    """Strip unsubstantiated VERIFIED labels from hypothesis derivations."""
    violations: list[Violation] = []

    # Build set of hypothesis IDs that have VERIFIED computations
    verified_ids: set[str] = set()
    for c in research_state.computations.values():
        if c.verdict == Verdict.VERIFIED and c.target_hypothesis:
            verified_ids.add(c.target_hypothesis)

    for hid, h in research_state.hypotheses.items():
        if not h.derivation or "VERIFIED" not in h.derivation:
            continue
        # Check each line in derivation for unsubstantiated VERIFIED
        new_lines = []
        changed = False
        for line in h.derivation.splitlines():
            if "VERIFIED" in line and not line.strip().startswith("##"):
                ids_in_line = _ER_WH_ID_RE.findall(line)
                if ids_in_line:
                    backed = any(id_ in verified_ids for id_ in ids_in_line)
                    if not backed:
                        line = line.replace("VERIFIED", "[unverified]")
                        changed = True
                        violations.append(Violation(
                            check="phantom_labels",
                            severity=ViolationSeverity.ERROR,
                            message="Unsubstantiated VERIFIED label stripped",
                            file="RESEARCH_STATE.md",
                            detail=f"{hid}: {', '.join(ids_in_line)}",
                        ))
            new_lines.append(line)
        if changed:
            h.derivation = "\n".join(new_lines)

    return violations


def check_stale_unverified_labels(research_state: ResearchState) -> list[Violation]:
    """Promote [unverified] labels to VERIFIED when backed by computation."""
    violations: list[Violation] = []

    # Build set of hypothesis IDs that have VERIFIED computations
    verified_ids: set[str] = set()
    for c in research_state.computations.values():
        if c.verdict == Verdict.VERIFIED and c.target_hypothesis:
            verified_ids.add(c.target_hypothesis)
            # Also add the ER form if WH was verified and has been promoted
            if c.target_hypothesis.startswith("WH-"):
                num = c.target_hypothesis.split("-")[1]
                er_form = f"ER-{num}"
                if er_form in research_state.hypotheses:
                    verified_ids.add(er_form)

    if not verified_ids:
        return []

    for hid, h in research_state.hypotheses.items():
        if not h.derivation or "[unverified]" not in h.derivation.lower():
            continue
        new_lines = []
        changed = False
        for line in h.derivation.splitlines():
            if "[unverified]" in line.lower():
                ids_in_line = _ER_WH_ID_RE.findall(line)
                if ids_in_line and any(id_ in verified_ids for id_ in ids_in_line):
                    line = line.replace("[unverified]", "VERIFIED")
                    line = line.replace("[Unverified]", "VERIFIED")
                    # If any WH-NNN on this line has a promoted ER-NNN, rename
                    for wh in [i for i in ids_in_line if i.startswith("WH-")]:
                        num = wh.split("-")[1]
                        er_form = f"ER-{num}"
                        if er_form in research_state.hypotheses:
                            line = line.replace(wh, er_form)
                    changed = True
                    violations.append(Violation(
                        check="stale_unverified_labels",
                        severity=ViolationSeverity.WARNING,
                        message=f"Promoted [unverified] → VERIFIED for {', '.join(ids_in_line)}",
                        file="RESEARCH_STATE.md",
                        detail=f"{hid}: {', '.join(ids_in_line)}",
                    ))
            new_lines.append(line)
        if changed:
            h.derivation = "\n".join(new_lines)

    return []


def check_critique_resolution_consistency(research_state: ResearchState) -> list[Violation]:
    """Check that resolved critiques are consistent with current state.

    For label-related critiques: flag if both WH-NNN and ER-NNN still co-exist.
    For any resolved critique: flag if the target ID has vanished entirely.
    """
    violations: list[Violation] = []

    _LABEL_KEYWORDS = re.compile(
        r'label|inconsisten|rename|mislabel|header', re.IGNORECASE
    )

    for crit_id, crit in research_state.critiques.items():
        if crit.status != CritiqueStatus.RESOLVED:
            continue
        if not crit.targets:
            continue

        body_text = crit.argument or ""
        is_label_critique = bool(_LABEL_KEYWORDS.search(body_text))

        for tid in crit.targets:
            num = tid.split("-")[1] if "-" in tid else ""
            wh_form = f"WH-{num}"
            er_form = f"ER-{num}"

            # Check if target vanished entirely
            if (tid not in research_state.hypotheses
                    and wh_form not in research_state.hypotheses
                    and er_form not in research_state.hypotheses):
                violations.append(Violation(
                    check="critique_resolution_consistency",
                    severity=ViolationSeverity.WARNING,
                    message=(
                        f"Resolved {crit_id} targets {tid} which no longer "
                        f"exists in research state"
                    ),
                    file="CRITIQUE_LOG.md",
                    detail=f"{crit_id}:{tid}",
                ))

            # For label critiques: check WH/ER co-existence
            if (is_label_critique
                    and wh_form in research_state.hypotheses
                    and er_form in research_state.hypotheses):
                violations.append(Violation(
                    check="critique_resolution_consistency",
                    severity=ViolationSeverity.WARNING,
                    message=(
                        f"Resolved {crit_id} (label critique) but {wh_form} and "
                        f"{er_form} still co-exist in research state"
                    ),
                    file="CRITIQUE_LOG.md",
                    detail=f"{crit_id}:{wh_form}+{er_form}",
                ))

    return violations


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_DEFAULT_CHECKS = [
    check_er_demotion_safety,
    check_phantom_labels,
    check_stale_unverified_labels,
    check_critique_resolution_consistency,
]


def validate_post_integration(
    research_state: ResearchState,
    *,
    iteration: int = 0,
    workspace: WorkspaceManager | None = None,
) -> list[Violation]:
    """Run all post-integration invariant checks. Returns violations to inject into next orchestrator context."""
    violations: list[Violation] = []
    for check in _DEFAULT_CHECKS:
        check_violations = check(research_state)
        if check_violations and workspace and hasattr(workspace, 'root') and workspace.root:
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
    from .research_state import RQStatus

    blockers: list[str] = []

    # Gate 1: At least one critic pass if verified results exist
    has_verified = any(
        c.verdict == Verdict.VERIFIED for c in research_state.computations.values()
    )
    if has_verified and metrics.last_critic_iteration == 0:
        blockers.append(
            "No critic pass has occurred yet. "
            "Emit task_type: critique to run a review before terminating."
        )

    # Gate 2: No unresolved HIGH critiques
    high_count = len([
        c for c in research_state.critiques.values()
        if c.status == CritiqueStatus.ACTIVE and c.severity == Severity.HIGH
    ])
    if high_count > 0:
        blockers.append(
            f"{high_count} unresolved HIGH critique(s). "
            "Emit task_type: resolve to address them."
        )

    # Gate 3: At least one computation when problem requires it
    meta = problem_meta or {}
    if meta.get("requires_numerical", False):
        if len(research_state.computations) == 0:
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
