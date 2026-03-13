"""Tests for the independent verification script."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sciralph.verify import (
    WorkspaceContents,
    ProcessEvent,
    ProcessAuditResult,
    load_workspace,
    rerun_computations,
    build_verification_prompt,
    build_process_audit_prompt,
    parse_verdict,
    parse_process_audit,
    append_process_audit_to_report,
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


def test_build_prompt_with_known_answer(tmp_path):
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)

    _, user_content = build_verification_prompt(contents, known_answer="0.7687")
    assert "Known Answer" in user_content
    assert "0.7687" in user_content


def test_build_prompt_without_known_answer(tmp_path):
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)

    _, user_content = build_verification_prompt(contents, known_answer=None)
    assert "Known Answer" not in user_content


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


def test_terminated_cleanly_via_research_state_status(tmp_path):
    """Detect clean termination from RESEARCH_STATE status even when
    CURRENT_TASK has a non-terminate task type (engine exit path 2)."""
    completed_state = """---
iteration: 8
status: completed
established_results: 3
---

# Research State
"""
    partially_complete_state = """---
iteration: 8
status: partially_complete
established_results: 1
---

# Research State
"""
    # status: completed + non-terminate task → should be clean
    ws1 = tmp_path / "ws_completed"
    ws1.mkdir()
    ws_dir1 = _make_workspace(ws1,
        current_task=CURRENT_TASK_NOT_TERMINATED,
        research_state=completed_state)
    contents1 = load_workspace(ws_dir1)
    assert contents1.terminated_cleanly is True

    # status: partially_complete + non-terminate task → should be clean
    ws2 = tmp_path / "ws_partial"
    ws2.mkdir()
    ws_dir2 = _make_workspace(ws2,
        current_task=CURRENT_TASK_NOT_TERMINATED,
        research_state=partially_complete_state)
    contents2 = load_workspace(ws_dir2)
    assert contents2.terminated_cleanly is True

    # No status in RESEARCH_STATE + non-terminate task → still not clean
    ws3 = tmp_path / "ws_no_status"
    ws3.mkdir()
    ws_dir3 = _make_workspace(ws3,
        current_task=CURRENT_TASK_NOT_TERMINATED)
    contents3 = load_workspace(ws_dir3)
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


# ---------------------------------------------------------------------------
# Process audit fixtures
# ---------------------------------------------------------------------------

METRICS_MD = """---
total_iterations: 10
total_input_tokens: 150000
total_output_tokens: 50000
---

# Metrics

| Iteration | Agent           | Input Tokens | Output Tokens |
|-----------|-----------------|--------------|---------------|
| 1         | orchestrator    | 5000         | 2000          |
| 2         | researcher      | 15000        | 5000          |
| 3         | computationalist| 10000        | 3000          |
| 4         | deep_critic     | 12000        | 4000          |
| 5         | orchestrator    | 8000         | 2500          |

## Alerts
- iteration 8: computationalist max rounds reached
"""

WELL_FORMED_PROCESS_RESPONSE = """
Let me analyze the multi-agent process.

<process_events>
EVENT-001 [SUCCESS] error_correction_cycle (iterations 3-7)
CRIT-001 identified missing c² factor in ER-002. Researcher corrected in iteration 5, computationalist re-verified (COMP-007 VERIFIED).
Evidence: CRIT-001, COMP-003, COMP-007

EVENT-002 [FAILURE] computation_stall (iterations 8-12)
Computationalist hit max rounds 3 times trying to verify ER-003 numerically. Same approach retried without adaptation.
Evidence: COMP-005, COMP-007, COMP-009

EVENT-003 [MIXED] good_sequencing (iterations 1-5)
Orchestrator correctly prioritized establishing the metric before computing derived quantities, but delayed critique resolution.
Evidence: ER-001, ER-002
</process_events>

<process_verdict>PARTIALLY_EFFECTIVE</process_verdict>

<process_summary>
The multi-agent system showed good error correction capability (CRIT-001 cycle) but suffered from computation stalls in later iterations. Budget usage was acceptable but could be improved by breaking stalls earlier.
</process_summary>

<token_efficiency>
Total tokens: 200k input, 65k output. Approximately 15% wasted on the computation stall in iterations 8-12. The researcher and computationalist consumed the bulk of the budget, which is expected for a derivation-heavy problem.
</token_efficiency>

<recommendations>
- Add stall detection for repeated INCONCLUSIVE verdicts on the same claim
- Increase critique resolution priority to avoid carrying unresolved HIGH critiques
- Consider adaptive compute timeout based on problem complexity
</recommendations>
"""

PARTIAL_PROCESS_RESPONSE = """
The process had issues.

<process_verdict>INEFFECTIVE</process_verdict>

<process_summary>
Multiple unresolved critiques and repeated stalls.
</process_summary>
"""


# ---------------------------------------------------------------------------
# Process audit tests
# ---------------------------------------------------------------------------

def test_parse_process_audit_well_formed():
    """All XML tags parsed correctly from a well-formed response."""
    result = parse_process_audit(WELL_FORMED_PROCESS_RESPONSE)

    assert result.verdict == "PARTIALLY_EFFECTIVE"
    assert "error correction" in result.summary.lower()
    assert len(result.events) == 3

    # Check first event
    ev0 = result.events[0]
    assert ev0.event_id == "EVENT-001"
    assert ev0.classification == "SUCCESS"
    assert ev0.event_type == "error_correction_cycle"
    assert ev0.iterations == "iterations 3-7"
    assert "CRIT-001" in ev0.description
    assert "CRIT-001" in ev0.evidence

    # Check second event
    ev1 = result.events[1]
    assert ev1.event_id == "EVENT-002"
    assert ev1.classification == "FAILURE"
    assert ev1.event_type == "computation_stall"

    # Check third event
    ev2 = result.events[2]
    assert ev2.classification == "MIXED"

    assert "200k" in result.token_efficiency or "Total tokens" in result.token_efficiency
    assert len(result.recommendations) == 3
    assert len(result.parse_warnings) == 0


def test_parse_process_audit_partial():
    """Missing tags should produce warnings, not crashes."""
    result = parse_process_audit(PARTIAL_PROCESS_RESPONSE)

    assert result.verdict == "INEFFECTIVE"
    assert "unresolved" in result.summary.lower()
    # Missing tags should generate warnings
    assert any("token_efficiency" in w.lower() for w in result.parse_warnings)
    assert any("recommendations" in w.lower() for w in result.parse_warnings)
    assert any("process_events" in w.lower() for w in result.parse_warnings)


def test_parse_process_audit_empty():
    """Empty response defaults to INEFFECTIVE with warnings."""
    result = parse_process_audit("")

    assert result.verdict == "INEFFECTIVE"
    assert len(result.parse_warnings) > 0
    assert any("process_verdict" in w.lower() for w in result.parse_warnings)


def test_build_process_audit_prompt_includes_metrics(tmp_path):
    """METRICS.md content should appear in user content."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "METRICS.md").write_text(METRICS_MD)

    contents = load_workspace(ws_dir, include_process_data=True)
    system, user_content = build_process_audit_prompt(contents)

    assert "METRICS.md" in user_content
    assert "total_iterations" in user_content
    assert "computationalist max rounds" in user_content
    # System prompt should be the process auditor prompt
    assert "process auditor" in system.lower()


def test_build_process_audit_prompt_includes_git_log(tmp_path):
    """Git log should appear in user content when available."""
    ws_dir = _make_workspace(tmp_path)
    # Simulate by setting git_log directly on contents
    contents = load_workspace(ws_dir)
    contents.git_log = "abc1234 iteration 1: orchestrator\ndef5678 iteration 2: researcher"
    system, user_content = build_process_audit_prompt(contents)

    assert "Git Log" in user_content
    assert "abc1234" in user_content
    assert "iteration 2: researcher" in user_content


def test_load_workspace_with_process_data(tmp_path):
    """include_process_data=True loads METRICS.md."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "METRICS.md").write_text(METRICS_MD)

    # Without process data
    contents_basic = load_workspace(ws_dir)
    assert contents_basic.metrics_md == ""
    assert contents_basic.git_log == ""

    # With process data
    contents_full = load_workspace(ws_dir, include_process_data=True)
    assert "total_iterations" in contents_full.metrics_md


def test_load_workspace_with_process_data_no_metrics(tmp_path):
    """Missing METRICS.md should not crash, just leave field empty."""
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir, include_process_data=True)
    assert contents.metrics_md == ""


def test_append_process_audit_patches_frontmatter(tmp_path):
    """Appending process audit should add process_verdict to YAML frontmatter."""
    ws_dir = _make_workspace(tmp_path)
    report_path = tmp_path / "VERIFICATION.md"
    report_path.write_text("---\nverdict: VALID\nconfidence: HIGH\n---\n\n# Verification Report\n\n## Summary\nAll good.\n")

    pa_result = ProcessAuditResult(
        verdict="PARTIALLY_EFFECTIVE",
        summary="Good error correction but some stalls.",
        events=[ProcessEvent(
            event_id="EVENT-001",
            classification="SUCCESS",
            event_type="error_correction_cycle",
            iterations="iterations 3-7",
            description="Fixed missing factor.",
            evidence="CRIT-001, COMP-003",
        )],
        token_efficiency="200k tokens, 15% waste.",
        recommendations=["Detect stalls earlier", "Prioritize HIGH critiques"],
    )

    append_process_audit_to_report(pa_result, ws_dir)

    report = report_path.read_text()
    # Frontmatter should have process_verdict
    assert "process_verdict: PARTIALLY_EFFECTIVE" in report
    # Original verdict should still be there
    assert "verdict: VALID" in report
    # Process audit sections should be present
    assert "# Process Audit" in report
    assert "Process Summary" in report
    assert "Good error correction" in report
    assert "EVENT-001" in report
    assert "Token Efficiency" in report
    assert "Recommendations" in report
    assert "Detect stalls earlier" in report


def test_append_process_audit_creates_report_if_missing(tmp_path):
    """Should create VERIFICATION.md if it doesn't exist."""
    ws_dir = _make_workspace(tmp_path)
    report_path = tmp_path / "VERIFICATION.md"
    assert not report_path.exists()

    pa_result = ProcessAuditResult(
        verdict="EFFECTIVE",
        summary="Excellent process.",
    )

    append_process_audit_to_report(pa_result, ws_dir)

    report = report_path.read_text()
    assert "process_verdict: EFFECTIVE" in report
    assert "# Process Audit" in report
