"""Tests for the independent verification script."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sciralph.verify import (
    WorkspaceContents,
    FormalEvalResult,
    ProcessEvent,
    ProcessAuditResult,
    load_workspace,
    load_reference_file,
    rerun_computations,
    run_formal_evaluation,
    build_verification_prompt,
    build_process_audit_prompt,
    parse_verdict,
    parse_process_audit,
    append_process_audit_to_report,
    write_verification_report,
    _summarize_event_log,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RESEARCH_STATE = """---
iteration: 10
established_results: 3
---

# Research State

## Working Hypotheses (WH) and Established Results (ER)

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

EVIDENCE_LOG = """---
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
                    evidence_log=EVIDENCE_LOG):
    """Create a mock workspace directory."""
    (tmp_path / "RESEARCH_STATE.md").write_text(research_state)
    (tmp_path / "CURRENT_TASK.md").write_text(current_task)
    (tmp_path / "CRITIQUE_LOG.md").write_text(critique_log)
    (tmp_path / "EVIDENCE_LOG.md").write_text(evidence_log)
    return str(tmp_path)


def test_load_workspace(tmp_path):
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)

    assert contents.research_state == RESEARCH_STATE
    assert contents.critique_log == CRITIQUE_LOG
    assert contents.evidence_log == EVIDENCE_LOG
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
    assert contents.evidence_log == ""
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
    assert "EVIDENCE_LOG.md" in user_content
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


# ---------------------------------------------------------------------------
# Event log tests
# ---------------------------------------------------------------------------

EVENT_LOG_LINES = """\
{"kind":"scaffold","ts":"2026-03-13T14:00:00+00:00","iter":1,"category":"loop_control","event":"p1_budget_override","detail":"compute -> synthesize"}
{"kind":"llm_call","ts":"2026-03-13T14:00:01+00:00","agent":"orchestrator","iter":1,"model":"claude-sonnet-4-6","input_tokens":2000,"output_tokens":800,"stop_reason":"end_turn","duration_s":5.0,"system_prompt_chars":3000,"user_content_chars":1000,"response_chars":500,"reasoning_tokens":0,"answer_tokens":0,"round":0}
{"kind":"llm_call","ts":"2026-03-13T14:00:10+00:00","agent":"researcher","iter":2,"model":"claude-sonnet-4-6","input_tokens":5000,"output_tokens":2000,"stop_reason":"end_turn","duration_s":8.0,"system_prompt_chars":3000,"user_content_chars":4000,"response_chars":1500,"reasoning_tokens":0,"answer_tokens":0,"round":0}
{"kind":"scaffold","ts":"2026-03-13T14:00:20+00:00","iter":3,"category":"call_reliability","event":"forced_final_call","detail":"max_rounds"}
{"kind":"scaffold","ts":"2026-03-13T14:00:25+00:00","iter":3,"category":"call_reliability","event":"api_retry","detail":"attempt=1/3, TimeoutError"}
"""


def test_load_workspace_event_log(tmp_path):
    """EVENT_LOG.jsonl is loaded when include_process_data=True."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "EVENT_LOG.jsonl").write_text(EVENT_LOG_LINES)

    contents = load_workspace(ws_dir, include_process_data=True)
    assert contents.event_log == EVENT_LOG_LINES
    assert "llm_call" in contents.event_log



def test_summarize_event_log_llm_table():
    """LLM call summary table is generated."""
    summary = _summarize_event_log(EVENT_LOG_LINES)
    assert "orchestrator" in summary
    assert "researcher" in summary
    assert "LLM Calls by Agent" in summary
    assert "2,000" in summary  # orchestrator input_tokens


def test_summarize_event_log_scaffold_categories():
    """Scaffold events are grouped by category."""
    summary = _summarize_event_log(EVENT_LOG_LINES)
    assert "call_reliability" in summary or "loop_control" in summary
    assert "p1_budget_override" in summary


def test_summarize_event_log_key_timeline():
    """Key events appear in timeline."""
    summary = _summarize_event_log(EVENT_LOG_LINES)
    assert "forced_final_call" in summary
    assert "api_retry" in summary


def test_summarize_event_log_empty():
    """Empty input returns empty string."""
    assert _summarize_event_log("") == ""
    assert _summarize_event_log("   \n  ") == ""


def test_summarize_event_log_truncation():
    """Summary is capped at max_chars."""
    # Generate many events
    lines = []
    for i in range(500):
        lines.append(f'{{"kind":"scaffold","ts":"T","iter":{i},"category":"call_reliability","event":"api_retry","detail":"attempt={i}"}}')
    raw = "\n".join(lines)
    summary = _summarize_event_log(raw, max_chars=1000)
    assert len(summary) <= 1000


def test_build_process_audit_prompt_includes_event_log(tmp_path):
    """Event log summary appears in the process audit prompt."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "EVENT_LOG.jsonl").write_text(EVENT_LOG_LINES)

    contents = load_workspace(ws_dir, include_process_data=True)
    system, user_content = build_process_audit_prompt(contents)

    assert "Event Log Summary" in user_content
    assert "orchestrator" in user_content
    assert "LLM Calls by Agent" in user_content


def test_build_process_audit_prompt_no_event_log(tmp_path):
    """Missing event log shows 'Not available'."""
    ws_dir = _make_workspace(tmp_path)

    contents = load_workspace(ws_dir, include_process_data=True)
    _, user_content = build_process_audit_prompt(contents)

    assert "Event Log Summary" in user_content
    assert "Not available" in user_content


# ---------------------------------------------------------------------------
# Formal answer evaluation tests
# ---------------------------------------------------------------------------

HAWKING_PROBLEM_DEF = {
    "answer": "T_H = hbar * c**3 / (8 * sp.pi * G * M * k_B)\n",
    "answer_template": (
        "import sympy as sp\n\n"
        "hbar, c, G, M, k_B = sp.symbols('hbar c G M k_B', positive=True)\n\n"
        "def answer(hbar, c, G, M, k_B):\n"
        "    T_H = ...\n"
        "    return T_H\n"
    ),
}

CORRECT_ANSWER_MD = (
    "import sympy as sp\n\n"
    "hbar, c, G, M, k_B = sp.symbols('hbar c G M k_B', positive=True)\n\n"
    "def answer(hbar, c, G, M, k_B):\n"
    "    T_H = hbar * c**3 / (8 * sp.pi * G * M * k_B)\n"
    "    return T_H\n"
)

INCORRECT_ANSWER_MD = (
    "import sympy as sp\n\n"
    "hbar, c, G, M, k_B = sp.symbols('hbar c G M k_B', positive=True)\n\n"
    "def answer(hbar, c, G, M, k_B):\n"
    "    T_H = hbar * c**3 / (4 * sp.pi * G * M * k_B)\n"
    "    return T_H\n"
)


def test_formal_eval_correct_answer(tmp_path):
    """Correct ANSWER.md → correct=True."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "ANSWER.md").write_text(CORRECT_ANSWER_MD)

    result = run_formal_evaluation(ws_dir, HAWKING_PROBLEM_DEF)

    assert not result.skipped
    assert result.correct is True
    assert result.method  # should report which method succeeded


def test_formal_eval_incorrect_answer(tmp_path):
    """Wrong coefficient → correct=False."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "ANSWER.md").write_text(INCORRECT_ANSWER_MD)

    result = run_formal_evaluation(ws_dir, HAWKING_PROBLEM_DEF)

    assert not result.skipped
    assert result.correct is False


def test_formal_eval_skip_no_problem(tmp_path):
    """No problem_def → skipped."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "ANSWER.md").write_text(CORRECT_ANSWER_MD)

    result = run_formal_evaluation(ws_dir, None)

    assert result.skipped
    assert "No problem" in result.skip_reason


def test_formal_eval_skip_no_answer(tmp_path):
    """Problem without answer field → skipped."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "ANSWER.md").write_text(CORRECT_ANSWER_MD)

    result = run_formal_evaluation(ws_dir, {"problem": "something"})

    assert result.skipped
    assert "No problem" in result.skip_reason


def test_formal_eval_skip_no_template(tmp_path):
    """Problem with answer but no answer_template → skipped."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "ANSWER.md").write_text(CORRECT_ANSWER_MD)

    result = run_formal_evaluation(ws_dir, {"answer": "42"})

    assert result.skipped
    assert "answer_template" in result.skip_reason


def test_formal_eval_skip_no_answer_md(tmp_path):
    """ANSWER.md missing → skipped."""
    ws_dir = _make_workspace(tmp_path)
    # No ANSWER.md written

    result = run_formal_evaluation(ws_dir, HAWKING_PROBLEM_DEF)

    assert result.skipped
    assert "ANSWER.md" in result.skip_reason


def test_formal_eval_already_fenced(tmp_path):
    """ANSWER.md already has fences → should not double-fence."""
    ws_dir = _make_workspace(tmp_path)
    fenced = f"```python\n{CORRECT_ANSWER_MD}\n```"
    (tmp_path / "ANSWER.md").write_text(fenced)

    result = run_formal_evaluation(ws_dir, HAWKING_PROBLEM_DEF)

    assert not result.skipped
    assert result.correct is True


def test_formal_eval_prompt_inclusion(tmp_path):
    """Formal eval result is included in the verification prompt."""
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)

    correct_eval = FormalEvalResult(correct=True, method="simplify", details="diff=0")
    _, user_content = build_verification_prompt(contents, formal_eval=correct_eval)

    assert "Formal Answer Evaluation" in user_content
    assert "CORRECT" in user_content
    assert "simplify" in user_content


def test_formal_eval_prompt_skipped_not_included(tmp_path):
    """Skipped formal eval should NOT appear in prompt."""
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)

    skipped_eval = FormalEvalResult(skipped=True, skip_reason="No ANSWER.md")
    _, user_content = build_verification_prompt(contents, formal_eval=skipped_eval)

    assert "Formal Answer Evaluation" not in user_content


def test_formal_eval_report_writing(tmp_path):
    """formal_answer field appears in VERIFICATION.md frontmatter and body."""
    ws_dir = _make_workspace(tmp_path)
    contents = load_workspace(ws_dir)
    verification = parse_verdict(WELL_FORMED_RESPONSE)

    correct_eval = FormalEvalResult(correct=True, method="simplify", details="diff=0")
    write_verification_report(verification, ws_dir, formal_eval=correct_eval)

    report = (tmp_path / "VERIFICATION.md").read_text()
    assert "formal_answer: correct" in report
    assert "Formal Answer Evaluation: CORRECT" in report
    assert "simplify" in report


def test_formal_eval_report_without_formal(tmp_path):
    """No formal eval → no formal_answer in report."""
    ws_dir = _make_workspace(tmp_path)
    verification = parse_verdict(WELL_FORMED_RESPONSE)

    write_verification_report(verification, ws_dir)

    report = (tmp_path / "VERIFICATION.md").read_text()
    assert "formal_answer" not in report


# ---------------------------------------------------------------------------
# Reference file loading
# ---------------------------------------------------------------------------

def test_load_reference_file_with_python_tag(tmp_path, monkeypatch):
    """Reference file with ```python tag → extracts answer expression."""
    monkeypatch.setattr("sciralph.verify.REFERENCES_DIR", tmp_path)
    ref = tmp_path / "my_problem.md"
    ref.write_text("```python\ndelta = 3 * x + y\n```\n\n# Typical Good Run\n...")

    answer, content = load_reference_file(Path("problems/my_problem.yaml"))

    assert answer == "delta = 3 * x + y"
    assert "Typical Good Run" in content


def test_load_reference_file_without_tag(tmp_path, monkeypatch):
    """Reference file with bare ``` block → still extracts answer."""
    monkeypatch.setattr("sciralph.verify.REFERENCES_DIR", tmp_path)
    ref = tmp_path / "my_problem.md"
    ref.write_text("```\nF = 1 - p**2\n```\n\n# Run description")

    answer, content = load_reference_file(Path("problems/my_problem.yaml"))

    assert answer == "F = 1 - p**2"


def test_load_reference_file_not_found(tmp_path, monkeypatch):
    """No matching reference file → (None, None)."""
    monkeypatch.setattr("sciralph.verify.REFERENCES_DIR", tmp_path)

    answer, content = load_reference_file(Path("problems/nonexistent.yaml"))

    assert answer is None
    assert content is None


def test_load_reference_file_none_path():
    """None problem path → (None, None)."""
    answer, content = load_reference_file(None)

    assert answer is None
    assert content is None


def test_load_reference_file_no_code_block(tmp_path, monkeypatch):
    """Reference file without code block → answer is None, content is returned."""
    monkeypatch.setattr("sciralph.verify.REFERENCES_DIR", tmp_path)
    ref = tmp_path / "my_problem.md"
    ref.write_text("# Just a description\nNo code block here.")

    answer, content = load_reference_file(Path("problems/my_problem.yaml"))

    assert answer is None
    assert "Just a description" in content


# ---------------------------------------------------------------------------
# Formal eval fallback to reference file
# ---------------------------------------------------------------------------

def test_formal_eval_fallback_to_reference(tmp_path, monkeypatch):
    """Empty answer in YAML + reference file with answer → formal eval proceeds."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "ANSWER.md").write_text(CORRECT_ANSWER_MD)

    # Problem def with empty answer but valid template
    problem_def = dict(HAWKING_PROBLEM_DEF)
    problem_def["answer"] = ""

    # Mock reference file to return the correct answer
    ref_answer = HAWKING_PROBLEM_DEF["answer"]
    monkeypatch.setattr(
        "sciralph.verify.load_reference_file",
        lambda path: (ref_answer, "# reference content"),
    )

    result = run_formal_evaluation(ws_dir, problem_def, problem_path=Path("test.yaml"))

    assert not result.skipped
    assert result.correct is True


def test_formal_eval_no_fallback_when_answer_present(tmp_path, monkeypatch):
    """YAML has answer → reference file is not consulted."""
    ws_dir = _make_workspace(tmp_path)
    (tmp_path / "ANSWER.md").write_text(CORRECT_ANSWER_MD)

    # Track whether load_reference_file was called
    called = []
    monkeypatch.setattr(
        "sciralph.verify.load_reference_file",
        lambda path: (called.append(1), None) or (None, None),
    )

    result = run_formal_evaluation(ws_dir, HAWKING_PROBLEM_DEF, problem_path=Path("test.yaml"))

    assert not result.skipped
    assert result.correct is True
    assert len(called) == 0  # reference file not consulted


# ---------------------------------------------------------------------------
# build_verification_prompt with reference content
# ---------------------------------------------------------------------------

def test_build_prompt_with_reference_content():
    """Reference content is included in verification prompt."""
    contents = WorkspaceContents(
        workspace_dir="/tmp/test",
        research_state="# State", evidence_log="# Evidence", critique_log="# Critiques",
    )
    ref_content = "# Typical Good Run\nExpected: 5 iterations, VALID verdict."

    _, user_content = build_verification_prompt(contents, reference_content=ref_content)

    assert "## Reference Document" in user_content
    assert "Typical Good Run" in user_content
    assert "Expected: 5 iterations" in user_content


def test_build_prompt_without_reference_content():
    """No reference content → no Reference Document section."""
    contents = WorkspaceContents(
        workspace_dir="/tmp/test",
        research_state="# State", evidence_log="# Evidence", critique_log="# Critiques",
    )

    _, user_content = build_verification_prompt(contents, reference_content=None)

    assert "Reference Document" not in user_content
