"""Independent verification of OpenDirac research workspaces."""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from rich.table import Table

from ..console import console

import time

import anthropic
import yaml

from ..config import Config, DEFAULTS
from ..llm import LLMResponse
from .workspace import (
    WorkspaceContents,
    RerunResult,
    REFERENCES_DIR,  # noqa: F401 — re-exported for back-compat during slice
    load_workspace,
    load_reference_file,
    rerun_computations,
)
from .formal_eval import (
    FormalEvalResult,
    run_formal_evaluation,
    render_formal_evaluation,
    write_formal_eval_report,
    load_or_run_formal_eval,
)

PROMPTS_DIR = Path(__file__).parent


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
# Dataclasses (diagnosis-only; workspace/formal-eval dataclasses moved to sibling modules)
# ---------------------------------------------------------------------------

@dataclass
class DiagnosisEvent:
    """A single event in a failure chain or correction chain."""
    event_id: str
    chain_type: str  # "correction_chain" / "failure_chain"
    classification: str  # CAUGHT / UNCAUGHT / PARTIAL
    agents_involved: list[str] = field(default_factory=list)
    iterations: str = ""
    description: str = ""
    root_cause: str = ""
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class DiagnosisResult:
    """Result of the unified diagnosis pass."""
    formal_outcome: str = ""  # CORRECT / INCORRECT / INCONCLUSIVE / SKIPPED
    diagnosis_mode: str = ""  # success_analysis / failure_analysis
    summary: str = ""
    events: list[DiagnosisEvent] = field(default_factory=list)
    weakest_link: str = ""
    recommendations: list[str] = field(default_factory=list)
    raw_response: str = ""
    parse_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Build diagnosis prompt
# ---------------------------------------------------------------------------

def _formal_eval_outcome(formal_eval: FormalEvalResult | None) -> str:
    """Return a string label for the formal evaluation outcome."""
    if formal_eval is None or formal_eval.skipped:
        return "SKIPPED"
    if formal_eval.correct is True:
        return "CORRECT"
    if formal_eval.correct is False:
        return "INCORRECT"
    return "INCONCLUSIVE"


def build_diagnosis_prompt(
    contents: WorkspaceContents,
    formal_eval: FormalEvalResult | None = None,
    rerun_results: list[RerunResult] | None = None,
    known_answer: str | None = None,
    reference_content: str | None = None,
) -> tuple[str, str]:
    """Assemble the system prompt and user content for the unified diagnosis LLM call."""
    system = (PROMPTS_DIR / "diagnosis.md").read_text()

    sections = []

    # Formal answer evaluation framing
    outcome = _formal_eval_outcome(formal_eval)
    if outcome == "CORRECT":
        sections.append(
            "## Formal Answer Evaluation: CORRECT\n\n"
            "An automated symbolic/numerical comparison confirmed the final answer is **CORRECT**."
        )
        if formal_eval.method:
            sections.append(f"(method: {formal_eval.method})")
        sections.append(
            "\nThe system arrived at the right answer. Focus your analysis on "
            "**correction chains**: errors that were made during the run and how the "
            "system caught and corrected them.\n"
        )
    elif outcome == "INCORRECT":
        sections.append(
            "## Formal Answer Evaluation: INCORRECT\n\n"
            "An automated symbolic/numerical comparison confirmed the final answer is **INCORRECT**."
        )
        if formal_eval.method:
            sections.append(f"(method: {formal_eval.method})")
        if formal_eval.details:
            sections.append(f"Details: {formal_eval.details}")
        sections.append(
            "\nThe system failed to reach the right answer. Focus your analysis on "
            "**failure chains**: root-cause analysis of where reasoning went wrong and "
            "why self-correction mechanisms failed to catch it.\n"
        )
    elif outcome == "INCONCLUSIVE":
        sections.append(
            "## Formal Answer Evaluation: INCONCLUSIVE\n\n"
            "The automated comparison could not determine correctness."
        )
        if formal_eval.error:
            sections.append(f"Error: {formal_eval.error}")
        sections.append(
            "\nAnalyze both the scientific validity of the result and the process quality.\n"
        )
    else:
        sections.append(
            "## Formal Answer Evaluation: SKIPPED\n\n"
            "No ground truth is available for automated comparison."
        )
        if formal_eval and formal_eval.skip_reason:
            sections.append(f"Reason: {formal_eval.skip_reason}")
        sections.append(
            "\nAnalyze both the scientific plausibility of the result and the process quality.\n"
        )

    # Known answer
    if known_answer:
        sections.append(
            f"## Known Answer\n\nThe expected answer is: **{known_answer}**\n"
        )

    # Reference document
    if reference_content:
        sections.append(
            "## Reference Document\n\n"
            "The following reference document contains the ground-truth answer "
            "and a description of what a typical successful run looks like.\n\n"
            f"```markdown\n{reference_content}\n```\n"
        )

    # Termination status
    if contents.terminated_cleanly:
        sections.append("## Termination Status\nThe research run terminated cleanly.\n")
    else:
        sections.append("## Termination Status\n⚠ The research run did NOT terminate cleanly. "
                        "Results may be incomplete.\n")

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

    # Background survey
    if contents.background_survey:
        sections.append(f"## Background Survey (Surveyor Output)\n\n```markdown\n{contents.background_survey}\n```\n")

    # Process data
    if contents.metrics_md:
        sections.append(f"## METRICS.md\n\n```markdown\n{contents.metrics_md}\n```\n")

    if contents.git_log:
        sections.append(f"## Git Log\n\n```\n{contents.git_log}\n```\n")

    # Event log summary
    if contents.event_log:
        summary = _summarize_event_log(contents.event_log)
        if summary:
            sections.append(f"## Event Log Summary\n\n{summary}\n")

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
# Parse diagnosis from LLM response
# ---------------------------------------------------------------------------

def _extract_tag(text: str, tag: str) -> str:
    """Extract content between <tag>...</tag>. Returns '' if not found."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def parse_diagnosis(response_text: str, formal_eval: FormalEvalResult | None = None) -> DiagnosisResult:
    """Parse XML-tagged sections from the diagnosis LLM response."""
    outcome = _formal_eval_outcome(formal_eval)
    mode = "success_analysis" if outcome == "CORRECT" else "failure_analysis"
    result = DiagnosisResult(
        formal_outcome=outcome,
        diagnosis_mode=mode,
        raw_response=response_text,
    )

    # Summary
    summary = _extract_tag(response_text, "diagnosis_summary")
    if summary:
        result.summary = summary
    else:
        result.parse_warnings.append("No <diagnosis_summary> tag found")

    # Chains (events)
    chains_text = _extract_tag(response_text, "chains")
    if chains_text:
        for m in re.finditer(
            r"(EVENT-\d+)\s+\[(CAUGHT|UNCAUGHT|PARTIAL)]\s+\(([^)]*)\)\s*\n(.*?)(?=EVENT-\d+|\Z)",
            chains_text,
            re.DOTALL,
        ):
            body = m.group(4).strip()

            # Parse structured fields from body
            agents = []
            agents_match = re.search(r"Agents?:\s*(.+)", body)
            if agents_match:
                agents = [a.strip() for a in agents_match.group(1).split(",")]

            root_cause = ""
            rc_match = re.search(r"Root cause:\s*(.+)", body)
            if rc_match:
                root_cause = rc_match.group(1).strip()

            evidence_ids = []
            ev_match = re.search(r"Evidence:\s*(.+)", body)
            if ev_match:
                evidence_ids = [e.strip() for e in ev_match.group(1).split(",")]

            # Description is everything before the structured fields
            description = body
            for pattern in [r"\nAgents?:.*", r"\nRoot cause:.*", r"\nEvidence:.*"]:
                description = re.sub(pattern, "", description, flags=re.DOTALL)
            description = description.strip()

            classification = m.group(2)
            chain_type = "correction_chain" if classification == "CAUGHT" else "failure_chain"

            result.events.append(DiagnosisEvent(
                event_id=m.group(1),
                chain_type=chain_type,
                classification=classification,
                agents_involved=agents,
                iterations=m.group(3).strip(),
                description=description,
                root_cause=root_cause,
                evidence_ids=evidence_ids,
            ))
    else:
        result.parse_warnings.append("No <chains> tag found")

    # Weakest link
    weakest = _extract_tag(response_text, "weakest_link")
    if weakest:
        result.weakest_link = weakest
    else:
        result.parse_warnings.append("No <weakest_link> tag found")

    # Recommendations
    recs_text = _extract_tag(response_text, "recommendations")
    if recs_text:
        for line in recs_text.splitlines():
            line = line.strip().lstrip("-").strip()
            if line:
                result.recommendations.append(line)
    else:
        result.parse_warnings.append("No <recommendations> tag found")

    return result


# ---------------------------------------------------------------------------
# Render diagnosis (Rich console output)
# ---------------------------------------------------------------------------

CLASSIFICATION_COLORS = {
    "CAUGHT": "green",
    "UNCAUGHT": "red",
    "PARTIAL": "yellow",
}


def render_diagnosis(result: DiagnosisResult) -> None:
    """Print the diagnosis result to the console using Rich."""
    # Mode banner
    if result.diagnosis_mode == "success_analysis":
        console.print("\n[bold green]--- Diagnosis: Success Analysis ---[/]")
    else:
        console.print("\n[bold red]--- Diagnosis: Failure Analysis ---[/]")
    console.print()

    if result.summary:
        console.print(f"[bold]Summary:[/] {result.summary}")
        console.print()

    if result.events:
        table = Table(title="Error/Correction Chains")
        table.add_column("Event", style="cyan")
        table.add_column("Status")
        table.add_column("Iterations")
        table.add_column("Agents")
        table.add_column("Description", max_width=50)
        for ev in result.events:
            color = CLASSIFICATION_COLORS.get(ev.classification, "white")
            table.add_row(
                ev.event_id,
                f"[{color}]{ev.classification}[/]",
                ev.iterations,
                ", ".join(ev.agents_involved) if ev.agents_involved else "",
                ev.description[:150],
            )
        console.print(table)
        console.print()

    if result.weakest_link:
        console.print(f"[bold]Weakest link:[/] {result.weakest_link}")
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


# ---------------------------------------------------------------------------
# Write diagnosis report
# ---------------------------------------------------------------------------

def write_diagnosis_report(result: DiagnosisResult, workspace_dir: str) -> None:
    """Append diagnosis sections to VERIFICATION.md (created by engine's formal eval)."""
    report_path = Path(workspace_dir) / "VERIFICATION.md"

    if report_path.exists():
        existing = report_path.read_text()
    else:
        existing = "---\n---\n\n# Verification Report\n"

    # Patch frontmatter to add diagnosis_mode
    fm_match = re.match(r"(---\n)(.*?)(---\n)", existing, re.DOTALL)
    if fm_match:
        fm_body = fm_match.group(2)
        fm_body = re.sub(r"diagnosis_mode:.*\n", "", fm_body)
        fm_body += f"diagnosis_mode: {result.diagnosis_mode}\n"
        existing = f"---\n{fm_body}---\n" + existing[fm_match.end():]

    # Build diagnosis sections
    sections = ["\n---\n"]

    if result.diagnosis_mode == "success_analysis":
        sections.append("# Diagnosis: Success Analysis\n")
    else:
        sections.append("# Diagnosis: Failure Analysis\n")

    if result.summary:
        sections.append(f"## Summary\n\n{result.summary}\n")

    if result.events:
        sections.append("## Error/Correction Chains\n")
        for ev in result.events:
            sections.append(
                f"### {ev.event_id} [{ev.classification}] ({ev.iterations})\n"
            )
            if ev.agents_involved:
                sections.append(f"**Agents:** {', '.join(ev.agents_involved)}\n")
            if ev.description:
                sections.append(f"{ev.description}\n")
            if ev.root_cause:
                sections.append(f"**Root cause:** {ev.root_cause}\n")
            if ev.evidence_ids:
                sections.append(f"**Evidence:** {', '.join(ev.evidence_ids)}\n")

    if result.weakest_link:
        sections.append(f"## Weakest Link\n\n{result.weakest_link}\n")

    if result.recommendations:
        sections.append("## Recommendations\n")
        for r in result.recommendations:
            sections.append(f"- {r}")
        sections.append("")

    report_path.write_text(existing + "\n".join(sections))
    console.print(f"[green]Diagnosis appended to {report_path}[/]")


# ---------------------------------------------------------------------------
# Event log summary
# ---------------------------------------------------------------------------

def _summarize_event_log(raw_text: str, max_chars: int = 4096) -> str:
    """Parse EVENT_LOG.jsonl lines into a structured text summary.

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


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_verify_parser() -> "argparse.ArgumentParser":
    """Build the CLI argument parser for verification."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="open_dirac.verification",
        description="Diagnosis of OpenDirac research workspaces.",
    )
    parser.add_argument("workspace_dir", type=Path,
                        help="Path to workspace directory")
    parser.add_argument("--model", type=str, default=DEFAULTS["verify_model"],
                        help=f"LLM model (default: {DEFAULTS['verify_model']})")
    return parser


def main():
    parser = build_verify_parser()
    args = parser.parse_args()

    workspace_dir = args.workspace_dir
    if not Path(workspace_dir).is_dir():
        print(f"Error: workspace directory not found: {workspace_dir}")
        sys.exit(1)

    model = args.model

    # Load workspace (single call — loads everything including process data)
    console.print(f"[bold]Loading workspace:[/] {workspace_dir}")
    contents = load_workspace(workspace_dir)

    if not contents.research_state:
        console.print("[red]Error: RESEARCH_STATE.md not found or empty.[/]")
        sys.exit(1)

    if contents.terminated_cleanly:
        console.print("[green]Run terminated cleanly.[/]")
    else:
        console.print("[yellow]Warning: run did NOT terminate cleanly.[/]")

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

    # Build reference lookup path
    problem_name = problem_def.get("name") if problem_def else None
    ref_lookup_path = Path(problem_name + ".yaml") if problem_name else None

    # Load reference file
    ref_answer_expr, reference_content = load_reference_file(ref_lookup_path)
    if reference_content:
        console.print("[bold]Reference file:[/] loaded")
    if not known_answer and ref_answer_expr:
        known_answer = ref_answer_expr
        console.print(f"[bold]Known answer (from reference):[/] {known_answer[:100]}...")

    # Formal answer evaluation (read from engine's report, or run fresh)
    console.print(f"\n[bold]Formal answer evaluation...[/]")
    formal_eval = load_or_run_formal_eval(workspace_dir, problem_def, ref_lookup_path)
    render_formal_evaluation(formal_eval)

    # Single diagnosis pass
    config = Config(model=model, workspace_dir=workspace_dir)
    system, user_content = build_diagnosis_prompt(
        contents, formal_eval=formal_eval,
        known_answer=known_answer, reference_content=reference_content,
    )

    console.print(f"\n[bold]Diagnosis ({model}, streaming)...[/]")
    response = _call_llm_streaming(system, user_content, config)
    console.print(f"[dim]({response.input_tokens} in / {response.output_tokens} out, {response.duration:.1f}s)[/]")

    # Parse, render, write
    result = parse_diagnosis(response.text, formal_eval=formal_eval)
    render_diagnosis(result)
    write_diagnosis_report(result, workspace_dir)


if __name__ == "__main__":
    main()
