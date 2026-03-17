"""SciRalph main loop engine."""

from dataclasses import dataclass, field

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import Config
from .llm import _is_transient
from .metrics import MetricsTracker
from .task import Task, TaskType
from .categories import CompensationCategory as CC
from .research_state import ResearchState
from .renderers import render_research_state_md, render_computation_log_md, render_critique_log_md
from .validation import validate_post_integration, can_terminate, Violation, ViolationSeverity
from .workspace import WorkspaceManager, log_scaffold_event
from .agents.orchestrator import OrchestratorAgent
from .agents.computationalist import ComputationalistAgent
from .agents.compute_verify import ComputeVerifyAgent
from .agents.compute_explore import ComputeExploreAgent
from .agents.research_verify import ResearchVerifyAgent
from .agents.research_explore import ResearchExploreAgent
from .agents.critic import CriticAgent
from .agents.compressor import CompressorAgent
from .agents.formatter import FormatterAgent
from .agents.strategist import StrategistAgent

console = Console()


@dataclass
class LoopState:
    """Inter-iteration state for the main research loop."""
    claim_failure_count: dict[str, int] = field(default_factory=dict)
    last_content_iteration: int = 0
    consecutive_termination_blocks: int = 0
    # Consumed-once feedback accumulators (cleared after orchestrator reads them)
    pending_violations: list = field(default_factory=list)
    pending_termination_blockers: list[str] = field(default_factory=list)
    pending_compute_verdicts: list[dict] = field(default_factory=list)
    pending_explore_results: list[dict] = field(default_factory=list)
    agent_failures: list[dict] = field(default_factory=list)


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
        self.research_state.problem_statement = problem.strip()
        self.research_state.title = self.workspace.root.name
        self.problem_meta = problem_meta or {}

        # Initialize agents
        self.orchestrator = OrchestratorAgent(self.config, self.workspace, self.metrics)
        self.computationalist = ComputationalistAgent(self.config, self.workspace, self.metrics)
        self.compute_verify = ComputeVerifyAgent(self.config, self.workspace, self.metrics)
        self.compute_explore = ComputeExploreAgent(self.config, self.workspace, self.metrics)
        self.research_verify = ResearchVerifyAgent(self.config, self.workspace, self.metrics)
        self.research_explore = ResearchExploreAgent(self.config, self.workspace, self.metrics)
        self.critic = CriticAgent(self.config, self.workspace, self.metrics)
        self.compressor = CompressorAgent(self.config, self.workspace, self.metrics)
        self.formatter = FormatterAgent(self.config, self.workspace, self.metrics, answer_template)
        self.strategist = StrategistAgent(self.config, self.workspace, self.metrics)

    def run(self):
        """Main loop: strategize → orchestrate → validate → override → dispatch → compress → git."""
        console.print(Panel("SciRalph Research System", style="bold blue"))

        self._run_strategist()

        while self.iteration < self.config.max_iterations:
            self.iteration += 1
            console.rule(f"[bold]ITERATION {self.iteration}[/bold]")
            self._update_research_iteration()

            # 1. Forced critic or orchestrator pass
            if self._critic_overdue():
                console.print(
                    f"[yellow]Forced critic (last at iter "
                    f"{self.metrics.last_critic_iteration}, "
                    f"threshold {self.config.critic_every_n})[/yellow]"
                )
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                                   "forced_critic",
                                   f"last_critic={self.metrics.last_critic_iteration}")
                task = self._make_forced_critic_task()
            else:
                task = self._run_orchestrator()

            # 2. Post-integration validation
            violations = validate_post_integration(
                self.research_state,
                iteration=self.iteration,
                workspace=self.workspace,
            )
            if violations:
                self._state.pending_violations.extend(violations)

            # 3. Enrichment for compute tasks
            if task.task_type in (TaskType.COMPUTE_VERIFY, TaskType.COMPUTE_EXPLORE, TaskType.RESEARCH_VERIFY):
                self._enrich_compute_task_with_prior_failures(task)

            # 4. Termination gate
            if task.task_type == TaskType.TERMINATE:
                allowed, blockers = can_terminate(
                    self.workspace, self.config, self.metrics, self.problem_meta,
                    research_state=self.research_state)
                if allowed:
                    console.print("[green]Orchestrator signaled completion.[/green]")
                    self._run_formatter()
                    self._set_research_status("completed")
                    self._render_files_for_git()
                    self._sync_research_state()
                    break
                self._state.consecutive_termination_blocks += 1
                self._state.pending_termination_blockers = blockers
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "termination_blocked",
                                   f"blockers: {'; '.join(b[:60] for b in blockers)}, "
                                   f"consecutive={self._state.consecutive_termination_blocks}")
                # Circuit breaker: after repeated blocks, auto-abandon remaining WHs
                if self._state.consecutive_termination_blocks >= self.config.max_termination_retries:
                    self._force_abandon_working_hypotheses()
                continue  # re-enter loop
            else:
                self._state.consecutive_termination_blocks = 0

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
            if task.task_type in (TaskType.COMPUTE_EXPLORE, TaskType.COMPUTE_VERIFY, TaskType.RESEARCH_VERIFY, TaskType.RESEARCH_EXPLORE):
                self._track_computation(task)

            # 7. Compression, metrics, structured state snapshot, render files, git
            self._check_compression()
            self._update_metrics()
            self._sync_research_state()
            self._render_files_for_git()
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
        # Pass research state reference for tool executor and context rendering
        self.orchestrator.research_state = self.research_state

        orch_task = Task(
            task_id="", task_type=TaskType.RESEARCH_EXPLORE,
            assigned_to="orchestrator", iteration=self.iteration,
        )
        orch_response = self.orchestrator.run(orch_task, self.iteration)
        task = self.orchestrator.parse_task(orch_response.text, iteration=self.iteration)
        self._print_task(task)
        return task

    def _run_strategist(self):
        """Run strategist agent before the main loop to produce a research plan."""
        console.print("[cyan]Strategist[/cyan] analyzing problem...")
        self.strategist.research_state = self.research_state
        task = Task(
            task_id="STRATEGY-000", task_type=TaskType.STRATEGIZE,
            assigned_to="strategist", iteration=0,
        )
        self.strategist.run(task, 0)
        self._apply_strategist_plan()
        self._sync_research_state()
        self._render_files_for_git()
        self.workspace.git_commit("Iteration 0: strategist — research plan")

    def _apply_strategist_plan(self):
        """Seed RQs and dead ends from the strategist's parsed plan."""
        from .research_state import FailedApproach, ResearchQuestion

        plan = self.strategist.parsed_plan
        if plan is None:
            return

        self.research_state.research_plan = plan

        # Seed initial RQs
        for rq_entry in self.strategist.initial_rqs:
            num = self.research_state.next_entity_num()
            rq_id = f"RQ-{num:03d}"
            self.research_state.research_questions[rq_id] = ResearchQuestion(
                id=rq_id,
                question=rq_entry.get("question", ""),
                context=rq_entry.get("context", ""),
                iteration_created=0,
            )
            # Link back to sub-problem
            sp_id = rq_entry.get("sub_problem", "")
            if sp_id and sp_id in plan.sub_problems:
                plan.sub_problems[sp_id].initial_rqs.append(rq_id)

        # Seed known pitfalls as FailedApproaches
        for pitfall in plan.known_pitfalls:
            self.research_state.failed_approaches.append(FailedApproach(
                description=f"[Strategist] {pitfall}",
                reason="Known pitfall identified during strategic planning.",
                iteration=0,
            ))

    def _should_suggest_replan(self) -> bool:
        """Heuristic: suggest re-planning when the research is stalled."""
        if self.iteration < 5:
            return False
        plan = self.research_state.research_plan
        if plan is None:
            return False
        # Don't suggest if plan was updated recently
        if self.iteration - plan.iteration_updated <= 3:
            return False
        abandoned_count = len(self.research_state.abandoned_hypotheses())
        established_count = len(self.research_state.established_hypotheses())
        return abandoned_count >= 3 and established_count == 0

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
        """Build prefix for orchestrator context with violations, blockers, and agent failures."""
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
        if self._state.pending_explore_results:
            lines.append(">>> EXPLORE RESULTS (previous iteration) <<<")
            for r in self._state.pending_explore_results:
                lines.append(f"- {r['target_id']}: {r['description']}  [{r['confidence']}]")
                if r.get("result"):
                    lines.append(f"  Result: {r['result'][:800]}")
                lines.append("  Consider: formulate a concrete WH from this result, or integrate directly.")
            lines.append(">>> END EXPLORE RESULTS <<<\n")
            self._state.pending_explore_results.clear()
        if self._state.pending_compute_verdicts:
            lines.append(">>> COMPUTATION VERDICTS (previous iteration) <<<")
            for v in self._state.pending_compute_verdicts:
                lines.append(f"- {v['verdict']}: {v['claim'][:120]}")
                lines.append(f"  Attempt {v['attempt']}/{self.config.stall_recompute_limit}")
                if v['attempt'] >= self.config.stall_recompute_limit:
                    lines.append("  STALLED — do NOT schedule another compute_verify. Route to researcher.")
                else:
                    lines.append("  You must address this: recompute, re-derive, or accept provisionally.")
            lines.append(">>> END COMPUTATION VERDICTS <<<\n")
            self._state.pending_compute_verdicts.clear()
        if self._state.agent_failures:
            lines.append(">>> AGENT FAILURES (previous iteration) <<<")
            for f in self._state.agent_failures:
                lines.append(f"  - {f['task_id']} ({f['agent']}): {f['event']}. {f['detail']}")
            lines.append(">>> END AGENT FAILURES <<<\n")
            self._state.agent_failures.clear()
        if self._should_suggest_replan():
            lines.append(
                ">>> STRATEGIC STALL: 3+ abandoned hypotheses with 0 established results. "
                "Consider dispatching task_type: strategize to re-evaluate your approach. <<<"
            )
        return "\n".join(lines)

    def _dispatch(self, task: Task) -> tuple[str, "LLMResponse | AgentResult"]:
        """Route task to the correct agent. Returns (agent_name, result)."""
        from .llm import AgentResult, LLMResponse  # noqa: F811

        tt = task.task_type

        if tt == TaskType.RESEARCH_EXPLORE:
            console.print("[green]ResearchExplore[/green] reasoning...")
            self.research_explore.research_state = self.research_state
            result = self.research_explore.run(task, self.iteration, on_round=self._on_compute_round)
            self._state.last_content_iteration = self.iteration
            return "research_explore", result

        elif tt == TaskType.COMPUTE_VERIFY:
            console.print("[magenta]ComputeVerify[/magenta] verifying...")
            self.compute_verify.research_state = self.research_state
            result = self.compute_verify.run(task, self.iteration, on_round=self._on_compute_round)
            self._state.last_content_iteration = self.iteration
            return "compute_verify", result

        elif tt == TaskType.COMPUTE_EXPLORE:
            console.print("[magenta]ComputeExplore[/magenta] exploring...")
            self.compute_explore.research_state = self.research_state
            result = self.compute_explore.run(task, self.iteration, on_round=self._on_compute_round)
            self._state.last_content_iteration = self.iteration
            return "compute_explore", result

        elif tt == TaskType.RESEARCH_VERIFY:
            console.print("[magenta]ResearchVerify[/magenta] verifying analytically...")
            self.research_verify.research_state = self.research_state
            result = self.research_verify.run(task, self.iteration, on_round=self._on_compute_round)
            self._state.last_content_iteration = self.iteration
            return "research_verify", result

        elif tt == TaskType.FORMAT:
            console.print("[cyan]Formatter[/cyan] producing ANSWER.md...")
            self.formatter.research_state = self.research_state
            result = self.formatter.run(task, self.iteration)
            return "formatter", result

        elif tt == TaskType.CRITIQUE:
            console.print("[red]Deep Critic[/red] reviewing...")
            self.critic.research_state = self.research_state
            response = self.critic.run(task, self.iteration)
            if self.critic._no_critiques_filed:
                console.print("[dim]Critic: no issues found[/dim]")
                self._state.pending_violations.append(
                    Violation(
                        check="critic_clean",
                        severity=ViolationSeverity.WARNING,
                        message=(
                            "Deep critic found NO issues. "
                            "Do NOT emit another critique task — proceed to "
                            "terminate."
                        ),
                    )
                )
            return "deep_critic", response

        elif tt == TaskType.STRATEGIZE:
            console.print("[cyan]Strategist[/cyan] re-planning...")
            self.strategist.research_state = self.research_state
            self.strategist._system_prompt = None  # Reset cached prompt for fresh context
            result = self.strategist.run(task, self.iteration)
            self._apply_strategist_plan()
            return "strategist", result

        else:
            console.print(f"[yellow]Unknown task type '{tt}', defaulting to research_explore[/yellow]")
            self.research_explore.research_state = self.research_state
            result = self.research_explore.run(task, self.iteration, on_round=self._on_compute_round)
            return "research_explore", result

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
        """Append prior failure context to CURRENT_TASK.md for compute retries."""
        import re as _re
        from .research_state import Verdict
        target_ids = set(_re.findall(r'(?:ER|WH)-\d+', task.body or ""))
        if task.target_claim:
            target_ids.update(_re.findall(r'(?:ER|WH)-\d+', task.target_claim))
        if not target_ids:
            return

        # Find non-VERIFIED verify computations matching target from state
        prior = [
            c for c in self.research_state.computations.values()
            if c.kind == "verify"
            and c.verdict != Verdict.VERIFIED
            and c.target_hypothesis in target_ids
        ]
        if not prior:
            return

        prior_sorted = sorted(prior, key=lambda c: (c.iteration, c.id))
        log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "compute_enrichment",
                           f"target={','.join(sorted(target_ids))}")
        task_text = self.workspace.read_file("CURRENT_TASK.md")
        most_recent = prior_sorted[-1]
        excerpt_parts = []
        if most_recent.verdict:
            excerpt_parts.append(f"**Verdict:** {most_recent.verdict}")
        if most_recent.method:
            excerpt_parts.append(f"**Method:** {most_recent.method}")
        if most_recent.result:
            excerpt_parts.append(f"**Result:** {most_recent.result}")
        if most_recent.notes:
            excerpt_parts.append(f"**Notes:** {most_recent.notes}")
        excerpt = "\n".join(excerpt_parts)[:self.config.prior_failure_excerpt_chars]

        addendum = (
            "\n\n---\n\n## Prior Computation Failure Context\n\n"
            f"**{len(prior)} prior failure(s) on this claim.** "
            "Diagnose the ROOT CAUSE before writing new code.\n\n"
            "### Most Recent Failed Result\n\n"
            + excerpt
        )
        if len(prior) > 1:
            addendum += f"\n\n({len(prior) - 1} earlier failure(s))\n"
        has_zero_output = any(c.zero_output for c in prior)
        if has_zero_output:
            addendum += (
                "\n\n**ZERO-OUTPUT STALL DETECTED:** A prior attempt produced no text at all.\n"
                "1. Write a brief plan in text BEFORE calling any tools\n"
                "2. Keep computations simple — verify ONE formula at a time\n"
                "3. Write intermediate results as text between tool calls\n"
            )
        self.workspace.write_file("CURRENT_TASK.md", task_text + addendum)

    def _track_computation(self, task: Task):
        """After computationalist runs, find the new computation in state and dispatch."""
        from .research_state import Verdict
        # Find most recently added computation for this iteration
        comps = [
            c for c in self.research_state.computations.values()
            if c.iteration == self.iteration
        ]
        if not comps:
            return
        comp = sorted(comps, key=lambda c: c.id)[-1]

        if comp.kind == "explore":
            if comp.result and comp.target_hypothesis and not comp.zero_output:
                self._state.pending_explore_results.append({
                    "target_id": comp.target_hypothesis,
                    "description": comp.claim[:500],
                    "result": comp.result[:500],
                    "confidence": comp.confidence or "partial",
                })
            else:
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                                   "explore_result_suppressed",
                                   f"target={comp.target_hypothesis}, zero_output={comp.zero_output}")
        else:
            key = comp.target_hypothesis
            if not key:
                return
            if comp.verdict == Verdict.VERIFIED:
                self._state.claim_failure_count.pop(key, None)
                return
            count = self._state.claim_failure_count.get(key, 0) + 1
            self._state.claim_failure_count[key] = count
            self._state.pending_compute_verdicts.append({
                "verdict": comp.verdict.value,
                "claim": key,
                "attempt": count,
            })
            log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                               "compute_verdict_failed",
                               f"target={key}, verdict={comp.verdict}, "
                               f"attempt={count}/{self.config.stall_recompute_limit}")

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
                        task_type=TaskType.RESEARCH_EXPLORE,
                        assigned_to="compressor",
                        iteration=self.iteration,
                        target_file=filename,
                    )
                    self.compressor.run(compress_task, self.iteration)

    def _update_research_iteration(self):
        """Update the iteration field in research state."""
        self.research_state.iteration = self.iteration

    def _set_research_status(self, status: str):
        """Update the status field in research state."""
        self.research_state.status = status

    def _render_files_for_git(self):
        """Render markdown files from ResearchState for git snapshots and verify.py."""
        self.workspace.write_file("RESEARCH_STATE.md", render_research_state_md(self.research_state))
        self.workspace.write_file("COMPUTATION_LOG.md", render_computation_log_md(self.research_state))
        self.workspace.write_file("CRITIQUE_LOG.md", render_critique_log_md(self.research_state))

    def _run_formatter(self):
        """Run the formatter agent to produce ANSWER.md."""
        console.print("[cyan]Formatter[/cyan] producing ANSWER.md...")
        fmt_task = Task(
            task_id=f"FORMAT-{self.iteration:03d}",
            task_type=TaskType.FORMAT,
            assigned_to="formatter",
            iteration=self.iteration,
        )
        self.formatter.research_state = self.research_state
        self.formatter.run(fmt_task, self.iteration)
        self._render_files_for_git()
        self.workspace.git_commit(
            f"Iteration {self.iteration}: formatter - ANSWER.md"
        )

    def _force_abandon_working_hypotheses(self):
        """Circuit breaker: auto-abandon all remaining WHs after repeated termination blocks."""
        from .research_state import FailedApproach, HypothesisStatus

        working = self.research_state.working_hypotheses()
        if not working:
            return
        ids = []
        for h in working:
            h.status = HypothesisStatus.ABANDONED
            h.iteration_modified = self.iteration
            self.research_state.failed_approaches.append(FailedApproach(
                description=f"Auto-abandoned {h.id} — {h.statement or h.id}",
                reason=(
                    f"Scaffolding circuit breaker: orchestrator attempted to terminate "
                    f"{self.config.max_termination_retries} times but {h.id} blocked it. "
                    f"No verify-kind VERIFIED computation was scheduled."
                ),
                iteration=self.iteration,
            ))
            ids.append(h.id)
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.LOOP_CONTROL,
            "termination_circuit_breaker",
            f"auto-abandoned {', '.join(ids)} after "
            f"{self._state.consecutive_termination_blocks} consecutive blocks",
        )
        console.print(
            f"[yellow]Circuit breaker: auto-abandoned {', '.join(ids)} "
            f"after {self._state.consecutive_termination_blocks} consecutive "
            f"termination blocks[/yellow]"
        )
        self._state.consecutive_termination_blocks = 0

    def _check_status_field(self) -> bool:
        """Check termination conditions beyond max_iterations."""
        if self.research_state.status in ("completed", "abandoned", "partially_complete"):
            log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "status_field_exit",
                               f"status={self.research_state.status}")
            return True
        return False

    def _sync_research_state(self):
        """Save ResearchState to workspace as JSON.

        The state is now authoritative — no rebuild from markdown needed.
        """
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
        self._render_files_for_git()
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
