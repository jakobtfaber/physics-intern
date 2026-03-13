"""SciRalph main loop engine."""

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
    detect_computation_stalls,
    _normalize_claim_key,
    count_er_sections,
    count_wh_sections,
)
from .metrics import MetricsTracker
from .task import Task, TaskType, TASK_TYPE_AGENT_MAP
from .validation import validate_post_integration, can_terminate, check_phantom_references, Violation, ViolationSeverity
from .workspace import WorkspaceManager, log_scaffold_event
from .agents.orchestrator import OrchestratorAgent
from .agents.researcher import ResearcherAgent
from .agents.computationalist import ComputationalistAgent
from .agents.critic import CriticAgent
from .agents.compressor import CompressorAgent

console = Console()


class SciRalph:
    """Main loop for the SciRalph research system."""

    def __init__(self, problem: str, config: Config | None = None,
                 problem_meta: dict | None = None):
        self.config = config or Config()
        self.metrics = MetricsTracker()
        self.workspace = WorkspaceManager(self.config)
        self.workspace.init(problem)
        self.config.audit_log = str(self.workspace.root / "AUDIT_LOG.jsonl")
        self.config.logs_dir = str(self.workspace.logs_dir)
        self.iteration = 0
        self._stale_iterations = 0
        self._pending_recompute_claim: str | None = None
        self._stalled_claims: set[str] = set()
        self._claim_failure_count: dict[str, int] = {}
        self._last_content_iteration: int = 0
        self.problem_meta = problem_meta or {}
        self._pending_violations: list = []
        self._pending_termination_blockers: list[str] = []
        self._displaced_tasks: list[dict] = []

        # Initialize agents
        self.orchestrator = OrchestratorAgent(self.config, self.workspace, self.metrics)
        self.researcher = ResearcherAgent(self.config, self.workspace, self.metrics)
        self.computationalist = ComputationalistAgent(self.config, self.workspace, self.metrics)
        self.critic = CriticAgent(self.config, self.workspace, self.metrics)
        self.compressor = CompressorAgent(self.config, self.workspace, self.metrics)

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
            violations = validate_post_integration(self.workspace, self.config, iteration=self.iteration)
            if violations:
                self._pending_violations.extend(violations)

            # 3. Pre-dispatch overrides (explicit priority chain)
            task = self._apply_overrides(task)

            # 4. Termination gate
            if task.task_type == TaskType.TERMINATE:
                allowed, blockers = can_terminate(
                    self.workspace, self.config, self.metrics, self.problem_meta)
                if allowed:
                    console.print("[green]Orchestrator signaled completion.[/green]")
                    self._set_research_status("completed")
                    break
                self._pending_termination_blockers = blockers
                log_scaffold_event(self.workspace.root, self.iteration, 6, "termination_blocked",
                                   f"blockers: {'; '.join(b[:60] for b in blockers)}")
                continue  # re-enter loop

            # 5. Dispatch to agent
            try:
                agent_name = self._dispatch(task)
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                self.metrics.alert(
                    self.iteration,
                    f"Dispatch failed (transient): {type(exc).__name__}: {exc}",
                )
                self._pending_violations.append(
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
                log_scaffold_event(self.workspace.root, self.iteration, 6, "dispatch_failure",
                                   f"{type(exc).__name__}: {str(exc)[:200]}")
                continue

            # 6. Post-dispatch checks
            if task.task_type == TaskType.COMPUTE:
                self._track_compute_verdict(task)
                self._update_stall_tracking()

            # 6b. Post-dispatch phantom check — catch refs introduced by agents
            post_phantoms = check_phantom_references(self.workspace)
            if post_phantoms:
                log_scaffold_event(self.workspace.root, self.iteration, 6, "post_dispatch_phantom",
                                   f"count={len(post_phantoms)}")
                self._pending_violations.extend(post_phantoms)

            # 7. Compression, metrics, git
            self._check_compression()
            self._update_metrics()
            self.workspace.git_commit(
                f"Iteration {self.iteration}: {agent_name} - {task.task_id}"
            )

            # 8. Post-dispatch status check (safety net)
            if self._check_status_field():
                console.print("[green]Research completed or abandoned.[/green]")
                break

        self._final_report()

    def _run_orchestrator(self) -> Task:
        """Run orchestrator pass: validate phantoms, set context prefix, get task."""
        console.print("[cyan]Orchestrator[/cyan] planning...")

        # Validate COMP/TASK references in RESEARCH_STATE
        phantoms = self.workspace.validate_comp_references()
        if phantoms:
            self.metrics.alert(
                self.iteration,
                f"Phantom references stripped: {', '.join(phantoms)}"
            )

        # Set context prefix for violations/blockers
        self.orchestrator.context_prefix = self._build_context_prefix()

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
        self._displaced_tasks.append(summary)
        self.metrics.alert(
            self.iteration,
            f"task_displaced: {original_task.task_id} ({original_task.task_type}) "
            f"displaced by {override_name}",
        )

    def _build_context_prefix(self) -> str:
        """Build prefix for orchestrator context with violations, blockers, and displaced tasks."""
        lines = []
        if self._pending_violations:
            # Separate ER-demotion violations from other violations
            demoted_wh_ids = []
            other_violations = []
            for v in self._pending_violations:
                if v.check == "er_promotion_gate" and v.severity == "error":
                    # Extract "WH-NNN" from message like "ER-001 ... demoted to WH-001"
                    parts = v.message.split("demoted to ")
                    wh_id = parts[1].strip() if len(parts) == 2 else v.detail
                    demoted_wh_ids.append(wh_id)
                else:
                    other_violations.append(v)
            # Emit dedicated banner for unverified claims requiring computation
            if demoted_wh_ids:
                lines.append(">>> UNVERIFIED CLAIMS REQUIRING COMPUTATION <<<")
                lines.append(
                    "The following claims were demoted from ER to WH because "
                    "they have NO VERIFIED computation in COMPUTATION_LOG:"
                )
                for wh_id in demoted_wh_ids:
                    lines.append(f"  - {wh_id}")
                lines.append("")
                lines.append(
                    "ACTION REQUIRED: Schedule COMPUTE tasks for these claims. "
                    "Do NOT re-promote them to ER — the scaffolding will demote "
                    "them again. Only a VERIFIED computation can establish an ER."
                )
                lines.append(">>> END UNVERIFIED CLAIMS <<<\n")
            # Emit remaining violations normally
            if other_violations:
                lines.append(">>> POST-INTEGRATION VIOLATIONS <<<")
                for v in other_violations:
                    lines.append(f"  [{v.severity}] {v.check}: {v.message}")
                lines.append(">>> END VIOLATIONS <<<\n")
            self._pending_violations.clear()
        if self._pending_termination_blockers:
            lines.append(">>> TERMINATION BLOCKED — YOU CANNOT TERMINATE YET <<<")
            lines.append("Your previous terminate request was REJECTED for these reasons:")
            for b in self._pending_termination_blockers:
                lines.append(f"  - {b}")
            lines.append(
                "Do NOT emit task_type: terminate again until you have addressed "
                "ALL blockers above. Emit the specific task_type indicated in each blocker."
            )
            lines.append(">>> END TERMINATION BLOCKERS <<<\n")
            self._pending_termination_blockers.clear()
        if self._displaced_tasks:
            lines.append(">>> DISPLACED TASKS (from previous iteration overrides) <<<")
            lines.append("Consider re-scheduling if still needed:")
            for d in self._displaced_tasks:
                lines.append(
                    f"  - {d['task_id']} ({d['task_type']}): displaced by "
                    f"{d['override']} at iteration {d['iteration']}. "
                    f"Summary: {d['body_summary']}"
                )
            lines.append(">>> END DISPLACED TASKS <<<\n")
            self._displaced_tasks.clear()
        return "\n".join(lines)

    def _apply_overrides(self, task: Task) -> Task:
        """Consolidated pre-dispatch override chain (explicit priority order)."""
        # P1: Budget enforcement (highest priority)
        budget_remaining = self.config.max_iterations - self.iteration
        if budget_remaining <= self.config.budget_override_margin and task.task_type not in (
                TaskType.SYNTHESIZE, TaskType.TERMINATE):
            console.print(
                f"[yellow]Budget enforcement: {budget_remaining} iteration(s) left, "
                f"overriding '{task.task_type}' -> 'synthesize'.[/yellow]"
            )
            self._log_displacement(task, "budget_enforcement")
            log_scaffold_event(self.workspace.root, self.iteration, 5, "p1_budget_override",
                               f"{task.task_type.value} -> synthesize")
            return self._make_budget_synthesize_task()

        # P2: Stale-loop -> force SYNTHESIZE (not break)
        if self._is_stale_loop(task):
            self._log_displacement(task, "stale_loop")
            log_scaffold_event(self.workspace.root, self.iteration, 5, "p2_stale_loop_override",
                               f"stale_iterations={self._stale_iterations}")
            return self._make_budget_synthesize_task()

        # P3: Forced critic (overdue) — never override terminal tasks
        if (self._critic_overdue()
                and task.task_type not in (TaskType.CRITIQUE, TaskType.SYNTHESIZE, TaskType.TERMINATE)):
            console.print(
                f"[yellow]Forcing critic pass (overdue: last critic at "
                f"iter {self.metrics.last_critic_iteration}, "
                f"threshold {self.config.critic_every_n}).[/yellow]"
            )
            self._log_displacement(task, "forced_critic")
            log_scaffold_event(self.workspace.root, self.iteration, 5, "p3_forced_critic",
                               f"last_critic={self.metrics.last_critic_iteration}")
            return self._make_forced_critic_task()

        # P3b: Block redundant critic (no new content since last review)
        if (task.task_type == TaskType.CRITIQUE
                and self.metrics.last_critic_iteration > 0
                and self.metrics.last_critic_iteration >= self._last_content_iteration):
            console.print(
                "[yellow]Skipping redundant critic — no new content since "
                f"iteration {self.metrics.last_critic_iteration} review.[/yellow]"
            )
            self._log_displacement(task, "redundant_critic")
            log_scaffold_event(self.workspace.root, self.iteration, 5, "p3b_redundant_critic_suppressed", "")
            return self._make_post_critic_synthesize_task()

        # P5: Block dispatch to stalled claim (checked before P4 as defense-in-depth)
        if task.task_type == TaskType.COMPUTE:
            claim_key = _normalize_claim_key(task.body)
            if claim_key in self._stalled_claims:
                self._pending_violations.append(
                    Violation(
                        check="stall_detection",
                        severity=ViolationSeverity.WARNING,
                        message=f"Stalled claim blocked: {claim_key[:80]}",
                        file="COMPUTATION_LOG.md",
                        detail=claim_key,
                    )
                )
                # Defense-in-depth: clear pending recompute if it targets the same claim
                if self._pending_recompute_claim:
                    pending_key = _normalize_claim_key(self._pending_recompute_claim)
                    if pending_key == claim_key:
                        self._pending_recompute_claim = None
                self._log_displacement(task, "stall_block")
                log_scaffold_event(self.workspace.root, self.iteration, 5, "p5_stall_block",
                                   f"claim={claim_key[:80]}")
                # Return a research task instead — let orchestrator rethink
                return Task(
                    task_id=task.task_id,
                    task_type=TaskType.RESEARCH,
                    assigned_to="researcher",
                    priority=task.priority,
                    iteration=task.iteration,
                    body=(
                        "# Alternative Approach Needed\n\n"
                        f"Computation stalled on: {claim_key[:200]}\n"
                        "Multiple attempts have failed. Consider an alternative derivation "
                        "or analytical approach.\n"
                    ),
                )

        # P4: REFUTED recompute
        if self._pending_recompute_claim:
            claim = self._pending_recompute_claim
            self._pending_recompute_claim = None
            if task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
                console.print("[yellow]Forcing recompute after REFUTED verdict.[/yellow]")
                self._log_displacement(task, "refuted_recompute")
                log_scaffold_event(self.workspace.root, self.iteration, 5, "p4_refuted_recompute",
                                   f"claim={claim[:80]}")
                return self._make_recompute_task(claim)
            else:
                log_scaffold_event(self.workspace.root, self.iteration, 5, "p4_refuted_suppressed",
                                   f"claim={claim[:80]}, task={task.task_type.value}")

        # P6: Enrichment (non-overriding, mutates task body)
        if task.task_type == TaskType.COMPUTE:
            self._enrich_compute_task_with_prior_failures(task)

        return task

    def _is_stale_loop(self, task: Task) -> bool:
        """Detect stale loop when research appears complete but orchestrator didn't terminate."""
        if task.task_type in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
            self._stale_iterations = 0
            return False
        state = self.workspace.read_file("RESEARCH_STATE.md")
        er_count = count_er_sections(state)
        wh_count = count_wh_sections(state)
        if er_count >= self.config.min_er_for_completion and wh_count == 0:
            self._stale_iterations += 1
            if self._stale_iterations >= 2:
                console.print(
                    "[yellow]Backstop: research appears complete but orchestrator "
                    "did not terminate. Forcing synthesize.[/yellow]"
                )
                self.metrics.alert(self.iteration, "Stale loop detected — forcing synthesize")
                return True
        else:
            self._stale_iterations = 0
        return False

    def _dispatch(self, task: Task) -> str:
        """Route task to the correct agent."""
        # Pre-dispatch cross-validation (Improvement 6B)
        expected_agent = TASK_TYPE_AGENT_MAP.get(task.task_type)
        if expected_agent:
            if not task.assigned_to or task.assigned_to not in (
                "orchestrator", "researcher", "computationalist", "deep_critic", "compressor"
            ):
                self.metrics.alert(
                    self.iteration,
                    f"Routing fix: empty/invalid assigned_to '{task.assigned_to}' "
                    f"for {task.task_type}, inferred '{expected_agent}'",
                )
                log_scaffold_event(self.workspace.root, self.iteration, 6, "routing_conflict_corrected",
                                   f"'{task.assigned_to}' -> '{expected_agent}' for {task.task_type.value}")
                task.assigned_to = expected_agent
            elif task.assigned_to != expected_agent:
                self.metrics.alert(
                    self.iteration,
                    f"Routing conflict: assigned_to='{task.assigned_to}' "
                    f"vs expected='{expected_agent}' for {task.task_type}; "
                    f"using task_type for routing",
                )
                log_scaffold_event(self.workspace.root, self.iteration, 6, "routing_conflict_corrected",
                                   f"'{task.assigned_to}' -> '{expected_agent}' for {task.task_type.value}")

        tt = task.task_type

        if tt in (TaskType.RESEARCH, TaskType.DERIVE, TaskType.RESOLVE, TaskType.SYNTHESIZE):
            console.print(f"[green]Researcher[/green] working on: {tt}")
            self.researcher.run(task, self.iteration)
            self._last_content_iteration = self.iteration
            return "researcher"

        elif tt == TaskType.COMPUTE:
            console.print("[magenta]Computationalist[/magenta] working...")
            self.computationalist.run(task, self.iteration, on_round=self._on_compute_round)
            self._last_content_iteration = self.iteration
            return "computationalist"

        elif tt == TaskType.CRITIQUE:
            console.print("[red]Deep Critic[/red] reviewing...")
            response = self.critic.run(task, self.iteration)
            if hasattr(response, 'text') and 'NO_CRITIQUES_FILED' in (response.text or ''):
                console.print("[dim]Critic: no issues found[/dim]")
                log_scaffold_event(self.workspace.root, self.iteration, 6, "no_critiques_filed", "")
                self._pending_violations.append(
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
            return "deep_critic"

        else:
            console.print(f"[yellow]Unknown task type '{tt}', defaulting to researcher[/yellow]")
            self.researcher.run(task, self.iteration)
            return "researcher"

    def _critic_overdue(self) -> bool:
        """Check if more than N iterations since last critic pass."""
        if (self.iteration - self.metrics.last_critic_iteration) < self.config.critic_every_n:
            return False
        # Skip if critic already reviewed the latest content
        if self.metrics.last_critic_iteration >= self._last_content_iteration:
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

    def _make_budget_synthesize_task(self) -> Task:
        """Create a forced synthesize task due to budget exhaustion."""
        task = Task(
            task_id=f"TASK-{self.iteration:03d}",
            task_type=TaskType.SYNTHESIZE,
            assigned_to="researcher",
            priority="high",
            iteration=self.iteration,
            body=(
                "# Budget-Enforced Synthesis\n\n"
                "Iteration budget nearly exhausted. Synthesize ALL Established Results into\n"
                "a final answer. Note unresolved items as limitations. Set status to\n"
                "'partially_complete' if gaps remain.\n"
            ),
        )
        self.workspace.write_file("CURRENT_TASK.md", task.to_markdown())
        return task

    def _make_post_critic_synthesize_task(self) -> Task:
        """Create a synthesize task when critic found no issues and no new content exists."""
        task = Task(
            task_id=f"TASK-{self.iteration:03d}",
            task_type=TaskType.SYNTHESIZE,
            assigned_to="researcher",
            priority="high",
            iteration=self.iteration,
            body=(
                "# Synthesis After Clean Review\n\n"
                "The deep critic found no issues on its last pass and no new research\n"
                "content has been produced since. Synthesize ALL Established Results into\n"
                "a final answer. Note unresolved items as limitations. Set status to\n"
                "'partially_complete' if gaps remain.\n"
            ),
        )
        self.workspace.write_file("CURRENT_TASK.md", task.to_markdown())
        return task

    def _enrich_compute_task_with_prior_failures(self, task: Task):
        """Append prior failure context to CURRENT_TASK.md for compute retries."""
        comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
        prior = find_prior_failures_for_claim(comp_log, task.body)
        if not prior:
            return
        log_scaffold_event(self.workspace.root, self.iteration, 5, "p6_enrichment",
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

        if verdict == "VERIFIED":
            self._claim_failure_count.pop(key, None)
            return

        # REFUTED, INCONCLUSIVE, or any non-VERIFIED
        count = self._claim_failure_count.get(key, 0) + 1
        self._claim_failure_count[key] = count

        if count < self.config.stall_recompute_limit:
            # Allow auto-recompute (existing P4 behavior, now gated)
            self._pending_recompute_claim = claim
            self.metrics.alert(
                self.iteration,
                f"{verdict} verdict (attempt {count}/{self.config.stall_recompute_limit}) "
                f"— will force recompute next iteration"
            )
            log_scaffold_event(self.workspace.root, self.iteration, 6,
                               "compute_verdict_failed",
                               f"claim={key[:80]}, verdict={verdict}, "
                               f"attempt={count}/{self.config.stall_recompute_limit}")
        else:
            # Escalate: block further recomputes, inform orchestrator
            self._stalled_claims.add(key)
            self._pending_violations.append(
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
            log_scaffold_event(self.workspace.root, self.iteration, 6,
                               "compute_verdict_stall_escalation",
                               f"claim={key[:80]}, verdict={verdict}, count={count}")

    def _update_stall_tracking(self):
        """Update stall tracking after compute dispatch."""
        comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
        stalls = detect_computation_stalls(comp_log, threshold=self.config.stall_threshold)
        for stall in stalls:
            self._stalled_claims.add(stall["claim"])

    def _make_recompute_task(self, claim: str) -> Task:
        """Create a forced compute task to re-verify a REFUTED claim after correction."""
        task = Task(
            task_id=f"TASK-{self.iteration:03d}",
            task_type=TaskType.COMPUTE,
            assigned_to="computationalist",
            priority="high",
            iteration=self.iteration,
            body=(
                "# Re-verification After REFUTED Verdict\n\n"
                "The previous computation REFUTED the following claim. The orchestrator has\n"
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
                if size > threshold * self.config.compress_hard_multiplier:
                    console.print(f"[yellow]Force-compressing {filename}[/yellow]")
                    compress_task = Task(
                        task_id=f"COMPRESS-{self.iteration:03d}",
                        task_type=TaskType.RESEARCH,
                        assigned_to="compressor",
                        iteration=self.iteration,
                        target_file=filename,
                    )
                    self.compressor.run(compress_task, self.iteration)
                elif size > threshold * self.config.compress_soft_multiplier:
                    console.print(f"[yellow]Compressing {filename}[/yellow]")
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

    def _check_status_field(self) -> bool:
        """Check termination conditions beyond max_iterations."""
        state = self.workspace.read_file("RESEARCH_STATE.md")
        for status in ("completed", "abandoned", "partially_complete"):
            if f'status: "{status}"' in state or f"status: {status}" in state:
                log_scaffold_event(self.workspace.root, self.iteration, 6, "status_field_exit",
                                   f"status={status}")
                return True
        return False

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
