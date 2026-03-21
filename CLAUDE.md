# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics. Uses iterative LLM calls with structured state in `ResearchState` and a layered review stack. Markdown files are write-only snapshots for git history and verification. Supports multiple LLM providers (Anthropic, OpenAI, Google Gemini, HuggingFace) via a provider abstraction layer.

## Key Documents

- `README.md` — User-facing overview, architecture diagram, quick start
- `CODEBASE.md` — Developer-oriented codebase reference (architecture, data flow, LLM failure compensation catalog, known issues)
- `PLAN.md` — Future work ideas and roadmap

## Project Structure

```
src/sciralph/
  main.py              — Entry point (reads problem YAML, CLI flags)
  engine.py            — Main loop (LoopState): forced_critic_or_orchestrator → validate → enrich_compute → terminate_gate → dispatch → result_track → compress → git
  research_state.py    — ResearchState dataclass: authoritative structured state (hypotheses, research_questions, critiques, failed_approaches, background_survey); query/mutation methods; JSON serialization (RESEARCH_GRAPH.json). Entities: Hypothesis (with depends_on, promotion_justification, evidence, review fields), ResearchQuestion (RQ-NNN, RQStatus: open/resolved/abandoned, evidence field), Evidence, ReviewResult, Critique, FailedApproach, BackgroundSurvey
  renderers.py         — Snapshot renderers (state → RESEARCH_STATE.md, EVIDENCE_LOG.md, CRITIQUE_LOG.md via _render_files_for_git()) and agent context builders (render_orchestrator_research_state, render_orchestrator_critique_log, render_critic_context, render_background_survey)
  orchestrator_tools.py — OrchestratorToolExecutor: 12 state-mutation tools (add/update/abandon/promote hypothesis, resolve critique, update section, append note, add/resolve research question, record dead end, set next task)
  tools.py             — ToolExecutor + ToolCall for researcher/computer (document_approach, execute_python with optional `filename` param, submit_result with optional `evidence_scripts`, report_progress); tools_for_task_type() for dynamic tool sets; `active_tools` property for round-based tool switching; `_sanitize_filename()`, `_save_output_file()`, `_script_names` tracking; structured output headers; `.output` companion files; NameError detection hint
  categories.py        — CompensationCategory enum (call_reliability, state_invariants, loop_control, output_normalization)
  validation.py        — Post-integration checks (4 checks) + termination gates
  verify.py            — Independent verification script (Claude Opus, streaming)
  config.py            — Config dataclass (model, provider, thresholds, timeouts)
  llm.py               — Provider-agnostic LLM wrapper (call_llm, run_agent_loop) with JSONL audit logging; checks tool_executor.active_tools each round for dynamic tool switching
  models.yaml          — Model registry (friendly keys → provider + model_id + env_key)
  task.py              — Task dataclass + TaskType enum + TASK_TYPE_AGENT_MAP for typed task handling and agent dispatch; structured dispatch fields (background, method_hints, assumptions, relevant_results)
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
    base.py            — BaseAgent ABC with template method + retry + tool-use dispatch; `max_tool_rounds` class var override
    orchestrator.py    — Plans tasks, mutates ResearchState via OrchestratorToolExecutor tools
    planner.py         — One-shot strategy agent; produces initial research strategy at iteration 0, stored in ResearchState.strategy
    researcher.py      — Analytical reasoning agent; writes Evidence objects to RQ/WH in ResearchState
    computer.py        — Computational agent with code execution; writes Evidence objects to RQ/WH in ResearchState
    reviewer.py        — Adversarial review agent; writes ReviewResult to WH in ResearchState; uses ReviewerToolExecutor
    critic.py          — One-shot strategic review with structured JSON output; writes Critique objects to ResearchState
    compressor.py      — File size management
    formatter.py       — Produces ANSWER.md from final research state (one-shot)
    surveyor.py        — Background surveyor: produces background notes mapping the research landscape (one-shot)
  prompts/             — Static .md system prompt files (one per agent): orchestrator.md, planner.md, researcher.md, computer.md, reviewer.md, deep_critic.md, compressor.md, formatter.md, surveyor.md, verifier.md (independent verify script), process_auditor.md
tests/                 — ~851 pytest tests across 24+ files
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

Nine agent roles (surveyor, planner, orchestrator, researcher, computer, reviewer, deep critic, compressor, formatter) take turns in a main loop. The surveyor runs once before the main loop (and can be re-invoked mid-loop), and the planner runs once after the surveyor at iteration 0. Three core agents handle evidence production and review:

| Agent | Role | Tools |
|-------|------|-------|
| **Researcher** | Analytical reasoning, derivation (one-shot, no tools, structured JSON output) | none (one-shot structured JSON output) |
| **Computer** | Computational work via code | document_approach, execute_python, submit_result, report_progress |
| **Reviewer** | Adversarial review (no code) | none (one-shot structured JSON output) |

Each agent gets a fresh context per call, built from `self.research_state` via renderers (no file read-back from disk). All authoritative state lives in `ResearchState` (`research_state.py`) — contains `problem_statement`, `conventions`, `strategy`, `situation_assessment`, `research_notes`, `status`, `title` (top-level), plus `hypotheses` (with `depends_on`, `promotion_justification`, `evidence`, `review` fields), `research_questions` (RQ-NNN, with `RQStatus`: open/resolved/abandoned, `evidence` field), `critiques`, `failed_approaches`, `background_survey` (BackgroundSurvey with background notes, produced by surveyor). Markdown files (RESEARCH_STATE.md, EVIDENCE_LOG.md, CRITIQUE_LOG.md) under `workspaces/<run>/` are write-only snapshots for git history and `verify.py` — rendered once per iteration by `_render_files_for_git()` in engine.py.

### Research Lifecycle

**RQ → evidence → WH → review → ER**

1. Orchestrator creates RQ, dispatches to researcher or computer
2. Agent produces Evidence (reasoning or code+output), stored on the RQ
3. Orchestrator formulates WH from evidence (`add_hypothesis` with `from_rq` auto-copies evidence)
4. Orchestrator dispatches to reviewer with focused context (WH + evidence + light state)
5. Reviewer submits review, stored on the WH
6. If VERIFIED: orchestrator promotes WH → ER

Entity numbering is unified: RQ, WH, and ER share a single counter so the same number tracks a claim through its lifecycle (RQ-003 → WH-003 → ER-003).

### Entity Dataclasses

- `Evidence` — `type` (research/compute), `reasoning`, `approach` (from document_approach), `scripts` (list, filtered to `evidence_scripts` when provided), `script_purposes` (dict mapping script name → purpose string), `output`, `method`, `result`, `confidence` (exact/approximate/partial), `summary` (one-sentence for banners), `iteration`
- `ReviewResult` — `verdict` (VERIFIED/REFUTED/INCONCLUSIVE), `summary`, `details`, `iteration`
- `Hypothesis` — `id`, `statement`, `status` (HypothesisStatus: WORKING/ESTABLISHED/REFUTED/ABANDONED), `derivation`, `critiques`, `iteration_created`, `iteration_modified`, `depends_on` (list of hypothesis IDs), `promotion_justification`, `evidence: Evidence | None`, `review: ReviewResult | None`
- `ResearchQuestion` — `id` (RQ-NNN), `question`, `context`, `resolved_to` (list of hypothesis IDs), `status` (RQStatus: OPEN/RESOLVED/ABANDONED), `iteration_created`, `iteration_resolved`, `evidence: Evidence | None`
- `Critique` — `id`, `targets`, `severity` (HIGH/MEDIUM/LOW — kept for backward compat with existing JSON, no longer used for gating), `argument`, `status` (ACTIVE/RESOLVED/WITHDRAWN), `resolution`, `iteration_filed`, `iteration_resolved`
- `FailedApproach` — `description`, `reason`, `related_entities`, `iteration`, `derivation_excerpt`

### Agent Details

- **Surveyor** runs before the main loop (iteration 0) to produce background notes. Can be re-invoked mid-loop via `task_type: survey`.
- **Planner** runs after the surveyor at iteration 0 to produce the initial research strategy. One-shot, no tools. Stores `parsed_strategy` in ResearchState.strategy.
- **Orchestrator** mutates ResearchState via 12 tools (add/update/abandon/promote hypothesis, resolve critique, update section, append note, add/resolve research question, record dead end, set next task), emits CURRENT_TASK.md. Integrates evidence results from the EVIDENCE RESULTS banner. Maintains Conventions and Situation Assessment sections, and append-only Research Notes. Strategy is set by the planner at iteration 0; the orchestrator's `update_section` tool does not accept "Strategy". Sees the background survey in its context.
- **Researcher** (`researcher`) — one-shot analytical reasoning with structured JSON output (no tools), matching the Reviewer and Deep Critic pattern. Parses `{reasoning, result, method, confidence, summary}` JSON from response text (target comes from `task.target_claim`). Builds `Evidence(type="research")` and stores on target RQ or WH; falls back to partial evidence on parse failure.
- **Computer** (`computer`) — computational work via code: `document_approach` + `execute_python` + `submit_result` + `report_progress`. Must call `document_approach` before first `execute_python` (enforced: tool removed from available set after first call via `active_tools` property). Builds `Evidence(type="compute")` with approach, scripts, and output.
- **Reviewer** (`reviewer`) — adversarial review, one-shot with structured JSON output (no tools). Gets focused context (WH + per-script `<computation>` blocks with purpose/code/output + original RQ + established results + conventions), NOT the full research state. Parses `{verdict, summary, details}` JSON from response text; builds `ReviewResult` stored on target WH.
- **Deep Critic** — one-shot strategic review with structured JSON output (no tools). Gets dedicated context via `render_critic_context()` (high-level: strategy, conventions, situation assessment, research notes, hypothesis summaries, dead ends, background survey, previous critiques — no derivations/scripts). Parses `{summary, details, critiques}` JSON from response text; focuses on research strategy, inter-result coherence, and systematic issues — NOT per-claim verification (that's the reviewer's job). Writes Critique objects to ResearchState.
- **Compressor** archives + shrinks files exceeding size thresholds.
- **Formatter** produces clean ANSWER.md from final research state (dispatched on successful termination).

After each orchestrator pass, `validation.py` runs post-integration checks on ResearchState directly (not markdown files). `validate_post_integration(research_state, *, iteration=0, workspace=None)` runs 4 checks: `check_er_demotion_safety` (demotes ER when `h.review.verdict == "REFUTED"`), `check_phantom_labels`, `check_stale_unverified_labels`, `check_critique_resolution_consistency`. Hypothesis promotion (WH→ER) is handled by the orchestrator's `promote_hypothesis` tool — requires `h.review.verdict == "VERIFIED"` and blocks promotion when unestablished dependencies exist (via `depends_on` field). Termination via `TERMINATE` goes through `can_terminate()` gates. Forced critic is a pre-orchestrator check in `run()`.

### Valid Task Types

The orchestrator emits one of these task types (defined in `TaskType` enum): `research`, `compute`, `review`, `critique`, `survey`, `plan`, `terminate`, `format`. The `format` task type is dispatched automatically by the engine on successful termination. `TASK_TYPE_AGENT_MAP` routes: RESEARCH→"researcher", COMPUTE→"computer", REVIEW→"reviewer", CRITIQUE→"deep_critic", SURVEY→"surveyor", PLAN→"planner".

## Conventions

- `call_llm` is a stateless function for one-shot agents; `run_agent_loop` handles tool-use agents
- Both use `_get_provider(config)` which creates/caches an `LLMProvider` instance based on `config.provider`
- Provider adapters in `providers/` handle API-specific concerns: tool format transformation, message format, stop reason normalization
- Tool definitions use OpenAI canonical format (`type: "function"`, `function: {name, description, parameters}`); Anthropic adapter transforms to `input_schema` format
- `AgentResult` (tool-use) is distinct from `LLMResponse` (one-shot) — accumulates tokens across rounds
- Tasks are typed via `Task` dataclass (in `task.py`) with `TaskType` enum — no untyped dicts; structured dispatch fields (`background`, `method_hints`, `assumptions`, `relevant_results`) carry orchestrator context
- Agent prompts are static `.md` files loaded at runtime — no templating
- YAML frontmatter parsing always falls back to regex on failure — never crash the loop
- Workspace git is managed by the scaffolding loop, not by agents
- BaseAgent `tools` class attribute: non-empty → agentic loop, empty → one-shot `call_llm`
- Critique regex constants (`CRIT_ID_RE`, `CRIT_HEADER_RE`, `CRIT_UNRESOLVED_RE`) and helpers are in `markdown.py`
- Critique ID format: `CRIT-NNN` (regex also accepts `CRITIQUE-NNN` for LLM drift tolerance)
- Strategy critiques: critic can file with `target_id: "STRATEGY"`, validation skips non-hyphenated targets
- Inter-iteration state is consolidated in `LoopState` dataclass under `self._state` (claim_failure_count, last_content_iteration, pending_violations, pending_termination_blockers, pending_compute_verdicts, pending_verified_results, pending_explore_results, agent_failures)
- `_track_agent_result()` dispatches based on task type: RESEARCH/COMPUTE checks evidence on target entity and adds to `_state.pending_explore_results` (EVIDENCE RESULTS banner); REVIEW checks `h.review` — VERIFIED goes to `_state.pending_verified_results` (VERIFIED HYPOTHESES banner), non-VERIFIED goes to `_state.pending_compute_verdicts` (VERIFICATION RESULTS banner)
- `_dispatch()` returns `(agent_name, result)` tuple; `_record_agent_failures()` inspects the result for `max_tokens`, `max_rounds_forced` stop reasons
- `_build_context_prefix()` emits 6 banner sections (consumed once then cleared): violations → termination blockers → evidence results → verified hypotheses → verification results → agent failures
- Post-integration checks are pure functions in `validation.py` taking `research_state: ResearchState` and returning `list[Violation]`; 4 checks total; validation uses `h.review.verdict` instead of scanning separate computation objects
- Agents render context from `self.research_state` via renderers — no file read-back from disk
- MD files (RESEARCH_STATE.md, EVIDENCE_LOG.md, CRITIQUE_LOG.md) are write-only for git snapshots and verify.py — rendered once per iteration by `_render_files_for_git()` in engine.py
- `run_agent_loop` checks `tool_executor.active_tools` each round for dynamic tool switching (used by ToolExecutor to remove `document_approach` after first call); empty end-turn recovery retries until `max_rounds` (C1); forced final call always includes exit tool with up to 3 retry attempts (C1); context-aware exit tool names in warnings (C2); `loop_exit_reason` tracking (C5); progress check injection after `progress_check_interval` consecutive `execute_python` rounds
- `submit_result` (computer only) uses the `stop_after_round` mechanism — executor sets `stop_after_round = True`, loop detects it and returns `stop_reason="executor_stop"`; `process_response` creates `Evidence` objects on target entity; Researcher uses one-shot structured JSON (no tools) — `process_response` parses JSON directly from response text
- Reviewer is one-shot (no tools): `process_response` parses structured JSON from response text via `_parse_review_json()`, creates `ReviewResult` on target WH; falls back to INCONCLUSIVE on parse failure
- `execute_python` accepts optional `filename` param — scripts saved as `{counter}_{sanitized}.py`; full output persisted to `.output` companion file before truncation; structured header prepended; `_script_names` tracks all script names; NameError in stderr appends FRESH PROCESS reminder
- `document_approach` — computer calls before first `execute_python`; `_approach_documented` flag prevents repeat calls; `active_tools` property removes it from tool set after first use
- Orchestrator tools (`orchestrator_tools.py`) mutate `self.research_state` directly; 12 tools total including `append_note` for research notes and `update_section` supporting Conventions and Situation Assessment (not Strategy — strategy is written by the planner agent, not the orchestrator); two-phase dispatch gate in `_set_next_task` rejects if entity-creating mutations occurred in same response; `add_hypothesis` with `from_rq` auto-copies evidence from RQ to new WH; `promote_hypothesis` checks `h.review.verdict == "VERIFIED"` and established dependencies
- `_call_provider_with_retry()` wraps every provider call with exponential-backoff retry
- Iteration counter is scaffolding-maintained (`_update_research_iteration()`), not LLM-dependent
- See `CODEBASE.md` §7 for the complete LLM failure compensation catalog

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

All core functionality is implemented and working (~851 tests passing):

- **Core loop** — eight agent roles (surveyor, planner, orchestrator, researcher, computer, reviewer, deep critic, compressor, formatter), main loop with surveyor pre-pass and planner strategy pass (iteration 0), orchestrator integration via EVIDENCE RESULTS banner, forced critic pre-check, review result signaling, termination gates (`can_terminate`), `_sync_research_state` on termination; unified entity numbering (RQ/WH/ER share one counter, `next_entity_num()`); `_render_files_for_git()` consolidates all MD file writes; stall heuristic (`_should_suggest_resurvey`) injects banner when 3+ abandoned hypotheses with 0 established results
- **Validation pipeline** — 4 post-integration checks operating on ResearchState directly (ER demotion safety via h.review.verdict, phantom labels, stale unverified labels, critique resolution consistency), violation injection into orchestrator context; WH→ER promotion via orchestrator's `promote_hypothesis` tool with dependency-aware guardrails, requires VERIFIED review result
- **LLM failure compensation** — 50+ mechanisms across 4 categories compensating for predictable LLM failures (see `CODEBASE.md` §7)
- **Multi-provider support** — `providers/` abstraction layer with Anthropic, OpenAI, Google Gemini, HuggingFace adapters; `models.yaml` registry with cost tracking; `--model`/`--provider` CLI flags; `verify.py` stays Anthropic-only
- **API resilience** — exponential-backoff retry on transient errors + tool-call JSON failures (`_call_provider_with_retry`); dispatch-level error catch; scaffolding-maintained iteration counter
- **Orchestrator** — integration duty, critique resolution, context prefix for violations/blockers/evidence/verdicts; 12 tools including `append_note`, `update_section` (Conventions/Situation Assessment only — strategy written by planner), `add_research_question`/`resolve_research_question`/`record_dead_end` for RQ lifecycle; structured dispatch with background/method_hints/assumptions/relevant_results
- **Evidence agents** — Researcher (analytical, no code) uses one-shot structured JSON output (no tools), matching Reviewer/Deep Critic pattern; Computer (computational, with code) uses document_approach (one-shot, removed after use via active_tools) + execute_python + submit_result; both produce Evidence objects stored on target RQ or WH; dynamic tool sets via `tools_for_task_type()`
- **Reviewer** — one-shot adversarial review with structured JSON output; gets focused context (WH + per-script computation blocks + light state); parses `{verdict, summary, details}` from response text; produces ReviewResult stored on WH
- **Deep Critic** — one-shot structured JSON review via `render_critic_context()` + `_parse_critic_json()`; focuses on research direction and inter-result coherence; writes Critique objects to ResearchState; `_no_critiques_filed` flag for clean review signaling
- **Verification** — independent verification script (Claude Opus, streaming), `run_and_verify.sh` convenience wrapper
- **Logging** — JSONL audit logging (metadata + cost per LLM call, round field for tool-use), full conversation logs
- **Scaffolding log** — `EVENT_LOG.jsonl` instrumentation across all 4 categories
- **LLM loop resilience** — empty end-turn recovery retries until max_rounds (C1), forced final with exit tool + retries (C1), context-aware exit tool names in warnings (C2), `loop_exit_reason` tracking (C5), dynamic tool switching via `active_tools`
