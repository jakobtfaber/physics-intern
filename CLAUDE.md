# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics. Uses iterative LLM calls with externally persisted state in Markdown files and a layered verification stack. Supports multiple LLM providers (Anthropic, OpenAI, Google Gemini, HuggingFace) via a provider abstraction layer.

## Key Documents

- `README.md` — User-facing overview, architecture diagram, quick start
- `CODEBASE.md` — Developer-oriented codebase reference (architecture, data flow, LLM failure compensation catalog, known issues)
- `PLAN.md` — Future work ideas and roadmap

## Project Structure

```
src/sciralph/
  main.py              — Entry point (reads problem YAML, CLI flags)
  engine.py            — Main loop (LoopState, Override chain): orchestrate → validate → override → dispatch → compress → git
  categories.py        — CompensationCategory enum (call_reliability, state_invariants, loop_control, output_normalization)
  validation.py        — Post-integration checks (ER gate, phantom labels, routing) + termination gates
  verify.py            — Independent verification script (Claude Opus, streaming)
  config.py            — Config dataclass (model, provider, thresholds, timeouts)
  llm.py               — Provider-agnostic LLM wrapper (call_llm, run_agent_loop) with JSONL audit logging
  models.yaml          — Model registry (friendly keys → provider + model_id + env_key)
  task.py              — Task dataclass + TaskType enum for typed task handling
  tools.py             — ToolExecutor + ToolCall for agentic tool-use (execute_python, submit_verdict)
  providers/
    __init__.py        — create_provider() factory + re-exports
    base.py            — LLMProvider ABC + ProviderResponse dataclass
    anthropic.py       — Anthropic Claude adapter
    openai.py          — OpenAI adapter
    google.py          — Google Gemini adapter
    huggingface.py     — HuggingFace Inference Providers adapter
  workspace.py         — File I/O + git operations on workspace/ + log_scaffold_event() + log_llm_call()
  markdown.py          — YAML frontmatter parsing, section extraction, critique helpers
  sandbox.py           — Python script execution with timeout
  metrics.py           — MetricsTracker (token counts, tool calls, alerts, Markdown rendering)
  agents/
    base.py            — BaseAgent ABC with template method + retry + tool-use dispatch
    orchestrator.py    — Plans tasks, integrates proposed changes
    researcher.py      — Derivations and reasoning
    computationalist.py — Agentic code execution via execute_python + submit_verdict tools, verdict writing
    critic.py          — Adversarial review, critique counting
    compressor.py      — File size management
  prompts/             — Static .md system prompt files (one per agent, plus verifier)
tests/                 — pytest tests (engine, validation, markdown, llm_retry, report_recommendations, verify, orchestrator, tools, config, computationalist, workspace, provider_smoke, task, metrics, conversation_log, sandbox, scaffold_log)
problems/
  tier1/               — 10 core problem definitions
  tier2/               — 12 advanced problem definitions
run_and_verify.sh      — Run a problem then verify results in one command
```

## Tech Stack

- Python 3.12+, `uv` for dependency management
- `anthropic` SDK (required), optional: `openai`, `google-genai`, `huggingface-hub`
- `rich` for console, `pyyaml`, `sympy`, `numpy`, `scipy`, `matplotlib`
- Tests: `pytest` (run with `uv run python -m pytest -v`, need `--extra dev`)

## Architecture

Five agents (orchestrator, researcher, computationalist, deep critic, compressor) take turns in a main loop. Each agent gets a fresh context per call. All state lives in Markdown files with YAML frontmatter under `workspaces/<run>/` (each run gets a timestamped subdirectory like `workspaces/20260313_142530_hawking_temperature_claude-sonnet-4-6/`; override with `--workspace-dir`).

- **Orchestrator** reads all state, integrates proposed changes into RESEARCH_STATE.md, emits CURRENT_TASK.md
- **Researcher** writes PROPOSED_CHANGES.md (never modifies RESEARCH_STATE directly)
- **Computationalist** uses `execute_python` and `submit_verdict` tools via agentic loop — writes code, sees output, iterates on errors, calls `submit_verdict` for structured exit or emits free-text COMPUTATION_LOG entry with VERDICT
- **Deep Critic** appends critiques to CRITIQUE_LOG.md
- **Compressor** archives + shrinks files exceeding size thresholds

The orchestrator integrates proposed changes on its next pass. After each orchestrator pass, `validation.py` runs 8 invariant checks (ER promotion gate with bidirectional WH↔ER correction, phantom labels/references, agent routing, ID consistency, critique resolution consistency, verified frontmatter backfill). Termination via `TERMINATE` goes through `can_terminate()` gates (critic pass required when VERIFIED computations exist, no unresolved HIGH critiques, numerical verification required when `requires_numerical: true` in problem YAML). The engine's `_apply_overrides()` consolidates all pre-dispatch overrides (budget, stale loop, forced critic, redundant critic suppression, REFUTED recompute, stall blocking, prior-failure enrichment) in explicit P1–P6 priority order. All LLM calls go through `_call_provider_with_retry()` with exponential-backoff retry on transient errors and tool-call JSON failures. Audit logging (JSONL) records metadata + cost for every LLM call.

### Valid Task Types

The orchestrator emits one of these task types (defined in `TaskType` enum): `research`, `derive`, `compute`, `critique`, `resolve`, `synthesize`, `terminate`.

## Conventions

- `call_llm` is a stateless function for one-shot agents; `run_agent_loop` handles tool-use agents
- Both use `_get_provider(config)` which creates/caches an `LLMProvider` instance based on `config.provider`
- Provider adapters in `providers/` handle API-specific concerns: tool format transformation, message format, stop reason normalization
- Tool definitions use OpenAI canonical format (`type: "function"`, `function: {name, description, parameters}`); Anthropic adapter transforms to `input_schema` format
- `AgentResult` (tool-use) is distinct from `LLMResponse` (one-shot) — accumulates tokens across rounds
- Tasks are typed via `Task` dataclass (in `task.py`) with `TaskType` enum — no untyped dicts
- Agent prompts are static `.md` files loaded at runtime — no templating
- YAML frontmatter parsing always falls back to regex on failure — never crash the loop
- Workspace git is managed by the scaffolding loop, not by agents
- BaseAgent `tools` class attribute: non-empty → agentic loop, empty → one-shot `call_llm`
- Critique regex constants (`CRIT_ID_RE`, `CRIT_HEADER_RE`, `CRIT_UNRESOLVED_RE`) and helpers (`extract_resolved_critique_ids`, `recount_critique_metadata`, `ensure_critique_metadata_consistent`) are in `markdown.py`
- Critique ID format: `CRIT-NNN` (regex also accepts `CRITIQUE-NNN` for LLM drift tolerance)
- Engine overrides are declarative: `_OVERRIDE_CHAIN` is a list of `Override(name, priority, condition, action)` objects iterated by `_apply_overrides()`; priority ordering: P1 budget > P2 stale loop > P3 forced critic > P3b redundant critic suppression > P5 stall blocking > P4 REFUTED/INCONCLUSIVE recompute; P6 enrichment runs after the loop (non-overriding)
- Inter-iteration state is consolidated in `LoopState` dataclass under `self._state` (stale_iterations, pending_recompute_claim/verdict, stalled_claims, claim_failure_count, last_content_iteration, pending_violations, pending_termination_blockers, displaced_tasks, agent_failures)
- `_track_compute_verdict()` maintains per-claim failure counters (`_state.claim_failure_count`); escalates to `_state.stalled_claims` at `config.stall_recompute_limit` (default 2); below stall limit sets `_state.pending_recompute_claim` and `_state.pending_recompute_verdict` (actual verdict string) and appends to `_state.agent_failures` for orchestrator awareness
- `_dispatch()` returns `(agent_name, result)` tuple; `_record_agent_failures()` inspects the result for `max_tokens`, `max_rounds_forced` stop reasons and appends to `_state.agent_failures`
- `_build_context_prefix()` emits 4 banner sections (consumed once then cleared): violations → blockers → displaced tasks → agent failures
- Post-integration checks are pure functions in `validation.py` returning `list[Violation]`; 8 checks total including critique resolution consistency; violations inject into orchestrator context via `context_prefix` (except `er_promotion_gate` demotions, which are enforced silently by state rewrite to prevent re-promotion churn)
- `run_agent_loop` forces a text-only final call on `max_rounds` exhaustion (no more empty stubs); `stop_reason="max_rounds_forced"`; two-round escalating warnings at `max_rounds-2` and `max_rounds-1` mention `submit_verdict` as preferred exit; interleaved text checkpoints via `_make_text_checkpoint_call()` fire at `text_checkpoint_interval` (default 2) consecutive zero-text rounds to recover text before bailout; `_synthesize_from_tool_history()` replaces the hardcoded stub with actual tool output excerpts when both forced call and retry produce empty text
- `submit_verdict` tool uses the `stop_after_round` mechanism (same as orchestrator's `set_next_task`) — executor sets `stop_after_round = True`, loop detects it and returns `stop_reason="executor_stop"`; `process_response` extracts structured data from the tool call to format the COMP entry
- `_call_provider_with_retry()` wraps every provider call with exponential-backoff retry (configurable via `api_retry_max`, `api_retry_initial_delay`, `api_retry_max_delay`); tool-call JSON failures are retryable
- Iteration counter is scaffolding-maintained (`_update_research_iteration()`), not LLM-dependent
- Problem YAMLs may include `requires_numerical: true/false` — consumed by `can_terminate()` gate
- See `CODEBASE.md` §7 for the complete LLM failure compensation catalog (50+ mechanisms across 4 categories)

## Running

```bash
# Install
uv sync --extra dev

# Tests
uv run python -m pytest -v

# Run with Anthropic (default, requires ANTHROPIC_API_KEY)
uv run python -m sciralph.main problems/tier1/hawking_temperature.yaml --max-iterations 5

# Run with a different provider (auto-resolved from models.yaml)
uv sync --extra openai
uv run python -m sciralph.main problems/tier1/hawking_temperature.yaml --model gpt-4o --max-iterations 5

# Verify a completed workspace (uses Claude Opus by default)
uv run python -m sciralph.verify workspaces/<run_dir>/ --write-report
uv run python -m sciralph.verify workspaces/<run_dir>/ --rerun-computations --write-report

# Run + verify in one command
./run_and_verify.sh problems/tier1/hawking_temperature.yaml --max-iterations 10
./run_and_verify.sh problems/tier1/qho_thermodynamics.yaml -- --rerun-computations
```

## Current Status

All core functionality is implemented and working (660 tests passing):

- **Core loop** — all five agents, main loop, orchestrator integration, consolidated override chain (`_apply_overrides` P1–P6 + P3b), termination gates (`can_terminate`)
- **Validation pipeline** — 8 post-integration checks (phantom references, ER promotion gate with bidirectional WH↔ER, phantom labels, stale unverified label promotion, verified frontmatter backfill, agent routing, ID consistency, critique resolution consistency), violation injection into orchestrator context
- **LLM failure compensation** — 50+ mechanisms across 4 categories compensating for predictable LLM failures (see `CODEBASE.md` §7)
- **Multi-provider support** — `providers/` abstraction layer with Anthropic, OpenAI, Google Gemini, HuggingFace adapters; `models.yaml` registry with cost tracking; `--model`/`--provider` CLI flags; `verify.py` stays Anthropic-only
- **API resilience** — exponential-backoff retry on transient errors + tool-call JSON failures (`_call_provider_with_retry`); dispatch-level error catch; scaffolding-maintained iteration counter
- **Orchestrator** — sub-problem decomposition, integration duty, critique resolution (4-pattern extraction), stale-iteration backstop, inline synthesis, context prefix for violations/blockers
- **Computationalist** — agentic tool-use with `execute_python` (requires `purpose` param) + `submit_verdict` (structured exit path), forced partial output on truncation, 3-valued verdict system (VERIFIED / REFUTED / INCONCLUSIVE), two-round escalating warnings
- **Deep Critic** — two-phase format, preamble stripping, self-retraction filtering, INCONCLUSIVE severity cap, NO_CRITIQUES_FILED handling
- **Verification** — independent verification script (Claude Opus, streaming), `run_and_verify.sh` convenience wrapper
- **Logging** — JSONL audit logging (metadata + cost per LLM call, round field for tool-use), full conversation logs
- **Scaffolding log** — `EVENT_LOG.jsonl` instrumentation across all 4 categories; every compensation mechanism emits structured events via `log_scaffold_event()` and LLM calls via `log_llm_call()` for profiling which mechanisms actually fire per model

Next steps: `read_file` tool for orchestrator/researcher/critic (see PLAN.md future work)
