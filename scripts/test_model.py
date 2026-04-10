#!/usr/bin/env python3
"""Live integration test for a model's reasoning and token tracking.

Usage:
    uv run python scripts/test_model.py <model-key>
    uv run python scripts/test_model.py deepseek-v3.2
    uv run python scripts/test_model.py gemini-3-flash-preview
    uv run python scripts/test_model.py --list          # list available models
"""

import argparse
import os
import sys
from pathlib import Path

# Add src to path so we can import open_dirac
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv()

import yaml
from rich.console import Console
from rich.table import Table

from open_dirac.config import _resolve_model
from open_dirac.providers import create_provider

console = Console()


def load_registry() -> dict:
    path = Path(__file__).parent.parent / "src" / "open_dirac" / "models.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def make_provider(model_key: str):
    """Resolve model key and create provider instance."""
    info = _resolve_model(model_key)
    if not info:
        console.print(f"[red]Unknown model key: {model_key}[/]")
        sys.exit(1)

    api_key = os.environ.get(info["env_key"], "")
    if not api_key and info["provider"] != "vllm":
        console.print(f"[red]Missing env var: {info['env_key']}[/]")
        sys.exit(1)

    provider = create_provider(
        info["provider"], api_key=api_key, **info.get("reasoning", {}))
    model_id = info["model_id"]
    max_tokens = info.get("max_output_tokens", 8192)
    return provider, model_id, max_tokens, info


# ── Test definitions ───────────────────────────────────────────────────────

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "Evaluate a mathematical expression and return the result.",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The math expression to evaluate, e.g. '7 * 13'",
                }
            },
            "required": ["expression"],
        },
    },
}


class Check:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail


def test_reasoning(provider, model_id, max_tokens) -> list[Check]:
    """Turn 1: simple reasoning prompt, no tools."""
    checks = []
    console.print("\n[bold]Turn 1: Reasoning (no tools)[/]")

    resp = provider.call(
        model=model_id,
        max_tokens=min(max_tokens, 4096),
        system="You are a helpful assistant. Think step by step.",
        messages=[{"role": "user", "content": "What is 7 * 13? Show your reasoning."}],
    )

    console.print(f"  text:              {repr(resp.text[:120])}{'...' if len(resp.text) > 120 else ''}")
    console.print(f"  input_tokens:      {resp.input_tokens}")
    console.print(f"  output_tokens:     {resp.output_tokens}")
    console.print(f"  reasoning_tokens:  {resp.reasoning_tokens}")
    console.print(f"  answer_tokens:     {resp.answer_tokens}")
    console.print(f"  reasoning_content: {len(resp.reasoning_content)} chars")
    console.print(f"  stop_reason:       {resp.stop_reason}")

    checks.append(Check(
        "text non-empty",
        bool(resp.text.strip()),
        f"{len(resp.text)} chars"))

    checks.append(Check(
        "output_tokens > 0",
        resp.output_tokens > 0,
        str(resp.output_tokens)))

    checks.append(Check(
        "input_tokens > 0",
        resp.input_tokens > 0,
        str(resp.input_tokens)))

    checks.append(Check(
        "invariant: output = reasoning + answer",
        resp.output_tokens == resp.reasoning_tokens + resp.answer_tokens,
        f"{resp.output_tokens} == {resp.reasoning_tokens} + {resp.answer_tokens}"))

    checks.append(Check(
        "reasoning_tokens > 0",
        resp.reasoning_tokens > 0,
        str(resp.reasoning_tokens)))

    checks.append(Check(
        "answer_tokens > 0",
        resp.answer_tokens > 0,
        str(resp.answer_tokens)))

    checks.append(Check(
        "reasoning_content captured",
        len(resp.reasoning_content) > 0,
        f"{len(resp.reasoning_content)} chars"))

    checks.append(Check(
        "stop_reason is end_turn",
        resp.stop_reason == "end_turn",
        resp.stop_reason))

    return checks


def test_tool_call(provider, model_id, max_tokens) -> list[Check]:
    """Turn 2: prompt that should trigger a tool call."""
    checks = []
    console.print("\n[bold]Turn 2: Tool call[/]")

    resp = provider.call(
        model=model_id,
        max_tokens=min(max_tokens, 4096),
        system="You are a helpful assistant. Use the calculate tool to evaluate math expressions.",
        messages=[{"role": "user", "content": "Please use the calculate tool to compute 7 * 13."}],
        tools=[TOOL_DEF],
    )

    tc_summary = f"{len(resp.tool_calls)} calls" if resp.tool_calls else "none"
    console.print(f"  text:              {repr(resp.text[:80])}")
    console.print(f"  tool_calls:        {tc_summary}")
    if resp.tool_calls:
        for tc in resp.tool_calls:
            console.print(f"    - {tc['name']}({tc['input']})")
    console.print(f"  output_tokens:     {resp.output_tokens}")
    console.print(f"  reasoning_tokens:  {resp.reasoning_tokens}")
    console.print(f"  answer_tokens:     {resp.answer_tokens}")
    console.print(f"  stop_reason:       {resp.stop_reason}")

    checks.append(Check(
        "tool_calls present",
        resp.tool_calls is not None and len(resp.tool_calls) > 0,
        tc_summary))

    checks.append(Check(
        "stop_reason is tool_use",
        resp.stop_reason == "tool_use",
        resp.stop_reason))

    checks.append(Check(
        "invariant: output = reasoning + answer",
        resp.output_tokens == resp.reasoning_tokens + resp.answer_tokens,
        f"{resp.output_tokens} == {resp.reasoning_tokens} + {resp.answer_tokens}"))

    # When tool calls are present and text is empty, answer_tokens should
    # still be nonzero (tool args contribute)
    if resp.tool_calls and not resp.text.strip():
        checks.append(Check(
            "answer_tokens > 0 (tool args counted)",
            resp.answer_tokens > 0,
            str(resp.answer_tokens)))

    return checks


def test_tool_round_trip(provider, model_id, max_tokens) -> list[Check]:
    """Turn 3: complete tool round-trip — call tool, then get final answer."""
    checks = []
    console.print("\n[bold]Turn 3: Tool round-trip (call + result + final answer)[/]")

    # First: get the tool call
    resp1 = provider.call(
        model=model_id,
        max_tokens=min(max_tokens, 4096),
        system="You are a helpful assistant. Use the calculate tool to evaluate math.",
        messages=[{"role": "user", "content": "Use calculate to compute 17 + 25."}],
        tools=[TOOL_DEF],
    )

    if not resp1.tool_calls:
        checks.append(Check("tool call obtained", False, "no tool call in response"))
        return checks

    checks.append(Check("tool call obtained", True, resp1.tool_calls[0]["name"]))

    # Build conversation with tool result
    messages = [
        {"role": "user", "content": "Use calculate to compute 17 + 25."},
        provider.format_assistant_message(resp1.raw_content),
    ]
    messages.extend(provider.build_tool_result_messages([{
        "tool_call_id": resp1.tool_calls[0]["id"],
        "name": resp1.tool_calls[0]["name"],
        "output": "42",
        "is_error": False,
    }]))

    # Second: get final answer using tool result
    resp2 = provider.call(
        model=model_id,
        max_tokens=min(max_tokens, 4096),
        system="You are a helpful assistant. Use the calculate tool to evaluate math.",
        messages=messages,
        tools=[TOOL_DEF],
    )

    console.print(f"  final text:        {repr(resp2.text[:120])}")
    console.print(f"  output_tokens:     {resp2.output_tokens}")
    console.print(f"  reasoning_tokens:  {resp2.reasoning_tokens}")
    console.print(f"  answer_tokens:     {resp2.answer_tokens}")

    checks.append(Check(
        "final answer non-empty",
        bool(resp2.text.strip()),
        f"{len(resp2.text)} chars"))

    checks.append(Check(
        "invariant: output = reasoning + answer",
        resp2.output_tokens == resp2.reasoning_tokens + resp2.answer_tokens,
        f"{resp2.output_tokens} == {resp2.reasoning_tokens} + {resp2.answer_tokens}"))

    checks.append(Check(
        "answer mentions 42",
        "42" in resp2.text,
        resp2.text[:80]))

    return checks


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test a model's reasoning and token tracking.")
    parser.add_argument("model_key", nargs="?", help="Model key from models.yaml")
    parser.add_argument("--list", action="store_true", help="List available models")
    parser.add_argument("--skip-tools", action="store_true",
                        help="Skip tool call tests (for models that don't support tools)")
    args = parser.parse_args()

    if args.list:
        registry = load_registry()
        table = Table(title="Available Models")
        table.add_column("Key", style="cyan")
        table.add_column("Provider")
        table.add_column("Model ID")
        table.add_column("Reasoning")
        for key, entry in registry.items():
            reasoning = ""
            for rk in ("reasoning_format", "thinking", "thinking_level", "reasoning_effort"):
                if rk in entry:
                    reasoning = f"{rk}={entry[rk]}"
                    break
            table.add_row(key, entry["provider"], entry.get("model_id", key), reasoning)
        console.print(table)
        return

    if not args.model_key:
        parser.print_help()
        sys.exit(1)

    provider, model_id, max_tokens, info = make_provider(args.model_key)

    console.print(f"[bold cyan]Testing: {args.model_key}[/]")
    console.print(f"  provider: {info['provider']}, model_id: {model_id}")
    console.print(f"  reasoning config: {info.get('reasoning', {})}")

    all_checks: list[Check] = []

    # Turn 1: reasoning
    all_checks.extend(test_reasoning(provider, model_id, max_tokens))

    # Turn 2 & 3: tool calls
    if not args.skip_tools:
        all_checks.extend(test_tool_call(provider, model_id, max_tokens))
        all_checks.extend(test_tool_round_trip(provider, model_id, max_tokens))
    else:
        console.print("\n[dim]Skipping tool call tests (--skip-tools)[/]")

    # Summary table
    console.print()
    table = Table(title=f"Results: {args.model_key}")
    table.add_column("Check", style="white")
    table.add_column("Status")
    table.add_column("Detail", style="dim")

    n_pass = 0
    n_fail = 0
    for c in all_checks:
        if c.passed:
            table.add_row(c.name, "[green]PASS[/]", c.detail)
            n_pass += 1
        else:
            table.add_row(c.name, "[red]FAIL[/]", c.detail)
            n_fail += 1

    console.print(table)
    console.print(f"\n[bold]{n_pass} passed, {n_fail} failed[/]")

    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
