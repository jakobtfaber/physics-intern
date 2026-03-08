"""SciRalph main loop engine."""

import re

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import Config
from .metrics import MetricsTracker
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
            orch_response = self.orchestrator.run({}, self.iteration)
            task = self.orchestrator.parse_task(orch_response.text, iteration=self.iteration)
            self._print_task(task)

            # Check for termination signal
            if task["task_type"] in ("synthesize", "terminate"):
                if task["task_type"] == "terminate":
                    console.print("[green]Orchestrator signaled completion.[/green]")
                    break
                # synthesize: dispatch it, then terminate next iteration
                self._stale_iterations = 0
            else:
                # Backstop: detect stale loops when research looks complete
                state = self.workspace.read_file("RESEARCH_STATE.md")
                er_count = len(re.findall(r'^## ER-\d+', state, re.MULTILINE))
                wh_count = len(re.findall(r'^## WH-\d+', state, re.MULTILINE))
                if er_count >= 3 and wh_count == 0:
                    self._stale_iterations += 1
                    if self._stale_iterations >= 2:
                        console.print(
                            "[yellow]Backstop: research appears complete but orchestrator "
                            "did not terminate. Forcing exit.[/yellow]"
                        )
                        break
                else:
                    self._stale_iterations = 0

            # Step 2: Force critic if overdue
            if self._critic_overdue() and task["task_type"] != "critique":
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
                f"Iteration {self.iteration}: {agent_name} - {task.get('task_id', 'unknown')}"
            )

            # Step 6: Check termination conditions
            if self._should_terminate():
                console.print("[green]Research completed or abandoned.[/green]")
                break

        self._final_report()

    def _dispatch(self, task: dict) -> str:
        """Route task to the correct agent."""
        task_type = task["task_type"]

        if task_type in ("research", "derive", "resolve", "synthesize"):
            console.print(f"[green]Researcher[/green] working on: {task_type}")
            self.researcher.run(task, self.iteration)
            return "researcher"

        elif task_type == "compute":
            console.print("[magenta]Computationalist[/magenta] working...")
            self.computationalist.run(task, self.iteration)
            return "computationalist"

        elif task_type == "critique":
            console.print("[red]Deep Critic[/red] reviewing...")
            self.critic.run(task, self.iteration)
            return "deep_critic"

        elif task_type == "compress":
            console.print(f"[yellow]Compressor[/yellow] compressing: {task.get('target_file')}")
            self.compressor.run(task, self.iteration)
            return "compressor"

        else:
            console.print(f"[yellow]Unknown task type '{task_type}', defaulting to researcher[/yellow]")
            self.researcher.run(task, self.iteration)
            return "researcher"

    def _critic_overdue(self) -> bool:
        """Check if more than N iterations since last critic pass."""
        return (self.iteration - self.metrics.last_critic_iteration) >= self.config.critic_every_n

    def _make_forced_critic_task(self) -> dict:
        """Create a forced critic task."""
        task_content = f"""---
task_id: "TASK-{self.iteration:03d}"
task_type: "critique"
assigned_to: "deep_critic"
priority: "high"
iteration: {self.iteration}
---

# Task Description

Mandatory periodic review. Perform a thorough critique of all Working
Hypotheses and recent Established Results in RESEARCH_STATE.md.
"""
        self.workspace.write_file("CURRENT_TASK.md", task_content)
        return {
            "task_id": f"TASK-{self.iteration:03d}",
            "task_type": "critique",
            "assigned_to": "deep_critic",
            "priority": "high",
            "iteration": self.iteration,
            "blocking_critiques": [],
            "target_file": "",
            "body": "",
        }

    def _check_compression(self):
        """Check file sizes against thresholds, force compression if critical."""
        for filename, threshold in self.config.compress_threshold.items():
            size = self.workspace.file_size(filename)
            if size > threshold:
                self.metrics.alert(
                    self.iteration,
                    f"{filename} size ({size}) exceeds threshold ({threshold})."
                )
                if size > threshold * 2:
                    console.print(f"[yellow]Force-compressing {filename}[/yellow]")
                    self.compressor.run({"target_file": filename}, self.iteration)

    def _should_terminate(self) -> bool:
        """Check termination conditions beyond max_iterations."""
        state = self.workspace.read_file("RESEARCH_STATE.md")
        if 'status: "completed"' in state or "status: completed" in state:
            return True
        if 'status: "abandoned"' in state or "status: abandoned" in state:
            return True
        return False

    def _update_metrics(self):
        """Write current metrics to METRICS.md."""
        file_sizes = {}
        for filename in self.config.compress_threshold:
            file_sizes[filename] = self.workspace.file_size(filename)
        md = self.metrics.to_markdown(file_sizes, self.config.compress_threshold)
        self.workspace.write_file("METRICS.md", md)

    def _print_task(self, task: dict):
        """Print task summary to console."""
        text = Text()
        text.append(f"Task: ", style="bold")
        text.append(f"{task.get('task_id', '?')} ", style="cyan")
        text.append(f"[{task.get('task_type', '?')}] ", style="yellow")
        text.append(f"-> {task.get('assigned_to', '?')}", style="green")
        console.print(text)

    def _final_report(self):
        """Print final summary."""
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
