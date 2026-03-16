"""Formal research state tracking.

Structured representation of hypotheses, computations, critiques, and their
relationships.  Transitioning from shadow state (built from Markdown) to
authoritative source of truth (agents mutate state, Markdown rendered from it).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class HypothesisStatus(StrEnum):
    WORKING = "working"
    ESTABLISHED = "established"
    REFUTED = "refuted"
    ABANDONED = "abandoned"


class Verdict(StrEnum):
    VERIFIED = "VERIFIED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class Severity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class CritiqueStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    WITHDRAWN = "withdrawn"


# ---------------------------------------------------------------------------
# Entity dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Hypothesis:
    id: str
    statement: str = ""
    status: HypothesisStatus = HypothesisStatus.WORKING
    derivation: str = ""
    supporting_comps: list[str] = field(default_factory=list)
    critiques: list[str] = field(default_factory=list)
    iteration_created: int = 0
    iteration_modified: int = 0


@dataclass
class Computation:
    id: str
    target_hypothesis: str = ""
    verdict: Verdict = Verdict.INCONCLUSIVE
    claim: str = ""
    method: str = ""
    key_results: dict[str, Any] = field(default_factory=dict)
    code_path: str = ""
    failure_detail: str = ""
    iteration: int = 0
    kind: str = "verify"  # "explore" or "verify"
    zero_output: bool = False
    confidence: str = ""  # explore only: exact/approximate/partial
    notes: str = ""
    result: str = ""


@dataclass
class Critique:
    id: str
    targets: list[str] = field(default_factory=list)
    severity: Severity = Severity.MEDIUM
    argument: str = ""
    status: CritiqueStatus = CritiqueStatus.ACTIVE
    resolution: str = ""
    iteration_filed: int = 0
    iteration_resolved: int | None = None


@dataclass
class FailedApproach:
    description: str = ""
    reason: str = ""
    related_comps: list[str] = field(default_factory=list)
    iteration: int = 0


# ---------------------------------------------------------------------------
# ResearchState
# ---------------------------------------------------------------------------

STATE_FILENAME = "RESEARCH_GRAPH.json"

_ER_WH_SECTION_RE = re.compile(r"^## ((?:ER|WH)-\d+)\s*[-—:]?\s*(.*)", re.MULTILINE)
_SECTION_END_RE = re.compile(r"^(?:##? )", re.MULTILINE)


@dataclass
class ResearchState:
    hypotheses: dict[str, Hypothesis] = field(default_factory=dict)
    computations: dict[str, Computation] = field(default_factory=dict)
    critiques: dict[str, Critique] = field(default_factory=dict)
    failed_approaches: list[FailedApproach] = field(default_factory=list)
    iteration: int = 0
    problem_statement: str = ""
    conventions: str = ""
    open_questions: str = ""
    status: str = "in_progress"
    title: str = ""

    # --- Query methods ---

    def verified_comps_for(self, hypothesis_id: str) -> list[Computation]:
        """VERIFIED computations targeting *hypothesis_id*."""
        return [
            c for c in self.computations.values()
            if c.target_hypothesis == hypothesis_id and c.verdict == Verdict.VERIFIED
        ]

    def has_verified_backing(self, hypothesis_id: str) -> bool:
        """True if *hypothesis_id* has at least one VERIFIED computation."""
        return any(
            c.target_hypothesis == hypothesis_id and c.verdict == Verdict.VERIFIED
            for c in self.computations.values()
        )

    def active_critiques_for(self, target_id: str) -> list[Critique]:
        """Active critiques mentioning *target_id*."""
        return [
            c for c in self.critiques.values()
            if target_id in c.targets and c.status == CritiqueStatus.ACTIVE
        ]

    def unresolved_high_critiques(self) -> list[Critique]:
        """All unresolved HIGH-severity critiques."""
        return [
            c for c in self.critiques.values()
            if c.severity == Severity.HIGH and c.status == CritiqueStatus.ACTIVE
        ]

    def comps_for_hypothesis(self, hypothesis_id: str) -> list[Computation]:
        """All computations targeting *hypothesis_id* (any verdict)."""
        return [
            c for c in self.computations.values()
            if c.target_hypothesis == hypothesis_id
        ]

    def established_hypotheses(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses.values() if h.status == HypothesisStatus.ESTABLISHED]

    def working_hypotheses(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses.values() if h.status == HypothesisStatus.WORKING]

    def abandoned_hypotheses(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses.values() if h.status == HypothesisStatus.ABANDONED]

    def failures_for_hypothesis(self, hypothesis_id: str) -> list[FailedApproach]:
        """Failed approaches mentioning *hypothesis_id*."""
        return [
            fa for fa in self.failed_approaches
            if hypothesis_id in fa.description or hypothesis_id in " ".join(fa.related_comps)
        ]

    def explore_only_hypotheses(self) -> list[Hypothesis]:
        """Working hypotheses with explore results but no verify-type VERIFIED."""
        result = []
        for h in self.hypotheses.values():
            if h.status != HypothesisStatus.WORKING:
                continue
            comps = self.comps_for_hypothesis(h.id)
            has_explore = any(c.kind == "explore" for c in comps)
            has_verified = any(
                c.kind == "verify" and c.verdict == Verdict.VERIFIED for c in comps
            )
            if has_explore and not has_verified:
                result.append(h)
        return result

    def refuted_targets(self) -> set[str]:
        """Set of hypothesis IDs with at least one REFUTED computation."""
        return {
            c.target_hypothesis for c in self.computations.values()
            if c.verdict == Verdict.REFUTED and c.target_hypothesis
        }

    def demote_hypothesis(self, hid: str) -> str | None:
        """Demote ER→WH: update status, rename key, fix computation target refs.

        Returns the new ID (e.g. 'WH-002') or None if hid not found / not ER.
        """
        if hid not in self.hypotheses or not hid.startswith("ER-"):
            return None
        num = hid.split("-")[1]
        new_id = f"WH-{num}"
        h = self.hypotheses.pop(hid)
        h.id = new_id
        h.status = HypothesisStatus.WORKING
        self.hypotheses[new_id] = h
        self.normalize_references()
        return new_id

    def next_hypothesis_num(self) -> int:
        """Max existing hypothesis number + 1."""
        nums = []
        for hid in self.hypotheses:
            parts = hid.split("-")
            if len(parts) == 2:
                try:
                    nums.append(int(parts[1]))
                except ValueError:
                    pass
        return max(nums, default=0) + 1

    def next_computation_num(self) -> int:
        """Max existing COMP number + 1."""
        nums = []
        for cid in self.computations:
            parts = cid.split("-")
            if len(parts) == 2 and parts[0] == "COMP":
                try:
                    nums.append(int(parts[1]))
                except ValueError:
                    pass
        return max(nums, default=0) + 1

    def next_critique_num(self) -> int:
        """Max existing CRIT number + 1."""
        nums = []
        for cid in self.critiques:
            parts = cid.split("-")
            if len(parts) == 2 and parts[0] == "CRIT":
                try:
                    nums.append(int(parts[1]))
                except ValueError:
                    pass
        return max(nums, default=0) + 1

    def detect_computation_stalls(self, threshold: int = 3) -> list[dict]:
        """Find claims with consecutive non-VERIFIED verify computations."""
        from collections import defaultdict
        from .research_state import Verdict  # noqa: F811
        # Group verify comps by target, sorted by iteration
        by_target: dict[str, list[Computation]] = defaultdict(list)
        for c in self.computations.values():
            if c.kind == "verify" and c.target_hypothesis:
                by_target[c.target_hypothesis].append(c)
        stalls = []
        for target, comps in by_target.items():
            comps_sorted = sorted(comps, key=lambda c: (c.iteration, c.id))
            consecutive = 0
            verdicts = []
            for c in comps_sorted:
                if c.verdict != Verdict.VERIFIED:
                    consecutive += 1
                    verdicts.append(c.verdict.value)
                else:
                    consecutive = 0
                    verdicts = []
            if consecutive >= threshold:
                stalls.append({
                    "claim": target,
                    "count": consecutive,
                    "verdicts": verdicts[-threshold:],
                })
        return stalls

    # --- Reference normalization ---

    def normalize_references(self):
        """Normalize target_hypothesis in computations to match current hypothesis IDs,
        and rebuild supporting_comps from scratch.

        When promote_hypothesis or demotion safety renames WH-002 → ER-002 (or
        vice versa), existing computations that targeted WH-002 become stale.
        This method fixes those backlinks by mapping the numeric suffix to the
        current hypothesis ID.
        """
        # Build alias map: number -> current ID  (e.g., "002" -> "ER-002")
        id_by_num: dict[str, str] = {}
        for hid in self.hypotheses:
            parts = hid.split("-")
            if len(parts) == 2:
                id_by_num[parts[1]] = hid

        # Update computation target_hypothesis to current form
        for comp in self.computations.values():
            target = comp.target_hypothesis
            if not target or "-" not in target:
                continue
            num = target.split("-")[1]
            if num in id_by_num and id_by_num[num] != target:
                comp.target_hypothesis = id_by_num[num]

        # Rebuild supporting_comps from scratch
        for h in self.hypotheses.values():
            h.supporting_comps = []
        for comp_id, comp in self.computations.items():
            target = comp.target_hypothesis
            if target and target in self.hypotheses:
                h = self.hypotheses[target]
                if comp_id not in h.supporting_comps:
                    h.supporting_comps.append(comp_id)

    # --- Serialization ---

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ResearchState:
        data = json.loads(text)
        state = cls(
            iteration=data.get("iteration", 0),
            problem_statement=data.get("problem_statement", ""),
            conventions=data.get("conventions", ""),
            open_questions=data.get("open_questions", ""),
            status=data.get("status", "in_progress"),
            title=data.get("title", ""),
        )
        for hid, hdata in data.get("hypotheses", {}).items():
            state.hypotheses[hid] = Hypothesis(
                id=hdata["id"],
                statement=hdata.get("statement", ""),
                status=HypothesisStatus(hdata.get("status", "working")),
                derivation=hdata.get("derivation", ""),
                supporting_comps=hdata.get("supporting_comps", []),
                critiques=hdata.get("critiques", []),
                iteration_created=hdata.get("iteration_created", 0),
                iteration_modified=hdata.get("iteration_modified", 0),
            )
        for cid, cdata in data.get("computations", {}).items():
            state.computations[cid] = Computation(
                id=cdata["id"],
                target_hypothesis=cdata.get("target_hypothesis", ""),
                verdict=Verdict(cdata.get("verdict", "INCONCLUSIVE")),
                claim=cdata.get("claim", ""),
                method=cdata.get("method", ""),
                key_results=cdata.get("key_results", {}),
                code_path=cdata.get("code_path", ""),
                failure_detail=cdata.get("failure_detail", ""),
                iteration=cdata.get("iteration", 0),
                kind=cdata.get("kind", "verify"),
                zero_output=cdata.get("zero_output", False),
                confidence=cdata.get("confidence", ""),
                notes=cdata.get("notes", ""),
                result=cdata.get("result", ""),
            )
        for crid, crdata in data.get("critiques", {}).items():
            state.critiques[crid] = Critique(
                id=crdata["id"],
                targets=crdata.get("targets", []),
                severity=Severity(crdata.get("severity", "MEDIUM")),
                argument=crdata.get("argument", ""),
                status=CritiqueStatus(crdata.get("status", "active")),
                resolution=crdata.get("resolution", ""),
                iteration_filed=crdata.get("iteration_filed", 0),
                iteration_resolved=crdata.get("iteration_resolved"),
            )
        for fdata in data.get("failed_approaches", []):
            state.failed_approaches.append(FailedApproach(
                description=fdata.get("description", ""),
                reason=fdata.get("reason", ""),
                related_comps=fdata.get("related_comps", []),
                iteration=fdata.get("iteration", 0),
            ))
        return state

    def save(self, workspace_root: Path) -> None:
        path = workspace_root / STATE_FILENAME
        path.write_text(self.to_json())

    @classmethod
    def load(cls, workspace_root: Path) -> ResearchState:
        path = workspace_root / STATE_FILENAME
        if path.exists():
            return cls.from_json(path.read_text())
        return cls()


# ---------------------------------------------------------------------------
# Shadow-state builder (Phase 1: parse from Markdown)
# ---------------------------------------------------------------------------

_H1_SECTION_RE = re.compile(r"^# (.+)", re.MULTILINE)


def _extract_h1_section(body: str, heading: str) -> str:
    """Extract the content of an H1 section by heading name."""
    pattern = re.compile(rf"^# {re.escape(heading)}\s*$", re.MULTILINE)
    m = pattern.search(body)
    if not m:
        return ""
    start = m.end()
    # Find next H1
    rest = body[start:]
    next_h1 = _H1_SECTION_RE.search(rest)
    end = start + next_h1.start() if next_h1 else len(body)
    return body[start:end].strip()


def _extract_hypothesis_sections(body: str) -> list[tuple[str, str, str]]:
    """Extract (id, title, body_text) for each ## WH-NNN / ## ER-NNN section."""
    results = []
    matches = list(_ER_WH_SECTION_RE.finditer(body))
    for i, m in enumerate(matches):
        hid = m.group(1)
        title = m.group(2).strip().rstrip("*").strip()
        content_start = m.end()
        # Content extends until the next ## or # header, or end of string
        if i + 1 < len(matches):
            content_end = matches[i + 1].start()
        else:
            # Find next # or ## header after this section
            rest = body[content_start:]
            end_match = _SECTION_END_RE.search(rest)
            content_end = content_start + end_match.start() if end_match else len(body)
        section_body = body[content_start:content_end].strip()
        results.append((hid, title, section_body))
    return results


def build_from_workspace(workspace: WorkspaceManager) -> ResearchState:
    """Build ResearchState by parsing current Markdown workspace files.

    This is the Phase 1 "shadow state" builder — Markdown remains
    authoritative; ResearchState is a structured mirror for queries.
    """
    from .markdown import (
        parse_frontmatter,
        _parse_comp_entries,
        _ER_WH_ID_RE,
        CRIT_HEADER_RE,
        CRIT_ID_RE,
    )

    state = ResearchState()

    # --- Hypotheses + top-level fields from RESEARCH_STATE.md ---
    research_text = workspace.read_file("RESEARCH_STATE.md")
    if research_text:
        meta, body = parse_frontmatter(research_text)
        state.iteration = meta.get("iteration", 0)
        state.status = meta.get("status", "in_progress")
        state.title = meta.get("title", "")

        # Extract top-level sections
        state.problem_statement = _extract_h1_section(body, "Problem Statement")
        state.conventions = _extract_h1_section(body, "Conventions")
        state.open_questions = _extract_h1_section(body, "Open Questions")

        for hid, title, section_body in _extract_hypothesis_sections(body):
            status = (HypothesisStatus.ESTABLISHED if hid.startswith("ER-")
                      else HypothesisStatus.WORKING)
            state.hypotheses[hid] = Hypothesis(
                id=hid,
                statement=title,
                status=status,
                derivation=section_body,
            )

    # --- Computations from COMPUTATION_LOG.md ---
    comp_text = workspace.read_file("COMPUTATION_LOG.md")
    if comp_text:
        entries = _parse_comp_entries(comp_text)
        for entry in entries:
            comp_id = entry["id"]
            if not comp_id.startswith("COMP-"):
                continue  # Skip TASK-* stubs from forced-call bailouts
            claim = entry.get("claim", "")
            # Determine target hypothesis: first ER/WH ID in claim, then body
            target_ids = _ER_WH_ID_RE.findall(claim)
            if not target_ids:
                target_ids = _ER_WH_ID_RE.findall(entry.get("body", ""))
            target = target_ids[0] if target_ids else ""

            verdict_str = entry.get("verdict", "INCONCLUSIVE")
            try:
                verdict = Verdict(verdict_str)
            except ValueError:
                verdict = Verdict.INCONCLUSIVE

            comp = Computation(
                id=comp_id,
                target_hypothesis=target,
                verdict=verdict,
                claim=claim,
                method=entry.get("method", ""),
                failure_detail=entry.get("notes", "") if verdict != Verdict.VERIFIED else "",
                notes=entry.get("notes", ""),
                result=entry.get("result", ""),
            )
            state.computations[comp_id] = comp

            # Link to hypothesis (alias-aware: check both WH-NNN and ER-NNN)
            linked = False
            if target and target in state.hypotheses:
                h = state.hypotheses[target]
                if comp_id not in h.supporting_comps:
                    h.supporting_comps.append(comp_id)
                linked = True
            if not linked and target and "-" in target:
                # Try the alias: WH-002 ↔ ER-002
                num = target.split("-")[1]
                alias = f"ER-{num}" if target.startswith("WH-") else f"WH-{num}"
                if alias in state.hypotheses:
                    h = state.hypotheses[alias]
                    if comp_id not in h.supporting_comps:
                        h.supporting_comps.append(comp_id)
                    comp.target_hypothesis = alias  # fix the stale reference

    # --- Critiques from CRITIQUE_LOG.md ---
    critique_text = workspace.read_file("CRITIQUE_LOG.md")
    if critique_text:
        crit_splits = CRIT_HEADER_RE.split(critique_text)
        crit_headers = CRIT_HEADER_RE.findall(critique_text)
        resolved_idx = critique_text.find("# Resolved Critiques")

        for header, body in zip(crit_headers, crit_splits[1:]):
            crit_id_match = CRIT_ID_RE.search(header)
            if not crit_id_match:
                continue
            crit_id = crit_id_match.group()

            # CRIT_HEADER_RE splits on "## CRIT-NNN"; severity/status
            # markers like [HIGH] [UNRESOLVED] land in the body's first line.
            first_line = (header + body.split("\n")[0]).upper()
            if "[HIGH]" in first_line:
                severity = Severity.HIGH
            elif "[LOW]" in first_line:
                severity = Severity.LOW
            else:
                severity = Severity.MEDIUM

            # Status
            if "[RESOLVED]" in first_line:
                crit_status = CritiqueStatus.RESOLVED
            elif "[WITHDRAWN]" in first_line:
                crit_status = CritiqueStatus.WITHDRAWN
            else:
                header_start = critique_text.find(header)
                if resolved_idx >= 0 and header_start > resolved_idx:
                    crit_status = CritiqueStatus.RESOLVED
                else:
                    crit_status = CritiqueStatus.ACTIVE

            targets = sorted(set(_ER_WH_ID_RE.findall(body)))

            resolution = ""
            res_match = re.search(r"\*\*Resolution:\*\*\s*(.+)", body)
            if res_match:
                resolution = res_match.group(1).strip()

            state.critiques[crit_id] = Critique(
                id=crit_id,
                targets=targets,
                severity=severity,
                argument=body.strip()[:500],
                status=crit_status,
                resolution=resolution,
            )

            # Link critiques to hypotheses
            for t in targets:
                if t in state.hypotheses:
                    if crit_id not in state.hypotheses[t].critiques:
                        state.hypotheses[t].critiques.append(crit_id)

    # --- Enrich computations from COMPUTATION_INDEX.jsonl ---
    jsonl_text = workspace.read_file("COMPUTATION_INDEX.jsonl")
    if jsonl_text:
        for line in jsonl_text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            comp_id = entry.get("id", "")
            if comp_id in state.computations:
                comp = state.computations[comp_id]
                comp.kind = entry.get("kind", comp.kind)
                comp.confidence = entry.get("confidence", comp.confidence)
                if entry.get("notes"):
                    comp.notes = entry["notes"]
                if entry.get("result"):
                    comp.result = entry["result"]
                if "no exit tool call" in entry.get("notes", ""):
                    comp.zero_output = True

    return state
