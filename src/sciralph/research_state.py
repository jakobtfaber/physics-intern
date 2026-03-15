"""Formal research state tracking.

Structured representation of hypotheses, computations, critiques, and their
relationships.  Phase 1 (shadow state) builds this from existing Markdown
files after each iteration; the Markdown files remain authoritative.
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

    def failures_for_hypothesis(self, hypothesis_id: str) -> list[FailedApproach]:
        """Failed approaches mentioning *hypothesis_id*."""
        return [
            fa for fa in self.failed_approaches
            if hypothesis_id in fa.description or hypothesis_id in " ".join(fa.related_comps)
        ]

    # --- Serialization ---

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ResearchState:
        data = json.loads(text)
        state = cls(iteration=data.get("iteration", 0))
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

    # --- Hypotheses from RESEARCH_STATE.md ---
    research_text = workspace.read_file("RESEARCH_STATE.md")
    if research_text:
        meta, body = parse_frontmatter(research_text)
        state.iteration = meta.get("iteration", 0)
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
            )
            state.computations[comp_id] = comp

            # Link to hypothesis
            if target and target in state.hypotheses:
                h = state.hypotheses[target]
                if comp_id not in h.supporting_comps:
                    h.supporting_comps.append(comp_id)

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

    return state
