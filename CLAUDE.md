# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics. Uses iterative LLM calls (Anthropic Claude) with externally persisted state in Markdown files and a layered verification stack.

## Key Documents

- `README.md` — User-facing overview, architecture diagram, quick start
- `DESIGN.md` — Full system design (architecture, file formats, agent prompts, pseudocode)
- `PLAN.md` — Implementation plan for Phase 1.5 (tool-use loop, agentic agents)

## Project Structure

```
src/sciralph/
  main.py              — Entry point (reads problem YAML, CLI flags)
  engine.py            — Main loop: orchestrate → dispatch → compress → metrics → git
  verify.py            — Independent verification script (Claude Opus, streaming)
  config.py            — Config dataclass (model, thresholds, timeouts, audit_log)
  llm.py               — Anthropic API wrapper (call_llm) with JSONL audit logging
  workspace.py         — File I/O + git operations on workspace/
  markdown.py          — YAML frontmatter parsing, section extraction, critique counting
  sandbox.py           — Python script execution with timeout
  metrics.py           — MetricsTracker (token counts, alerts, Markdown rendering)
  agents/
    base.py            — BaseAgent ABC with template method + retry
    orchestrator.py    — Plans tasks, integrates proposed changes
    researcher.py      — Derivations and reasoning
    computationalist.py — Code extraction, execution, failure flagging
    critic.py          — Adversarial review, critique counting
    compressor.py      — File size management
  prompts/             — Static .md system prompt files (one per agent, plus verifier)
tests/                 — pytest tests (markdown, sandbox, metrics, orchestrator, verify)
problems/              — YAML problem definitions
run_and_verify.sh      — Run a problem then verify results in one command
```

## Tech Stack

- Python 3.12+, `uv` for dependency management
- `anthropic` SDK, `rich` for console, `pyyaml`, `sympy`, `numpy`, `scipy`, `matplotlib`
- Tests: `pytest` (run with `uv run python -m pytest -v`, need `--extra dev`)

## Architecture

Five agents (orchestrator, researcher, computationalist, deep critic, compressor) take turns in a main loop. Each agent gets a fresh context per call. All state lives in Markdown files with YAML frontmatter under `workspaces/<run>/` (each run gets a timestamped subdirectory like `workspaces/20260307_142530_hawking_temperature/`; override with `--workspace-dir`).

- **Orchestrator** reads all state, integrates proposed changes into RESEARCH_STATE.md, emits CURRENT_TASK.md
- **Researcher** writes PROPOSED_CHANGES.md (never modifies RESEARCH_STATE directly)
- **Computationalist** extracts/runs Python code, appends to COMPUTATION_LOG.md; flags execution failures prominently
- **Deep Critic** appends critiques to CRITIQUE_LOG.md
- **Compressor** archives + shrinks files exceeding size thresholds

The orchestrator integrates proposed changes on its next pass. Forced critic passes every N iterations. Audit logging (JSONL) records metadata for every LLM call.

### Valid Task Types

The orchestrator emits one of these task types: `research`, `derive`, `compute`, `critique`, `resolve`, `synthesize`, `terminate`.

## Conventions

- `call_llm` is a stateless function, not a class; accepts optional `agent_name`/`iteration` for audit logging
- Agent prompts are static `.md` files loaded at runtime — no templating
- YAML frontmatter parsing always falls back to regex on failure — never crash the loop
- Workspace git is managed by the scaffolding loop, not by agents
- Code extraction from computationalist: take last ```python``` fenced block
- Critique ID format: `CRIT-NNN` (regex also accepts `CRITIQUE-NNN` for LLM drift tolerance)

## Running

```bash
# Install
uv sync --extra dev

# Tests
uv run python -m pytest -v

# Run (requires ANTHROPIC_API_KEY in .env or env var)
uv run python -m sciralph.main problems/hawking_temperature.yaml --max-iterations 5

# Verify a completed workspace (uses Claude Opus by default)
uv run python -m sciralph.verify workspaces/<run_dir>/ --write-report
uv run python -m sciralph.verify workspaces/<run_dir>/ --rerun-computations --write-report

# Run + verify in one command
./run_and_verify.sh problems/hawking_temperature.yaml --max-iterations 10
./run_and_verify.sh problems/qho_thermodynamics.yaml -- --rerun-computations
```

## Current Status

- Phase 1 (core loop): DONE — all agents, main loop, 28 tests passing
- Phase 1.1 (critical fixes): DONE — integration gap, sandbox interpreter, termination detection, truncation overlap, compressor guard, critique counting
- Post-e2e bug fixes: DONE — execution failure banners, orchestrator task types + termination, critique regex broadening, audit logging
- Phase 1.5 Step 1 (warm-up removal): DONE — orchestrator now autonomously decomposes sub-problems
- Phase 1.5 (next steps): full prompt/response logging, tool-use loop, agentic agents, external reference files — see PLAN.md
