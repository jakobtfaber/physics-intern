"""SciRalph main loop engine."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import Config
from .llm import _is_transient
from .metrics import MetricsTracker
from .task import Task, TaskType
from .categories import CompensationCategory as CC
from .research_state import ResearchState, Verdict
from .renderers import render_research_state_md, render_evidence_log_md, render_critique_log_md
from .validation import validate_post_integration, can_terminate, Violation, ViolationSeverity
from .workspace import WorkspaceManager, log_scaffold_event
from .agents.orchestrator import OrchestratorAgent
from .agents.researcher import ResearcherAgent
from .agents.computer import ComputerAgent
from .agents.reviewer import ReviewerAgent
from .agents.critic import CriticAgent
from .agents.compressor import CompressorAgent
from .agents.formatter import FormatterAgent
from .agents.surveyor import SurveyorAgent
from .agents.planner import PlannerAgent

console = Console()


def _fmt_duration(seconds: float) -> str:
    """Format a duration as e.g. '6.3s' or '2m05s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


@dataclass
class DispatchRecord:
    """One-line record of what was dispatched in a given iteration."""
    iteration: int
    task_type: str        # "compute", "review", "critique", etc.
    target: str | None    # "WH-001", "RQ-003", or None
    outcome: str          # "evidence (exact)", "REFUTED", "3 critique(s)", etc.


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
    pending_verified_results: list[dict] = field(default_factory=list)
    pending_explore_results: list[dict] = field(default_factory=list)
    agent_failures: list[dict] = field(default_factory=list)
    pending_critic_result: dict | None = None
    # Persistent dispatch history (never cleared)
    dispatch_history: list[DispatchRecord] = field(default_factory=list)


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
        self.researcher = ResearcherAgent(self.config, self.workspace, self.metrics)
        self.computer = ComputerAgent(self.config, self.workspace, self.metrics)
        self.reviewer = ReviewerAgent(self.config, self.workspace, self.metrics)
        self.critic = CriticAgent(self.config, self.workspace, self.metrics)
        self.compressor = CompressorAgent(self.config, self.workspace, self.metrics)
        self.formatter = FormatterAgent(self.config, self.workspace, self.metrics, answer_template)
        self.surveyor = SurveyorAgent(self.config, self.workspace, self.metrics)
        self.planner = PlannerAgent(self.config, self.workspace, self.metrics)

    @classmethod
    def resume(cls, workspace_path: Path | str,
               config_overrides: dict | None = None,
               answer_template: str = "") -> "SciRalph":
        """Resume a previously interrupted run from its last committed state."""
        import yaml as _yaml

        workspace_path = Path(workspace_path)

        # 1. Load config
        config = Config.load(workspace_path, overrides=config_overrides)
        config.workspace_dir = str(workspace_path)

        # 2. Load problem.yaml from workspace
        problem_yaml_path = workspace_path / "problem.yaml"
        if not problem_yaml_path.exists():
            raise FileNotFoundError(
                f"No problem.yaml in {workspace_path} — cannot resume "
                f"(workspace predates the resume feature?)"
            )
        with open(problem_yaml_path) as f:
            problem_def = _yaml.safe_load(f)
        problem_meta = {
            "steps": problem_def.get("steps", []),
        }
        if not answer_template:
            answer_template = problem_def.get("answer_template", "")

        # 3. Build engine without calling __init__
        engine = cls.__new__(cls)
        engine.config = config
        engine.metrics = MetricsTracker()
        engine.workspace = WorkspaceManager(config)
        engine.workspace.attach()
        engine.config.logs_dir = str(engine.workspace.logs_dir)
        engine.problem_meta = problem_meta

        # 4. Load research state
        engine.research_state = ResearchState.load(workspace_path)
        engine.iteration = engine.research_state.iteration

        # 5. Reconstruct loop state
        engine._state = _reconstruct_loop_state(engine.research_state)

        # 6. Reconstruct last critic iteration
        engine.metrics.last_critic_iteration = _find_last_critic_iteration(workspace_path)

        # 7. Initialize agents (same as __init__)
        engine.orchestrator = OrchestratorAgent(config, engine.workspace, engine.metrics)
        engine.researcher = ResearcherAgent(config, engine.workspace, engine.metrics)
        engine.computer = ComputerAgent(config, engine.workspace, engine.metrics)
        engine.reviewer = ReviewerAgent(config, engine.workspace, engine.metrics)
        engine.critic = CriticAgent(config, engine.workspace, engine.metrics)
        engine.compressor = CompressorAgent(config, engine.workspace, engine.metrics)
        engine.formatter = FormatterAgent(config, engine.workspace, engine.metrics, answer_template)
        engine.surveyor = SurveyorAgent(config, engine.workspace, engine.metrics)
        engine.planner = PlannerAgent(config, engine.workspace, engine.metrics)

        console.print(Panel(
            f"Resuming from iteration {engine.iteration}",
            style="bold blue",
        ))
        return engine

    def run(self):
        """Main loop: survey → orchestrate → validate → override → dispatch → compress → git."""
        console.print(Panel("SciRalph Research System", style="bold blue"))

        # Skip surveyor if background survey already exists (e.g. on resume)
        if self.research_state.background_survey is None:
            self._run_surveyor()
        else:
            console.print("[dim]Surveyor skipped (background survey already exists)[/dim]")

        # Run planner to produce initial strategy (skip if already set, e.g. on resume)
        if not self.research_state.strategy:
            self._run_planner()
        else:
            console.print("[dim]Planner skipped (strategy already exists)[/dim]")

        if self.research_state.status == "completed":
            console.print("[yellow]This workspace is already completed.[/yellow]")
            return

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
                # Clear stale termination blockers — the forced critic addresses the critic gate
                self._state.pending_termination_blockers.clear()
            else:
                try:
                    task = self._run_orchestrator()
                except Exception as exc:
                    if not _is_transient(exc):
                        raise
                    self.metrics.alert(
                        self.iteration,
                        f"Orchestrator failed (transient): {type(exc).__name__}: {exc}",
                    )
                    self._state.pending_violations.append(
                        Violation(
                            check="orchestrator_failure",
                            severity=ViolationSeverity.WARNING,
                            message=(
                                f"Orchestrator failed with transient error: "
                                f"{type(exc).__name__}: {str(exc)[:200]}"
                            ),
                        )
                    )
                    console.print(
                        f"[yellow]Orchestrator failed (transient), skipping to "
                        f"next iteration: {exc}[/yellow]"
                    )
                    log_scaffold_event(
                        self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                        "orchestrator_failure",
                        f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                    continue

            # 2. Post-integration validation
            violations = validate_post_integration(
                self.research_state,
                iteration=self.iteration,
                workspace=self.workspace,
            )
            if violations:
                self._state.pending_violations.extend(violations)

            # 3. Enrichment for compute tasks
            if task.task_type == TaskType.COMPUTE:
                self._enrich_compute_task_with_prior_failures(task)

            # 4. Termination gate
            if task.task_type == TaskType.TERMINATE:
                allowed, blockers = can_terminate(
                    self.workspace, self.config, self.metrics, self.problem_meta,
                    research_state=self.research_state)
                if allowed:
                    console.print("[green]Orchestrator signaled completion.[/green]")
                    rejection = self._run_formatter()
                    if rejection is None:
                        self._set_research_status("completed")
                        self._render_files_for_git()
                        self._sync_research_state()
                        break
                    # Formatter rejected — loop back to orchestrator
                    self._state.consecutive_termination_blocks += 1
                    blockers = [
                        f"FORMATTER REJECTED the answer: {rejection}. "
                        "The established results do not contain concrete "
                        "values matching the answer template. Dispatch "
                        "research or compute tasks to derive the missing "
                        "concrete results before terminating again."
                    ]
                    log_scaffold_event(
                        self.workspace.root, self.iteration,
                        CC.LOOP_CONTROL, "formatter_rejection_loopback",
                        f"consecutive={self._state.consecutive_termination_blocks}, "
                        f"reason={rejection[:200]}",
                    )
                    if self._state.consecutive_termination_blocks >= self.config.max_termination_retries:
                        console.print(
                            "[yellow]Circuit breaker: accepting best-effort "
                            "formatter output despite rejection[/yellow]"
                        )
                        self._render_files_for_git()
                        self.workspace.git_commit(
                            f"Iteration {self.iteration}: formatter - "
                            "ANSWER.md (best-effort, rejected)"
                        )
                        self._set_research_status("partially_complete")
                        self._render_files_for_git()
                        self._sync_research_state()
                        break
                else:
                    self._state.consecutive_termination_blocks += 1
                    blockers = list(blockers)
                self._state.pending_termination_blockers = blockers
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "termination_blocked",
                                   f"blockers: {'; '.join(b[:60] for b in blockers)}, "
                                   f"consecutive={self._state.consecutive_termination_blocks}")
                # Circuit breaker: after repeated blocks, auto-abandon remaining WHs
                if self._state.consecutive_termination_blocks >= self.config.max_termination_retries:
                    self._force_abandon_working_hypotheses()
                self._append_dispatch_record(task)
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
            if task.task_type in (TaskType.RESEARCH, TaskType.COMPUTE, TaskType.REVIEW):
                self._track_agent_result(task)
            self._append_dispatch_record(task)

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
        self.orchestrator.context_suffix = self._build_context_suffix()
        # Pass research state reference for tool executor and context rendering
        self.orchestrator.research_state = self.research_state

        orch_task = Task(
            task_id="", task_type=TaskType.RESEARCH,
            assigned_to="orchestrator", iteration=self.iteration,
        )
        orch_response = self.orchestrator.run(orch_task, self.iteration, on_round=self._on_agent_round)
        task = self.orchestrator.parse_task(orch_response.text, iteration=self.iteration)
        self._print_task(task)
        return task

    def _run_surveyor(self):
        """Run surveyor agent before the main loop to produce a background survey."""
        console.print("[cyan]Surveyor[/cyan] analyzing problem...")
        self.surveyor.research_state = self.research_state
        task = Task(
            task_id="SURVEY-000", task_type=TaskType.SURVEY,
            assigned_to="surveyor", iteration=0,
        )
        result = self.surveyor.run(task, 0)
        self._print_call_summary(result)
        self._apply_survey()
        self._sync_research_state()
        self._render_files_for_git()
        self.workspace.git_commit("Iteration 0: surveyor — background survey")

    def _apply_survey(self):
        """Store the surveyor's parsed survey in research state."""
        survey = self.surveyor.parsed_survey
        if survey is None:
            return

        self.research_state.background_survey = survey
        # Seed conventions from surveyor's conventions section
        if survey.conventions_and_definitions and not self.research_state.conventions:
            self.research_state.conventions = survey.conventions_and_definitions.strip()

    def _run_planner(self):
        """Run planner agent to produce an initial research strategy."""
        console.print("[cyan]Planner[/cyan] formulating strategy...")
        self.planner.research_state = self.research_state
        task = Task(
            task_id="PLAN-000", task_type=TaskType.PLAN,
            assigned_to="planner", iteration=0,
        )
        result = self.planner.run(task, 0)
        self._print_call_summary(result)
        self._apply_strategy()
        self._sync_research_state()
        self._render_files_for_git()
        self.workspace.git_commit("Iteration 0: planner — research strategy")

    def _apply_strategy(self):
        """Store the planner's parsed strategy in research state."""
        strategy = self.planner.parsed_strategy
        if strategy is None:
            return

        self.research_state.strategy = strategy

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

    def _append_dispatch_record(self, task: Task):
        """Derive outcome from authoritative state and append a DispatchRecord."""
        tt = task.task_type
        target = task.target_claim or None

        if tt in (TaskType.RESEARCH, TaskType.COMPUTE):
            ev = None
            if target and target in self.research_state.research_questions:
                evs = self.research_state.research_questions[target].evidence
                ev = evs[-1] if evs else None
            elif target and target in self.research_state.hypotheses:
                evs = self.research_state.hypotheses[target].evidence
                ev = evs[-1] if evs else None
            if ev and ev.result:
                outcome = f"evidence ({ev.confidence})" if ev.confidence else "evidence"
            else:
                outcome = "no evidence"

        elif tt == TaskType.REVIEW:
            if target and target in self.research_state.hypotheses:
                h = self.research_state.hypotheses[target]
                if h.review:
                    outcome = h.review.verdict
                else:
                    outcome = "no review produced"
            else:
                outcome = "no review produced"

        elif tt == TaskType.CRITIQUE:
            cr = self._state.pending_critic_result
            if cr is None:
                outcome = "no critiques"
            elif cr.get("clean"):
                outcome = "no critiques"
            else:
                outcome = f"{cr['count']} critique(s)"

        elif tt == TaskType.TERMINATE:
            outcome = "blocked"

        else:
            outcome = "completed"

        self._state.dispatch_history.append(DispatchRecord(
            iteration=self.iteration,
            task_type=tt.value,
            target=target,
            outcome=outcome,
        ))

    def _build_context_suffix(self) -> str:
        """Build suffix for orchestrator context with violations, blockers, and agent failures."""
        lines = []
        if self._state.dispatch_history:
            lines.append("<tasks_dispatch_history>")
            for rec in self._state.dispatch_history:
                target_str = f" → {rec.target}" if rec.target else ""
                lines.append(f"Iter {rec.iteration}: {rec.task_type}{target_str} | {rec.outcome}")
            lines.append("</tasks_dispatch_history>\n")
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
            lines.append("")
            lines.append("Pre-dispatch checklist (verify before retrying termination):")
            lines.append("1. Every FILL IN placeholder in the answer template has a concrete ER.")
            lines.append("2. ER expressions are explicit closed-form SymPy (no abstract operators or opaque functions).")
            lines.append("3. MCQ answers are a concrete letter from the given set, not prose.")
            lines.append("4. Return types match the template (tuple elements, etc.).")
            lines.append(">>> END TERMINATION BLOCKERS <<<\n")
            self._state.pending_termination_blockers.clear()
        if self._state.pending_explore_results:
            lines.append(">>> EVIDENCE RESULTS (previous iteration) <<<")
            for r in self._state.pending_explore_results:
                provenance = f"  [from {r['task_id']}: {r['task_type']} on {r['target_id']}]"
                lines.append(f"- {r['target_id']}: {r['description']}  [{r['confidence']}]{provenance}")
                if r.get("result"):
                    lines.append(f"  Result: {r['result'][:800]}")
                _is_failure = r.get("result", "").startswith(("Agent produced no exit tool call", "Failed to parse structured"))
                if _is_failure:
                    lines.append("  NOTE: This evidence is from a failed agent run — do NOT treat it as usable evidence.")
            lines.append(">>> END EVIDENCE RESULTS <<<\n")
            self._state.pending_explore_results.clear()
        if self._state.pending_verified_results:
            lines.append(">>> VERIFIED HYPOTHESES (previous iteration) <<<")
            for v in self._state.pending_verified_results:
                provenance = f"  [from {v['task_id']}]" if v.get("task_id") else ""
                lines.append(f"- {v['claim']} VERIFIED by reviewer{provenance}")
                if v.get("reasoning"):
                    lines.append(f"  Reasoning: {v['reasoning']}")
            lines.append(">>> END VERIFIED HYPOTHESES <<<\n")
            self._state.pending_verified_results.clear()
        if self._state.pending_compute_verdicts:
            lines.append(">>> VERIFICATION RESULTS (previous iteration) <<<")
            for v in self._state.pending_compute_verdicts:
                provenance = f"  [from {v['task_id']}]" if v.get("task_id") else ""
                lines.append(f"- {v['verdict']}: {v['claim'][:120]}{provenance}")
                lines.append(f"  Attempt {v['attempt']}/{self.config.stall_recompute_limit}")
                if v.get('notes'):
                    lines.append(f"  Notes: {v['notes']}")
                if v.get('details'):
                    lines.append(f"  Details: {v['details']}")
                if v['attempt'] >= self.config.stall_recompute_limit:
                    lines.append("  STALLED — do NOT schedule another review. Try alternative evidence.")
            lines.append(">>> END VERIFICATION RESULTS <<<\n")
            self._state.pending_compute_verdicts.clear()
        if self._state.pending_critic_result is not None:
            cr = self._state.pending_critic_result
            if cr.get("clean"):
                lines.append(">>> DEEP CRITIC RESULT (previous iteration) <<<")
                lines.append("The deep critic reviewed the research and found NO issues.")
                if cr.get("can_terminate"):
                    lines.append(
                        "You previously attempted to terminate — you may now retry "
                        "task_type: terminate."
                    )
                lines.append(">>> END DEEP CRITIC RESULT <<<\n")
            else:
                lines.append(">>> DEEP CRITIC RESULT (previous iteration) <<<")
                lines.append(
                    f"The deep critic filed {cr['count']} critique(s). "
                    "See the critique log in your context for details."
                )
                lines.append(">>> END DEEP CRITIC RESULT <<<\n")
            self._state.pending_critic_result = None
        if self._state.agent_failures:
            lines.append(">>> AGENT FAILURES (previous iteration) <<<")
            for f in self._state.agent_failures:
                lines.append(f"  - [{f['task_id']}] {f['agent']}: {f['event']}. {f['detail']}")
            lines.append(">>> END AGENT FAILURES <<<\n")
            self._state.agent_failures.clear()
        # Pending work summary — always present so the orchestrator sees current state
        pending = self._render_pending_work()
        if pending:
            lines.append(pending)
        return "\n".join(lines)

    def _render_pending_work(self) -> str:
        """Render a summary of open RQs, non-reviewed WHs, and unresolved critiques."""
        from .research_state import CritiqueStatus

        state = self.research_state
        if not state:
            return ""

        lines: list[str] = []
        whs = state.working_hypotheses()
        if whs:
            wh_items = []
            for h in whs:
                if h.evidence:
                    status = h.review.verdict.upper() if h.review else f"has {len(h.evidence)} evidence, PENDING REVIEW"
                else:
                    status = "no evidence"
                wh_items.append(f"{h.id} ({status})")
            lines.append(f"  WH: {', '.join(wh_items)}")

        open_rqs = state.open_research_questions()
        if open_rqs:
            rq_items = []
            for rq in open_rqs:
                ev = f"{len(rq.evidence)} evidence" if rq.evidence else "no evidence"
                rq_items.append(f"{rq.id} ({ev})")
            lines.append(f"  Open RQs: {', '.join(rq_items)}")

        unresolved = [c for c in state.critiques.values() if c.status == CritiqueStatus.ACTIVE]
        if unresolved:
            lines.append(f"  Unresolved critiques: {', '.join(c.id for c in unresolved)}")

        if not lines:
            return ""
        return ">>> PENDING WORK <<<\n" + "\n".join(lines) + "\n>>> END PENDING WORK <<<\n"

    def _dispatch(self, task: Task) -> tuple[str, "LLMResponse | AgentResult"]:
        """Route task to the correct agent. Returns (agent_name, result)."""
        from .llm import AgentResult, LLMResponse  # noqa: F811

        tt = task.task_type

        if tt == TaskType.RESEARCH:
            console.print("[green]Researcher[/green] reasoning...")
            self.researcher.research_state = self.research_state
            result = self.researcher.run(task, self.iteration)
            self._state.last_content_iteration = self.iteration
            return "researcher", result

        elif tt == TaskType.COMPUTE:
            console.print("[magenta]Computer[/magenta] computing...")
            self.computer.research_state = self.research_state
            result = self.computer.run(task, self.iteration, on_round=self._on_compute_round)
            self._state.last_content_iteration = self.iteration
            return "computer", result

        elif tt == TaskType.REVIEW:
            console.print("[magenta]Reviewer[/magenta] reviewing...")
            self.reviewer.research_state = self.research_state
            result = self.reviewer.run(task, self.iteration, on_round=self._on_agent_round)
            self._state.last_content_iteration = self.iteration
            return "reviewer", result

        elif tt == TaskType.FORMAT:
            console.print("[cyan]Formatter[/cyan] producing ANSWER.md...")
            self.formatter.research_state = self.research_state
            result = self.formatter.run(task, self.iteration)
            self._print_call_summary(result)
            return "formatter", result

        elif tt == TaskType.CRITIQUE:
            console.print("[red]Deep Critic[/red] reviewing...")
            self.critic.research_state = self.research_state
            response = self.critic.run(task, self.iteration, on_round=self._on_agent_round)
            if self.critic._no_critiques_filed:
                console.print("[dim]Critic: no issues found[/dim]")
                result_info: dict = {"clean": True}
                if self._state.consecutive_termination_blocks > 0:
                    result_info["can_terminate"] = True
                self._state.pending_critic_result = result_info
            else:
                crits = list(self.research_state.critiques.values())
                recent = [c for c in crits if c.iteration_filed == self.iteration]
                if recent:
                    console.print(
                        f"[red]Critic filed {len(recent)} critique(s)[/red]"
                    )
                    self._state.pending_critic_result = {
                        "clean": False,
                        "count": len(recent),
                    }
            return "deep_critic", response

        else:
            console.print(f"[yellow]Unknown task type '{tt}', defaulting to researcher[/yellow]")
            self.researcher.research_state = self.research_state
            result = self.researcher.run(task, self.iteration)
            return "researcher", result

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
        target_ids = set(_re.findall(r'(?:ER|WH)-\d+', task.body or ""))
        if task.target_claim:
            target_ids.update(_re.findall(r'(?:ER|WH)-\d+', task.target_claim))
        if not target_ids:
            return

        # Check claim failure count for these targets
        failure_count = sum(
            self._state.claim_failure_count.get(tid, 0)
            for tid in target_ids
        )
        if not failure_count:
            return

        log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "compute_enrichment",
                           f"target={','.join(sorted(target_ids))}, failures={failure_count}")
        task_text = self.workspace.read_file("CURRENT_TASK.md")
        addendum = (
            "\n\n---\n\n## Prior Failure Context\n\n"
            f"**{failure_count} prior failure(s) on this claim.** "
            "Diagnose the ROOT CAUSE before writing new code.\n"
        )
        self.workspace.write_file("CURRENT_TASK.md", task_text + addendum)

    def _track_agent_result(self, task: Task):
        """After researcher/computer/reviewer runs, track results for orchestrator banners."""
        tt = task.task_type
        target_id = task.target_claim

        if tt in (TaskType.RESEARCH, TaskType.COMPUTE):
            # Find evidence on the target entity
            ev = None
            if target_id in self.research_state.research_questions:
                evs = self.research_state.research_questions[target_id].evidence
                ev = evs[-1] if evs else None
            elif target_id in self.research_state.hypotheses:
                evs = self.research_state.hypotheses[target_id].evidence
                ev = evs[-1] if evs else None
            elif target_id in self.research_state.critiques:
                evs = self.research_state.critiques[target_id].evidence
                ev = evs[-1] if evs else None

            if ev and ev.result:
                description = ev.summary or (ev.method[:500] if ev.method else "unknown")
                self._state.pending_explore_results.append({
                    "target_id": target_id,
                    "description": description,
                    "result": ev.result[:500],
                    "confidence": ev.confidence or "partial",
                    "task_id": task.task_id,
                    "task_type": task.task_type.value,
                })
                snippet = (ev.summary or ev.result[:120]).replace("\n", " ")
                conf = f", {ev.confidence}" if ev.confidence else ""
                console.print(f"  [blue]Evidence for {target_id}[/blue]{conf}: {snippet}")
            else:
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                                   "evidence_suppressed", f"target={target_id}")
                console.print(f"  [dim]No evidence produced for {target_id}[/dim]")

        elif tt == TaskType.REVIEW:
            if not target_id or target_id not in self.research_state.hypotheses:
                return
            h = self.research_state.hypotheses[target_id]
            if not h.review:
                return
            verdict = h.review.verdict
            if verdict == Verdict.VERIFIED:
                self._state.pending_verified_results.append({
                    "claim": target_id,
                    "verdict": verdict,
                    "task_id": task.task_id,
                    "reasoning": h.review.summary or "",
                })
                self._state.claim_failure_count.pop(target_id, None)
                console.print(f"  [green]{target_id} VERIFIED[/green]")
            elif verdict == Verdict.REFUTED:
                count = self._state.claim_failure_count.get(target_id, 0) + 1
                self._state.claim_failure_count[target_id] = count
                self._state.pending_compute_verdicts.append({
                    "verdict": verdict,
                    "claim": target_id,
                    "attempt": count,
                    "notes": h.review.summary or "",
                    "details": h.review.details or "",
                    "task_id": task.task_id,
                })
                detail = h.review.summary[:120].replace("\n", " ") if h.review.summary else ""
                console.print(f"  [red]{target_id} REFUTED[/red] — {detail}")
            else:
                count = self._state.claim_failure_count.get(target_id, 0) + 1
                self._state.claim_failure_count[target_id] = count
                self._state.pending_compute_verdicts.append({
                    "verdict": verdict,
                    "claim": target_id,
                    "attempt": count,
                    "notes": h.review.summary or "",
                    "details": h.review.details or "",
                    "task_id": task.task_id,
                })
                detail = h.review.summary[:120].replace("\n", " ") if h.review.summary else ""
                console.print(f"  [yellow]{target_id} INCONCLUSIVE[/yellow] — {detail}")

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
        """Update the iteration field in research state."""
        self.research_state.iteration = self.iteration

    def _set_research_status(self, status: str):
        """Update the status field in research state."""
        self.research_state.status = status

    def _render_files_for_git(self):
        """Render markdown files from ResearchState for git snapshots and verify.py."""
        self.workspace.write_file("RESEARCH_STATE.md", render_research_state_md(self.research_state))
        self.workspace.write_file("EVIDENCE_LOG.md", render_evidence_log_md(self.research_state))
        self.workspace.write_file("CRITIQUE_LOG.md", render_critique_log_md(self.research_state))

    def _run_formatter(self) -> str | None:
        """Run the formatter agent to produce ANSWER.md.

        Returns ``None`` on success, or a rejection reason string if the
        formatter could not produce a valid answer matching the template.
        """
        console.print("[cyan]Formatter[/cyan] producing ANSWER.md...")
        fmt_task = Task(
            task_id=f"FORMAT-{self.iteration:03d}",
            task_type=TaskType.FORMAT,
            assigned_to="formatter",
            iteration=self.iteration,
        )
        self.formatter.research_state = self.research_state
        result = self.formatter.run(fmt_task, self.iteration)
        self._print_call_summary(result)

        rejection = self.formatter.rejection_reason
        if rejection:
            console.print(f"[yellow]Formatter rejected: {rejection}[/yellow]")
            log_scaffold_event(
                self.workspace.root, self.iteration,
                CC.LOOP_CONTROL, "formatter_rejection", rejection[:200],
            )
            return rejection

        self._render_files_for_git()
        self.workspace.git_commit(
            f"Iteration {self.iteration}: formatter - ANSWER.md"
        )
        return None

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
                    f"No review-kind VERIFIED result was obtained."
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
        if task.target_claim:
            text.append(f"{task.target_claim} ", style="bold magenta")
        text.append(f"-> {task.assigned_to}", style="green")
        console.print(text)

    def _on_compute_round(self, round_num, stop_reason, tool_calls,
                          total_input, total_output,
                          round_input, round_output, round_duration):
        """Progress callback for agent tool-use rounds."""
        tokens = f"{round_input:,}in + {round_output:,}out"
        dur = _fmt_duration(round_duration)
        if stop_reason == "forced_partial":
            console.print(f"  round {round_num}: forced final call ({tokens}, {dur})", style="dim magenta")
            return
        n_tools = len(tool_calls)
        errors = sum(1 for tc in tool_calls if tc.is_error)
        if errors:
            status = f"{n_tools} tool call{'s' if n_tools != 1 else ''}, {errors} error{'s' if errors != 1 else ''}"
        else:
            status = f"{n_tools} tool call{'s' if n_tools != 1 else ''}"
        console.print(f"  round {round_num}: {status} ({tokens}, {dur})", style="dim magenta")

    # Alias so orchestrator/critic use the same callback
    _on_agent_round = _on_compute_round

    def _print_call_summary(self, result):
        """Print a one-line timing/token summary for one-shot LLM calls."""
        from .llm import LLMResponse, AgentResult
        if isinstance(result, AgentResult):
            tokens = f"{result.total_input_tokens:,}in + {result.total_output_tokens:,}out"
        elif isinstance(result, LLMResponse):
            tokens = f"{result.input_tokens:,}in + {result.output_tokens:,}out"
        else:
            return
        dur = _fmt_duration(result.duration)
        console.print(f"  ({tokens}, {dur})", style="dim")

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


# ---------------------------------------------------------------------------
# Resume helpers (module-level, pure functions)
# ---------------------------------------------------------------------------

def _reconstruct_loop_state(research_state: ResearchState) -> LoopState:
    """Rebuild LoopState from a loaded ResearchState.

    Only reconstructs durable fields — consumed-once banners are always
    empty between iterations, so they default to empty.
    """
    state = LoopState()

    # claim_failure_count: hypotheses with non-VERIFIED review that are still WORKING
    for h in research_state.hypotheses.values():
        if (h.review
                and h.review.verdict in (Verdict.REFUTED, "INCONCLUSIVE")
                and h.status == "working"):
            state.claim_failure_count[h.id] = 1

    # last_content_iteration: max iteration from evidence/review across entities
    max_iter = 0
    for h in research_state.hypotheses.values():
        for ev in h.evidence:
            if ev.iteration is not None:
                max_iter = max(max_iter, ev.iteration)
        if h.review and h.review.iteration is not None:
            max_iter = max(max_iter, h.review.iteration)
    for rq in research_state.research_questions.values():
        for ev in rq.evidence:
            if ev.iteration is not None:
                max_iter = max(max_iter, ev.iteration)
    state.last_content_iteration = max_iter

    return state


def _find_last_critic_iteration(workspace_path: Path | str) -> int:
    """Parse EVENT_LOG.jsonl for the last deep_critic LLM call iteration."""
    log_path = Path(workspace_path) / "EVENT_LOG.jsonl"
    if not log_path.exists():
        return 0
    max_iter = 0
    try:
        for line in log_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("kind") == "llm_call" and entry.get("agent") == "deep_critic":
                max_iter = max(max_iter, entry.get("iter", 0))
    except OSError:
        return 0
    return max_iter
