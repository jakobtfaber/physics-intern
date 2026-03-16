"""Deep Critic agent: adversarial review of research state."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..llm import LLMResponse
from ..markdown import (
    parse_frontmatter,
    insert_into_active_critiques,
    filter_self_retracted_critiques,
    render_frontmatter,
    ensure_critique_metadata_consistent,
    CRIT_ID_RE,
)
from .base import BaseAgent
from ..categories import CompensationCategory as CC
from ..workspace import log_scaffold_event

if TYPE_CHECKING:
    from ..task import Task


class CriticAgent(BaseAgent):
    name = "deep_critic"
    prompt_file = "deep_critic.md"

    def build_context(self, task: Task, iteration: int) -> str:
        parts = [
            "## RESEARCH_STATE.md\n",
            self.workspace.read_file("RESEARCH_STATE.md"),
            "\n## COMPUTATION_LOG.md\n",
            self.workspace.read_file("COMPUTATION_LOG.md"),
            "\n## Your Previous Critiques (do not repeat)\n",
            self.workspace.read_file("CRITIQUE_LOG.md"),
        ]
        return "\n".join(parts)

    def _strip_preamble(self, text: str) -> str:
        """Remove text before the first ## CRIT- heading."""
        match = re.search(r'^## CRIT', text, re.MULTILINE)
        if match:
            return text[match.start():]
        return text  # no CRIT header found — keep all (could be NO_CRITIQUES_FILED)

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Insert new critiques into Active section and update frontmatter counts."""
        stripped = self._strip_preamble(response.text)
        if stripped != response.text:
            log_scaffold_event(self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION, "preamble_stripped", "")
        filtered_text, retracted = filter_self_retracted_critiques(stripped)

        if filtered_text.strip():
            content = self.workspace.read_file("CRITIQUE_LOG.md")
            content = insert_into_active_critiques(content, filtered_text)
            self.workspace.write_file("CRITIQUE_LOG.md", content)
            # Write JSONL index entries for new critiques
            self._write_critique_index_entries(filtered_text, iteration)

        if retracted:
            log_scaffold_event(self.workspace.root, iteration, CC.OUTPUT_NORMALIZATION, "critique_self_retracted",
                               f"count={len(retracted)}")
            self._log_retractions(retracted, iteration)
            # Write WITHDRAWN entries to JSONL index
            for summary in retracted:
                crit_match = CRIT_ID_RE.search(summary)
                if crit_match:
                    entry = {
                        "id": crit_match.group(),
                        "status": "WITHDRAWN",
                        "iteration": iteration,
                        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                    self.workspace.append_file(
                        "CRITIQUE_INDEX.jsonl", json.dumps(entry, ensure_ascii=False) + "\n"
                    )

        self._update_critique_metadata()

    def _write_critique_index_entries(self, filtered_text: str, iteration: int):
        """Extract critique blocks from filtered text and write JSONL index entries."""
        # Split into individual critique blocks
        from ..markdown import CRIT_HEADER_RE
        headers = list(CRIT_HEADER_RE.finditer(filtered_text))
        splits = CRIT_HEADER_RE.split(filtered_text)
        if not headers:
            return

        for i, header_match in enumerate(headers):
            header = header_match.group()
            body = splits[i + 1] if i + 1 < len(splits) else ""

            crit_id_match = CRIT_ID_RE.search(header)
            crit_id = crit_id_match.group() if crit_id_match else f"CRIT-{iteration:03d}"

            # Extract severity from header
            severity = "MEDIUM"
            for sev in ("HIGH", "MEDIUM", "LOW"):
                if f"[{sev}]" in header.upper() + body.split("\n")[0].upper():
                    severity = sev
                    break

            # Extract status
            status = "WITHDRAWN" if "[WITHDRAWN]" in header + body.split("\n")[0] else "UNRESOLVED"

            # Extract target from **Target:** line
            target_match = re.search(r"\*\*Target:\*\*\s*(\S+)", body)
            target_id = target_match.group(1) if target_match else ""

            # Extract summary (first line of Phase 2 or first line of critique)
            summary = ""
            phase2_match = re.search(r"###\s*Phase\s*2.*?\n(.+?)(?:\n|$)", body, re.IGNORECASE)
            if phase2_match:
                summary = phase2_match.group(1).strip()[:200]

            entry = {
                "id": crit_id,
                "iteration": iteration,
                "severity": severity,
                "status": status,
                "target_id": target_id,
                "summary": summary,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self.workspace.append_file(
                "CRITIQUE_INDEX.jsonl", json.dumps(entry, ensure_ascii=False) + "\n"
            )

    def _log_retractions(self, retracted: list[str], iteration: int):
        """Log retracted critiques as HTML comments (invisible to agents) and alert."""
        lines = [f"<!-- Self-retracted critiques (iteration {iteration}):"]
        for summary in retracted:
            lines.append(f"  - {summary}")
        lines.append("-->")
        self.workspace.append_file("CRITIQUE_LOG.md", "\n" + "\n".join(lines) + "\n")
        self.metrics.alert(
            iteration,
            f"Critic self-retraction: {len(retracted)} critique(s) filtered",
        )

    def _update_critique_metadata(self):
        """Recount unresolved critiques and update frontmatter."""
        content = self.workspace.read_file("CRITIQUE_LOG.md")
        updated = ensure_critique_metadata_consistent(content)
        # Add last_critic_pass timestamp (critic-specific)
        meta, body = parse_frontmatter(updated)
        meta["last_critic_pass"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.workspace.write_file("CRITIQUE_LOG.md", render_frontmatter(meta, body))
