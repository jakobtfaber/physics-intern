# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics. Uses iterative LLM calls with structured state in `ResearchState` and a layered review stack. Markdown files are write-only snapshots for git history and verification. Supports multiple LLM providers (Anthropic, OpenAI, Google Gemini, HuggingFace) via a provider abstraction layer.

## Key Documents

- `README.md` — User-facing overview, architecture diagram, quick start
- `CODEBASE.md` — Extensive developer-oriented codebase reference (architecture, data flow, LLM failure compensation catalog, known issues). Use it only if you want a deep dive into the code; otherwise, this CLAUDE.md, README.md and inline code comments should suffice.

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

### Agent Details

- **Surveyor** runs before the main loop (iteration 0) to produce background notes. Can be re-invoked mid-loop via `task_type: survey`.
- **Planner** runs after the surveyor at iteration 0 to produce the initial research strategy. One-shot, no tools. Stores `parsed_strategy` in ResearchState.strategy.
- **Orchestrator** mutates ResearchState via 10 tools (add/update/abandon/promote hypothesis, resolve critique, update section, append note, add/resolve research question, set next task), emits CURRENT_TASK.md. Integrates evidence results from the EVIDENCE RESULTS banner. Maintains Conventions, Situation Assessment, and Strategy sections, and append-only Research Notes. Strategy is initially set by the planner at iteration 0; the orchestrator can update Strategy via `update_section` when evidence forces a pivot. Sees the background survey in its context.
- **Researcher** (`researcher`) — one-shot analytical reasoning with structured JSON output (no tools), matching the Reviewer and Deep Critic pattern. Parses `{reasoning, result, method, confidence, summary}` JSON from response text (target comes from `task.target_claim`). Builds `Evidence(type="research")` and stores on target RQ or WH; falls back to partial evidence on parse failure.
- **Computer** (`computer`) — computational work via code: `document_approach` + `execute_python` + `submit_result` + `report_progress`. Must call `document_approach` before first `execute_python` (enforced: tool removed from available set after first call via `active_tools` property). Builds `Evidence(type="compute")` with approach, scripts, and output.
- **Reviewer** (`reviewer`) — adversarial review, one-shot with structured JSON output (no tools). Gets focused context (WH + per-script `<computation>` blocks with purpose/code/output + original RQ + established results + conventions), NOT the full research state. Parses `{verdict, summary, details}` JSON from response text; builds `ReviewResult` stored on target WH.
- **Deep Critic** — one-shot strategic review with structured JSON output (no tools). Auto-triggered by the engine after a VERIFIED review when at least `critic_every_n` iterations have passed since the last critic run. NOT dispatchable by the orchestrator. Gets dedicated context via `render_critic_context()` (high-level: strategy, conventions, situation assessment, research notes, hypothesis summaries, dead ends, background survey, previous critiques — no derivations/scripts). Parses `{summary, details, critiques}` JSON from response text; focuses on research strategy, inter-result coherence, and systematic issues — NOT per-claim verification (that's the reviewer's job). Writes Critique objects to ResearchState.
- **Compressor** archives + shrinks files exceeding size thresholds.
- **Formatter** produces clean ANSWER.md from final research state (dispatched on successful termination).

After each orchestrator pass, `validation.py` runs post-integration checks on ResearchState directly (not markdown files). `validate_post_integration(research_state, *, iteration=0, workspace=None)` runs 4 checks: `check_er_demotion_safety` (demotes ER when `h.review.verdict == "REFUTED"`), `check_phantom_labels`, `check_stale_unverified_labels`, `check_critique_resolution_consistency`. Hypothesis promotion (WH→ER) is auto-performed by the engine after a VERIFIED review when dependencies are satisfied (`_auto_promote` in engine.py); the orchestrator's `promote_hypothesis` tool is a fallback for cases where auto-promotion was skipped due to unestablished dependencies. Termination via `TERMINATE` goes through `can_terminate()` gates. The deep critic auto-triggers post-dispatch when the latest task was a VERIFIED review and `critic_every_n` iterations have elapsed since the last critic run. Auto-review triggers in two ways: (1) `add_hypothesis` sets `task_data` with `task_type: "review"` so the engine dispatches the reviewer immediately after WH creation; (2) the engine's `_should_auto_review()` detects when a researcher/computer deposits evidence on a WH with a review older than the newest evidence, and auto-dispatches a re-review.

### Valid Task Types

The orchestrator emits one of these task types: `research`, `compute`, `terminate`; `review` is auto-triggered by `add_hypothesis` (which sets `task_data` with `task_type: "review"`) and by the engine after new evidence on a WH with a stale review. The `TaskType` enum (in `task.py`) also includes `critique`, `survey`, `plan`, `format` — used internally by the engine but not dispatchable by the orchestrator. The `format` task type is dispatched automatically by the engine on successful termination; `critique` is auto-triggered after VERIFIED reviews. `TASK_TYPE_AGENT_MAP` routes: RESEARCH→"researcher", COMPUTE→"computer", REVIEW→"reviewer", CRITIQUE→"deep_critic", SURVEY→"surveyor", PLAN→"planner".

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
- `_build_context_suffix()` emits 7 banner sections (consumed once then cleared): violations → termination blockers → evidence results → verified hypotheses → verification results → deep critic result → agent failures
- Post-integration checks are pure functions in `validation.py` taking `research_state: ResearchState` and returning `list[Violation]`; 4 checks total; validation uses `h.review.verdict` instead of scanning separate computation objects
- Agents render context from `self.research_state` via renderers — no file read-back from disk
- MD files (RESEARCH_STATE.md, EVIDENCE_LOG.md, CRITIQUE_LOG.md) are write-only for git snapshots and verify.py — rendered once per iteration by `_render_files_for_git()` in engine.py
- `run_agent_loop` checks `tool_executor.active_tools` each round for dynamic tool switching (used by ToolExecutor to remove `document_approach` after first call); empty end-turn recovery retries until `max_rounds` (C1); forced final call always includes exit tool with up to 3 retry attempts (C1); context-aware exit tool names in warnings (C2); `loop_exit_reason` tracking (C5); progress check injection after `progress_check_interval` consecutive `execute_python` rounds
- `submit_result` (computer only) uses the `stop_after_round` mechanism — executor sets `stop_after_round = True`, loop detects it and returns `stop_reason="executor_stop"`; `process_response` creates `Evidence` objects on target entity; Researcher uses one-shot structured JSON (no tools) — `process_response` parses JSON directly from response text
- Reviewer is one-shot (no tools): `process_response` parses structured JSON from response text via `_parse_review_json()`, creates `ReviewResult` on target WH; falls back to INCONCLUSIVE on parse failure
- `execute_python` accepts optional `filename` param — scripts saved as `{counter}_{sanitized}.py`; full output persisted to `.output` companion file before truncation; structured header prepended; `_script_names` tracks all script names; NameError in stderr appends FRESH PROCESS reminder
- `document_approach` — computer calls before first `execute_python`; `_approach_documented` flag prevents repeat calls; `active_tools` property removes it from tool set after first use
- Orchestrator tools (`orchestrator_tools.py`) mutate `self.research_state` directly; 9 mutation tools plus 3 dispatch tools (`dispatch_researcher`, `dispatch_computer`, `request_termination`); `add_hypothesis` is also an exit tool (sets `stop_after_round = True` and populates `task_data` with a review task — the engine auto-dispatches the reviewer for the new WH); `add_hypothesis` requires `from_rq` (every WH must originate from an RQ with gathered evidence) and auto-copies evidence from RQ to new WH; the engine also auto-reviews WHs when a researcher/computer deposits evidence newer than an existing review (re-review after REFUTED); `promote_hypothesis` checks `h.review.verdict == "VERIFIED"` and established dependencies (mainly a fallback — the engine auto-promotes after VERIFIED reviews via `_auto_promote`)
- `_call_provider_with_retry()` wraps every provider call with exponential-backoff retry
- Iteration counter is scaffolding-maintained (`_update_research_iteration()`), not LLM-dependent
- See `CODEBASE.md` §7 for the complete LLM failure compensation catalog
