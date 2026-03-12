# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics. Uses iterative LLM calls (Anthropic Claude) with externally persisted state in Markdown files and a layered verification stack.

## Key Documents

- `README.md` — User-facing overview, architecture diagram, quick start
- `PLAN.md` — Future work ideas and roadmap

## Project Structure

```
src/sciralph/
  main.py              — Entry point (reads problem YAML, CLI flags)
  engine.py            — Main loop: orchestrate → validate → override → dispatch → compress → git
  validation.py        — Post-integration checks (ER gate, phantom labels, routing) + termination gates
  verify.py            — Independent verification script (Claude Opus, streaming)
  config.py            — Config dataclass (model, thresholds, timeouts, audit_log)
  llm.py               — Anthropic API wrapper (call_llm, run_agent_loop) with JSONL audit logging
  task.py              — Task dataclass + TaskType enum for typed task handling
  tools.py             — ToolExecutor + ToolCall for agentic tool-use (execute_python)
  workspace.py         — File I/O + git operations on workspace/
  markdown.py          — YAML frontmatter parsing, section extraction, critique helpers
  sandbox.py           — Python script execution with timeout
  metrics.py           — MetricsTracker (token counts, tool calls, alerts, Markdown rendering)
  agents/
    base.py            — BaseAgent ABC with template method + retry + tool-use dispatch
    orchestrator.py    — Plans tasks, integrates proposed changes
    researcher.py      — Derivations and reasoning
    computationalist.py — Agentic code execution via execute_python tool, verdict writing
    critic.py          — Adversarial review, critique counting
    compressor.py      — File size management
  prompts/             — Static .md system prompt files (one per agent, plus verifier)
tests/                 — pytest tests (markdown, sandbox, metrics, orchestrator, computationalist, verify, workspace, task, engine, validation, report_recommendations, conversation_log, config)
problems/
  tier1/               — 10 core problem definitions
  tier2/               — 12 advanced problem definitions
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
- **Computationalist** uses `execute_python` tool via agentic loop — writes code, sees output, iterates on errors, emits COMPUTATION_LOG entry with VERDICT
- **Deep Critic** appends critiques to CRITIQUE_LOG.md
- **Compressor** archives + shrinks files exceeding size thresholds

The orchestrator integrates proposed changes on its next pass. After each orchestrator pass, `validation.py` runs invariant checks (ER promotion gate, phantom labels/references, agent routing, ID consistency). Termination via `TERMINATE` goes through `can_terminate()` gates (critic pass required, no unresolved HIGH critiques, numerical verification required when `requires_numerical: true` in problem YAML). The engine's `_apply_overrides()` consolidates all pre-dispatch overrides (budget, stale loop, forced critic, REFUTED recompute, stall blocking) in explicit priority order. Audit logging (JSONL) records metadata for every LLM call.

### Valid Task Types

The orchestrator emits one of these task types (defined in `TaskType` enum): `research`, `derive`, `compute`, `critique`, `resolve`, `synthesize`, `terminate`.

## Conventions

- `call_llm` is a stateless function for one-shot agents; `run_agent_loop` handles tool-use agents
- `AgentResult` (tool-use) is distinct from `LLMResponse` (one-shot) — accumulates tokens across rounds
- Tasks are typed via `Task` dataclass (in `task.py`) with `TaskType` enum — no untyped dicts
- Agent prompts are static `.md` files loaded at runtime — no templating
- YAML frontmatter parsing always falls back to regex on failure — never crash the loop
- Workspace git is managed by the scaffolding loop, not by agents
- BaseAgent `tools` class attribute: non-empty → agentic loop, empty → one-shot `call_llm`
- Critique regex constants (`CRIT_ID_RE`, `CRIT_HEADER_RE`, `CRIT_UNRESOLVED_RE`) and helpers (`extract_resolved_critique_ids`, `recount_critique_metadata`) are in `markdown.py`
- Critique ID format: `CRIT-NNN` (regex also accepts `CRITIQUE-NNN` for LLM drift tolerance)
- Engine overrides live in `_apply_overrides()` with explicit priority: budget > stale loop > forced critic > REFUTED recompute > stall blocking > enrichment
- Post-integration checks are pure functions in `validation.py` returning `list[Violation]`; violations inject into orchestrator context via `context_prefix`
- `run_agent_loop` forces a text-only final call on `max_rounds` exhaustion (no more empty stubs); `stop_reason="max_rounds_forced"`
- Problem YAMLs may include `requires_numerical: true/false` — consumed by `can_terminate()` gate

## Running

```bash
# Install
uv sync --extra dev

# Tests
uv run python -m pytest -v

# Run (requires ANTHROPIC_API_KEY in .env or env var)
uv run python -m sciralph.main problems/tier1/hawking_temperature.yaml --max-iterations 5

# Verify a completed workspace (uses Claude Opus by default)
uv run python -m sciralph.verify workspaces/<run_dir>/ --write-report
uv run python -m sciralph.verify workspaces/<run_dir>/ --rerun-computations --write-report

# Run + verify in one command
./run_and_verify.sh problems/tier1/hawking_temperature.yaml --max-iterations 10
./run_and_verify.sh problems/tier1/qho_thermodynamics.yaml -- --rerun-computations
```

## Current Status

All core functionality is implemented and working (365 tests passing). Phase 2 engine hardening complete + report recommendations implemented:

- **Core loop** — all five agents, main loop, orchestrator integration, consolidated override chain (`_apply_overrides`), termination gates (`can_terminate`)
- **Validation pipeline** — 7 post-integration checks (phantom references, ER promotion gate, phantom labels, stale unverified label promotion, verified frontmatter backfill, agent routing, ID consistency), violation injection into orchestrator context
- **Orchestrator** — sub-problem decomposition, integration duty, critique resolution (multi-line capture), stale-iteration backstop, momentum/compute-first/single-target-compute/stall-detection rules, inline synthesis (writes `## Synthesis` directly into RESEARCH_STATE.md then emits terminate), context prefix for violations/blockers
- **Computationalist** — agentic tool-use with `execute_python`, forced partial output on truncation, 3-valued verdict system (VERIFIED / REFUTED / INCONCLUSIVE), COMP-only counter
- **Deep Critic** — two-phase format, preamble stripping, self-retraction filtering, INCONCLUSIVE severity cap
- **Compressor** — archival + compression with forced compression at 2x threshold
- **Tool-use infrastructure** — `run_agent_loop` with forced text-only final call on `max_rounds` or zero-text bailout, checkpoint message at round N, per-computation token alert, stall detection (threshold=2)
- **Verification** — independent verification script (Claude Opus, streaming), `run_and_verify.sh` convenience wrapper
- **Logging** — JSONL audit logging (metadata per LLM call, round field for tool-use), full conversation logs

Next steps: `read_file` tool for orchestrator/researcher/critic (see PLAN.md future work)
