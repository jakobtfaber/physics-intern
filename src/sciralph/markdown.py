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


def insert_into_active_critiques(text: str, new_content: str) -> str:
    """Insert new critique entries under '# Active Critiques', before '# Resolved Critiques'.

    If the headings are missing, falls back to appending to the body.
    """
    active_marker = "# Active Critiques"
    resolved_marker = "# Resolved Critiques"

    active_idx = text.find(active_marker)
    resolved_idx = text.find(resolved_marker)

    if active_idx == -1:
        # No Active section — append to end of body
        return text + "\n" + new_content

    if resolved_idx == -1 or resolved_idx < active_idx:
        # No Resolved section after Active — append after Active content
        return text + "\n" + new_content

    # Insert new content just before the "# Resolved Critiques" heading
    return text[:resolved_idx].rstrip() + "\n\n" + new_content.strip() + "\n\n" + text[resolved_idx:]


def resolve_critique(text: str, crit_id: str, resolution_note: str) -> str:
    """Move a critique from Active to Resolved, changing [UNRESOLVED] -> [RESOLVED].

    Adds a resolution note. Returns text unchanged if crit_id is not found
    or already resolved.
    """
    # Find the critique block (## CRIT-NNN ... until next ## or end)
    # Use a pattern that captures the full block
    pattern = re.compile(
        r'(#{2,3} CRIT(?:IQUE)?-' + re.escape(crit_id.split('-')[-1])
        + r'\s*\[(\w+)\]\s*\[UNRESOLVED\].*?)(?=\n#{1,3} |\Z)',
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return text  # Not found or already resolved

    block = match.group(1)
    # Mark as resolved and add resolution note
    resolved_block = block.replace("[UNRESOLVED]", "[RESOLVED]")
    resolved_block = resolved_block.rstrip() + f"\n- **Resolution:** {resolution_note}\n"

    # Remove from current position
    text_without = text[:match.start()] + text[match.end():]

    # Append to Resolved Critiques section
    resolved_marker = "# Resolved Critiques"
    resolved_idx = text_without.find(resolved_marker)
    if resolved_idx == -1:
        # No Resolved section — append at end
        return text_without.rstrip() + "\n\n# Resolved Critiques\n\n" + resolved_block
    else:
        insert_pos = resolved_idx + len(resolved_marker)
        return (
            text_without[:insert_pos].rstrip()
            + "\n\n"
            + resolved_block.strip()
            + "\n"
            + text_without[insert_pos:]
        )
