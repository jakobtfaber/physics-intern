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
    filter_self_retracted_critiques,
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


def test_resolve_critique_with_phase_subheadings():
    """Resolving a critique with ### Phase 1/Phase 2 removes the entire block from Active."""
    text = """\
---
total_critiques: 2
unresolved_high: 1
unresolved_medium: 1
---

# Active Critiques

## CRIT-010 [HIGH] [UNRESOLVED]
- **Target:** WH-002
- **Filed:** iteration 3

### Phase 1: Reproduce
1. Start with metric ansatz
2. Apply field equations

### Phase 2: Objection
- **What is wrong:** Missing factor of 2 in normalization
- **Why it matters:** Changes final temperature by factor 2

## CRIT-011 [MEDIUM] [UNRESOLVED]
- **Target:** ER-001
- **Filed:** iteration 4
- **Critique:** Check boundary conditions.

# Resolved Critiques
"""
    result = resolve_critique(text, "CRIT-010", "Factor of 2 corrected.")

    # CRIT-010 should be in Resolved section
    resolved_idx = result.find("# Resolved Critiques")
    crit010_idx = result.find("CRIT-010")
    assert crit010_idx > resolved_idx

    # Phase 1/Phase 2 content must NOT remain in Active section
    active_idx = result.find("# Active Critiques")
    active_section = result[active_idx:resolved_idx]
    assert "Phase 1" not in active_section, "Phase 1 body orphaned in Active"
    assert "Phase 2" not in active_section, "Phase 2 body orphaned in Active"
    assert "metric ansatz" not in active_section
    assert "Missing factor of 2" not in active_section

    # Phase content SHOULD be in Resolved section
    resolved_section = result[resolved_idx:]
    assert "Phase 1" in resolved_section
    assert "Phase 2" in resolved_section
    assert "Factor of 2 corrected." in resolved_section

    # CRIT-011 should still be in Active
    assert "CRIT-011" in active_section
    counts = count_unresolved_critiques(result)
    assert counts["HIGH"] == 0
    assert counts["MEDIUM"] == 1


# --- Tests for filter_self_retracted_critiques ---


def test_filter_mixed_response():
    """One genuine MEDIUM + one self-retracted LOW → only MEDIUM survives."""
    text = """\
## CRIT-010 [MEDIUM] [UNRESOLVED]
- **Target:** WH-002
- **Filed:** iteration 3

### Phase 1: Reproduce
1. Start with metric ansatz
2. Apply field equations

### Phase 2: Objection
- **What is wrong:** Missing factor of 2 in normalization
- **Why it matters:** Changes final temperature by factor 2

## CRIT-011 [LOW] [UNRESOLVED]
- **Target:** WH-003
- **Filed:** iteration 3

### Phase 1: Reproduce
1. Start with Hawking formula
2. Derive entropy expression
3. Matches claimed result

### Phase 2: Objection
- Reproduction succeeded, no issues found. Documenting what was checked.
- **Suggested verification:** None needed.
"""
    filtered, retracted = filter_self_retracted_critiques(text)
    assert "CRIT-010" in filtered
    assert "CRIT-011" not in filtered
    assert len(retracted) == 1
    assert "CRIT-011" in retracted[0]
    assert "WH-003" in retracted[0]


def test_filter_genuine_low_kept():
    """LOW with real objection (notation issue) → not filtered."""
    text = """\
## CRIT-012 [LOW] [UNRESOLVED]
- **Target:** WH-001
- **Filed:** iteration 2

### Phase 1: Reproduce
1. Follow the derivation step by step.

### Phase 2: Objection
- **What is wrong:** Inconsistent notation: kappa used for both surface gravity and a coupling constant.
- **Why it matters:** Could confuse later derivations.
- **Suggested verification:** Notation audit.
"""
    filtered, retracted = filter_self_retracted_critiques(text)
    assert "CRIT-012" in filtered
    assert retracted == []


def test_filter_all_retracted():
    """Two self-retracted LOWs → empty filtered text, two retracted summaries."""
    text = """\
## CRIT-013 [LOW] [UNRESOLVED]
- **Target:** WH-001
- **Filed:** iteration 4

### Phase 1: Reproduce
1. Verified all steps.

### Phase 2: Objection
- Reproduction succeeded, no issues found.
- **Suggested verification:** None needed.

## CRIT-014 [LOW] [UNRESOLVED]
- **Target:** ER-001
- **Filed:** iteration 4

### Phase 1: Reproduce
1. Checked the algebra.

### Phase 2: Objection
- Reproduction succeeded, no flaws detected. Documenting successful verification.
"""
    filtered, retracted = filter_self_retracted_critiques(text)
    assert filtered.strip() == ""
    assert len(retracted) == 2


def test_filter_no_critiques_filed_marker():
    """NO_CRITIQUES_FILED marker → empty text, empty retracted list."""
    text = "NO_CRITIQUES_FILED: Reviewed 5 claims, no issues found."
    filtered, retracted = filter_self_retracted_critiques(text)
    assert filtered == ""
    assert retracted == []


def test_filter_high_never_filtered():
    """HIGH with retraction-like language → never suppressed."""
    text = """\
## CRIT-015 [HIGH] [UNRESOLVED]
- **Target:** WH-004
- **Filed:** iteration 5

### Phase 1: Reproduce
1. Attempted to reproduce the entropy bound derivation.

### Phase 2: Objection
- Reproduction succeeded, no issues found in algebra, BUT the boundary
  condition is applied at the wrong surface.
- **Why it matters:** Invalidates the area law derivation.
"""
    filtered, retracted = filter_self_retracted_critiques(text)
    assert "CRIT-015" in filtered
    assert retracted == []


def test_filter_critique_nnn_drift_tolerance():
    """CRITIQUE-NNN format (LLM drift) is also handled."""
    text = """\
## CRITIQUE-015 [LOW] [UNRESOLVED]
- **Target:** WH-005
- **Filed:** iteration 6

### Phase 1: Reproduce
1. Steps match claimed result.

### Phase 2: Objection
- Reproduction succeeded, no errors. Documenting successful verification.
- **Suggested verification:** None needed.
"""
    filtered, retracted = filter_self_retracted_critiques(text)
    assert "CRITIQUE-015" not in filtered
    assert len(retracted) == 1
    assert "CRITIQUE-015" in retracted[0]
