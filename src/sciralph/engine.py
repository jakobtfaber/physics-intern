"""SciRalph main loop engine."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import Config
from .llm import _is_transient, ContextTooLongError
from .metrics import MetricsTracker
from .task import Task, TaskType
from .utils.categories import CompensationCategory as CC
from .research_state import ResearchState, Verdict
from .rendering import render_research_state_md, render_evidence_log_md, render_critique_log_md
from .validation import validate_post_integration, can_terminate, Violation, ViolationSeverity
from .workspace import WorkspaceManager, log_scaffold_event
from .agents.orchestrator import OrchestratorAgent
from .agents.researcher import ResearcherAgent
from .agents.computer import ComputerAgent
from .agents.reviewer import ReviewerAgent
from .agents.critic import CriticAgent
from .agents.formatter import FormatterAgent
from .agents.surveyor import SurveyorAgent
from .agents.planner import PlannerAgent
from .agents.adjudicator import AdjudicatorAgent
from .verification.verify import (
    run_formal_evaluation, render_formal_evaluation,
    write_formal_eval_report, load_reference_file,
)

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
    last_verified_review_iteration: int = 0
    pending_system_events: list[str] = field(default_factory=list)
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
        self.workspace.init(problem)
        self.config.logs_dir = str(self.workspace.logs_dir)
        self.iteration = 0
        self._state = LoopState()
        self.research_state = ResearchState()
        self.research_state.problem_statement = problem.strip()
        self.research_state.answer_template = answer_template.strip() if answer_template else ""
        self.research_state.title = self.workspace.root.name
        self.problem_meta = problem_meta or {}

        # Initialize agents
        self.orchestrator = OrchestratorAgent(self.config, self.workspace, self.metrics)
        self.researcher = ResearcherAgent(self.config, self.workspace, self.metrics)
        self.computer = ComputerAgent(self.config, self.workspace, self.metrics)
        self.reviewer = ReviewerAgent(self.config, self.workspace, self.metrics)
        self.critic = CriticAgent(self.config, self.workspace, self.metrics)
        self.formatter = FormatterAgent(self.config, self.workspace, self.metrics, answer_template)
        self.surveyor = SurveyorAgent(self.config, self.workspace, self.metrics)
        self.planner = PlannerAgent(self.config, self.workspace, self.metrics)
        self.adjudicator = AdjudicatorAgent(self.config, self.workspace, self.metrics)

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
        # Ensure answer_template is populated (backward compat: old states lack this field)
        if not engine.research_state.answer_template and answer_template:
            engine.research_state.answer_template = answer_template.strip()

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
        engine.formatter = FormatterAgent(config, engine.workspace, engine.metrics, answer_template)
        engine.surveyor = SurveyorAgent(config, engine.workspace, engine.metrics)
        engine.planner = PlannerAgent(config, engine.workspace, engine.metrics)
        engine.adjudicator = AdjudicatorAgent(config, engine.workspace, engine.metrics)

        console.print(Panel(
            f"Resuming from iteration {engine.iteration}",
            style="bold blue",
        ))
        return engine

    def run(self):
        """Main loop: survey → orchestrate → validate → override → dispatch → git."""
        console.print(Panel("SciRalph Research System", style="bold blue"))

        # Skip surveyor if survey fields already populated (e.g. on resume)
        if not self.research_state.survey_background:
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
            self._auto_expire_critiques()

            # 1. Orchestrator pass
            try:
                task = self._run_orchestrator()
            except ContextTooLongError as exc:
                console.print(
                    f"[yellow]Orchestrator context too long — skipping to "
                    f"next iteration: {exc}[/yellow]"
                )
                self.metrics.alert(
                    self.iteration,
                    f"Orchestrator context too long: {exc.input_tokens} input tokens",
                )
                log_scaffold_event(
                    self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                    "context_too_long",
                    f"agent=orchestrator, input_tokens={exc.input_tokens}",
                )
                continue
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
                    rejection = self._run_formatter(answer_ers=task.answer_ers)
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
            except ContextTooLongError as exc:
                self._handle_context_too_long(task, exc)
                continue
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                self._handle_dispatch_error(task, exc)
                continue

            # 5b. Record agent failure signals for orchestrator context
            self._record_agent_failures(task, agent_name, agent_result)

            # 6. Post-dispatch checks
            if task.task_type in (TaskType.RESEARCH, TaskType.COMPUTE, TaskType.REVIEW):
                self._track_agent_result(task)
            self._append_dispatch_record(task)

            # 6b. Auto-review for WHs with new evidence after stale review
            auto_review_target = self._should_auto_review(task)
            if auto_review_target:
                console.print(
                    f"[yellow]Auto-dispatching reviewer for {auto_review_target} "
                    f"(new evidence after stale review)[/yellow]"
                )
                log_scaffold_event(
                    self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                    "auto_review", f"target={auto_review_target}, trigger=new_evidence",
                )
                review_task = self._make_auto_review_task(auto_review_target)
                try:
                    agent_name_r, result_r = self._dispatch(review_task)
                    self._record_agent_failures(review_task, agent_name_r, result_r)
                    self._track_agent_result(review_task)
                    self._append_dispatch_record(review_task)
                except ContextTooLongError as exc:
                    self._handle_context_too_long(review_task, exc)
                except Exception as exc:
                    if not _is_transient(exc):
                        raise
                    self._handle_dispatch_error(review_task, exc)

            # 6c. Auto-trigger critic after VERIFIED review
            if self._should_trigger_critic():
                console.print(
                    f"[yellow]Auto-triggering critic after VERIFIED review "
                    f"(last critic at iter {self.metrics.last_critic_iteration})[/yellow]"
                )
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                                   "forced_critic",
                                   f"trigger=verified_review, "
                                   f"last_critic={self.metrics.last_critic_iteration}")
                critic_task = self._make_forced_critic_task()
                try:
                    agent_name_c, result_c = self._dispatch(critic_task)
                    self._record_agent_failures(critic_task, agent_name_c, result_c)
                    self._append_dispatch_record(critic_task)
                except ContextTooLongError as exc:
                    self._handle_context_too_long(critic_task, exc)
                except Exception as exc:
                    if not _is_transient(exc):
                        raise
                    self._handle_dispatch_error(critic_task, exc)

            # 6d. Periodic critic safeguard (no verified review required)
            elif self._should_force_periodic_critic():
                console.print(
                    f"[yellow]Forcing periodic critic — no critic in "
                    f"{self.iteration - self.metrics.last_critic_iteration} iterations "
                    f"(safeguard: critic_every_n={self.config.critic_every_n})[/yellow]"
                )
                log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                                   "forced_critic",
                                   f"trigger=periodic_safeguard, "
                                   f"last_critic={self.metrics.last_critic_iteration}")
                critic_task = self._make_forced_critic_task()
                try:
                    agent_name_c, result_c = self._dispatch(critic_task)
                    self._record_agent_failures(critic_task, agent_name_c, result_c)
                    self._append_dispatch_record(critic_task)
                except ContextTooLongError as exc:
                    self._handle_context_too_long(critic_task, exc)
                except Exception as exc:
                    if not _is_transient(exc):
                        raise
                    self._handle_dispatch_error(critic_task, exc)

            # 6e. Route critic findings to specialist agents
            self._route_critiques()

            # 7. Metrics, structured state snapshot, render files, git
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

        # Best-effort formatter when loop exhausted without completion
        if (self.iteration > 0
                and self.research_state.status not in ("completed", "partially_complete", "abandoned")):
            console.print(
                "[yellow]Max iterations reached without completion — "
                "attempting best-effort formatter.[/yellow]"
            )
            self._set_research_status("partially_complete")
            self.formatter.best_effort = True
            try:
                rejection = self._run_formatter(answer_ers=None)
                if rejection:
                    console.print(
                        f"[yellow]Best-effort formatter rejected: {rejection}[/yellow]"
                    )
            except Exception as exc:
                console.print(
                    f"[yellow]Best-effort formatter failed: "
                    f"{type(exc).__name__}: {exc}[/yellow]"
                )
            finally:
                self.formatter.best_effort = False
            self._render_files_for_git()
            self._sync_research_state()
            self.workspace.git_commit(
                f"Iteration {self.iteration}: best-effort formatter "
                "(max iterations reached)"
            )

        self._final_report()
        self._run_formal_verification()

    def _run_formal_verification(self):
        """Run formal (symbolic/numerical) answer evaluation at end of run."""
        import yaml

        problem_path = self.workspace.root / "problem.yaml"
        if not problem_path.exists():
            console.print("[dim]Formal verification skipped: no problem.yaml in workspace[/]")
            return

        try:
            with open(problem_path) as f:
                problem_def = yaml.safe_load(f)
        except Exception as exc:
            console.print(f"[yellow]Formal verification skipped: could not read problem.yaml: {exc}[/]")
            return

        # Build reference lookup path from problem name
        problem_name = problem_def.get("name") if problem_def else None
        ref_lookup_path = Path(problem_name + ".yaml") if problem_name else None

        console.print(f"\n[bold]Formal answer evaluation...[/]")

        try:
            result = run_formal_evaluation(
                str(self.workspace.root), problem_def, problem_path=ref_lookup_path,
            )
            render_formal_evaluation(result)
            write_formal_eval_report(result, str(self.workspace.root))
            self.workspace.git_commit("Formal answer evaluation")
        except Exception as exc:
            console.print(f"[yellow]Formal verification failed: {type(exc).__name__}: {exc}[/]")

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
        """Distribute the surveyor's parsed survey into research state fields."""
        survey = self.surveyor.parsed_survey
        if not survey:
            return

        # §1 (background) and §2 (key insights) stored as separate fields
        if survey.get("background"):
            self.research_state.survey_background = survey["background"]
        elif survey.get("raw_notes"):
            # Fallback: no structured sections, use raw text
            self.research_state.survey_background = survey["raw_notes"]
        if survey.get("key_insights"):
            self.research_state.key_insights = survey["key_insights"]

        if survey.get("known_methods"):
            self.research_state.survey_methods = survey["known_methods"]
        if survey.get("known_pitfalls"):
            self.research_state.known_pitfalls = survey["known_pitfalls"]
        if survey.get("conventions_and_definitions") and not self.research_state.conventions:
            self.research_state.conventions = survey["conventions_and_definitions"].strip()
        if survey.get("sanity_checks") and not self.research_state.sanity_checks:
            self.research_state.sanity_checks = list(survey["sanity_checks"])
        if survey.get("problem_summary"):
            self.research_state.problem_summary = survey["problem_summary"].strip()

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

    def _handle_context_too_long(self, task: Task, exc: ContextTooLongError) -> None:
        """Log context-too-long error and record for orchestrator — do not crash."""
        self.metrics.alert(
            self.iteration,
            f"Context too long for {task.task_id}: {exc.input_tokens} input tokens "
            f"(model limit {exc.max_context})",
        )
        self._state.agent_failures.append({
            "task_id": task.task_id, "agent": task.task_type.value,
            "event": "context_too_long",
            "detail": (
                f"Context exceeded model limit (~{exc.input_tokens} input tokens, "
                f"limit {exc.max_context}). Simplify or decompose the task."
            ),
            "iteration": self.iteration,
        })
        console.print(
            f"[yellow]Context too long for {task.task_id} — skipping "
            f"(~{exc.input_tokens} input tokens, limit {exc.max_context})[/yellow]"
        )
        log_scaffold_event(
            self.workspace.root, self.iteration, CC.LOOP_CONTROL,
            "context_too_long",
            f"task={task.task_id}, input_tokens={exc.input_tokens}, "
            f"max_context={exc.max_context}",
        )

    def _handle_dispatch_error(self, task: Task, exc: Exception) -> None:
        """Handle transient dispatch errors — log and skip to next iteration."""
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
            f"[yellow]Dispatch failed (transient), skipping: {exc}[/yellow]"
        )
        log_scaffold_event(self.workspace.root, self.iteration, CC.LOOP_CONTROL, "dispatch_failure",
                           f"{type(exc).__name__}: {str(exc)[:200]}")

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
            elif target and target in self.research_state.critiques:
                evs = self.research_state.critiques[target].evidence
                ev = evs[-1] if evs else None
            if ev and ev.result:
                outcome = f"evidence ({ev.confidence})" if ev.confidence else "evidence"
            else:
                outcome = "no evidence"

        elif tt == TaskType.REVIEW:
            h = None
            if target and target in self.research_state.hypotheses:
                h = self.research_state.hypotheses[target]
            elif target and target.startswith("WH-"):
                er_id = f"ER-{target.split('-')[1]}"
                if er_id in self.research_state.hypotheses:
                    h = self.research_state.hypotheses[er_id]
            if h and h.review:
                outcome = f"{h.review.verdict} → {h.id}"
            else:
                outcome = "no review produced"

        elif tt == TaskType.CRITIQUE:
            recent = [
                c for c in self.research_state.critiques.values()
                if c.iteration_filed == self.iteration
            ]
            if recent:
                outcome = f"{len(recent)} critique(s)"
            else:
                outcome = "no critiques"

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
        # Dispatch history goes into research-state via dispatch_history_text
        if self._state.dispatch_history:
            cutoff = max(self.iteration - 4, 0)
            recent = [r for r in self._state.dispatch_history if r.iteration >= cutoff]
            omitted = len(self._state.dispatch_history) - len(recent)
            dh_lines = ["<tasks_dispatch_history>"]
            if omitted > 0:
                dh_lines.append(f"(...{omitted} earlier dispatch(es) omitted)")
            for rec in recent:
                target_str = f" → {rec.target}" if rec.target else ""
                dh_lines.append(f"Iter {rec.iteration}: {rec.task_type}{target_str} | {rec.outcome}")
            dh_lines.append("</tasks_dispatch_history>")
            self.orchestrator.dispatch_history_text = "\n".join(dh_lines)
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
                "Do request termination again until you have addressed "
                "ALL blockers above."
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
                ev_label = f" [{r['evidence_id']}]" if r.get("evidence_id") else ""
                provenance = f"  [from {r['task_id']}: {r['task_type']} on {r['target_id']}]"
                lines.append(f"-{ev_label} {r['target_id']}: {r['description']}  [{r['confidence']}]{provenance}")
                if r.get("result"):
                    lines.append(f"  Result: {r['result']}")
                _is_failure = r.get("result", "").startswith(("Agent produced no exit tool call", "Failed to parse structured"))
                if _is_failure:
                    lines.append("  NOTE: This evidence is from a failed agent run — do NOT treat it as usable evidence.")
                # --- Evidence accumulation nudges ---
                tid = r["target_id"]
                count = r.get("evidence_count", 0)
                types = r.get("evidence_types", {})
                is_rq = r.get("target_is_rq", False)
                if is_rq and not _is_failure:
                    lines.append(
                        f"  -> ACTION NEEDED: {tid} now has {count} evidence item(s) on a Research Question."
                        " Consider promoting to a Working Hypothesis (add_hypothesis) so it undergoes adversarial review."
                    )
                rq_cap = self.config.rq_evidence_cap
                if count >= rq_cap and is_rq:
                    lines.append(
                        f"  >> BLOCKED: {tid} has {count} evidence items (cap={rq_cap}) WITHOUT a Working Hypothesis."
                        " dispatch_researcher / dispatch_computer WILL BE REJECTED until you"
                        " promote this RQ to a WH (add_hypothesis) or resolve/abandon it."
                    )
                if count >= 3 and len(types) == 1:
                    only_type = next(iter(types))
                    alt = "researcher" if only_type == "compute" else "computer"
                    lines.append(
                        f"  >> NOTE: All {count} evidence items on {tid} are type '{only_type}'."
                        f" Consider dispatching a {alt} for a different analytical perspective."
                    )
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
        if self._state.agent_failures:
            lines.append(">>> AGENT FAILURES (previous iteration) <<<")
            for f in self._state.agent_failures:
                lines.append(f"  - [{f['task_id']}] {f['agent']}: {f['event']}. {f['detail']}")
            lines.append(">>> END AGENT FAILURES <<<\n")
            self._state.agent_failures.clear()
        # System events from critique routing (ER demotions, strategy revisions, etc.)
        if self._state.pending_system_events:
            lines.append(">>> SYSTEM EVENTS (between iterations) <<<")
            for event in self._state.pending_system_events:
                lines.append(f"- {event}")
            lines.append(">>> END SYSTEM EVENTS <<<\n")
            self._state.pending_system_events.clear()
        # Pending work summary — always present so the orchestrator sees current state
        pending = self._render_pending_work()
        if pending:
            lines.append(pending)
        return "\n".join(lines)

    def _render_pending_work(self) -> str:
        """Render a summary of open RQs, working WHs, and dangling WHs."""
        from .research_state import Verdict

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

            # Dangling WHs: REFUTED or INCONCLUSIVE, still WORKING
            dangling = [
                h for h in whs
                if h.review and h.review.verdict in (Verdict.REFUTED, Verdict.INCONCLUSIVE)
            ]
            if dangling:
                lines.append("  >>> ATTENTION: resolve these WHs before dispatching to RQs <<<")
                for h in dangling:
                    rc_note = f", refuted {h.refuted_count}x" if h.refuted_count else ""
                    lines.append(
                        f"    {h.id}: {h.review.verdict}{rc_note}"
                        " — gather new evidence or abandon"
                    )

        open_rqs = state.open_research_questions()
        if open_rqs:
            rq_items = []
            for rq in open_rqs:
                ev = f"{len(rq.evidence)} evidence" if rq.evidence else "no evidence"
                rq_items.append(f"{rq.id} ({ev})")
            lines.append(f"  Open RQs: {', '.join(rq_items)}")

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
            else:
                crits = list(self.research_state.critiques.values())
                recent = [c for c in crits if c.iteration_filed == self.iteration]
                if recent:
                    console.print(
                        f"[red]Critic filed {len(recent)} critique(s)[/red]"
                    )
            return "deep_critic", response

        else:
            console.print(f"[yellow]Unknown task type '{tt}', defaulting to researcher[/yellow]")
            self.researcher.research_state = self.research_state
            result = self.researcher.run(task, self.iteration)
            return "researcher", result

    def _should_trigger_critic(self) -> bool:
        """Check if the critic should auto-trigger after a VERIFIED review."""
        # Trigger on every established result, no delay constraint
        return self._state.last_verified_review_iteration == self.iteration

    def _should_force_periodic_critic(self) -> bool:
        """Force critic if it hasn't run in critic_every_n iterations.

        Safeguard against runs where no VERIFIED review happens for a long
        time — the critic still gets to review strategy and research direction.
        """
        gap = self.iteration - self.metrics.last_critic_iteration
        return gap >= self.config.critic_every_n

    # ------------------------------------------------------------------
    # Critique routing (synchronous, between iterations)
    # ------------------------------------------------------------------

    def _route_critiques(self) -> None:
        """Route critic findings to specialist agents (synchronous).

        Phase 1: Adjudicate ER-targeted critiques via the adjudicator.
        Phase 2: Route strategy/coordination critiques (and any ER demotions
                 from phase 1) to the planner for strategy revision.
        """
        from .research_state import CritiqueStatus, HypothesisStatus, RQStatus, ResearchQuestion

        new_critiques = [
            c for c in self.research_state.critiques.values()
            if c.iteration_filed == self.iteration and c.status == CritiqueStatus.ACTIVE
        ]
        if not new_critiques:
            return

        # Separate by target_type
        er_critiques = [c for c in new_critiques if c.target_type == "er"]
        strategy_critiques = [c for c in new_critiques if c.target_type in ("strategy", "coordination")]
        untyped = [c for c in new_critiques if c.target_type not in ("er", "strategy", "coordination")]

        # Warn and auto-resolve untyped critiques
        for c in untyped:
            console.print(f"  [yellow]{c.id} has no target_type — auto-resolving[/yellow]")
            c.status = CritiqueStatus.RESOLVED
            c.resolution = "Auto-resolved: missing target_type in critic output"
            c.resolution_type = "dismissed"
            c.iteration_resolved = self.iteration

        # Phase 1: Adjudicate ER-targeted critiques
        er_demotions: list[dict] = []
        for crit in er_critiques:
            try:
                result = self._adjudicate_er_critique(crit)
                if result and result.get("demoted"):
                    er_demotions.append(result)
            except Exception as exc:
                console.print(f"  [red]Adjudication failed for {crit.id}: {exc}[/red]")
                log_scaffold_event(
                    self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
                    "adjudication_error", f"{crit.id}: {exc}",
                )

        # Phase 2: Strategy assessment (if demotions or strategy critiques)
        if er_demotions or strategy_critiques:
            try:
                self._invoke_planner_revision(strategy_critiques, er_demotions)
            except Exception as exc:
                console.print(f"  [red]Planner revision failed: {exc}[/red]")
                log_scaffold_event(
                    self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
                    "planner_revision_error", str(exc),
                )

    def _adjudicate_er_critique(self, crit) -> dict | None:
        """Invoke the adjudicator to evaluate an ER-targeted critique.

        Returns dict with demotion info if ER was overturned, else None.
        """
        from .research_state import CritiqueStatus, HypothesisStatus, RQStatus, ResearchQuestion

        target_id = crit.targets[0] if crit.targets else None
        if not target_id or target_id not in self.research_state.hypotheses:
            console.print(f"  [dim]{crit.id} targets unknown entity {target_id} — dismissing[/dim]")
            crit.status = CritiqueStatus.RESOLVED
            crit.resolution = f"Target {target_id} not found"
            crit.resolution_type = "dismissed"
            crit.iteration_resolved = self.iteration
            return None

        console.print(f"  [cyan]Adjudicator[/cyan] evaluating {crit.id} against {target_id}...")
        adjud_task = Task(
            task_id=f"ADJUD-{self.iteration:03d}-{crit.id}",
            task_type=TaskType.ADJUDICATE,
            assigned_to="adjudicator",
            iteration=self.iteration,
            target_claim=target_id,
            critique_argument=crit.argument,
        )
        self.adjudicator.research_state = self.research_state
        self.adjudicator.run(adjud_task, self.iteration, on_round=self._on_agent_round)
        result = self.adjudicator.adjudication_result

        if not result:
            console.print(f"  [yellow]{crit.id}: adjudicator returned no result[/yellow]")
            return None

        adjudication = result.get("adjudication", "needs_evidence")
        reasoning = result.get("reasoning", "")[:200]

        if adjudication == "valid":
            from .research_state import FailedApproach
            # Collect dependents before first demotion (normalize_references
            # rewrites depends_on from ER-NNN to WH-NNN after demotion)
            dependent_ids = [
                hid for hid, h in self.research_state.hypotheses.items()
                if h.status == HypothesisStatus.ESTABLISHED
                and target_id in h.depends_on
            ]
            # Demote ER → WH and auto-abandon
            console.print(f"  [red]{crit.id} VALID — demoting {target_id}[/red]")
            new_id = self.research_state.demote_hypothesis(target_id)
            if new_id:
                h = self.research_state.hypotheses[new_id]
                h.status = HypothesisStatus.ABANDONED
                h.review = None  # stale VERIFIED review must not trigger re-promotion
                h.iteration_modified = self.iteration
                self.research_state.failed_approaches.append(FailedApproach(
                    description=f"Overturned {target_id} — {h.statement}",
                    reason=f"Adjudicator ruled critique {crit.id} valid: {reasoning}",
                    related_entities=[new_id],
                    derivation_excerpt=(h.derivation[:300] if h.derivation else ""),
                    iteration=self.iteration,
                ))
            # Cascade: demote and auto-abandon dependents
            for dep_id in dependent_ids:
                console.print(f"  [red]Cascade: demoting {dep_id} (depends on {target_id})[/red]")
                dep_new_id = self.research_state.demote_hypothesis(dep_id)
                if dep_new_id:
                    dep_h = self.research_state.hypotheses[dep_new_id]
                    dep_h.status = HypothesisStatus.ABANDONED
                    dep_h.review = None  # prevent stale re-promotion
                    dep_h.iteration_modified = self.iteration
                    self.research_state.failed_approaches.append(FailedApproach(
                        description=f"Cascade from overturned {target_id} — {dep_h.statement}",
                        reason=f"Depends on {target_id} which was overturned",
                        related_entities=[dep_new_id],
                        iteration=self.iteration,
                    ))
                self._state.pending_system_events.append(
                    f"{dep_id} DEMOTED and ABANDONED (depends on overturned {target_id})"
                )
            crit.status = CritiqueStatus.RESOLVED
            crit.resolution = f"Adjudicator ruled valid: {reasoning}"
            crit.resolution_type = "accepted"
            crit.iteration_resolved = self.iteration
            self._state.pending_system_events.append(
                f"{target_id} OVERTURNED and ABANDONED: {crit.id} ruled valid by adjudicator."
            )
            log_scaffold_event(
                self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
                "er_demotion", f"{target_id} overturned by {crit.id}",
            )
            return {"demoted": target_id, "critique": crit.id, "reasoning": reasoning}

        elif adjudication == "invalid":
            console.print(f"  [green]{crit.id} INVALID — {target_id} stands[/green]")
            counter = result.get("counter_argument", "")[:200]
            crit.status = CritiqueStatus.RESOLVED
            crit.resolution = f"Adjudicator ruled invalid: {counter}"
            crit.resolution_type = "dismissed"
            crit.iteration_resolved = self.iteration
            self._state.pending_system_events.append(
                f"{crit.id} against {target_id} DISMISSED by adjudicator."
            )
            return None

        else:  # needs_evidence
            console.print(f"  [yellow]{crit.id} NEEDS EVIDENCE — creating RQ[/yellow]")
            scope = result.get("investigation_scope", "Investigate the disputed claim.")
            num = self.research_state.next_entity_num()
            rq_id = f"RQ-{num:03d}"
            self.research_state.research_questions[rq_id] = ResearchQuestion(
                id=rq_id,
                question=scope,
                context=f"Created by adjudicator for {crit.id} targeting {target_id}.",
                iteration_created=self.iteration,
            )
            self._state.pending_system_events.append(
                f"{crit.id}: adjudicator needs evidence — created {rq_id}."
            )
            return None

    def _invoke_planner_revision(self, strategy_critiques, er_demotions):
        """Invoke the planner in revise mode to assess strategy after critiques/demotions."""
        from .research_state import CritiqueStatus, HypothesisStatus

        # Build trigger text
        trigger_parts: list[str] = []
        for d in er_demotions:
            trigger_parts.append(
                f"ER {d['demoted']} was overturned by critique {d['critique']}. "
                f"Adjudicator reasoning: {d['reasoning']}"
            )
        for c in strategy_critiques:
            trigger_parts.append(
                f"Critique {c.id} [{c.severity}] targeting {c.target_type}: {c.argument}"
            )
        trigger_text = "\n\n".join(trigger_parts)

        console.print(f"  [cyan]Planner[/cyan] revising strategy...")
        revise_task = Task(
            task_id=f"PLAN-REVISE-{self.iteration:03d}",
            task_type=TaskType.PLAN_REVISE,
            assigned_to="planner",
            iteration=self.iteration,
            body=trigger_text,
        )
        self.planner.research_state = self.research_state
        self.planner.run(revise_task, self.iteration, on_round=self._on_agent_round)

        # Apply results
        if self.planner.parsed_strategy:
            self.research_state.strategy = self.planner.parsed_strategy
            console.print("  [green]Strategy updated[/green]")

        if self.planner.parsed_sanity_checks:
            self.research_state.sanity_checks = self.planner.parsed_sanity_checks
            console.print(f"  [dim]Sanity checks updated ({len(self.planner.parsed_sanity_checks)} checks)[/dim]")

        if self.planner.parsed_entity_actions:
            for action in self.planner.parsed_entity_actions:
                eid = action.get("id", "")
                act = action.get("action", "keep")
                reason = action.get("reason", "")
                if act == "keep":
                    concern = action.get("concern", "")
                    if concern and eid:
                        self._state.pending_system_events.append(
                            f"PLANNER CONCERN on {eid}: {concern}"
                        )
                elif act == "abandon" and eid in self.research_state.hypotheses:
                    from .research_state import FailedApproach
                    h = self.research_state.hypotheses[eid]
                    h.status = HypothesisStatus.ABANDONED
                    h.iteration_modified = self.iteration
                    self.research_state.failed_approaches.append(FailedApproach(
                        description=f"Abandoned {eid} — {h.statement}",
                        reason=f"Planner revision: {reason}",
                        related_entities=[eid],
                        derivation_excerpt=(h.derivation[:300] if h.derivation else ""),
                        iteration=self.iteration,
                    ))
                    self._state.pending_system_events.append(
                        f"{eid} ABANDONED by planner revision: {reason}"
                    )
                    console.print(f"  [red]{eid} abandoned: {reason[:60]}[/red]")

        rationale = self.planner.parsed_revision_rationale or "No rationale provided."

        # Resolve strategy/coordination critiques using planner's assessments
        assessments_by_id: dict[str, dict] = {}
        if self.planner.parsed_critique_assessments:
            for a in self.planner.parsed_critique_assessments:
                assessments_by_id[a.get("id", "")] = a

        for c in strategy_critiques:
            assessment = assessments_by_id.get(c.id)
            c.status = CritiqueStatus.RESOLVED
            c.iteration_resolved = self.iteration

            if assessment and assessment.get("verdict") == "dismiss":
                dismiss_reason = assessment.get("reason", "Dismissed by planner")[:200]
                c.resolution = f"Dismissed by planner: {dismiss_reason}"
                c.resolution_type = "dismissed"
                console.print(f"  [yellow]{c.id} dismissed by planner: {dismiss_reason[:60]}[/yellow]")
            else:
                c.resolution = f"Addressed in strategy revision: {rationale[:120]}"
                c.resolution_type = "accepted"
                console.print(f"  [green]{c.id} accepted by planner[/green]")

        accepted_ids = [c.id for c in strategy_critiques if c.resolution_type == "accepted"]
        dismissed_ids = [c.id for c in strategy_critiques if c.resolution_type == "dismissed"]
        label_parts: list[str] = []
        if accepted_ids:
            label_parts.append(f"accepted: {', '.join(accepted_ids)}")
        if dismissed_ids:
            label_parts.append(f"dismissed: {', '.join(dismissed_ids)}")
        event_label = f"STRATEGY REVISED ({'; '.join(label_parts)})" if label_parts else "STRATEGY REVISED"
        self._state.pending_system_events.append(
            f"{event_label}: {rationale}"
        )

        log_scaffold_event(
            self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
            "strategy_revision", rationale[:200],
        )

    def _auto_promote(self, wh_id: str):
        """Auto-promote a VERIFIED WH to ER if dependencies are satisfied.

        After promotion, cascades: scans remaining WHs for VERIFIED ones
        whose dependencies are now all established, and promotes those too.
        """
        from .research_state import HypothesisStatus, Verdict
        state = self.research_state

        # Seed the cascade with the initial candidate
        candidates = [wh_id]
        while candidates:
            current_id = candidates.pop(0)
            if current_id not in state.hypotheses:
                continue
            h = state.hypotheses[current_id]
            # Must be WORKING and VERIFIED to promote
            if h.status != HypothesisStatus.WORKING:
                continue
            if not h.review or h.review.verdict != Verdict.VERIFIED:
                continue
            unestablished = state.unestablished_dependencies(current_id)
            if unestablished:
                console.print(
                    f"  [dim]Auto-promote skipped for {current_id} "
                    f"(unestablished deps: {', '.join(unestablished)})[/dim]"
                )
                continue
            # Promote
            h = state.hypotheses.pop(current_id)
            num = current_id.split("-")[1]
            er_id = f"ER-{num}"
            h.id = er_id
            h.status = HypothesisStatus.ESTABLISHED
            h.iteration_modified = self.iteration
            state.hypotheses[er_id] = h
            state.normalize_references()
            log_scaffold_event(
                self.workspace.root, self.iteration, CC.STATE_INVARIANTS,
                "auto_promote", f"{current_id} → {er_id}",
            )
            console.print(f"  [bold green]{current_id} → {er_id}[/] auto-promoted")
            # Cascade: find VERIFIED WHs that might now have all deps met
            for hid, hyp in state.hypotheses.items():
                if (
                    hid.startswith("WH-")
                    and hyp.review
                    and hyp.review.verdict == Verdict.VERIFIED
                    and hid not in candidates
                ):
                    candidates.append(hid)

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

    def _should_auto_review(self, task: Task) -> str | None:
        """Check if a WH needs auto-review after new evidence from this task.

        Returns the WH ID to review, or None.
        Triggers when a researcher/computer deposits evidence on a WH that
        already has a review older than the newest evidence (re-review cycle).
        """
        if task.task_type not in (TaskType.RESEARCH, TaskType.COMPUTE):
            return None
        target_id = task.target_claim
        if not target_id or not target_id.startswith("WH-"):
            return None
        h = self.research_state.hypotheses.get(target_id)
        if not h or not h.evidence or not h.review:
            return None
        from .research_state import HypothesisStatus
        if h.status == HypothesisStatus.ABANDONED:
            return None
        # Check if any evidence is newer than the review
        review_iter = h.review.iteration or 0
        newest_evidence_iter = max(
            (ev.iteration or 0) for ev in h.evidence
        )
        if newest_evidence_iter > review_iter:
            return target_id
        return None

    def _make_auto_review_task(self, wh_id: str) -> Task:
        """Create an auto-review task for a WH."""
        task = Task(
            task_id=f"TASK-{self.iteration:03d}",
            task_type=TaskType.REVIEW,
            assigned_to="reviewer",
            priority="high",
            iteration=self.iteration,
            target_claim=wh_id,
            body=f"Auto-triggered review of {wh_id}.",
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
            evs: list = []
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
                # Compute evidence metadata for banner nudges
                active_evs = [e for e in evs if not e.refuted]
                type_counts: dict[str, int] = {}
                for e in active_evs:
                    type_counts[e.type] = type_counts.get(e.type, 0) + 1

                description = ev.summary or (ev.method[:500] if ev.method else "unknown")
                self._state.pending_explore_results.append({
                    "target_id": target_id,
                    "description": description,
                    "result": ev.result,
                    "confidence": ev.confidence or "partial",
                    "task_id": task.task_id,
                    "task_type": task.task_type.value,
                    "evidence_id": ev.id,
                    "evidence_count": len(active_evs),
                    "evidence_types": type_counts,
                    "target_is_rq": target_id.startswith("RQ-"),
                })
                snippet = (ev.summary or ev.result[:120]).replace("\n", " ")
                conf = f", {ev.confidence}" if ev.confidence else ""
                ev_tag = f" [{ev.id}]" if ev.id else ""
                console.print(f"  [blue]Evidence for {target_id}[/blue]{ev_tag}{conf}: {snippet}")
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
                self._state.last_verified_review_iteration = self.iteration
                console.print(f"  [green]{target_id} VERIFIED[/green]")
                # Auto-promote WH→ER if dependencies are satisfied
                if target_id.startswith("WH-"):
                    self._auto_promote(target_id)
                    # Update banner entry if promotion happened
                    er_id = f"ER-{target_id.split('-')[1]}"
                    if er_id in self.research_state.hypotheses:
                        self._state.pending_verified_results[-1]["claim"] = er_id
            elif verdict == Verdict.REFUTED:
                # Mark existing evidence as refuted (specific evidence was challenged)
                for ev in h.evidence:
                    ev.refuted = True
                h.refuted_count += 1
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
                # INCONCLUSIVE: keep existing evidence (not wrong, just insufficient)
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

    def _update_research_iteration(self):
        """Update the iteration field in research state."""
        self.research_state.iteration = self.iteration

    def _auto_expire_critiques(self) -> None:
        """Auto-expire MEDIUM/LOW critiques older than auto_expire_iterations."""
        from .research_state import CritiqueStatus, Severity

        ttl = self.config.auto_expire_iterations
        if ttl <= 0:
            return
        expired = []
        for crit in self.research_state.critiques.values():
            if crit.status != CritiqueStatus.ACTIVE:
                continue
            if crit.severity == Severity.HIGH:
                continue
            if self.iteration - crit.iteration_filed >= ttl:
                crit.status = CritiqueStatus.RESOLVED
                crit.resolution_type = "expired"
                crit.resolution = f"Auto-expired after {self.iteration - crit.iteration_filed} iterations without resolution"
                crit.iteration_resolved = self.iteration
                expired.append(crit.id)
        if expired:
            log_scaffold_event(
                self.workspace.root, self.iteration, CC.LOOP_CONTROL,
                "auto_expire_critiques", f"ids={','.join(expired)}",
            )
            console.print(f"[dim]Auto-expired {len(expired)} critique(s): {', '.join(expired)}[/dim]")

    def _set_research_status(self, status: str):
        """Update the status field in research state."""
        self.research_state.status = status

    def _render_files_for_git(self):
        """Render markdown files from ResearchState for git snapshots and verify.py."""
        self.workspace.write_file("RESEARCH_STATE.md", render_research_state_md(self.research_state))
        self.workspace.write_file("EVIDENCE_LOG.md", render_evidence_log_md(self.research_state))
        self.workspace.write_file("CRITIQUE_LOG.md", render_critique_log_md(self.research_state))

    def _run_formatter(self, answer_ers: list[str] | None = None) -> str | None:
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
            answer_ers=answer_ers or [],
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
        md = self.metrics.to_markdown()
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
