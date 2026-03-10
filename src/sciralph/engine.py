"""SciRalph main loop engine."""

import re

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import Config
from .markdown import (
    parse_frontmatter,
    render_frontmatter,
    find_prior_failures_for_claim,
    _parse_comp_entries,
)
from .metrics import MetricsTracker
from .task import Task, TaskType
from .workspace import WorkspaceManager
from .agents.orchestrator import OrchestratorAgent
from .agents.researcher import ResearcherAgent
from .agents.computationalist import ComputationalistAgent
from .agents.critic import CriticAgent
from .agents.compressor import CompressorAgent

console = Console()


class SciRalph:
    """Main loop for the SciRalph research system."""

    def __init__(self, problem: str, config: Config | None = None):
        self.config = config or Config()
        self.metrics = MetricsTracker()
        self.workspace = WorkspaceManager(self.config)
        self.workspace.init(problem)
        self.config.audit_log = str(self.workspace.root / "AUDIT_LOG.jsonl")
        self.config.logs_dir = str(self.workspace.logs_dir)
        self.iteration = 0
        self._stale_iterations = 0
        self._pending_recompute_claim: str | None = None

        # Initialize agents
        self.orchestrator = OrchestratorAgent(self.config, self.workspace, self.metrics)
        self.researcher = ResearcherAgent(self.config, self.workspace, self.metrics)
        self.computationalist = ComputationalistAgent(self.config, self.workspace, self.metrics)
        self.critic = CriticAgent(self.config, self.workspace, self.metrics)
        self.compressor = CompressorAgent(self.config, self.workspace, self.metrics)

    def run(self):
        """Main loop per DESIGN.md section 5.1."""
        console.print(Panel("SciRalph Research System", style="bold blue"))

        while self.iteration < self.config.max_iterations:
            self.iteration += 1
            console.rule(f"[bold]ITERATION {self.iteration}[/bold]")

            # Step 1: Orchestrator decides next task
            console.print("[cyan]Orchestrator[/cyan] planning...")
            orch_task = Task(
                task_id="", task_type=TaskType.RESEARCH,
                assigned_to="orchestrator", iteration=self.iteration,
            )
            orch_response = self.orchestrator.run(orch_task, self.iteration)
            task = self.orchestrator.parse_task(orch_response.text, iteration=self.iteration)

            # Validate COMP/TASK references in RESEARCH_STATE
            phantoms = self.workspace.validate_comp_references()
            if phantoms:
                self.metrics.alert(
                    self.iteration,
                    f"Phantom references stripped: {', '.join(phantoms)}"
                )

            # Auto-recompute after REFUTED verdict
            if self._pending_recompute_claim:
                claim = self._pending_recompute_claim
                self._pending_recompute_claim = None
                if task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
                    console.print("[yellow]Forcing recompute after REFUTED verdict.[/yellow]")
                    task = self._make_recompute_task(claim)

            # Budget enforcement: hard override when budget exhausted
            budget_remaining = self.config.max_iterations - self.iteration
            if budget_remaining <= 1 and task.task_type not in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
                console.print(
                    f"[yellow]Budget enforcement: {budget_remaining} iteration(s) left, "
                    f"overriding '{task.task_type}' -> 'synthesize'.[/yellow]"
                )
                self.metrics.alert(self.iteration, f"Budget override: {task.task_type} -> synthesize")
                task = self._make_budget_synthesize_task()

            # Enrich compute tasks with prior failure context
            if task.task_type == TaskType.COMPUTE:
                self._enrich_compute_task_with_prior_failures(task)

            self._print_task(task)

            # Check for termination signal
            if task.task_type in (TaskType.SYNTHESIZE, TaskType.TERMINATE):
                if task.task_type == TaskType.TERMINATE:
                    console.print("[green]Orchestrator signaled completion.[/green]")
                    self._set_research_status("completed")
                    break
                # synthesize: dispatch it, then terminate next iteration
                self._stale_iterations = 0
            else:
                # Backstop: detect stale loops when research looks complete
                state = self.workspace.read_file("RESEARCH_STATE.md")
                er_count = len(re.findall(r'^## ER-\d+', state, re.MULTILINE))
                wh_count = len(re.findall(r'^## WH-\d+', state, re.MULTILINE))
                if er_count >= self.config.min_er_for_completion and wh_count == 0:
                    self._stale_iterations += 1
                    if self._stale_iterations >= 2:
                        console.print(
                            "[yellow]Backstop: research appears complete but orchestrator "
                            "did not terminate. Forcing exit.[/yellow]"
                        )
                        self._set_research_status("completed")
                        break
                else:
                    self._stale_iterations = 0

            # Step 2: Force critic if overdue
            if self._critic_overdue() and task.task_type != TaskType.CRITIQUE:
                console.print(
                    f"[yellow]Forcing critic pass (overdue: last critic at "
                    f"iter {self.metrics.last_critic_iteration}, "
                    f"threshold {self.config.critic_every_n}).[/yellow]"
                )
                task = self._make_forced_critic_task()

            # Step 3: Dispatch to appropriate agent
            agent_name = self._dispatch(task)

            # Step 4: File size check & compression
            self._check_compression()

            # Step 5: Metrics & git commit
            self._update_metrics()
            self.workspace.git_commit(
                f"Iteration {self.iteration}: {agent_name} - {task.task_id}"
            )

            # Step 6: Check termination conditions
            if self._should_terminate():
                console.print("[green]Research completed or abandoned.[/green]")
                break

        self._final_report()

    def _dispatch(self, task: Task) -> str:
        """Route task to the correct agent."""
        tt = task.task_type

        if tt in (TaskType.RESEARCH, TaskType.DERIVE, TaskType.RESOLVE, TaskType.SYNTHESIZE):
            console.print(f"[green]Researcher[/green] working on: {tt}")
            self.researcher.run(task, self.iteration)
            return "researcher"

        elif tt == TaskType.COMPUTE:
            console.print("[magenta]Computationalist[/magenta] working...")
            self.computationalist.run(task, self.iteration)
            self._check_for_refuted_verdict(task)
            return "computationalist"

        elif tt == TaskType.CRITIQUE:
            console.print("[red]Deep Critic[/red] reviewing...")
            response = self.critic.run(task, self.iteration)
            # Check for silent failure (near-empty output)
            output_tokens = (
                response.output_tokens if hasattr(response, 'output_tokens')
                else response.total_output_tokens
            )
            if output_tokens < 200:
                self.metrics.alert(
                    self.iteration,
                    f"Critic underflow: {output_tokens} output tokens — retrying once"
                )
                console.print(
                    f"[yellow]Critic produced only {output_tokens} tokens, retrying...[/yellow]"
                )
                self.critic.run(task, self.iteration)
            return "deep_critic"

        else:
            console.print(f"[yellow]Unknown task type '{tt}', defaulting to researcher[/yellow]")
            self.researcher.run(task, self.iteration)
            return "researcher"

    def _critic_overdue(self) -> bool:
        """Check if more than N iterations since last critic pass."""
        return (self.iteration - self.metrics.last_critic_iteration) >= self.config.critic_every_n

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

    def _enrich_compute_task_with_prior_failures(self, task: Task):
        """Append prior failure context to CURRENT_TASK.md for compute retries."""
        comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
        prior = find_prior_failures_for_claim(comp_log, task.body)
        if not prior:
            return
        task_text = self.workspace.read_file("CURRENT_TASK.md")
        addendum = (
            "\n\n---\n\n## Prior Computation Failure Context\n\n"
            f"**{len(prior)} prior failure(s) on this claim.** "
            "Diagnose the ROOT CAUSE before writing new code.\n\n"
            "### Most Recent Failed Result\n\n"
            + prior[0][:3000]
        )
        if len(prior) > 1:
            addendum += f"\n\n({len(prior) - 1} earlier failure(s) in COMPUTATION_LOG.md)\n"
        self.workspace.write_file("CURRENT_TASK.md", task_text + addendum)

    def _check_for_refuted_verdict(self, task: Task):
        """After computationalist runs, check if latest verdict is REFUTED."""
        comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
        entries = _parse_comp_entries(comp_log)
        if entries and entries[-1]["verdict"] == "REFUTED":
            claim = entries[-1].get("claim", task.body)
            self._pending_recompute_claim = claim
            self.metrics.alert(
                self.iteration,
                "REFUTED verdict detected — will force recompute next iteration"
            )

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
                if size > threshold * 2:
                    console.print(f"[yellow]Force-compressing {filename}[/yellow]")
                    compress_task = Task(
                        task_id=f"COMPRESS-{self.iteration:03d}",
                        task_type=TaskType.RESEARCH,
                        assigned_to="compressor",
                        iteration=self.iteration,
                        target_file=filename,
                    )
                    self.compressor.run(compress_task, self.iteration)
                elif size > threshold * 1.5:
                    console.print(f"[yellow]Compressing {filename}[/yellow]")
                    compress_task = Task(
                        task_id=f"COMPRESS-{self.iteration:03d}",
                        task_type=TaskType.RESEARCH,
                        assigned_to="compressor",
                        iteration=self.iteration,
                        target_file=filename,
                    )
                    self.compressor.run(compress_task, self.iteration)

    def _set_research_status(self, status: str):
        """Update the status field in RESEARCH_STATE.md frontmatter."""
        text = self.workspace.read_file("RESEARCH_STATE.md")
        if not text:
            return
        meta, body = parse_frontmatter(text)
        meta["status"] = status
        self.workspace.write_file("RESEARCH_STATE.md", render_frontmatter(meta, body))

    def _should_terminate(self) -> bool:
        """Check termination conditions beyond max_iterations."""
        state = self.workspace.read_file("RESEARCH_STATE.md")
        for status in ("completed", "abandoned", "partially_complete"):
            if f'status: "{status}"' in state or f"status: {status}" in state:
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

    def _final_report(self):
        """Flush metrics and print final summary."""
        self._update_metrics()
        self.workspace.git_commit(f"Final metrics flush (iteration {self.iteration})")
        console.rule("[bold green]SESSION COMPLETE[/bold green]")
        console.print(f"Total iterations: {self.iteration}")
        console.print(f"Total LLM calls: {len(self.metrics.calls)}")
        console.print(f"Total input tokens: {self.metrics.total_input_tokens:,}")
        console.print(f"Total output tokens: {self.metrics.total_output_tokens:,}")
        console.print(f"Workspace: {self.workspace.root.resolve()}")
        if self.metrics.alerts:
            console.print(f"\n[yellow]Alerts ({len(self.metrics.alerts)}):[/yellow]")
            for a in self.metrics.alerts[-5:]:
                console.print(f"  [iter {a['iteration']}] {a['message']}")
