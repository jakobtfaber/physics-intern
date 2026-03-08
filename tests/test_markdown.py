"""Tests for markdown parsing utilities."""

import pytest
from pathlib import Path
from sciralph.markdown import (
    parse_frontmatter,
    render_frontmatter,
    tail_entries,
    extract_section_by_id,
    count_unresolved_critiques,
    insert_into_active_critiques,
    resolve_critique,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_frontmatter_basic():
    text = "---\ntitle: hello\nstatus: ok\n---\n\nBody here."
    meta, body = parse_frontmatter(text)
    assert meta["title"] == "hello"
    assert meta["status"] == "ok"
    assert "Body here." in body


def test_parse_frontmatter_no_frontmatter():
    text = "Just a body with no frontmatter."
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_parse_frontmatter_invalid_yaml():
    text = "---\n: broken yaml [[\n---\n\nBody."
    meta, body = parse_frontmatter(text)
    # Should not crash, returns fallback or empty
    assert isinstance(meta, dict)
    assert "Body." in body


def test_render_frontmatter():
    meta = {"title": "test", "status": "ok"}
    body = "Some content."
    result = render_frontmatter(meta, body)
    assert result.startswith("---\n")
    assert "title: test" in result
    assert "Some content." in result


def test_tail_entries():
    text = (FIXTURES / "computation_log.md").read_text()
    result = tail_entries(text, 1)
    assert "COMP-001" in result
    assert "COMP-002" not in result


def test_tail_entries_all():
    text = (FIXTURES / "computation_log.md").read_text()
    result = tail_entries(text, 5)
    assert "COMP-001" in result
    assert "COMP-002" in result


def test_extract_section_by_id():
    text = (FIXTURES / "critique_log.md").read_text()
    section = extract_section_by_id(text, "CRIT-001")
    assert "HIGH" in section
    assert "UNRESOLVED" in section


def test_extract_section_by_id_not_found():
    text = "## Section A\nContent A\n## Section B\nContent B"
    result = extract_section_by_id(text, "NONEXISTENT")
    assert result == ""


def test_count_unresolved_critiques():
    text = (FIXTURES / "critique_log.md").read_text()
    counts = count_unresolved_critiques(text)
    assert counts["HIGH"] == 1
    assert counts["MEDIUM"] == 1
    assert counts["LOW"] == 0


def test_count_unresolved_critiques_critique_prefix():
    """Test that CRITIQUE-NNN format (LLM drift) is also matched."""
    text = """
## CRITIQUE-001 [HIGH] [UNRESOLVED]
- **Target:** WH-1
- **Critique:** Something wrong.

## CRITIQUE-002 [LOW] [UNRESOLVED]
- **Target:** WH-2
"""
    counts = count_unresolved_critiques(text)
    assert counts["HIGH"] == 1
    assert counts["LOW"] == 1
    assert counts["MEDIUM"] == 0


def test_count_unresolved_critiques_empty():
    counts = count_unresolved_critiques("No critiques here.")
    assert counts == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}


# --- Tests for insert_into_active_critiques ---


def test_insert_into_active_critiques():
    text = (FIXTURES / "critique_log.md").read_text()
    new_critique = "## CRIT-004 [HIGH] [UNRESOLVED]\n- **Target:** WH-3\n- **Critique:** New issue.\n"
    result = insert_into_active_critiques(text, new_critique)

    # New critique should appear before Resolved section
    active_idx = result.find("# Active Critiques")
    resolved_idx = result.find("# Resolved Critiques")
    crit004_idx = result.find("CRIT-004")
    assert active_idx < crit004_idx < resolved_idx

    # Existing critiques should still be present
    assert "CRIT-001" in result
    assert "CRIT-002" in result
    assert "CRIT-003" in result


def test_insert_into_active_no_sections():
    text = "---\ntotal_critiques: 0\n---\n\nSome body.\n"
    new_critique = "## CRIT-001 [HIGH] [UNRESOLVED]\n- New.\n"
    result = insert_into_active_critiques(text, new_critique)
    assert "CRIT-001" in result


# --- Tests for resolve_critique ---


def test_resolve_critique():
    text = (FIXTURES / "critique_log.md").read_text()
    result = resolve_critique(text, "CRIT-001", "Verified by computation.")

    # CRIT-001 should now be RESOLVED
    assert "[RESOLVED]" in extract_section_by_id(result, "CRIT-001")
    assert "Verified by computation." in result

    # CRIT-001 should be in Resolved section, not Active
    resolved_idx = result.find("# Resolved Critiques")
    crit001_idx = result.find("CRIT-001")
    assert crit001_idx > resolved_idx

    # Unresolved count should drop
    counts = count_unresolved_critiques(result)
    assert counts["HIGH"] == 0
    assert counts["MEDIUM"] == 1  # CRIT-002 still unresolved


def test_resolve_critique_already_resolved():
    text = (FIXTURES / "critique_log.md").read_text()
    # CRIT-003 is already resolved — should return text unchanged
    result = resolve_critique(text, "CRIT-003", "Duplicate resolution.")
    assert result == text


def test_resolve_critique_not_found():
    text = (FIXTURES / "critique_log.md").read_text()
    result = resolve_critique(text, "CRIT-999", "Not found.")
    assert result == text
