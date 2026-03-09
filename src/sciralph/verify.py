"""Independent verification of SciRalph research workspaces."""

import glob
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table

import time

import anthropic

from .config import Config
from .llm import LLMResponse  # noqa: F401 — reuse dataclass, call via streaming
from .markdown import parse_frontmatter
from .sandbox import ExecutionResult, execute_python

console = Console()

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _call_llm_streaming(system: str, user_content: str, config: Config) -> LLMResponse:
    """Call the Anthropic API with streaming (required for Opus with high max_tokens)."""
    client = anthropic.Anthropic(api_key=config.api_key)
    start = time.time()

    chunks: list[str] = []
    input_tokens = 0
    output_tokens = 0
    stop_reason = ""

    with client.messages.stream(
        model=config.model,
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
    computation_log: str = ""
    current_task: str = ""
    computation_scripts: list[str] = field(default_factory=list)
    terminated_cleanly: bool = False
    frontmatter: dict = field(default_factory=dict)


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


# ---------------------------------------------------------------------------
# Load workspace
# ---------------------------------------------------------------------------

def load_workspace(workspace_dir: str) -> WorkspaceContents:
    """Read all relevant files from a completed workspace."""
    ws = Path(workspace_dir)
    contents = WorkspaceContents(workspace_dir=workspace_dir)

    for fname, attr in [
        ("RESEARCH_STATE.md", "research_state"),
        ("CRITIQUE_LOG.md", "critique_log"),
        ("COMPUTATION_LOG.md", "computation_log"),
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

    return contents


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
) -> tuple[str, str]:
    """Assemble the system prompt and user content for the verifier LLM call."""
    system = (PROMPTS_DIR / "verifier.md").read_text()

    sections = []

    # Termination status
    if contents.terminated_cleanly:
        sections.append("## Termination Status\nThe research run terminated cleanly (task_type: terminate).\n")
    else:
        sections.append("## Termination Status\n⚠ The research run did NOT terminate cleanly. "
                        "Results may be incomplete.\n")

    # Workspace files
    for label, text in [
        ("RESEARCH_STATE.md", contents.research_state),
        ("COMPUTATION_LOG.md", contents.computation_log),
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

def write_verification_report(result: VerificationResult, workspace_dir: str) -> None:
    """Write VERIFICATION.md into the workspace."""
    lines = ["---"]
    lines.append(f"verdict: {result.verdict}")
    if result.confidence:
        lines.append(f"confidence: {result.confidence}")
    lines.append("---\n")

    lines.append("# Verification Report\n")

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
    parser.add_argument("--model", type=str, default="claude-opus-4-20250514",
                        help="LLM model (default: claude-opus-4-20250514)")
    parser.add_argument("--max-tokens", type=int, default=16384,
                        help="Max output tokens (default: 16384)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Computation timeout in seconds (default: 60)")
    parser.add_argument("--rerun-computations", action="store_true",
                        help="Re-run computation scripts before verification")
    parser.add_argument("--write-report", action="store_true",
                        help="Write VERIFICATION.md into workspace")
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
    do_rerun = args.rerun_computations
    timeout = args.timeout
    write_report = args.write_report

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

    # Build prompt and call LLM
    config = Config(model=model, max_tokens=max_tokens, workspace_dir=workspace_dir)
    system, user_content = build_verification_prompt(contents, rerun_results)

    console.print(f"\n[bold]Calling {model} (streaming)...[/]")
    response = _call_llm_streaming(system, user_content, config)
    console.print(f"[dim]({response.input_tokens} in / {response.output_tokens} out, {response.duration:.1f}s)[/]")

    # Parse and render
    result = parse_verdict(response.text)
    render_verdict(result)

    if write_report:
        write_verification_report(result, workspace_dir)


if __name__ == "__main__":
    main()
