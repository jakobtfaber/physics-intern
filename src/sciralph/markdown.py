"""Markdown parsing with YAML frontmatter support."""

import re
import yaml


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SECTION_RE = re.compile(r"^## .+", re.MULTILINE)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split text into (frontmatter_dict, body). Returns ({}, text) on failure."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    yaml_str = match.group(1)
    body = text[match.end():]
    try:
        meta = yaml.safe_load(yaml_str)
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = _fallback_parse(yaml_str)
    return meta, body


def _fallback_parse(yaml_str: str) -> dict:
    """Regex fallback for broken YAML frontmatter."""
    result = {}
    for line in yaml_str.strip().splitlines():
        m = re.match(r'^(\w[\w_]*)\s*:\s*(.+)$', line)
        if m:
            key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            # Try to parse as int
            try:
                val = int(val)
            except (ValueError, TypeError):
                pass
            result[key] = val
    return result


def render_frontmatter(meta: dict, body: str) -> str:
    """Render a dict + body back into frontmatter Markdown."""
    yaml_str = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{yaml_str}\n---\n\n{body}"


def tail_entries(text: str, n: int) -> str:
    """Return the last N '## ' sections from text."""
    parts = re.split(r'(?=^## )', text, flags=re.MULTILINE)
    # First part is everything before the first ## section (frontmatter + intro)
    sections = [p for p in parts if p.startswith("## ")]
    if not sections:
        return text
    selected = sections[-n:]
    return "\n".join(selected)


def extract_section_by_id(text: str, section_id: str) -> str:
    """Extract a section whose heading contains section_id (e.g. 'CRIT-010')."""
    parts = re.split(r'(?=^## )', text, flags=re.MULTILINE)
    for part in parts:
        if section_id in part:
            return part.strip()
    return ""


def count_unresolved_critiques(text: str) -> dict[str, int]:
    """Count unresolved critiques by severity from CRITIQUE_LOG.md content."""
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    # Match patterns like ## CRIT-010 [HIGH] [UNRESOLVED]
    for match in re.finditer(r'#{2,3} CRIT(?:IQUE)?-\d+\s*\[(\w+)\]\s*\[UNRESOLVED\]', text):
        severity = match.group(1).upper()
        if severity in counts:
            counts[severity] += 1
    return counts
