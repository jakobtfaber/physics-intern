"""Metrics tracking for OpenDirac iterations."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CallRecord:
    iteration: int
    agent: str
    input_tokens: int
    output_tokens: int
    duration: float
    max_tokens_hit: bool
    rounds: int = 1
    tool_calls: int = 0
    truncated: bool = False
    reasoning_tokens: int = 0
    answer_tokens: int = 0


class MetricsTracker:
    """Track per-iteration metrics, alerts, and file sizes."""

    def __init__(self):
        self.calls: list[CallRecord] = []
        self.alerts: list[dict] = []
        self.last_critic_iteration: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_reasoning_tokens: int = 0
        self.total_answer_tokens: int = 0
        self.max_tokens_reached_count: int = 0
        self.total_tool_calls: int = 0

    def record_call(self, iteration: int, agent: str, input_tokens: int,
                    output_tokens: int, duration: float, max_tokens_hit: bool,
                    rounds: int = 1, tool_calls: int = 0, truncated: bool = False,
                    reasoning_tokens: int = 0, answer_tokens: int = 0):
        self.calls.append(CallRecord(
            iteration=iteration, agent=agent,
            input_tokens=input_tokens, output_tokens=output_tokens,
            duration=duration, max_tokens_hit=max_tokens_hit,
            rounds=rounds, tool_calls=tool_calls, truncated=truncated,
            reasoning_tokens=reasoning_tokens, answer_tokens=answer_tokens,
        ))
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_reasoning_tokens += reasoning_tokens
        self.total_answer_tokens += answer_tokens
        self.total_tool_calls += tool_calls
        if max_tokens_hit:
            self.max_tokens_reached_count += 1
        if agent == "deep_critic":
            self.last_critic_iteration = iteration

    def alert(self, iteration: int, message: str):
        self.alerts.append({"iteration": iteration, "message": message})

    def to_markdown(self) -> str:
        """Render metrics as METRICS.md content."""
        total_iters = max((c.iteration for c in self.calls), default=0)
        has_tool_calls = any(c.tool_calls > 0 for c in self.calls)

        meta = {
            "total_iterations": total_iters,
            "total_llm_calls": len(self.calls),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "max_tokens_reached_count": self.max_tokens_reached_count,
        }
        if self.total_reasoning_tokens > 0:
            meta["total_reasoning_tokens"] = self.total_reasoning_tokens
            meta["total_answer_tokens"] = self.total_answer_tokens
        if has_tool_calls:
            meta["total_tool_calls"] = self.total_tool_calls

        lines = ["---"]
        for k, v in meta.items():
            lines.append(f"{k}: {v}")
        lines.append("---\n")

        lines.append("# Per-Iteration Metrics\n")
        if has_tool_calls:
            lines.append("| Iter | Agent | Input Tokens | Output Tokens | Max Tokens Hit | Rounds | Tool Calls | Duration (s) |")
            lines.append("|------|-------|-------------|---------------|----------------|--------|------------|-------------|")
            for c in reversed(self.calls):
                lines.append(
                    f"| {c.iteration} | {c.agent} | {c.input_tokens} | {c.output_tokens} "
                    f"| {'yes' if c.max_tokens_hit else 'no'} | {c.rounds} | {c.tool_calls} | {c.duration:.1f} |"
                )
        else:
            lines.append("| Iter | Agent | Input Tokens | Output Tokens | Max Tokens Hit | Duration (s) |")
            lines.append("|------|-------|-------------|---------------|----------------|-------------|")
            for c in reversed(self.calls):
                lines.append(
                    f"| {c.iteration} | {c.agent} | {c.input_tokens} | {c.output_tokens} "
                    f"| {'yes' if c.max_tokens_hit else 'no'} | {c.duration:.1f} |"
                )

        if self.alerts:
            lines.append("\n# Alerts")
            for a in self.alerts:
                lines.append(f"- [iter {a['iteration']}] {a['message']}")

        return "\n".join(lines) + "\n"
