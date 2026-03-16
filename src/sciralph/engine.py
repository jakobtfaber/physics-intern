"""SciRalph main loop engine."""

from collections.abc import Callable
from dataclasses import dataclass, field

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import Config
from .llm import _is_transient
from .markdown import (
    parse_frontmatter,
    render_frontmatter,
    find_prior_failures_for_claim,
    _parse_comp_entries,
    _normalize_claim_key,
)
from .metrics import MetricsTracker
from .task import Task, TaskType, TASK_TYPE_AGENT_MAP
from .categories import CompensationCategory as CC
from .research_state import build_from_workspace as _build_research_state, ResearchState
from .validation import validate_post_integration, can_terminate, check_phantom_references, Violation, ViolationSeverity
from .workspace import WorkspaceManager, log_scaffold_event
from .agents.orchestrator import OrchestratorAgent
from .agents.researcher import ResearcherAgent
from .agents.computationalist import ComputationalistAgent
from .agents.critic import CriticAgent
from .agents.compressor import CompressorAgent
from .agents.formatter import FormatterAgent

console = Console()


@dataclass
class LoopState:
    """Inter-iteration state for the main research loop."""
    pending_recompute_claim: str | None = None
    pending_recompute_verdict: str | None = None
    stalled_claims: set[str] = field(default_factory=set)
    claim_failure_count: dict[str, int] = field(default_factory=dict)
    last_content_iteration: int = 0
    # Consumed-once feedback accumulators (cleared after orchestrator reads them)
    pending_violations: list = field(default_factory=list)
    pending_termination_blockers: list[str] = field(default_factory=list)
    displaced_tasks: list[dict] = field(default_factory=list)
    agent_failures: list[dict] = field(default_factory=list)


@dataclass
class Override:
    """A single pre-dispatch override in the priority chain."""
    name: str
    priority: int
    condition: Callable[["SciRalph", Task], bool]
    action: Callable[["SciRalph", Task], Task]


# ---------------------------------------------------------------------------
# Override condition/action functions
# ---------------------------------------------------------------------------

def _p3_forced_critic_condition(engine: "SciRalph", task: Task) -> bool:
    return (engine._critic_overdue()
            and task.task_type not in (TaskType.CRITIQUE, TaskType.SYNTHESIZE, TaskType.TERMINATE))


def _p3_forced_critic_action(engine: "SciRalph", task: Task) -> Task:
    console.print(
        f"[yellow]Forcing critic pass (overdue: last critic at "
        f"iter {engine.metrics.last_critic_iteration}, "
        f"threshold {engine.config.critic_every_n}).[/yellow]"
    )
    engine._log_displacement(task, "forced_critic")
    log_scaffold_event(engine.workspace.root, engine.iteration, CC.LOOP_CONTROL,
                       "p3_forced_critic",
                       f"last_critic={engine.metrics.last_critic_iteration}")
    return engine._make_forced_critic_task()


def _p4_refuted_recompute_condition(engine: "SciRalph", task: Task) -> bool:
    """Pure check — no side effects."""
    if not engine._state.pending_recompute_claim:
        return False
    return task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE)


def _p4_refuted_recompute_action(engine: "SciRalph", task: Task) -> Task:
    # Consume pending state (moved from condition)
    claim = engine._state.pending_recompute_claim
    verdict = engine._state.pending_recompute_verdict or "REFUTED"
    engine._state.pending_recompute_claim = None
    engine._state.pending_recompute_verdict = None
    console.print(f"[yellow]Forcing recompute after {verdict} verdict.[/yellow]")
    engine._log_displacement(task, "refuted_recompute")
    log_scaffold_event(engine.workspace.root, engine.iteration, CC.LOOP_CONTROL,
                       "p4_refuted_recompute",
                       f"claim={claim[:80]}, verdict={verdict}")
    recompute_task = engine._make_recompute_task(claim, verdict)
    engine._enrich_compute_task_with_prior_failures(recompute_task)
    log_scaffold_event(engine.workspace.root, engine.iteration, CC.LOOP_CONTROL,
                       "p4_recompute_enriched", f"claim={claim[:80]}")
    return recompute_task


_OVERRIDE_CHAIN: list[Override] = [
    Override("forced_critic", 3, _p3_forced_critic_condition, _p3_forced_critic_action),
    Override("refuted_recompute", 6, _p4_refuted_recompute_condition, _p4_refuted_recompute_action),
]


class SciRalph:
    """Main loop for the SciRalph research system."""

    def __init__(self, problem: str, config: Config | None = None,
                 problem_meta: dict | None = None,
                 answer_template: str = ""):
        self.config = config or Config()
        self.metrics = MetricsTracker()
        self.workspace = WorkspaceManager(self.config)
        # Append answer template to problem so all agents see the expected output format
        if answer_template:
            problem = problem.rstrip() + "\n\n# Expected answer format\n\n" + answer_template.strip()
        self.workspace.init(problem)
        self.config.logs_dir = str(self.workspace.logs_dir)
        self.iteration = 0
        self._state = LoopState()
        self.research_state = ResearchState()
        self.problem_meta = problem_meta or {}

        # Initialize agents
        self.orchestrator = OrchestratorAgent(self.config, self.workspace, self.metrics)
        self.researcher = ResearcherAgent(self.config, self.workspace, self.metrics)
        self.computationalist = ComputationalistAgent(self.config, self.workspace, self.metrics)
        self.critic = CriticAgent(self.config, self.workspace, self.metrics)
        self.compressor = CompressorAgent(self.config, self.workspace, self.metrics)
        self.formatter = FormatterAgent(self.config, self.workspace, self.metrics, answer_template)

    def run(self):
        """Main loop: orchestrate → validate → override → dispatch → compress → git."""
        console.print(Panel("SciRalph Research System", style="bold blue"))

        while self.iteration < self.config.max_iterations:
            self.iteration += 1
            console.rule(f"[bold]ITERATION {self.iteration}[/bold]")
            self._update_research_iteration()

            # 1. Orchestrator pass -> CURRENT_TASK.md
            task = self._run_orchestrator()

            # 2. Post-integration validation (Layer B hook -- stub returns [])
            violations = validate_post_integration(
                self.workspace, self.config,
                iteration=self.iteration,
                research_state=getattr(self, "research_state", None),
            )
            if violations:
                self._state.pending_violations.extend(violations)

            # 3. Pre-dispatch overrides (explicit priority chain)
            task = self._apply_overrides(task)

            # 4. Termination gate
            if task.task_type == TaskType.TERMINATE:
                allowed, blockers = can_terminate(
                    self.workspace, self.config, self.metrics, self.problem_meta)
                if allowed:
                    console.print("[green]Orchestrator signaled completion.[/green]")
                    self._run_formatter()
                    self._set_research_status("completed")
                    break
                self._state.pending_termination_blockers = blockers
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "termination_blocked",
                                   f"blockers: {'; '.join(b[:60] for b in blockers)}")
                continue  # re-enter loop

            # 5. Dispatch to agent
            try:
                agent_name, agent_result = self._dispatch(task)
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                self.metrics.alert(
                    self.iteration,
                    f"Dispatch failed (transient): {type(exc).__name__}: {exc}",
                )
                self._state.pending_violations.append(
                    Violation(
                        check="dispatch_failure",
                        severity=ViolationSeverity.WARNING,
                        message=(
                            f"Agent dispatch failed with transient error: "
                            f"{type(exc).__name__}: {str(exc)[:200]}"
                        ),
                        file="CURRENT_TASK.md",
                    )
                )
                console.print(
                    f"[yellow]Dispatch failed (transient), skipping to next "
                    f"iteration: {exc}[/yellow]"
                )
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "dispatch_failure",
                                   f"{type(exc).__name__}: {str(exc)[:200]}")
                continue

            # 5b. Record agent failure signals for orchestrator context
            self._record_agent_failures(task, agent_name, agent_result)

            # 6. Post-dispatch checks
            if task.task_type == TaskType.COMPUTE:
                self._track_compute_verdict(task)

            # 6b. Post-dispatch phantom check — catch refs introduced by agents
            post_phantoms = check_phantom_references(self.workspace)
            if post_phantoms:
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "post_dispatch_phantom",
                                   f"count={len(post_phantoms)}")
                self._state.pending_violations.extend(post_phantoms)

            # 7. Compression, metrics, structured state snapshot, git
            self._check_compression()
            self._update_metrics()
            self._sync_research_state()
            self.workspace.git_commit(
                f"Iteration {self.iteration}: {agent_name} - {task.task_id}"
            )

            # 8. Post-dispatch status check (safety net)
            if self._check_status_field():
                console.print("[green]Research completed or abandoned.[/green]")
                break

        self._final_report()

    def _run_orchestrator(self) -> Task:
        """Run orchestrator pass: set context prefix, get task."""
        console.print("[cyan]Orchestrator[/cyan] planning...")

        # Set context prefix for violations/blockers
        self.orchestrator.context_prefix = self._build_context_prefix()
        # Pass research state reference for tool executor
        self.orchestrator._research_state_ref = getattr(self, "research_state", None)

        orch_task = Task(
            task_id="", task_type=TaskType.RESEARCH,
            assigned_to="orchestrator", iteration=self.iteration,
        )
        orch_response = self.orchestrator.run(orch_task, self.iteration)
        task = self.orchestrator.parse_task(orch_response.text, iteration=self.iteration)
        self._print_task(task)
        return task

    def _log_displacement(self, original_task: Task, override_name: str):
        """Record a task displacement for transparency."""
        summary = {
            "task_id": original_task.task_id,
            "task_type": str(original_task.task_type),
            "body_summary": original_task.body[:80].replace("\n", " "),
            "override": override_name,
            "iteration": self.iteration,
        }
        self._state.displaced_tasks.append(summary)
        self.metrics.alert(
            self.iteration,
            f"task_displaced: {original_task.task_id} ({original_task.task_type}) "
            f"displaced by {override_name}",
        )

    def _record_agent_failures(self, task: Task, agent_name: str, result):
        """Inspect agent result for failure signals and record for orchestrator context."""
        from .llm import AgentResult

        stop = getattr(result, "stop_reason", None)

        # max_tokens truncation (one-shot or agentic)
        if stop == "max_tokens":
            out_tok = getattr(result, "output_tokens", None) or getattr(result, "total_output_tokens", None) or 0
            self._state.agent_failures.append({
                "task_id": task.task_id, "agent": agent_name,
                "event": "max_tokens_truncation",
                "detail": (
                    f"Output hit token limit ({out_tok} tokens). "
                    f"Decompose into smaller subtasks, each targeting a single "
                    f"derivation step or sub-claim."
                ),
                "iteration": self.iteration,
            })
            log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                               "agent_failure_max_tokens",
                               f"task={task.task_id}, agent={agent_name}")

        # max_rounds exhaustion (agentic only)
        if stop == "max_rounds_forced" and isinstance(result, AgentResult):
            self._state.agent_failures.append({
                "task_id": task.task_id, "agent": agent_name,
                "event": "max_rounds_exhaustion",
                "detail": f"Exhausted {result.rounds} tool-use rounds without completing.",
                "iteration": self.iteration,
            })
            log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                               "agent_failure_max_rounds",
                               f"task={task.task_id}, agent={agent_name}, rounds={result.rounds}")

    def _build_context_prefix(self) -> str:
        """Build prefix for orchestrator context with violations, blockers, and displaced tasks."""
        lines = []
        if self._state.pending_violations:
            lines.append(">>> POST-INTEGRATION VIOLATIONS <<<")
            for v in self._state.pending_violations:
                lines.append(f"  [{v.severity}] {v.check}: {v.message}")
            lines.append(">>> END VIOLATIONS <<<\n")
            self._state.pending_violations.clear()
        if self._state.pending_termination_blockers:
            lines.append(">>> TERMINATION BLOCKED — YOU CANNOT TERMINATE YET <<<")
            lines.append("Your previous terminate request was REJECTED for these reasons:")
            for b in self._state.pending_termination_blockers:
                lines.append(f"  - {b}")
            lines.append(
                "Do NOT emit task_type: terminate again until you have addressed "
                "ALL blockers above. Emit the specific task_type indicated in each blocker."
            )
            lines.append(">>> END TERMINATION BLOCKERS <<<\n")
            self._state.pending_termination_blockers.clear()
        if self._state.displaced_tasks:
            lines.append(">>> DISPLACED TASKS (from previous iteration overrides) <<<")
            lines.append("Consider re-scheduling if still needed:")
            for d in self._state.displaced_tasks:
                lines.append(
                    f"  - {d['task_id']} ({d['task_type']}): displaced by "
                    f"{d['override']} at iteration {d['iteration']}. "
                    f"Summary: {d['body_summary']}"
                )
            lines.append(">>> END DISPLACED TASKS <<<\n")
            self._state.displaced_tasks.clear()
        if self._state.agent_failures:
            lines.append(">>> AGENT FAILURES (previous iteration) <<<")
            for f in self._state.agent_failures:
                lines.append(f"  - {f['task_id']} ({f['agent']}): {f['event']}. {f['detail']}")
            lines.append(">>> END AGENT FAILURES <<<\n")
            self._state.agent_failures.clear()
        return "\n".join(lines)

    def _apply_overrides(self, task: Task) -> Task:
        """Consolidated pre-dispatch override chain (declarative priority order).

        Iterates _OVERRIDE_CHAIN sorted by priority. The first matching override
        wins. P6 enrichment is non-overriding and runs after the loop.
        """
        for override in _OVERRIDE_CHAIN:
            if override.condition(self, task):
                return override.action(self, task)

        # Consume unconsumed pending recompute (suppressed by SYNTHESIZE/TERMINATE)
        if self._state.pending_recompute_claim:
            claim = self._state.pending_recompute_claim
            log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                               "p4_refuted_suppressed",
                               f"claim={claim[:80]}, task={task.task_type.value}")
            self._state.pending_recompute_claim = None
            self._state.pending_recompute_verdict = None

        # P6: Enrichment (non-overriding, mutates task body)
        if task.task_type == TaskType.COMPUTE:
            self._enrich_compute_task_with_prior_failures(task)

        return task

    def _dispatch(self, task: Task) -> tuple[str, "LLMResponse | AgentResult"]:
        """Route task to the correct agent. Returns (agent_name, result)."""
        from .llm import AgentResult, LLMResponse  # noqa: F811

        # Pre-dispatch cross-validation (Improvement 6B)
        expected_agent = TASK_TYPE_AGENT_MAP.get(task.task_type)
        if expected_agent:
            if not task.assigned_to or task.assigned_to not in (
                "orchestrator", "researcher", "computationalist", "deep_critic", "compressor", "formatter"
            ):
                self.metrics.alert(
                    self.iteration,
                    f"Routing fix: empty/invalid assigned_to '{task.assigned_to}' "
                    f"for {task.task_type}, inferred '{expected_agent}'",
                )
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "routing_conflict_corrected",
                                   f"'{task.assigned_to}' -> '{expected_agent}' for {task.task_type.value}")
                task.assigned_to = expected_agent
            elif task.assigned_to != expected_agent:
                self.metrics.alert(
                    self.iteration,
                    f"Routing conflict: assigned_to='{task.assigned_to}' "
                    f"vs expected='{expected_agent}' for {task.task_type}; "
                    f"using task_type for routing",
                )
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "routing_conflict_corrected",
                                   f"'{task.assigned_to}' -> '{expected_agent}' for {task.task_type.value}")

        tt = task.task_type

        if tt in (TaskType.RESEARCH, TaskType.DERIVE, TaskType.RESOLVE, TaskType.SYNTHESIZE):
            console.print(f"[green]Researcher[/green] working on: {tt}")
            result = self.researcher.run(task, self.iteration)
            self._state.last_content_iteration = self.iteration
            self._clear_stall_for_research_task(task)
            return "researcher", result

        elif tt == TaskType.COMPUTE:
            console.print("[magenta]Computationalist[/magenta] working...")
            result = self.computationalist.run(task, self.iteration, on_round=self._on_compute_round)
            self._state.last_content_iteration = self.iteration
            return "computationalist", result

        elif tt == TaskType.FORMAT:
            console.print("[cyan]Formatter[/cyan] producing ANSWER.md...")
            result = self.formatter.run(task, self.iteration)
            return "formatter", result

        elif tt == TaskType.CRITIQUE:
            console.print("[red]Deep Critic[/red] reviewing...")
            response = self.critic.run(task, self.iteration)
            if hasattr(response, 'text') and 'NO_CRITIQUES_FILED' in (response.text or ''):
                console.print("[dim]Critic: no issues found[/dim]")
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "no_critiques_filed", "")
                self._state.pending_violations.append(
                    Violation(
                        check="critic_clean",
                        severity=ViolationSeverity.WARNING,
                        message=(
                            "Deep critic found NO issues (NO_CRITIQUES_FILED). "
                            "Do NOT emit another critique task — proceed to "
                            "synthesize or terminate."
                        ),
                        file="CRITIQUE_LOG.md",
                    )
                )
            return "deep_critic", response

        else:
            console.print(f"[yellow]Unknown task type '{tt}', defaulting to researcher[/yellow]")
            result = self.researcher.run(task, self.iteration)
            return "researcher", result

    def _clear_stall_for_research_task(self, task: Task):
        """Clear stall state when a research task addresses a stalled claim."""
        if task.task_type not in (TaskType.RESEARCH, TaskType.DERIVE, TaskType.RESOLVE):
            return
        from .markdown import _ER_WH_ID_RE
        task_ids = set(_ER_WH_ID_RE.findall(task.body))
        if task.target_claim:
            task_ids.update(_ER_WH_ID_RE.findall(task.target_claim))
        if not task_ids:
            return
        for stalled_key in list(self._state.stalled_claims):
            key_ids = set(_ER_WH_ID_RE.findall(stalled_key))
            if key_ids & task_ids:
                self._state.stalled_claims.discard(stalled_key)
                self._state.claim_failure_count.pop(stalled_key, None)
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                                   "stall_cleared_by_research",
                                   f"claim={stalled_key[:80]}")

    def _critic_overdue(self) -> bool:
        """Check if more than N iterations since last critic pass."""
        if (self.iteration - self.metrics.last_critic_iteration) < self.config.critic_every_n:
            return False
        # Skip if critic already reviewed the latest content
        if self.metrics.last_critic_iteration >= self._state.last_content_iteration:
            return False
        return True

    def _make_forced_critic_task(self) -> Task:
        """Create a forced critic task."""
        task = Task(
            task_id=f"TASK-{self.iteration:03d}",
            task_type=TaskType.CRITIQUE,
            assigned_to="deep_critic",
            priority="high",
            iteration=self.iteration,
            body=(
                "# Task Description\n\n"
                "Mandatory periodic review. Perform a thorough critique of all Working\n"
                "Hypotheses and recent Established Results in RESEARCH_STATE.md.\n"
            ),
        )
        self.workspace.write_file("CURRENT_TASK.md", task.to_markdown())
        return task

    def _enrich_compute_task_with_prior_failures(self, task: Task):
        """Append prior failure context to CURRENT_TASK.md for compute retries.

        Consults both the formal ResearchState (failed_approaches) and
        COMPUTATION_LOG.md (Markdown fallback) for comprehensive context.
        """
        comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
        prior = find_prior_failures_for_claim(comp_log, task.body)

        # Also check formal state for failed approaches targeting this claim
        if hasattr(self, "research_state"):
            from .markdown import _ER_WH_ID_RE
            task_ids = set(_ER_WH_ID_RE.findall(task.body))
            for fa in self.research_state.failed_approaches:
                if task_ids and any(tid in fa.description for tid in task_ids):
                    excerpt = f"**Prior failure (iter {fa.iteration}):** {fa.description}\n**Reason:** {fa.reason}"
                    if excerpt not in prior:
                        prior.append(excerpt)

        if not prior:
            return
        log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "p6_enrichment",
                           f"claim={_normalize_claim_key(task.body)[:80]}")
        task_text = self.workspace.read_file("CURRENT_TASK.md")
        addendum = (
            "\n\n---\n\n## Prior Computation Failure Context\n\n"
            f"**{len(prior)} prior failure(s) on this claim.** "
            "Diagnose the ROOT CAUSE before writing new code.\n\n"
            "### Most Recent Failed Result\n\n"
            + prior[0][:self.config.prior_failure_excerpt_chars]
        )
        if len(prior) > 1:
            addendum += f"\n\n({len(prior) - 1} earlier failure(s) in COMPUTATION_LOG.md)\n"
        # Check for zero-output stall in prior failures
        has_zero_output = any("Agent produced no text output" in p for p in prior)
        if has_zero_output:
            addendum += (
                "\n\n**ZERO-OUTPUT STALL DETECTED:** A prior attempt produced no text at all.\n"
                "1. Write a brief plan in text BEFORE calling any tools\n"
                "2. Keep computations simple — verify ONE formula at a time\n"
                "3. Write intermediate results as text between tool calls\n"
            )
        self.workspace.write_file("CURRENT_TASK.md", task_text + addendum)

    def _track_compute_verdict(self, task: Task):
        """After computationalist runs, track verdict and manage recompute/stall."""
        comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
        entries = _parse_comp_entries(comp_log)
        if not entries:
            return

        last = entries[-1]
        verdict = last["verdict"]
        claim = last.get("claim", task.body)
        key = _normalize_claim_key(claim)
        if not key:
            return

        # Register computation in formal state with authoritative target link
        self._register_computation(last, task)

        if verdict == "VERIFIED":
            self._state.claim_failure_count.pop(key, None)
            self._state.stalled_claims.discard(key)  # unblock on successful verification
            return

        # Record failure in formal state
        self._record_failed_computation(last, task, verdict)

        # REFUTED, INCONCLUSIVE, or any non-VERIFIED
        count = self._state.claim_failure_count.get(key, 0) + 1
        self._state.claim_failure_count[key] = count

        if count < self.config.stall_recompute_limit:
            # Allow auto-recompute (existing P4 behavior, now gated)
            self._state.pending_recompute_claim = claim
            self._state.pending_recompute_verdict = verdict
            self._state.agent_failures.append({
                "task_id": task.task_id, "agent": "computationalist",
                "event": f"{verdict.lower()}_verdict",
                "detail": f"Attempt {count}/{self.config.stall_recompute_limit}. Will force recompute next iteration.",
                "iteration": self.iteration,
            })
            self.metrics.alert(
                self.iteration,
                f"{verdict} verdict (attempt {count}/{self.config.stall_recompute_limit}) "
                f"— will force recompute next iteration"
            )
            log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                               "compute_verdict_failed",
                               f"claim={key[:80]}, verdict={verdict}, "
                               f"attempt={count}/{self.config.stall_recompute_limit}")
        else:
            # Escalate: block further recomputes, inform orchestrator
            self._state.stalled_claims.add(key)
            self._state.pending_violations.append(
                Violation(
                    check="computation_stall",
                    severity=ViolationSeverity.WARNING,
                    message=(
                        f"Claim '{key[:80]}' has failed verification {count} times. "
                        "Do NOT schedule another computation on this claim. Either "
                        "(a) route to researcher for an analytical alternative, "
                        "(b) try a fundamentally different computational method, "
                        "or (c) accept provisionally and move on."
                    ),
                    file="COMPUTATION_LOG.md",
                    detail=key,
                )
            )
            self.metrics.alert(
                self.iteration,
                f"{verdict} verdict (attempt {count}) — claim escalated to stall"
            )
            log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                               "compute_verdict_stall_escalation",
                               f"claim={key[:80]}, verdict={verdict}, count={count}")

    def _make_recompute_task(self, claim: str, verdict: str = "REFUTED") -> Task:
        """Create a forced compute task to re-verify a claim after a non-VERIFIED verdict."""
        # Extract target WH/ER ID from claim for formal linking
        from .markdown import _ER_WH_ID_RE
        target_ids = _ER_WH_ID_RE.findall(claim)
        target_claim = target_ids[0] if target_ids else ""
        task = Task(
            task_id=f"TASK-{self.iteration:03d}",
            task_type=TaskType.COMPUTE,
            assigned_to="computationalist",
            priority="high",
            iteration=self.iteration,
            target_claim=target_claim,
            body=(
                f"# Re-verification After {verdict} Verdict\n\n"
                f"The previous computation returned {verdict} for the following claim. "
                "The orchestrator has\n"
                "integrated corrections. Verify the CORRECTED version now appears in\n"
                "RESEARCH_STATE.md and compute a fresh verification.\n\n"
                f"**Claim to re-verify:** {claim[:500]}\n"
            ),
        )
        self.workspace.write_file("CURRENT_TASK.md", task.to_markdown())
        return task

    def _check_compression(self):
        """Check file sizes against thresholds, compress if needed."""
        for filename, threshold in self.config.compress_threshold.items():
            size = self.workspace.file_size(filename)
            if size > threshold:
                self.metrics.alert(
                    self.iteration,
                    f"{filename} size ({size}) exceeds threshold ({threshold})."
                )
                if size > threshold * self.config.compress_soft_multiplier:
                    console.print(f"[yellow]Compressing {filename} ({size}/{threshold})[/yellow]")
                    compress_task = Task(
                        task_id=f"COMPRESS-{self.iteration:03d}",
                        task_type=TaskType.RESEARCH,
                        assigned_to="compressor",
                        iteration=self.iteration,
                        target_file=filename,
                    )
                    self.compressor.run(compress_task, self.iteration)

    def _update_research_iteration(self):
        """Update the iteration field in RESEARCH_STATE.md frontmatter."""
        text = self.workspace.read_file("RESEARCH_STATE.md")
        if not text:
            return
        meta, body = parse_frontmatter(text)
        meta["iteration"] = self.iteration
        self.workspace.write_file("RESEARCH_STATE.md", render_frontmatter(meta, body))

    def _set_research_status(self, status: str):
        """Update the status field in RESEARCH_STATE.md frontmatter."""
        text = self.workspace.read_file("RESEARCH_STATE.md")
        if not text:
            return
        meta, body = parse_frontmatter(text)
        meta["status"] = status
        self.workspace.write_file("RESEARCH_STATE.md", render_frontmatter(meta, body))

    def _run_formatter(self):
        """Run the formatter agent to produce ANSWER.md."""
        console.print("[cyan]Formatter[/cyan] producing ANSWER.md...")
        fmt_task = Task(
            task_id=f"FORMAT-{self.iteration:03d}",
            task_type=TaskType.FORMAT,
            assigned_to="formatter",
            iteration=self.iteration,
        )
        self.formatter.run(fmt_task, self.iteration)
        self.workspace.git_commit(
            f"Iteration {self.iteration}: formatter - ANSWER.md"
        )

    def _check_status_field(self) -> bool:
        """Check termination conditions beyond max_iterations."""
        state = self.workspace.read_file("RESEARCH_STATE.md")
        for status in ("completed", "abandoned", "partially_complete"):
            if f'status: "{status}"' in state or f"status: {status}" in state:
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "status_field_exit",
                                   f"status={status}")
                return True
        return False

    def _record_failed_computation(self, comp_entry: dict, task: Task, verdict: str):
        """Record a non-VERIFIED computation as a FailedApproach in the research state."""
        if not hasattr(self, "research_state"):
            return
        from .research_state import FailedApproach

        comp_id = comp_entry.get("id", "")
        claim = comp_entry.get("claim", task.body[:200])
        method = comp_entry.get("method", "")
        notes = comp_entry.get("notes", "")
        result = comp_entry.get("result", "")

        description = f"{verdict} on: {claim}"
        if method:
            description += f"\nMethod: {method}"
        reason = notes or result or f"Computation returned {verdict}"

        self.research_state.failed_approaches.append(FailedApproach(
            description=description,
            reason=reason,
            related_comps=[comp_id] if comp_id else [],
            iteration=self.iteration,
        ))

    def _register_computation(self, comp_entry: dict, task: Task):
        """Register a computation in the formal research state with authoritative target link."""
        if not hasattr(self, "research_state"):
            return
        from .research_state import Computation, Verdict
        from .markdown import _ER_WH_ID_RE

        comp_id = comp_entry["id"]
        if not comp_id.startswith("COMP-"):
            return  # Skip TASK-* stubs from forced-call bailouts
        verdict_str = comp_entry.get("verdict", "INCONCLUSIVE")
        try:
            verdict = Verdict(verdict_str)
        except ValueError:
            verdict = Verdict.INCONCLUSIVE

        # Authoritative target: task.target_claim > IDs in claim > IDs in body
        target = task.target_claim
        if not target:
            claim = comp_entry.get("claim", "")
            ids = _ER_WH_ID_RE.findall(claim)
            if not ids:
                ids = _ER_WH_ID_RE.findall(comp_entry.get("body", ""))
            target = ids[0] if ids else ""

        self.research_state.computations[comp_id] = Computation(
            id=comp_id,
            target_hypothesis=target,
            verdict=verdict,
            claim=comp_entry.get("claim", ""),
            method=comp_entry.get("method", ""),
            iteration=self.iteration,
        )

    def _sync_research_state(self):
        """Build structured ResearchState from Markdown and save to workspace.

        Preserves authoritative target_hypothesis links registered via
        _register_computation (which may use task.target_claim).
        """
        # Save abandoned hypotheses (not visible in Markdown after removal)
        from .research_state import HypothesisStatus
        abandoned_hypotheses = {
            hid: h for hid, h in self.research_state.hypotheses.items()
            if h.status == HypothesisStatus.ABANDONED
        }
        # Save authoritative data before rebuilding
        authoritative_targets = {
            comp_id: comp.target_hypothesis
            for comp_id, comp in self.research_state.computations.items()
            if comp.target_hypothesis
        }
        authoritative_resolutions = {
            cid: (c.iteration_resolved, c.resolution)
            for cid, c in self.research_state.critiques.items()
            if c.iteration_resolved is not None
        }
        self.research_state = _build_research_state(self.workspace)
        # Restore authoritative targets that may be better than substring-derived ones
        for comp_id, target in authoritative_targets.items():
            if comp_id in self.research_state.computations:
                self.research_state.computations[comp_id].target_hypothesis = target
        # Restore authoritative critique resolution metadata
        for cid, (iter_res, res_text) in authoritative_resolutions.items():
            if cid in self.research_state.critiques:
                self.research_state.critiques[cid].iteration_resolved = iter_res
                if res_text:
                    self.research_state.critiques[cid].resolution = res_text
        # Restore abandoned hypotheses (removed from Markdown, kept in graph)
        for hid, h in abandoned_hypotheses.items():
            if hid not in self.research_state.hypotheses:
                self.research_state.hypotheses[hid] = h
        # Fix stale WH↔ER backlinks and rebuild supporting_comps
        self.research_state.normalize_references()
        self.research_state.save(self.workspace.root)

    def _update_metrics(self):
        """Write current metrics to METRICS.md."""
        file_sizes = {}
        for filename in self.config.compress_threshold:
            file_sizes[filename] = self.workspace.file_size(filename)
        md = self.metrics.to_markdown(file_sizes, self.config.compress_threshold)
        self.workspace.write_file("METRICS.md", md)

    def _print_task(self, task: Task):
        """Print task summary to console."""
        text = Text()
        text.append("Task: ", style="bold")
        text.append(f"{task.task_id} ", style="cyan")
        text.append(f"[{task.task_type}] ", style="yellow")
        text.append(f"-> {task.assigned_to}", style="green")
        console.print(text)

    def _on_compute_round(self, round_num, stop_reason, tool_calls, total_input, total_output):
        """Progress callback for computationalist tool-use rounds."""
        tokens = f"{total_input + total_output:,}tok"
        if stop_reason == "forced_partial":
            console.print(f"  round {round_num}: forced final call ({tokens})", style="dim magenta")
            return
        n_tools = len(tool_calls)
        errors = sum(1 for tc in tool_calls if tc.is_error)
        if errors:
            status = f"{n_tools} tool call{'s' if n_tools != 1 else ''}, {errors} error{'s' if errors != 1 else ''}"
        else:
            status = f"{n_tools} tool call{'s' if n_tools != 1 else ''}"
        console.print(f"  round {round_num}: {status} ({tokens})", style="dim magenta")

    def _final_report(self):
        """Flush metrics and print final summary."""
        self._update_metrics()
        self.workspace.git_commit(f"Final metrics flush (iteration {self.iteration})")
        console.rule("[bold green]SESSION COMPLETE[/bold green]")
        console.print(f"Total iterations: {self.iteration}")
        console.print(f"Total LLM calls: {len(self.metrics.calls)}")
        console.print(f"Total input tokens: {self.metrics.total_input_tokens:,}")
        console.print(f"Total output tokens: {self.metrics.total_output_tokens:,}")
        if self.config.input_cost or self.config.output_cost:
            cost = (self.metrics.total_input_tokens * self.config.input_cost
                    + self.metrics.total_output_tokens * self.config.output_cost) / 1_000_000
            console.print(f"Estimated cost: ${cost:.2f}")
        console.print(f"Workspace: {self.workspace.root.resolve()}")
        if self.metrics.alerts:
            console.print(f"\n[yellow]Alerts ({len(self.metrics.alerts)}):[/yellow]")
            for a in self.metrics.alerts[-5:]:
                console.print(f"  [iter {a['iteration']}] {a['message']}")
