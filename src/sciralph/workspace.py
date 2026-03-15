"""Workspace manager for SciRalph file I/O and git operations."""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .markdown import render_frontmatter


class WorkspaceManager:
    """Manages the workspace directory, file I/O, and git."""

    def __init__(self, config: Config):
        self.root = Path(config.workspace_dir)
        self.computations_dir = self.root / "computations"
        self.archive_dir = self.root / "archive"
        self.logs_dir = self.root / "logs"

    def init(self, problem: str):
        """Create workspace, initialize all .md files, git init."""
        self.problem_statement = problem.strip()
        self.root.mkdir(parents=True, exist_ok=True)
        self.computations_dir.mkdir(exist_ok=True)
        self.archive_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # RESEARCH_STATE.md
        research_meta = {
            "problem_id": "research-session",
            "title": problem[:80],
            "status": "not_started",
            "last_updated": now,
            "iteration": 0,
        }
        research_body = f"""# Problem Statement

{problem.strip()}

# Conventions

(To be populated by the orchestrator as conventions become clear.)

# Established Results

Results here have survived critique and computational verification.

# Working Hypotheses

Not yet fully verified. Subject to critique.

# Dead Ends

# Open Questions
"""
        self.write_file("RESEARCH_STATE.md", render_frontmatter(research_meta, research_body))

        # CRITIQUE_LOG.md
        critique_meta = {
            "total_critiques": 0,
            "unresolved_high": 0,
            "unresolved_medium": 0,
            "unresolved_low": 0,
            "last_critic_pass": "never",
        }
        self.write_file("CRITIQUE_LOG.md", render_frontmatter(
            critique_meta, "# Active Critiques\n\n# Resolved Critiques\n"))

        # COMPUTATION_LOG.md
        comp_meta = {
            "total_computations": 0,
            "last_computation": "never",
        }
        self.write_file("COMPUTATION_LOG.md", render_frontmatter(
            comp_meta, "# Computations\n"))

        # METRICS.md
        self.write_file("METRICS.md", render_frontmatter(
            {"total_iterations": 0, "total_llm_calls": 0,
             "total_input_tokens": 0, "total_output_tokens": 0,
             "max_tokens_reached_count": 0, "retries": 0},
            "# Per-Iteration Metrics\n\n# Alerts\n"))

        # Git init
        subprocess.run(["git", "init"], cwd=str(self.root),
                        capture_output=True, check=False)
        subprocess.run(["git", "add", "-A"], cwd=str(self.root),
                        capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", "Initial workspace setup"],
                        cwd=str(self.root), capture_output=True, check=False)

    def read_file(self, filename: str) -> str:
        """Read a file from the workspace. Returns empty string if missing."""
        path = self.root / filename
        if path.exists():
            return path.read_text()
        return ""

    def write_file(self, filename: str, content: str):
        """Write content to a workspace file."""
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def append_file(self, filename: str, content: str):
        """Append content to a workspace file."""
        path = self.root / filename
        with open(path, "a") as f:
            f.write(content)

    def read_file_tail(self, filename: str, n_entries: int = 5) -> str:
        """Read the last N ## sections from a file."""
        from .markdown import tail_entries
        text = self.read_file(filename)
        if not text:
            return ""
        return tail_entries(text, n_entries)

    def file_size(self, filename: str) -> int:
        """Return the character count of a file. 0 if missing."""
        path = self.root / filename
        if path.exists():
            return len(path.read_text())
        return 0

    def delete_file(self, filename: str):
        """Delete a file from the workspace if it exists."""
        path = self.root / filename
        if path.exists():
            path.unlink()

    def file_exists(self, filename: str) -> bool:
        """Check if a file exists in the workspace."""
        return (self.root / filename).exists()

    def archive_file(self, filename: str):
        """Archive a file before compression."""
        src = self.root / filename
        if not src.exists():
            return
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = self.archive_dir / f"{src.stem}_{now}{src.suffix}"
        shutil.copy2(str(src), str(dest))

    def validate_comp_references(self) -> list[str]:
        """Strip phantom COMP/TASK references from RESEARCH_STATE.md.

        Returns list of stripped reference IDs for alerting.
        """
        from .markdown import _parse_comp_entries, flatten_unverified_brackets
        from .validation import _build_task_comp_mapping

        state = self.read_file("RESEARCH_STATE.md")
        comp_log = self.read_file("COMPUTATION_LOG.md")

        # Flatten nested bracket markers first
        state = flatten_unverified_brackets(state)

        # Get valid IDs from COMPUTATION_LOG
        entries = _parse_comp_entries(comp_log)
        valid_ids = {e["id"] for e in entries}

        # Expand valid_ids: accept TASK-NNN when a corresponding COMP exists
        task_comp = _build_task_comp_mapping(entries)
        for task_id, comp_set in task_comp.items():
            if comp_set & valid_ids:
                valid_ids.add(task_id)

        # Find bare COMP-NNN / TASK-NNN references (exclude already-wrapped)
        ref_pattern = re.compile(r'(?<!\[)\b((?:COMP|TASK)-\d+)\b(?!:unverified\])')
        found_refs = set(ref_pattern.findall(state))

        # Identify phantoms
        phantoms = sorted(found_refs - valid_ids)

        # Write back if we flattened brackets or found phantoms
        original = self.read_file("RESEARCH_STATE.md")
        if not phantoms and state == original:
            return []

        # Strip phantom references: replace bare "COMP-NNN" with "[COMP-NNN:unverified]"
        for phantom in phantoms:
            state = re.sub(
                r'(?<!\[)\b' + re.escape(phantom) + r'\b(?!:unverified\])',
                f'[{phantom}:unverified]',
                state,
            )

        self.write_file("RESEARCH_STATE.md", state)
        return phantoms

    def git_commit(self, message: str):
        """Stage all changes and commit."""
        subprocess.run(["git", "add", "-A"], cwd=str(self.root),
                        capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", message, "--allow-empty"],
                        cwd=str(self.root), capture_output=True, check=False)


def log_scaffold_event(
    workspace_dir: str | Path,
    iteration: int,
    category: str,
    event: str,
    detail: str = "",
) -> None:
    """Append one scaffold event to EVENT_LOG.jsonl. Never raises."""
    try:
        entry = {
            "kind": "scaffold",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "iter": iteration,
            "category": category,
            "event": event,
            "detail": detail,
        }
        with open(Path(workspace_dir) / "EVENT_LOG.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_llm_call(
    workspace_dir: str | Path,
    agent: str,
    iteration: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    stop_reason: str,
    duration_s: float,
    system_prompt_chars: int,
    user_content_chars: int,
    response_chars: int,
    reasoning_tokens: int = 0,
    answer_tokens: int = 0,
    round: int = 0,
) -> None:
    """Append one LLM-call event to EVENT_LOG.jsonl. Never raises."""
    try:
        entry = {
            "kind": "llm_call",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent": agent,
            "iter": iteration,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "stop_reason": stop_reason,
            "duration_s": duration_s,
            "system_prompt_chars": system_prompt_chars,
            "user_content_chars": user_content_chars,
            "response_chars": response_chars,
            "reasoning_tokens": reasoning_tokens,
            "answer_tokens": answer_tokens,
            "round": round,
        }
        with open(Path(workspace_dir) / "EVENT_LOG.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
