"""Tests for the independent verification script."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sciralph.verify import (
    WorkspaceContents,
    load_workspace,
    rerun_computations,
    build_verification_prompt,
    parse_verdict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RESEARCH_STATE = """---
iteration: 10
established_results: 3
---

# Research State

## Established Results

### ER-001: Schwarzschild metric
The line element is ds² = -(1-2M/r)dt² + (1-2M/r)⁻¹dr² + r²dΩ²

### ER-002: Surface gravity
κ = 1/(4M)

### ER-003: Hawking temperature
T_H = ℏκ/(2πk_B) = ℏ/(8πk_B M)
"""

CURRENT_TASK_TERMINATED = """---
task_type: terminate
reason: Research complete
iteration: 10
---

Research objectives achieved.
"""

CURRENT_TASK_NOT_TERMINATED = """---
task_type: research
iteration: 5
---

Continue investigating.
"""

CRITIQUE_LOG = """---
total_critiques: 1
---

# Active Critiques

## CRIT-001 [LOW] [UNRESOLVED]
- **Target:** ER-001
- **Filed:** iteration 4

### Phase 1: Reproduce
Standard Schwarzschild derivation.

### Phase 2: Objection
- **What is wrong:** Minor: signature convention not stated explicitly.

# Resolved Critiques
"""

COMPUTATION_LOG = """---
total_computations: 1
---

## Computation 1 — iteration 3
Verified ER-002 surface gravity numerically.
Verdict: VERIFIED
"""

WELL_FORMED_RESPONSE = """
Let me evaluate each result.

<verdict>VALID</verdict>

<confidence>HIGH</confidence>

<summary>
All three established results are correct. The Hawking temperature derivation
follows standard methods and arrives at the canonical result.
</summary>

<result_assessment>
ER-001: VALID
- Standard Schwarzschild metric, correctly stated.

ER-002: VALID
- Surface gravity κ = 1/(4M) is correct for Schwarzschild.

ER-003: VALID
- Hawking temperature follows from ER-002 via the standard Unruh-like argument.
</result_assessment>

<chain_valid>YES — Results form a correct logical chain: metric → surface gravity → temperature.</chain_valid>

<unresolved_concerns>
- Signature convention (-, +, +, +) is implicit but standard.
</unresolved_concerns>
"""

PARTIAL_RESPONSE = """
The results look mostly correct.

<verdict>PARTIALLY_VALID</verdict>

<summary>
Two of three results are correct but ER-002 has a potential issue.
</summary>
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _make_workspace(tmp_path, *, research_state=RESEARCH_STATE,
                    current_task=CURRENT_TASK_TERMINATED,
                    critique_log=CRITIQUE_LOG,
                    computation_log=COMPUTATION_LOG):
    """Create a mock workspace directory."""
    (tmp_path / "RESEARCH_STATE.md").write_text(research_state)
    (tmp_path / "CURRENT_TASK.md").write_text(current_task)
    (tmp_path / "CRITIQUE_LOG.md").write_text(critique_log)
    (tmp_path / "COMPUTATION_LOG.md").write_text(computation_log)
    return str(tmp_path)


def test_load_workspace(tmp_path):
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)

    assert contents.research_state == RESEARCH_STATE
    assert contents.critique_log == CRITIQUE_LOG
    assert contents.computation_log == COMPUTATION_LOG
    assert contents.terminated_cleanly is True
    assert contents.frontmatter.get("task_type") == "terminate"


def test_load_workspace_not_terminated(tmp_path):
    ws_dir = _make_workspace(tmp_path, current_task=CURRENT_TASK_NOT_TERMINATED)
    contents = load_workspace(ws_dir)

    assert contents.terminated_cleanly is False


def test_load_workspace_missing_files(tmp_path):
    """Gracefully handle missing workspace files."""
    # Only create RESEARCH_STATE
    (tmp_path / "RESEARCH_STATE.md").write_text(RESEARCH_STATE)
    contents = load_workspace(str(tmp_path))

    assert contents.research_state == RESEARCH_STATE
    assert contents.critique_log == ""
    assert contents.computation_log == ""
    assert contents.current_task == ""
    assert contents.terminated_cleanly is False


def test_load_workspace_with_scripts(tmp_path):
    ws_dir = _make_workspace(tmp_path)
    comp_dir = tmp_path / "computations"
    comp_dir.mkdir()
    (comp_dir / "check_001.py").write_text("print('ok')")
    (comp_dir / "check_002.py").write_text("print('ok')")

    contents = load_workspace(ws_dir)
    assert len(contents.computation_scripts) == 2


def test_parse_verdict_valid():
    result = parse_verdict(WELL_FORMED_RESPONSE)

    assert result.verdict == "VALID"
    assert result.confidence == "HIGH"
    assert "correct" in result.summary.lower()
    assert len(result.result_assessments) == 3
    assert result.result_assessments[0].result_id == "ER-001"
    assert result.result_assessments[0].verdict == "VALID"
    assert result.result_assessments[2].result_id == "ER-003"
    assert "YES" in result.chain_valid
    assert len(result.unresolved_concerns) >= 1
    assert len(result.parse_warnings) == 0


def test_parse_verdict_partial():
    """Missing tags should produce fallback with warnings."""
    result = parse_verdict(PARTIAL_RESPONSE)

    assert result.verdict == "PARTIALLY_VALID"
    assert "Two of three" in result.summary
    # Missing tags should generate warnings
    assert any("confidence" in w.lower() for w in result.parse_warnings)
    assert any("chain_valid" in w.lower() for w in result.parse_warnings)
    assert any("result_assessment" in w.lower() for w in result.parse_warnings)


def test_parse_verdict_empty():
    """Empty response should return INCONCLUSIVE with warnings."""
    result = parse_verdict("")

    assert result.verdict == "INCONCLUSIVE"
    assert len(result.parse_warnings) > 0


def test_build_prompt_includes_all_files(tmp_path):
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)
    system, user_content = build_verification_prompt(contents)

    assert "RESEARCH_STATE.md" in user_content
    assert "COMPUTATION_LOG.md" in user_content
    assert "CRITIQUE_LOG.md" in user_content
    assert "terminated cleanly" in user_content
    assert "ER-001" in user_content
    # System prompt should be the verifier prompt
    assert "scientific referee" in system.lower()


def test_build_prompt_not_terminated(tmp_path):
    ws_dir = _make_workspace(tmp_path, current_task=CURRENT_TASK_NOT_TERMINATED)
    contents = load_workspace(ws_dir)
    _, user_content = build_verification_prompt(contents)

    assert "did NOT terminate cleanly" in user_content


def test_build_prompt_with_rerun_results(tmp_path):
    from sciralph.sandbox import ExecutionResult
    from sciralph.verify import RerunResult

    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)

    rerun = [
        RerunResult(
            script_path="/tmp/check_001.py",
            execution=ExecutionResult(stdout="All checks passed\n", stderr="", returncode=0, timed_out=False),
        ),
        RerunResult(
            script_path="/tmp/check_002.py",
            execution=ExecutionResult(stdout="", stderr="Error!", returncode=1, timed_out=False),
        ),
    ]

    _, user_content = build_verification_prompt(contents, rerun_results=rerun)
    assert "Re-run Results" in user_content
    assert "check_001.py" in user_content
    assert "SUCCESS" in user_content
    assert "FAILED" in user_content


def test_terminated_cleanly_detection(tmp_path):
    """Frontmatter parsing correctly detects termination."""
    # Terminated
    ws_dir = _make_workspace(tmp_path, current_task=CURRENT_TASK_TERMINATED)
    contents = load_workspace(ws_dir)
    assert contents.terminated_cleanly is True

    # Not terminated — use a fresh tmp dir
    tmp2 = tmp_path / "ws2"
    tmp2.mkdir()
    ws_dir2 = _make_workspace(tmp2, current_task=CURRENT_TASK_NOT_TERMINATED)
    contents2 = load_workspace(ws_dir2)
    assert contents2.terminated_cleanly is False

    # Missing CURRENT_TASK
    tmp3 = tmp_path / "ws3"
    tmp3.mkdir()
    (tmp3 / "RESEARCH_STATE.md").write_text("# state")
    contents3 = load_workspace(str(tmp3))
    assert contents3.terminated_cleanly is False


def test_rerun_computations(tmp_path):
    """Re-run a trivial computation script via sandbox."""
    ws_dir = _make_workspace(tmp_path)
    comp_dir = tmp_path / "computations"
    comp_dir.mkdir()
    (comp_dir / "trivial.py").write_text("print(2 + 2)")

    results = rerun_computations(str(tmp_path), timeout=10)
    assert len(results) == 1
    assert results[0].execution.returncode == 0
    assert "4" in results[0].execution.stdout
