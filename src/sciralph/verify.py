"""Independent verification of SciRalph research workspaces."""

import json
import glob
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table

import time

import anthropic
import yaml

from .config import Config, DEFAULTS
from .llm import LLMResponse  # noqa: F401 — reuse dataclass, call via streaming
from .utils.markdown import parse_frontmatter
from .utils.sandbox import ExecutionResult, execute_python

console = Console()

PROMPTS_DIR = Path(__file__).parent / "prompts"
REFERENCES_DIR = Path(__file__).parent.parent.parent / "references"


def _call_llm_streaming(system: str, user_content: str, config: Config) -> LLMResponse:
    """Call the Anthropic API with streaming (required for Opus with high max_tokens)."""
    client = anthropic.Anthropic(api_key=config.api_key)
    start = time.time()

    chunks: list[str] = []
    input_tokens = 0
    output_tokens = 0
    stop_reason = ""

    with client.messages.stream(
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        for event in stream:
            if hasattr(event, "type"):
                if event.type == "content_block_delta" and hasattr(event.delta, "text"):
                    chunks.append(event.delta.text)
                elif event.type == "message_delta" and hasattr(event, "usage"):
                    output_tokens = event.usage.output_tokens
                    if hasattr(event.delta, "stop_reason"):
                        stop_reason = event.delta.stop_reason
                elif event.type == "message_start" and hasattr(event, "message"):
                    input_tokens = event.message.usage.input_tokens

    duration = time.time() - start
    return LLMResponse(
        text="".join(chunks),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=stop_reason or "end_turn",
        duration=duration,
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WorkspaceContents:
    """Loaded workspace files and metadata."""
    workspace_dir: str
    research_state: str = ""
    critique_log: str = ""
    evidence_log: str = ""
    current_task: str = ""
    computation_scripts: list[str] = field(default_factory=list)
    terminated_cleanly: bool = False
    frontmatter: dict = field(default_factory=dict)
    metrics_md: str = ""
    git_log: str = ""
    event_log: str = ""
    background_survey: str = ""


@dataclass
class ResultAssessment:
    """Per-ER verdict from the verifier."""
    result_id: str
    verdict: str  # VALID / INVALID / UNCERTAIN
    notes: str = ""


@dataclass
class RerunResult:
    """Result of re-running a computation script."""
    script_path: str
    execution: ExecutionResult | None = None


@dataclass
class VerificationResult:
    """Overall verification outcome."""
    verdict: str  # VALID / PARTIALLY_VALID / INVALID / INCONCLUSIVE
    confidence: str = ""
    summary: str = ""
    result_assessments: list[ResultAssessment] = field(default_factory=list)
    chain_valid: str = ""
    unresolved_concerns: list[str] = field(default_factory=list)
    raw_response: str = ""
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class FormalEvalResult:
    """Result of formal (symbolic/numerical) answer evaluation."""
    correct: bool | None = None  # True/False/None (errored)
    method: str = ""
    error: str | None = None
    details: str = ""
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class ProcessEvent:
    """A single process event identified by the process auditor."""
    event_id: str
    classification: str  # SUCCESS / FAILURE / MIXED
    event_type: str
    iterations: str = ""
    description: str = ""
    evidence: str = ""


@dataclass
class ProcessAuditResult:
    """Result of the process audit pass."""
    verdict: str  # EFFECTIVE / PARTIALLY_EFFECTIVE / INEFFECTIVE
    summary: str = ""
    events: list[ProcessEvent] = field(default_factory=list)
    token_efficiency: str = ""
    recommendations: list[str] = field(default_factory=list)
    raw_response: str = ""
    parse_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Load workspace
# ---------------------------------------------------------------------------

def load_workspace(workspace_dir: str, *, include_process_data: bool = False) -> WorkspaceContents:
    """Read all relevant files from a completed workspace.

    Args:
        workspace_dir: Path to the workspace directory.
        include_process_data: If True, also load METRICS.md and git log
            (needed for process audit but not for science verification).
    """
    ws = Path(workspace_dir)
    contents = WorkspaceContents(workspace_dir=workspace_dir)

    for fname, attr in [
        ("RESEARCH_STATE.md", "research_state"),
        ("CRITIQUE_LOG.md", "critique_log"),
        ("EVIDENCE_LOG.md", "evidence_log"),
        ("CURRENT_TASK.md", "current_task"),
    ]:
        path = ws / fname
        if path.exists():
            setattr(contents, attr, path.read_text())

    # Parse frontmatter from CURRENT_TASK to check termination
    if contents.current_task:
        fm, _ = parse_frontmatter(contents.current_task)
        contents.frontmatter = fm
        contents.terminated_cleanly = fm.get("task_type") == "terminate"

    # Also check RESEARCH_STATE status — the engine may exit via
    # _should_terminate() which sets status: completed/partially_complete
    # without writing a terminate task to CURRENT_TASK.
    if not contents.terminated_cleanly and contents.research_state:
        rs_fm, _ = parse_frontmatter(contents.research_state)
        rs_status = rs_fm.get("status", "")
        if rs_status in ("completed", "partially_complete"):
            contents.terminated_cleanly = True

    # Glob computation scripts
    scripts = sorted(glob.glob(str(ws / "computations" / "*.py")))
    contents.computation_scripts = scripts

    # Process audit data (optional)
    if include_process_data:
        metrics_path = ws / "METRICS.md"
        if metrics_path.exists():
            contents.metrics_md = metrics_path.read_text()

        # Event log
        event_log_path = ws / "EVENT_LOG.jsonl"
        if event_log_path.exists():
            contents.event_log = event_log_path.read_text()

        # Background survey from JSON state (not in markdown snapshots)
        from .research_state import STATE_FILENAME
        from .renderers import render_background_survey

        state_path = ws / STATE_FILENAME
        if state_path.exists():
            try:
                from .research_state import ResearchState
                state = ResearchState.from_json(state_path.read_text())
                if state.survey_background:
                    contents.background_survey = render_background_survey(state)
            except Exception:
                pass  # Non-critical — proceed without survey

        # Git log from workspace (may not be a git repo)
        try:
            result = subprocess.run(
                ["git", "log", "--oneline"],
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                contents.git_log = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass  # Not a git repo or git not available

    return contents


# ---------------------------------------------------------------------------
# Reference file loading
# ---------------------------------------------------------------------------

def load_reference_file(problem_path: Path | None) -> tuple[str | None, str | None]:
    """Load reference file matching a problem YAML, if it exists.

    Returns:
        (answer_expr, full_content) where answer_expr is the content
        of the first code block (a SymPy assignment string) and
        full_content is the entire file text.  Both are None if no
        reference file is found.
    """
    if problem_path is None:
        return None, None

    ref_path = REFERENCES_DIR / f"{problem_path.stem}.md"
    if not ref_path.exists():
        return None, None

    content = ref_path.read_text()

    # Extract first fenced code block
    m = re.search(r"```(?:python)?\s*\n(.*?)```", content, re.DOTALL)
    answer_expr = m.group(1).strip() if m else None

    return answer_expr, content


# ---------------------------------------------------------------------------
# Formal answer evaluation
# ---------------------------------------------------------------------------

def run_formal_evaluation(
    workspace_dir: str,
    problem_def: dict | None,
    *,
    problem_path: Path | None = None,
) -> FormalEvalResult:
    """Run formal (symbolic/numerical) answer evaluation against ground truth.

    Preconditions (all must be met, otherwise skip gracefully):
    1. problem_def is provided and has an 'answer' field (or reference file fallback)
    2. problem_def has an 'answer_template' field
    3. ANSWER.md exists in the workspace
    """
    from .evaluate import evaluate_response

    # Precondition 1: problem_def with answer
    if not problem_def:
        return FormalEvalResult(skipped=True, skip_reason="No problem definition or no answer field")

    answer_val = problem_def.get("answer")
    answer_empty = answer_val is None or (isinstance(answer_val, str) and not answer_val.strip())

    reference_code: str | None = None
    if answer_empty:
        # Fallback: try reference file
        ref_answer, _ = load_reference_file(problem_path)
        if ref_answer:
            if "def answer" in ref_answer:
                # Function-style reference: direct exec+call comparison
                reference_code = ref_answer
                console.print("  [dim]Using answer function from reference file[/]")
            else:
                # Legacy expression-style reference
                problem_def = dict(problem_def)  # shallow copy to avoid mutating caller's dict
                problem_def["answer"] = ref_answer
                console.print("  [dim]Using answer from reference file[/]")
        else:
            return FormalEvalResult(skipped=True, skip_reason="No problem definition or no answer field")

    # Precondition 2: answer_template
    if not problem_def.get("answer_template"):
        return FormalEvalResult(skipped=True, skip_reason="No answer_template in problem definition")

    # Precondition 3: ANSWER.md exists
    answer_path = Path(workspace_dir) / "ANSWER.md"
    if not answer_path.exists():
        return FormalEvalResult(skipped=True, skip_reason="ANSWER.md not found in workspace")

    raw_content = answer_path.read_text()
    if not raw_content.strip():
        return FormalEvalResult(skipped=True, skip_reason="ANSWER.md is empty")

    # Wrap raw content in fences if not already fenced
    if "```python" in raw_content:
        fenced_content = raw_content
    else:
        fenced_content = f"```python\n{raw_content}\n```"

    # Call evaluate_response
    try:
        eval_result = evaluate_response(
            fenced_content, problem_def, reference_code=reference_code,
        )
    except Exception as exc:
        return FormalEvalResult(
            correct=None,
            method="evaluation_error",
            error=str(exc),
            details="Exception during evaluate_response()",
        )

    return FormalEvalResult(
        correct=eval_result.get("correct"),
        method=eval_result.get("method", ""),
        error=eval_result.get("error"),
        details=eval_result.get("details", ""),
    )


def render_formal_evaluation(result: FormalEvalResult) -> None:
    """Print the formal evaluation result to the console using Rich."""
    if result.skipped:
        console.print(f"[dim]  Skipped: {result.skip_reason}[/]")
        return

    if result.correct is True:
        console.print("[green bold]  CORRECT[/]")
    elif result.correct is False:
        console.print("[red bold]  INCORRECT[/]")
    else:
        console.print("[yellow bold]  INCONCLUSIVE[/]")

    if result.method:
        console.print(f"  Method: {result.method}")
    if result.error:
        console.print(f"  [red]Error: {result.error}[/]")
    if result.details:
        console.print(f"  [dim]{result.details}[/]")
    console.print()


# ---------------------------------------------------------------------------
# Re-run computations
# ---------------------------------------------------------------------------

def rerun_computations(workspace_dir: str, timeout: int = 60) -> list[RerunResult]:
    """Re-run all computation scripts in the workspace."""
    ws = Path(workspace_dir)
    scripts = sorted(glob.glob(str(ws / "computations" / "*.py")))
    results = []
    for script in scripts:
        execution = execute_python(script, timeout=timeout, cwd=workspace_dir)
        results.append(RerunResult(script_path=script, execution=execution))
    return results


# ---------------------------------------------------------------------------
# Build verification prompt
# ---------------------------------------------------------------------------

def build_verification_prompt(
    contents: WorkspaceContents,
    rerun_results: list[RerunResult] | None = None,
    known_answer: str | None = None,
    formal_eval: FormalEvalResult | None = None,
    reference_content: str | None = None,
) -> tuple[str, str]:
    """Assemble the system prompt and user content for the verifier LLM call."""
    system = (PROMPTS_DIR / "verifier.md").read_text()

    sections = []

    # Formal answer evaluation (Phase 1 result, if available)
    if formal_eval and not formal_eval.skipped:
        if formal_eval.correct is True:
            verdict_str = "CORRECT"
        elif formal_eval.correct is False:
            verdict_str = "INCORRECT"
        else:
            verdict_str = "INCONCLUSIVE"
        lines = [
            "## Formal Answer Evaluation\n",
            f"An automated symbolic/numerical comparison against the known ground truth returned: **{verdict_str}**",
            f"(method: {formal_eval.method})",
        ]
        if formal_eval.error:
            lines.append(f"Error: {formal_eval.error}")
        if formal_eval.details:
            lines.append(f"Details: {formal_eval.details}")
        lines.append("\nUse this as hard evidence when assessing the final answer, "
                      "but still evaluate the derivation chain independently.\n")
        sections.append("\n".join(lines))

    # Known answer (from problem YAML, if provided)
    if known_answer:
        sections.append(f"## Known Answer\n\nThe expected answer for this problem is: **{known_answer}**\n\n"
                        "Use this to check whether the research arrived at the correct numerical value or expression. "
                        "A matching answer with a flawed derivation is still VALID but you can raise your concerns.\n")

    # Reference document (ground truth + typical good run description)
    if reference_content:
        sections.append(
            "## Reference Document\n\n"
            "The following reference document contains the ground-truth answer "
            "and a description of what a typical successful run looks like. "
            "Use this to assess both the correctness of the final answer and "
            "the quality of the research process.\n\n"
            f"```markdown\n{reference_content}\n```\n"
        )

    # Termination status
    if contents.terminated_cleanly:
        sections.append("## Termination Status\nThe research run terminated cleanly (task_type: terminate).\n")
    else:
        sections.append("## Termination Status\n⚠ The research run did NOT terminate cleanly. "
                        "Results may be incomplete.\n")

    # Workspace files
    for label, text in [
        ("RESEARCH_STATE.md", contents.research_state),
        ("EVIDENCE_LOG.md", contents.evidence_log),
        ("CRITIQUE_LOG.md", contents.critique_log),
    ]:
        if text:
            sections.append(f"## {label}\n\n```markdown\n{text}\n```\n")
        else:
            sections.append(f"## {label}\n\n(File not found or empty.)\n")

    # Re-run results
    if rerun_results:
        sections.append("## Computation Re-run Results\n")
        for rr in rerun_results:
            name = Path(rr.script_path).name
            ex = rr.execution
            if ex is None:
                sections.append(f"### {name}\n(Not executed.)\n")
                continue
            status = "TIMED OUT" if ex.timed_out else (
                "SUCCESS" if ex.returncode == 0 else f"FAILED (rc={ex.returncode})")
            sections.append(f"### {name} — {status}\n")
            if ex.stdout.strip():
                sections.append(f"**stdout:**\n```\n{ex.stdout.strip()}\n```\n")
            if ex.stderr.strip():
                sections.append(f"**stderr:**\n```\n{ex.stderr.strip()}\n```\n")

    user_content = "\n".join(sections)
    return system, user_content


# ---------------------------------------------------------------------------
# Parse verdict from LLM response
# ---------------------------------------------------------------------------

def _extract_tag(text: str, tag: str) -> str:
    """Extract content between <tag>...</tag>. Returns '' if not found."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def parse_verdict(response_text: str) -> VerificationResult:
    """Parse XML-tagged sections from the verifier LLM response."""
    result = VerificationResult(verdict="INCONCLUSIVE", raw_response=response_text)

    # Extract top-level tags
    verdict = _extract_tag(response_text, "verdict")
    if verdict and verdict in ("VALID", "PARTIALLY_VALID", "INVALID", "INCONCLUSIVE"):
        result.verdict = verdict
    elif verdict:
        result.verdict = verdict.upper()
        result.parse_warnings.append(f"Non-standard verdict: {verdict}")
    else:
        result.parse_warnings.append("No <verdict> tag found — defaulting to INCONCLUSIVE")

    confidence = _extract_tag(response_text, "confidence")
    if confidence:
        result.confidence = confidence.upper()
    else:
        result.parse_warnings.append("No <confidence> tag found")

    summary = _extract_tag(response_text, "summary")
    if summary:
        result.summary = summary
    else:
        result.parse_warnings.append("No <summary> tag found")

    chain_valid = _extract_tag(response_text, "chain_valid")
    if chain_valid:
        result.chain_valid = chain_valid
    else:
        result.parse_warnings.append("No <chain_valid> tag found")

    # Parse per-result assessments
    assessment_text = _extract_tag(response_text, "result_assessment")
    if assessment_text:
        for m in re.finditer(r"(ER-\d+):\s*(VALID|INVALID|UNCERTAIN)\b(.*?)(?=ER-\d+:|\Z)",
                             assessment_text, re.DOTALL):
            result.result_assessments.append(ResultAssessment(
                result_id=m.group(1),
                verdict=m.group(2),
                notes=m.group(3).strip().lstrip("-").strip(),
            ))
    else:
        result.parse_warnings.append("No <result_assessment> tag found")

    # Parse unresolved concerns
    concerns_text = _extract_tag(response_text, "unresolved_concerns")
    if concerns_text:
        for line in concerns_text.splitlines():
            line = line.strip().lstrip("-").strip()
            if line and line.lower() != "none.":
                result.unresolved_concerns.append(line)
    else:
        result.parse_warnings.append("No <unresolved_concerns> tag found")

    return result


# ---------------------------------------------------------------------------
# Render verdict (Rich console output)
# ---------------------------------------------------------------------------

VERDICT_COLORS = {
    "VALID": "green",
    "PARTIALLY_VALID": "yellow",
    "INVALID": "red",
    "INCONCLUSIVE": "dim",
}


def render_verdict(result: VerificationResult) -> None:
    """Print the verification result to the console using Rich."""
    color = VERDICT_COLORS.get(result.verdict, "white")
    console.print()
    console.print(f"  Verdict:    [{color} bold]{result.verdict}[/]")
    if result.confidence:
        console.print(f"  Confidence: {result.confidence}")
    console.print()

    if result.summary:
        console.print(f"[bold]Summary:[/] {result.summary}")
        console.print()

    if result.result_assessments:
        table = Table(title="Per-Result Assessment")
        table.add_column("Result", style="cyan")
        table.add_column("Verdict")
        table.add_column("Notes", max_width=60)
        for ra in result.result_assessments:
            v_color = {"VALID": "green", "INVALID": "red", "UNCERTAIN": "yellow"}.get(ra.verdict, "white")
            table.add_row(ra.result_id, f"[{v_color}]{ra.verdict}[/]", ra.notes[:200])
        console.print(table)
        console.print()

    if result.chain_valid:
        console.print(f"[bold]Chain coherence:[/] {result.chain_valid}")
        console.print()

    if result.unresolved_concerns:
        console.print("[bold]Unresolved concerns:[/]")
        for c in result.unresolved_concerns:
            console.print(f"  - {c}")
        console.print()

    if result.parse_warnings:
        console.print("[dim]Parse warnings:[/]")
        for w in result.parse_warnings:
            console.print(f"  [dim]- {w}[/]")
        console.print()


# ---------------------------------------------------------------------------
# Write verification report
# ---------------------------------------------------------------------------

def write_verification_report(
    result: VerificationResult,
    workspace_dir: str,
    formal_eval: FormalEvalResult | None = None,
) -> None:
    """Write VERIFICATION.md into the workspace."""
    lines = ["---"]
    lines.append(f"verdict: {result.verdict}")
    if result.confidence:
        lines.append(f"confidence: {result.confidence}")
    if formal_eval and not formal_eval.skipped:
        if formal_eval.correct is True:
            lines.append("formal_answer: correct")
        elif formal_eval.correct is False:
            lines.append("formal_answer: incorrect")
        else:
            lines.append("formal_answer: inconclusive")
    lines.append("---\n")

    lines.append("# Verification Report\n")

    # Formal evaluation section
    if formal_eval and not formal_eval.skipped:
        if formal_eval.correct is True:
            verdict_str = "CORRECT"
        elif formal_eval.correct is False:
            verdict_str = "INCORRECT"
        else:
            verdict_str = "INCONCLUSIVE"
        lines.append(f"## Formal Answer Evaluation: {verdict_str}\n")
        lines.append(f"- Method: {formal_eval.method}")
        if formal_eval.error:
            lines.append(f"- Error: {formal_eval.error}")
        if formal_eval.details:
            lines.append(f"- Details: {formal_eval.details}")
        lines.append("")

    if result.summary:
        lines.append(f"## Summary\n\n{result.summary}\n")

    if result.result_assessments:
        lines.append("## Per-Result Assessment\n")
        for ra in result.result_assessments:
            lines.append(f"### {ra.result_id}: {ra.verdict}\n")
            if ra.notes:
                lines.append(f"{ra.notes}\n")

    if result.chain_valid:
        lines.append(f"## Chain Coherence\n\n{result.chain_valid}\n")

    if result.unresolved_concerns:
        lines.append("## Unresolved Concerns\n")
        for c in result.unresolved_concerns:
            lines.append(f"- {c}")
        lines.append("")

    if result.parse_warnings:
        lines.append("## Parse Warnings\n")
        for w in result.parse_warnings:
            lines.append(f"- {w}")
        lines.append("")

    report_path = Path(workspace_dir) / "VERIFICATION.md"
    report_path.write_text("\n".join(lines))
    console.print(f"[green]Report written to {report_path}[/]")


# ---------------------------------------------------------------------------
# Process audit
# ---------------------------------------------------------------------------

def _summarize_event_log(raw_text: str, max_chars: int = 4096) -> str:
    """Parse EVENT_LOG.jsonl lines into a structured text summary for the process auditor.

    Produces:
    - LLM call table: per-agent call count, token totals, avg duration
    - Scaffold events grouped by layer: event counts
    - Timeline of key events (overrides, stalls, bailouts, retries, verdict failures)
    """
    if not raw_text.strip():
        return ""

    llm_calls: list[dict] = []
    scaffold_events: list[dict] = []
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        kind = entry.get("kind", "")
        if kind == "llm_call":
            llm_calls.append(entry)
        elif kind == "scaffold":
            scaffold_events.append(entry)

    sections: list[str] = []

    # --- LLM call summary ---
    if llm_calls:
        agent_stats: dict[str, dict] = {}
        for c in llm_calls:
            agent = c.get("agent", "unknown")
            s = agent_stats.setdefault(agent, {"count": 0, "in": 0, "out": 0, "dur": 0.0})
            s["count"] += 1
            s["in"] += c.get("input_tokens", 0)
            s["out"] += c.get("output_tokens", 0)
            s["dur"] += c.get("duration_s", 0.0)

        lines = ["### LLM Calls by Agent", ""]
        lines.append("| Agent | Calls | Input Tok | Output Tok | Avg Duration |")
        lines.append("|-------|------:|----------:|-----------:|-------------:|")
        for agent in sorted(agent_stats):
            s = agent_stats[agent]
            avg = s["dur"] / s["count"] if s["count"] else 0
            lines.append(f"| {agent} | {s['count']} | {s['in']:,} | {s['out']:,} | {avg:.1f}s |")
        total_in = sum(s["in"] for s in agent_stats.values())
        total_out = sum(s["out"] for s in agent_stats.values())
        lines.append(f"| **Total** | {len(llm_calls)} | {total_in:,} | {total_out:,} | |")
        sections.append("\n".join(lines))

    # --- Scaffold events by category ---
    if scaffold_events:
        cat_counts: dict[str, dict[str, int]] = {}
        for e in scaffold_events:
            cat = e.get("category", "unknown")
            event = e.get("event", "unknown")
            cat_counts.setdefault(cat, {})
            cat_counts[cat][event] = cat_counts[cat].get(event, 0) + 1

        lines = ["### Scaffold Events by Category", ""]
        for cat in sorted(cat_counts):
            events_str = ", ".join(f"{ev}({n})" for ev, n in sorted(cat_counts[cat].items()))
            lines.append(f"- **{cat}:** {events_str}")
        sections.append("\n".join(lines))

    # --- Timeline of key events ---
    key_event_types = {
        "api_retry", "forced_final_call", "progress_check",
        "tool_call_failure_fallback", "p1_budget_override", "p2_stale_loop_override",
        "p3_forced_critic", "p4_refuted_recompute", "p5_stall_block",
        "compute_verdict_failed", "compute_verdict_stall_escalation",
        "termination_blocked", "dispatch_failure",
    }
    key_events = [e for e in scaffold_events if e.get("event", "") in key_event_types]
    if key_events:
        lines = ["### Key Event Timeline", ""]
        for e in key_events[:30]:  # cap timeline entries
            lines.append(f"- iter {e.get('iter', '?')}: **{e.get('event', '')}** — {e.get('detail', '')}")
        if len(key_events) > 30:
            lines.append(f"- ... ({len(key_events) - 30} more)")
        sections.append("\n".join(lines))

    if not sections:
        return ""

    result = "\n\n".join(sections)
    if len(result) > max_chars:
        result = result[:max_chars - 20] + "\n\n[... truncated]"
    return result


def build_process_audit_prompt(contents: WorkspaceContents) -> tuple[str, str]:
    """Assemble the system prompt and user content for the process auditor LLM call."""
    system = (PROMPTS_DIR / "process_auditor.md").read_text()

    sections = []

    # Termination status
    if contents.terminated_cleanly:
        sections.append("## Termination Status\nThe research run terminated cleanly.\n")
    else:
        sections.append("## Termination Status\n⚠ The research run did NOT terminate cleanly.\n")

    # Core workspace files
    for label, text in [
        ("RESEARCH_STATE.md", contents.research_state),
        ("EVIDENCE_LOG.md", contents.evidence_log),
        ("CRITIQUE_LOG.md", contents.critique_log),
        ("CURRENT_TASK.md", contents.current_task),
    ]:
        if text:
            sections.append(f"## {label}\n\n```markdown\n{text}\n```\n")
        else:
            sections.append(f"## {label}\n\n(File not found or empty.)\n")

    # Background survey (surveyor output — not in RESEARCH_STATE.md)
    if contents.background_survey:
        sections.append(f"## Background Survey (Surveyor Output)\n\n```markdown\n{contents.background_survey}\n```\n")
    else:
        sections.append("## Background Survey (Surveyor Output)\n\n(Not available.)\n")

    # Process-specific data
    if contents.metrics_md:
        sections.append(f"## METRICS.md\n\n```markdown\n{contents.metrics_md}\n```\n")
    else:
        sections.append("## METRICS.md\n\n(Not available.)\n")

    if contents.git_log:
        sections.append(f"## Git Log\n\n```\n{contents.git_log}\n```\n")
    else:
        sections.append("## Git Log\n\n(Not available.)\n")

    # Event log summary
    if contents.event_log:
        summary = _summarize_event_log(contents.event_log)
        if summary:
            sections.append(f"## Event Log Summary\n\n{summary}\n")
        else:
            sections.append("## Event Log Summary\n\n(Log present but could not be parsed.)\n")
    else:
        sections.append("## Event Log Summary\n\n(Not available.)\n")

    user_content = "\n".join(sections)
    return system, user_content


def parse_process_audit(response_text: str) -> ProcessAuditResult:
    """Parse XML-tagged sections from the process auditor LLM response."""
    result = ProcessAuditResult(verdict="INEFFECTIVE", raw_response=response_text)

    # Verdict
    verdict = _extract_tag(response_text, "process_verdict")
    if verdict:
        verdict_upper = verdict.strip().upper()
        if verdict_upper in ("EFFECTIVE", "PARTIALLY_EFFECTIVE", "INEFFECTIVE"):
            result.verdict = verdict_upper
        else:
            result.verdict = verdict_upper
            result.parse_warnings.append(f"Non-standard process verdict: {verdict}")
    else:
        result.parse_warnings.append("No <process_verdict> tag found — defaulting to INEFFECTIVE")

    # Summary
    summary = _extract_tag(response_text, "process_summary")
    if summary:
        result.summary = summary
    else:
        result.parse_warnings.append("No <process_summary> tag found")

    # Token efficiency
    token_eff = _extract_tag(response_text, "token_efficiency")
    if token_eff:
        result.token_efficiency = token_eff
    else:
        result.parse_warnings.append("No <token_efficiency> tag found")

    # Recommendations
    recs_text = _extract_tag(response_text, "recommendations")
    if recs_text:
        for line in recs_text.splitlines():
            line = line.strip().lstrip("-").strip()
            if line:
                result.recommendations.append(line)
    else:
        result.parse_warnings.append("No <recommendations> tag found")

    # Process events
    events_text = _extract_tag(response_text, "process_events")
    if events_text:
        # Parse EVENT-NNN lines
        for m in re.finditer(
            r"(EVENT-\d+)\s+\[(SUCCESS|FAILURE|MIXED)]\s+(\S+)\s+\(([^)]*)\)\s*\n(.*?)(?=EVENT-\d+|\Z)",
            events_text,
            re.DOTALL,
        ):
            description = m.group(5).strip()
            evidence = ""
            # Extract evidence line if present
            ev_match = re.search(r"Evidence:\s*(.+)", description)
            if ev_match:
                evidence = ev_match.group(1).strip()
                description = description[:ev_match.start()].strip()
            result.events.append(ProcessEvent(
                event_id=m.group(1),
                classification=m.group(2),
                event_type=m.group(3),
                iterations=m.group(4).strip(),
                description=description,
                evidence=evidence,
            ))
    else:
        result.parse_warnings.append("No <process_events> tag found")

    return result


PROCESS_VERDICT_COLORS = {
    "EFFECTIVE": "green",
    "PARTIALLY_EFFECTIVE": "yellow",
    "INEFFECTIVE": "red",
}


def render_process_audit(result: ProcessAuditResult) -> None:
    """Print the process audit result to the console using Rich."""
    color = PROCESS_VERDICT_COLORS.get(result.verdict, "white")
    console.print()
    console.print("[bold]--- Process Audit ---[/]")
    console.print(f"  Process Verdict: [{color} bold]{result.verdict}[/]")
    console.print()

    if result.summary:
        console.print(f"[bold]Summary:[/] {result.summary}")
        console.print()

    if result.events:
        table = Table(title="Process Events")
        table.add_column("Event", style="cyan")
        table.add_column("Type")
        table.add_column("Class")
        table.add_column("Iterations")
        table.add_column("Description", max_width=50)
        for ev in result.events:
            ev_color = {"SUCCESS": "green", "FAILURE": "red", "MIXED": "yellow"}.get(ev.classification, "white")
            table.add_row(
                ev.event_id,
                ev.event_type,
                f"[{ev_color}]{ev.classification}[/]",
                ev.iterations,
                ev.description[:150],
            )
        console.print(table)
        console.print()

    if result.token_efficiency:
        console.print(f"[bold]Token Efficiency:[/] {result.token_efficiency}")
        console.print()

    if result.recommendations:
        console.print("[bold]Recommendations:[/]")
        for r in result.recommendations:
            console.print(f"  - {r}")
        console.print()

    if result.parse_warnings:
        console.print("[dim]Parse warnings:[/]")
        for w in result.parse_warnings:
            console.print(f"  [dim]- {w}[/]")
        console.print()


def append_process_audit_to_report(result: ProcessAuditResult, workspace_dir: str) -> None:
    """Append process audit sections to an existing VERIFICATION.md, patching frontmatter."""
    report_path = Path(workspace_dir) / "VERIFICATION.md"

    if report_path.exists():
        existing = report_path.read_text()
    else:
        existing = "---\n---\n"

    # Patch frontmatter to add process_verdict
    # Match the closing --- of frontmatter and insert before it
    fm_match = re.match(r"(---\n)(.*?)(---\n)", existing, re.DOTALL)
    if fm_match:
        fm_body = fm_match.group(2)
        # Remove existing process_verdict if present
        fm_body = re.sub(r"process_verdict:.*\n", "", fm_body)
        fm_body += f"process_verdict: {result.verdict}\n"
        existing = f"---\n{fm_body}---\n" + existing[fm_match.end():]

    # Build process audit sections
    sections = ["\n---\n\n# Process Audit\n"]

    if result.summary:
        sections.append(f"## Process Summary\n\n{result.summary}\n")

    sections.append(f"## Process Verdict: {result.verdict}\n")

    if result.token_efficiency:
        sections.append(f"## Token Efficiency\n\n{result.token_efficiency}\n")

    if result.events:
        sections.append("## Process Events\n")
        for ev in result.events:
            sections.append(f"### {ev.event_id} [{ev.classification}] {ev.event_type} ({ev.iterations})\n")
            if ev.description:
                sections.append(f"{ev.description}\n")
            if ev.evidence:
                sections.append(f"**Evidence:** {ev.evidence}\n")

    if result.recommendations:
        sections.append("## Recommendations\n")
        for r in result.recommendations:
            sections.append(f"- {r}")
        sections.append("")

    report_path.write_text(existing + "\n".join(sections))
    console.print(f"[green]Process audit appended to {report_path}[/]")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_verify_parser() -> "argparse.ArgumentParser":
    """Build the CLI argument parser for verification."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="sciralph.verify",
        description="Independent verification of SciRalph research workspaces.",
    )
    parser.add_argument("workspace_dir", type=str,
                        help="Path to workspace directory")
    parser.add_argument("--model", type=str, default=DEFAULTS["verify_model"],
                        help=f"LLM model (default: {DEFAULTS['verify_model']})")
    parser.add_argument("--max-tokens", type=int, default=DEFAULTS["max_tokens"],
                        help=f"Max output tokens (default: {DEFAULTS['max_tokens']})")
    return parser


def main():
    parser = build_verify_parser()
    args = parser.parse_args()

    workspace_dir = args.workspace_dir
    if not Path(workspace_dir).is_dir():
        print(f"Error: workspace directory not found: {workspace_dir}")
        sys.exit(1)

    model = args.model
    max_tokens = args.max_tokens
    do_rerun = False
    timeout = 60
    write_report = True
    do_process_audit = True

    # Load workspace
    console.print(f"[bold]Loading workspace:[/] {workspace_dir}")
    contents = load_workspace(workspace_dir)

    if not contents.research_state:
        console.print("[red]Error: RESEARCH_STATE.md not found or empty.[/]")
        sys.exit(1)

    if contents.terminated_cleanly:
        console.print("[green]Run terminated cleanly.[/]")
    else:
        console.print("[yellow]Warning: run did NOT terminate cleanly.[/]")

    # Optional computation re-run
    rerun_results = None
    if do_rerun:
        console.print(f"[bold]Re-running {len(contents.computation_scripts)} computation(s)...[/]")
        rerun_results = rerun_computations(workspace_dir, timeout=timeout)
        for rr in rerun_results:
            name = Path(rr.script_path).name
            ex = rr.execution
            status = "TIMEOUT" if ex.timed_out else ("OK" if ex.returncode == 0 else "FAIL")
            console.print(f"  {name}: {status}")

    # Load problem definition from workspace (copied there by main.py at run start)
    problem_def = None
    known_answer = None
    problem_path = Path(workspace_dir) / "problem.yaml"
    if problem_path.exists():
        with open(problem_path) as f:
            problem_def = yaml.safe_load(f)
        answer_val = problem_def.get("answer")
        if answer_val is not None:
            known_answer = str(answer_val)
            console.print(f"[bold]Known answer:[/] {known_answer}")

    # Build a path with the original problem stem for reference file lookup
    problem_name = problem_def.get("name") if problem_def else None
    ref_lookup_path = Path(problem_name + ".yaml") if problem_name else None

    # Load reference file (if available)
    ref_answer_expr, reference_content = load_reference_file(ref_lookup_path)
    if reference_content:
        console.print("[bold]Reference file:[/] loaded")
    if not known_answer and ref_answer_expr:
        known_answer = ref_answer_expr
        console.print(f"[bold]Known answer (from reference):[/] {known_answer[:100]}...")

    # Phase 1: Formal answer evaluation (fast, deterministic)
    console.print(f"\n[bold]Phase 1: Formal answer evaluation...[/]")
    formal_eval = run_formal_evaluation(workspace_dir, problem_def, problem_path=ref_lookup_path)
    render_formal_evaluation(formal_eval)

    # Build prompt and call LLM (science verification)
    config = Config(model=model, max_tokens=max_tokens, workspace_dir=workspace_dir)
    system, user_content = build_verification_prompt(
        contents, rerun_results, known_answer=known_answer, formal_eval=formal_eval,
        reference_content=reference_content,
    )

    console.print(f"\n[bold]Phase 2a: Science verification ({model}, streaming)...[/]")
    response = _call_llm_streaming(system, user_content, config)
    console.print(f"[dim]({response.input_tokens} in / {response.output_tokens} out, {response.duration:.1f}s)[/]")

    # Parse and render
    result = parse_verdict(response.text)
    render_verdict(result)

    if write_report:
        write_verification_report(result, workspace_dir, formal_eval=formal_eval)

    # Process audit (second LLM pass)
    if do_process_audit:
        console.print(f"\n[bold]Phase 2b: Process audit ({model}, streaming)...[/]")
        process_contents = load_workspace(workspace_dir, include_process_data=True)
        pa_system, pa_user = build_process_audit_prompt(process_contents)
        pa_response = _call_llm_streaming(pa_system, pa_user, config)
        console.print(f"[dim]({pa_response.input_tokens} in / {pa_response.output_tokens} out, {pa_response.duration:.1f}s)[/]")

        pa_result = parse_process_audit(pa_response.text)
        render_process_audit(pa_result)

        if write_report:
            append_process_audit_to_report(pa_result, workspace_dir)


if __name__ == "__main__":
    main()
