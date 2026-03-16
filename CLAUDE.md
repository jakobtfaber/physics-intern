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
  engine.py            — Main loop (LoopState): forced_critic_or_orchestrator → validate → enrich_compute → terminate_gate → dispatch → verdict_track → compress → git
  categories.py        — CompensationCategory enum (call_reliability, state_invariants, loop_control, output_normalization)
  validation.py        — Post-integration checks (ER demotion safety, phantom labels, routing) + termination gates
  verify.py            — Independent verification script (Claude Opus, streaming)
  config.py            — Config dataclass (model, provider, thresholds, timeouts)
  llm.py               — Provider-agnostic LLM wrapper (call_llm, run_agent_loop) with JSONL audit logging
  models.yaml          — Model registry (friendly keys → provider + model_id + env_key)
  task.py              — Task dataclass + TaskType enum for typed task handling
  tools.py             — ToolExecutor + ToolCall for agentic tool-use (execute_python, submit_verdict, submit_result, report_progress); tools_for_task_type() for dynamic tool sets
  critic_tools.py      — CriticToolExecutor + submit_critique/finish_review tools for agentic critic
  renderers.py         — Snapshot renderers (render_research_state_md, render_computation_log_md, render_critique_log_md) and per-agent context renderers (render_orchestrator_context, render_researcher_context, render_computationalist_context, render_critic_context)
  computation_index.py — JSONL helpers for COMPUTATION_INDEX.jsonl (read/write/query computation records)
  critique_index.py    — JSONL helpers for CRITIQUE_INDEX.jsonl (read/write/query critique records)
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
    computationalist.py — Agentic code execution via execute_python + submit_verdict/submit_result + report_progress tools; writes COMPUTATION_INDEX.jsonl alongside COMPUTATION_LOG.md; process_response routes to _format_explore_entry, _format_verify_entry, or _format_inconclusive_stub
    critic.py          — Adversarial review via submit_critique/finish_review tools (agentic, uses CriticToolExecutor)
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

Five agents (orchestrator, researcher, computationalist, deep critic, compressor) take turns in a main loop. Each agent gets a fresh context per call. All state lives in Markdown files with YAML frontmatter under `workspaces/<run>/` (each run gets a timestamped subdirectory like `workspaces/20260313_142530_hawking_temperature_claude-sonnet-4-6/`; override with `--workspace-dir`). `ResearchState` (`research_state.py`) is the structured graph representation — contains `problem_statement`, `conventions`, `open_questions`, `status`, `title` (top-level), plus `hypotheses`, `computations` (with `kind`, `confidence`, `notes`, `result` fields), `critiques`, `failed_approaches`. `renderers.py` can produce Markdown files from state (snapshot renderers) and per-agent context strings (context renderers).

- **Orchestrator** mutates ResearchState via tools (add/update/abandon/promote hypothesis, resolve critique, update section), renders state → RESEARCH_STATE.md + CRITIQUE_LOG.md, emits CURRENT_TASK.md
- **Researcher** writes PROPOSED_CHANGES.md (never modifies RESEARCH_STATE directly)
- **Computationalist** uses `execute_python` and `submit_verdict`/`submit_result` tools via agentic loop — writes Computation objects to ResearchState, renders COMPUTATION_LOG.md from state
- **Deep Critic** uses `submit_critique`/`finish_review` tools (agentic) — writes Critique objects to ResearchState, renders CRITIQUE_LOG.md from state
- **Compressor** archives + shrinks files exceeding size thresholds

The orchestrator integrates proposed changes on its next pass. After each orchestrator pass, `validation.py` runs post-integration checks. Hypothesis promotion (WH→ER) is handled by the orchestrator's `promote_hypothesis` tool (uses ResearchState queries for guardrails), not by automatic validation. Termination via `TERMINATE` goes through `can_terminate()` gates. Forced critic is a pre-orchestrator check in `run()`. Compute dispatch routes to explore mode (`compute_explore`) or verify mode (`compute_verify`); explore results are signaled to the orchestrator via `pending_explore_results` in LoopState; non-VERIFIED verify verdicts go into a COMPUTATION VERDICTS banner. Compute enrichment (`_enrich_compute_task_with_prior_failures`) uses ResearchState queries. ResearchState is authoritative — agents mutate it via tools, then render to Markdown for git snapshots and agent context. `_sync_research_state()` just saves JSON, no rebuild from markdown. All LLM calls go through `_call_provider_with_retry()` with exponential-backoff retry. Audit logging (JSONL) records metadata + cost for every LLM call.

### Valid Task Types

The orchestrator emits one of these task types (defined in `TaskType` enum): `research`, `derive`, `compute` (backward compat), `compute_explore`, `compute_verify`, `critique`, `resolve`, `synthesize`, `terminate`. `set_next_task` in `orchestrator_tools.py` accepts all three compute variants.

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
- Critique regex constants (`CRIT_ID_RE`, `CRIT_HEADER_RE`, `CRIT_UNRESOLVED_RE`) and helpers (`recount_critique_metadata`, `ensure_critique_metadata_consistent`) are in `markdown.py`
- Critique ID format: `CRIT-NNN` (regex also accepts `CRITIQUE-NNN` for LLM drift tolerance)
- Inter-iteration state is consolidated in `LoopState` dataclass under `self._state` (claim_failure_count, last_content_iteration, pending_violations, pending_termination_blockers, pending_compute_verdicts, pending_explore_results, agent_failures)
- `_track_computation()` dispatches to explore or verify handling: explore results go into `_state.pending_explore_results` (rendered in context prefix as EXPLORE RESULTS banner); non-VERIFIED verify verdicts go into `_state.pending_compute_verdicts` (COMPUTATION VERDICTS banner); maintains per-claim failure counters (`_state.claim_failure_count`); appends to `_state.agent_failures`; no auto-recompute or stall escalation
- `_dispatch()` returns `(agent_name, result)` tuple; `_record_agent_failures()` inspects the result for `max_tokens`, `max_rounds_forced` stop reasons and appends to `_state.agent_failures`
- `_build_context_prefix()` emits 5 banner sections (consumed once then cleared): violations → termination blockers → computation verdicts → explore results → agent failures
- Post-integration checks are pure functions in `validation.py` returning `list[Violation]`; 7 checks total including critique resolution consistency; violations inject into orchestrator context via `context_prefix` (except `er_demotion_safety` demotions, which are enforced silently by state rewrite to prevent re-promotion churn)
- `run_agent_loop` forces a single text-only final call on `max_rounds` exhaustion via agent-agnostic user message (system prompt unchanged); empty text is honest failure; `stop_reason="max_rounds_forced"`; agent-agnostic warnings at `max_rounds-2` and `max_rounds-1` (no agent-specific references); progress check injection after `progress_check_interval` (default 3) consecutive `execute_python` rounds injects a user message requiring `report_progress` tool call
- `submit_verdict` (verify mode, accepts `target_id` param) and `submit_result` (explore mode) both use the `stop_after_round` mechanism (same as orchestrator's `set_next_task`) — executor sets `stop_after_round = True`, loop detects it and returns `stop_reason="executor_stop"`; `process_response` creates `Computation` objects in ResearchState, renders COMPUTATION_LOG.md from state
- `submit_critique` and `finish_review` (critic tools) use the same `stop_after_round` mechanism — `CriticToolExecutor` accumulates structured critiques, `process_response` creates `Critique` objects in ResearchState, renders CRITIQUE_LOG.md from state
- Orchestrator tools (`orchestrator_tools.py`) mutate `self.research_state` directly — no regex surgery on markdown; `process_response` renders state → markdown via `renderers.py`
- `tools_for_task_type(task_type)` in `tools.py` returns the appropriate tool set for a given task type (explore vs. verify vs. default)
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

All core functionality is implemented and working (670 tests passing):

- **Core loop** — all five agents, main loop, orchestrator integration, forced critic pre-check, compute verdict signaling, termination gates (`can_terminate`)
- **Validation pipeline** — 7 post-integration checks (phantom references, ER demotion safety, phantom labels, stale unverified label promotion, verified frontmatter backfill, agent routing, ID consistency, critique resolution consistency), violation injection into orchestrator context; WH→ER promotion via orchestrator's `promote_hypothesis` tool
- **LLM failure compensation** — 50+ mechanisms across 4 categories compensating for predictable LLM failures (see `CODEBASE.md` §7)
- **Multi-provider support** — `providers/` abstraction layer with Anthropic, OpenAI, Google Gemini, HuggingFace adapters; `models.yaml` registry with cost tracking; `--model`/`--provider` CLI flags; `verify.py` stays Anthropic-only
- **API resilience** — exponential-backoff retry on transient errors + tool-call JSON failures (`_call_provider_with_retry`); dispatch-level error catch; scaffolding-maintained iteration counter
- **Orchestrator** — sub-problem decomposition, integration duty, critique resolution (4-pattern extraction), stale-iteration backstop, inline synthesis, context prefix for violations/blockers
- **Computationalist** — agentic tool-use with `execute_python` (requires `purpose` param) + `submit_verdict`/`submit_result` (structured exit paths for verify/explore modes) + `report_progress` (progress check tool); dynamic tool sets via `tools_for_task_type()`; writes COMPUTATION_INDEX.jsonl alongside COMPUTATION_LOG.md; forced partial output on truncation, 3-valued verdict system (VERIFIED / REFUTED / INCONCLUSIVE), two-round escalating warnings, progress check injection
- **Deep Critic** — agentic tool-use with `submit_critique`/`finish_review` tools (via `CriticToolExecutor`); writes Critique objects to ResearchState; `_no_critiques_filed` flag for clean review signaling
- **Verification** — independent verification script (Claude Opus, streaming), `run_and_verify.sh` convenience wrapper
- **Logging** — JSONL audit logging (metadata + cost per LLM call, round field for tool-use), full conversation logs
- **Scaffolding log** — `EVENT_LOG.jsonl` instrumentation across all 4 categories; every compensation mechanism emits structured events via `log_scaffold_event()` and LLM calls via `log_llm_call()` for profiling which mechanisms actually fire per model

Next steps: Switch remaining agent `build_context()` methods to use renderers (currently still read markdown files); full validation simplification to use ResearchState queries; delete `computation_index.py`/`critique_index.py` once validation is updated; simplify `markdown.py` by removing functions only used by eliminated code paths
