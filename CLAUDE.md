# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics. Uses iterative LLM calls with structured state in `ResearchState` and a layered verification stack. Markdown files are write-only snapshots for git history and verification. Supports multiple LLM providers (Anthropic, OpenAI, Google Gemini, HuggingFace) via a provider abstraction layer.

## Key Documents

- `README.md` — User-facing overview, architecture diagram, quick start
- `CODEBASE.md` — Developer-oriented codebase reference (architecture, data flow, LLM failure compensation catalog, known issues)
- `PLAN.md` — Future work ideas and roadmap

## Project Structure

```
src/sciralph/
  main.py              — Entry point (reads problem YAML, CLI flags)
  engine.py            — Main loop (LoopState): forced_critic_or_orchestrator → validate → enrich_compute → terminate_gate → dispatch → verdict_track → compress → git
  research_state.py    — ResearchState dataclass: authoritative structured state (hypotheses, research_questions, computations, critiques, failed_approaches, research_plan); query/mutation methods; JSON serialization (RESEARCH_GRAPH.json). Entities: Hypothesis (with depends_on, promotion_justification fields), ResearchQuestion (RQ-NNN, RQStatus: open/resolved/abandoned), Computation, Critique, FailedApproach, SubProblem, ResearchPlan
  renderers.py         — Snapshot renderers (state → RESEARCH_STATE.md, COMPUTATION_LOG.md, CRITIQUE_LOG.md via _render_files_for_git()) and agent context builders (render_orchestrator_context, render_computationalist_context, render_critic_context, render_computation_log_tail)
  orchestrator_tools.py — OrchestratorToolExecutor: 10 state-mutation tools (add/update/abandon/promote hypothesis, resolve critique, update section, add/resolve research question, record dead end, set next task)
  critic_tools.py      — CriticToolExecutor + submit_critique/finish_review tools for agentic critic
  tools.py             — ToolExecutor + ToolCall for computationalist (execute_python, submit_verdict/submit_result, report_progress); tools_for_task_type() for dynamic tool sets (TOOL_DEFINITIONS, EXPLORE_TOOLS, RESEARCH_VERIFY_TOOLS); exit_tool_name property; task_type parameter
  categories.py        — CompensationCategory enum (call_reliability, state_invariants, loop_control, output_normalization)
  validation.py        — Post-integration checks (4 checks) + termination gates
  verify.py            — Independent verification script (Claude Opus, streaming)
  config.py            — Config dataclass (model, provider, thresholds, timeouts)
  llm.py               — Provider-agnostic LLM wrapper (call_llm, run_agent_loop) with JSONL audit logging
  models.yaml          — Model registry (friendly keys → provider + model_id + env_key)
  task.py              — Task dataclass + TaskType enum + TASK_TYPE_AGENT_MAP for typed task handling and agent dispatch
  providers/
    __init__.py        — create_provider() factory + re-exports
    base.py            — LLMProvider ABC + ProviderResponse dataclass
    anthropic.py       — Anthropic Claude adapter
    openai.py          — OpenAI adapter
    google.py          — Google Gemini adapter
    huggingface.py     — HuggingFace Inference Providers adapter
  workspace.py         — File I/O + git operations on workspace/ + log_scaffold_event() + log_llm_call() (no validate_comp_references)
  markdown.py          — YAML frontmatter parsing, section extraction, critique helpers
  sandbox.py           — Python script execution with timeout
  metrics.py           — MetricsTracker (token counts, tool calls, alerts, Markdown rendering)
  agents/
    base.py            — BaseAgent ABC with template method + retry + tool-use dispatch
    orchestrator.py    — Plans tasks, mutates ResearchState via OrchestratorToolExecutor tools
    computationalist.py — Base agentic code execution; writes Computation objects to ResearchState
    compute_verify.py  — ComputeVerifyAgent (inherits ComputationalistAgent): verify mode (execute_python + submit_verdict + report_progress)
    compute_explore.py — ComputeExploreAgent (inherits ComputationalistAgent): explore mode (execute_python + submit_result + report_progress)
    research_verify.py — ResearchVerifyAgent (inherits ComputationalistAgent): research_verify mode (submit_verdict + report_progress, no execute_python)
    research_explore.py — ResearchExploreAgent (inherits ComputationalistAgent): analytical exploration mode (submit_result + report_progress, no execute_python)
    critic.py          — Agentic adversarial review via submit_critique/finish_review tools (CriticToolExecutor); writes Critique objects to ResearchState
    compressor.py      — File size management
    formatter.py       — Produces ANSWER.md from final research state (one-shot)
    strategist.py      — Strategic research planner: decomposes problem into sub-problems with approaches and pitfalls (one-shot)
  prompts/             — Static .md system prompt files (one per agent, plus verifier): orchestrator.md, computationalist.md, compute_verify.md, compute_explore.md, research_verify.md, research_explore.md, deep_critic.md, compressor.md, formatter.md, strategist.md, verifier.md, process_auditor.md
tests/                 — ~727 pytest tests across 24+ files (engine, validation, markdown, llm_retry, report_recommendations, verify, orchestrator, orchestrator_tools, tools, config, computationalist, critic_tools, renderers, research_state, workspace, provider_smoke, huggingface_repair, task, metrics, conversation_log, reasoning_tokens, sandbox, scaffold_log, strategist)
problems/
  tier1/               — 10 core problem definitions
  tier2/               — 12 advanced problem definitions
  critpt/              — Critical-path problems (quantum error correction decomposition)
run_and_verify.sh      — Run a problem then verify results in one command
```

## Tech Stack

- Python 3.12+, `uv` for dependency management
- `anthropic` SDK (required), optional: `openai`, `google-genai`, `huggingface-hub`
- `rich` for console, `pyyaml`, `sympy`, `numpy`, `scipy`, `matplotlib`
- Tests: `pytest` (run with `uv run python -m pytest -v`, need `--extra dev`)

## Architecture

Nine agent roles (strategist, orchestrator, research_explore, compute_verify, compute_explore, research_verify, deep critic, compressor, formatter) take turns in a main loop. The strategist runs once before the main loop (and can be re-invoked mid-loop), following a 2x2 dispatch matrix:

|               | Explore (RQ → WH)    | Verify (WH → ER)     |
|---------------|----------------------|----------------------|
| **Reasoning** | research_explore     | research_verify      |
| **Code**      | compute_explore      | compute_verify       |

Each agent gets a fresh context per call, built from `self.research_state` via renderers (no file read-back from disk). All authoritative state lives in `ResearchState` (`research_state.py`) — contains `problem_statement`, `conventions`, `status`, `title` (top-level), plus `hypotheses` (with `depends_on`, `promotion_justification` fields), `research_questions` (RQ-NNN, with `RQStatus`: open/resolved/abandoned), `computations` (with `kind`, `confidence`, `notes`, `result` fields), `critiques`, `failed_approaches`, `research_plan` (ResearchPlan with SubProblem entities, produced by strategist). Markdown files (RESEARCH_STATE.md, COMPUTATION_LOG.md, CRITIQUE_LOG.md) under `workspaces/<run>/` (each run gets a timestamped subdirectory like `workspaces/20260313_142530_hawking_temperature_claude-sonnet-4-6/`; override with `--workspace-dir`) are write-only snapshots for git history and `verify.py` — rendered once per iteration by `_render_files_for_git()` in engine.py. `renderers.py` produces both file snapshots and per-agent context strings. Entity numbering is unified: RQ, WH, and ER share a single counter so the same number tracks a claim through its lifecycle (RQ-003 → WH-003 → ER-003).

- **Strategist** runs before the main loop (iteration 0) to decompose the problem into sub-problems (SP-NNN) with approaches, alternatives, and known pitfalls; produces a `ResearchPlan` in ResearchState and seeds initial RQs and dead ends. Can be re-invoked mid-loop via `task_type: strategize` when the orchestrator detects strategic stall.
- **Orchestrator** mutates ResearchState via tools (add/update/abandon/promote hypothesis, resolve critique, update section, add/resolve research question, record dead end), emits CURRENT_TASK.md. Integrates explore results from the EXPLORE RESULTS banner. Sees the research plan in its context.
- **ResearchExploreAgent** (`research_explore`) — analytical exploration, derivation, critique resolution: `submit_result` + `report_progress` (no `execute_python`); inherits ComputationalistAgent
- **ComputeExploreAgent** (`compute_explore`) — exploratory computation via code: `execute_python` + `submit_result` + `report_progress`; inherits ComputationalistAgent
- **ResearchVerifyAgent** (`research_verify`) — analytical verification without code: `submit_verdict` + `report_progress` (no `execute_python`); inherits ComputationalistAgent
- **ComputeVerifyAgent** (`compute_verify`) — numerical verification via code: `execute_python` + `submit_verdict` + `report_progress`; inherits ComputationalistAgent
- **Deep Critic** uses `submit_critique`/`finish_review` tools (agentic) — writes Critique objects to ResearchState
- **Compressor** archives + shrinks files exceeding size thresholds
- **Formatter** produces clean ANSWER.md from final research state (dispatched on successful termination)

After each orchestrator pass, `validation.py` runs post-integration checks on ResearchState directly (not markdown files). `validate_post_integration(research_state, *, iteration=0, workspace=None)` runs 4 checks: `check_er_demotion_safety`, `check_phantom_labels`, `check_stale_unverified_labels`, `check_critique_resolution_consistency`. Hypothesis promotion (WH→ER) is handled by the orchestrator's `promote_hypothesis` tool — requires a VERIFIED computation with kind in {verify, research_verify}, and blocks promotion when unestablished dependencies exist (via `depends_on` field). Termination via `TERMINATE` goes through `can_terminate()` gates. Forced critic is a pre-orchestrator check in `run()`. `TASK_TYPE_AGENT_MAP` routes task types to agent names: COMPUTE/COMPUTE_VERIFY→compute_verify, COMPUTE_EXPLORE→compute_explore, RESEARCH_EXPLORE→research_explore, RESEARCH_VERIFY→research_verify, CRITIQUE→deep_critic, STRATEGIZE→strategist. Explore results (from both compute_explore and research_explore) are signaled to the orchestrator via `pending_explore_results` in LoopState; non-VERIFIED verify verdicts go into a COMPUTATION VERDICTS banner; failed explore results are suppressed (noise reduction). Compute enrichment (`_enrich_compute_task_with_prior_failures`) uses ResearchState queries. ResearchState is authoritative — agents mutate it via tools, then renderers produce context strings for the next agent and file snapshots for git. `_sync_research_state()` just saves JSON (also called on termination), no rebuild from markdown. All LLM calls go through `_call_provider_with_retry()` with exponential-backoff retry. Audit logging (JSONL) records metadata + cost for every LLM call.

### Valid Task Types

The orchestrator emits one of these task types (defined in `TaskType` enum): `research_explore`, `compute_explore`, `compute_verify`, `research_verify`, `critique`, `strategize`, `terminate`, `format`. The `format` task type is dispatched automatically by the engine on successful termination. The `strategize` task type re-invokes the strategist for mid-loop re-planning.

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
- Post-integration checks are pure functions in `validation.py` taking `research_state: ResearchState` and returning `list[Violation]`; 4 checks total (ER demotion safety, phantom labels, stale unverified labels, critique resolution consistency); violations inject into orchestrator context via `context_prefix` (except `er_demotion_safety` demotions, which are enforced silently by state rewrite to prevent re-promotion churn)
- Agents render context from `self.research_state` via renderers — no file read-back from disk; `render_computation_log_tail(state, n)` in renderers.py provides recent computation context
- MD files (RESEARCH_STATE.md, COMPUTATION_LOG.md, CRITIQUE_LOG.md) are write-only for git snapshots and verify.py — rendered once per iteration by `_render_files_for_git()` in engine.py
- The orchestrator has `self.research_state` (set by engine before each call); `_update_research_iteration()` and `_set_research_status()` only update ResearchState (no file read-modify-write)
- `run_agent_loop` forces a single text-only final call on `max_rounds` exhaustion via agent-agnostic user message (system prompt unchanged); empty text is honest failure; `stop_reason="max_rounds_forced"` (produces `zero_output`); agent-agnostic warnings at `max_rounds-2` and `max_rounds-1` use context-aware exit tool names (C2); empty end-turn recovery (C1) re-prompts the model when it produces an end_turn with no text and no tool calls; `loop_exit_reason` tracking (C5) records why the loop terminated; progress check injection after `progress_check_interval` (default 3) consecutive `execute_python` rounds injects a user message requiring `report_progress` tool call
- `submit_verdict` (verify/research_verify mode, accepts `target_id` param) and `submit_result` (explore mode) both use the `stop_after_round` mechanism (same as orchestrator's `set_next_task`) — executor sets `stop_after_round = True`, loop detects it and returns `stop_reason="executor_stop"`; `process_response` creates `Computation` objects in ResearchState; `ToolExecutor.exit_tool_name` property returns the context-appropriate exit tool name based on `task_type`
- `submit_critique` and `finish_review` (critic tools) use the same `stop_after_round` mechanism — `CriticToolExecutor` accumulates structured critiques, `process_response` creates `Critique` objects in ResearchState
- Orchestrator tools (`orchestrator_tools.py`) mutate `self.research_state` directly — no regex surgery on markdown; 10 tools total including `add_research_question`, `resolve_research_question`, and `record_dead_end` for RQ lifecycle and dead-end tracking
- `tools_for_task_type(task_type)` in `tools.py` returns the appropriate tool set for a given task type (explore vs. verify vs. research_verify vs. default); RESEARCH_VERIFY_TOOLS = [submit_verdict, report_progress] (no execute_python)
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

All core functionality is implemented and working (~668 tests passing):

- **Core loop** — nine agent roles (strategist, orchestrator, research_explore, compute_verify, compute_explore, research_verify, deep critic, compressor, formatter) following a 2x2 dispatch matrix (reasoning/code × explore/verify), main loop with strategist pre-pass (iteration 0), orchestrator integration via EXPLORE RESULTS banner, forced critic pre-check, compute verdict signaling, termination gates (`can_terminate`), `_sync_research_state` on termination (A3); unified entity numbering (RQ/WH/ER share one counter, `next_entity_num()`); `_render_files_for_git()` consolidates all MD file writes; strategic stall heuristic (`_should_suggest_replan`) injects banner when 3+ abandoned hypotheses with 0 established results
- **Validation pipeline** — 4 post-integration checks operating on ResearchState directly (ER demotion safety, phantom labels, stale unverified labels, critique resolution consistency), violation injection into orchestrator context; WH→ER promotion via orchestrator's `promote_hypothesis` tool with dependency-aware guardrails (blocks on unestablished `depends_on`), requires VERIFIED computation with kind in {verify, research_verify}
- **LLM failure compensation** — 50+ mechanisms across 4 categories compensating for predictable LLM failures (see `CODEBASE.md` §7)
- **Multi-provider support** — `providers/` abstraction layer with Anthropic, OpenAI, Google Gemini, HuggingFace adapters; `models.yaml` registry with cost tracking; `--model`/`--provider` CLI flags; `verify.py` stays Anthropic-only
- **API resilience** — exponential-backoff retry on transient errors + tool-call JSON failures (`_call_provider_with_retry`); dispatch-level error catch; scaffolding-maintained iteration counter
- **Orchestrator** — sub-problem decomposition, integration duty, critique resolution (4-pattern extraction), stale-iteration backstop, inline synthesis, context prefix for violations/blockers; 10 tools including `add_research_question`/`resolve_research_question`/`record_dead_end` for RQ lifecycle and dead-end tracking; `problem_statement` populated on init (A1); `total_computations` counts all entries (A2)
- **Specialized agents** — four agents (ComputeVerifyAgent, ComputeExploreAgent, ResearchVerifyAgent, ResearchExploreAgent) inheriting from ComputationalistAgent; agentic tool-use with dynamic tool sets via `tools_for_task_type()`; `exit_tool_name` property for context-aware exit; writes Computation objects to ResearchState; context built from `self.research_state` via renderers (no file read-back); forced partial output on truncation, 3-valued verdict system (VERIFIED / REFUTED / INCONCLUSIVE), context-aware escalating warnings (C2), progress check injection; failed explore results suppressed (C3), zero_output collapsed in COMPUTATION_LOG (C4), zero_output for max_rounds_forced (A4)
- **Deep Critic** — agentic tool-use with `submit_critique`/`finish_review` tools (via `CriticToolExecutor`); writes Critique objects to ResearchState; `_no_critiques_filed` flag for clean review signaling
- **Verification** — independent verification script (Claude Opus, streaming), `run_and_verify.sh` convenience wrapper
- **Logging** — JSONL audit logging (metadata + cost per LLM call, round field for tool-use), full conversation logs
- **Scaffolding log** — `EVENT_LOG.jsonl` instrumentation across all 4 categories; every compensation mechanism emits structured events via `log_scaffold_event()` and LLM calls via `log_llm_call()` for profiling which mechanisms actually fire per model; `executor_stop_signal` and `orchestrator_tool_mutations` events removed for noise reduction (E1)
- **LLM loop resilience** — empty end-turn recovery (C1), context-aware exit tool names in warnings (C2), `loop_exit_reason` tracking (C5)

Next steps: Simplify `markdown.py` by removing functions only used by eliminated code paths (e.g. `count_unresolved_critiques`, `_parse_comp_entries`, `detect_computation_stalls`, `find_prior_failures_for_claim`); remove legacy `computationalist` agent name from dispatch
