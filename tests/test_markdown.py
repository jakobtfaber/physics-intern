"""Tests for markdown parsing utilities."""

import pytest
from pathlib import Path
from sciralph.markdown import (
    parse_frontmatter,
    render_frontmatter,
    tail_entries,
    extract_section_by_id,
    count_unresolved_critiques,
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
