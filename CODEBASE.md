# SciRalph — Codebase Reference

> **Purpose of this document:** A developer-oriented map of the codebase as it exists today (March 2026). Covers architecture, data flow, configuration, the LLM failure compensation stack, and known issues.

## Table of Contents

- [1. Architecture Overview](#1-architecture-overview)
- [2. The Main Loop](#2-the-main-loop)
- [3. Agent System](#3-agent-system)
- [4. Infrastructure Layer](#4-infrastructure-layer)
- [5. Workspace Files & Data Flow](#5-workspace-files--data-flow)
- [6. Configuration](#6-configuration)
- [7. LLM Failure Compensation](#7-llm-failure-compensation)
- [8. Verification](#8-verification)
- [9. Testing](#9-testing)
- [10. Known Issues](#10-known-issues)
- [11. Documentation Status](#11-documentation-status)

---

## 1. Architecture Overview

SciRalph is a multi-agent scaffolding system for autonomous scientific research in theoretical physics. Eight agent roles take turns in a main loop: researcher (analytical reasoning), computer (code execution), reviewer (adversarial review), deep critic (strategy review), surveyor, planner, compressor, and formatter. All research state lives in a structured `ResearchState` object — agents mutate it via tools, and Markdown files are rendered from it for git snapshots and agent context. LLM calls go through a provider abstraction layer supporting Anthropic, OpenAI, Google Gemini, and HuggingFace.

```
                        ┌──────────────────┐
                        │     main.py      │  CLI entry point
                        │  (parse args,    │  Loads problem YAML,
                        │   build config)  │  creates workspace name
                        └────────┬─────────┘
                                 │
                        ┌────────▼─────────┐
                        │    engine.py     │  SciRalph.run()
                        │   (main loop)    │  Iteration → dispatch → commit
                        └────────┬─────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                   │
     ┌────────▼────────┐ ┌──────▼──────┐ ┌──────────▼─────────┐
     │  agents/base.py │ │  llm.py     │ │  workspace.py      │
     │  (template      │ │  (provider- │ │  (file I/O, git)   │
     │   method)       │ │   agnostic) │ │                    │
     └────────┬────────┘ └──────┬──────┘ └────────────────────┘
              │                 │
     ┌────────▼────────────────▼───────────────────┐
     │                   Agents                     │
     │  orchestrator    researcher                  │
     │  computer        reviewer                    │
     │  critic          compressor                  │
     │  formatter       surveyor                    │
     │  planner                                     │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │         research_state.py                    │
     │  ResearchState: authoritative structured     │
     │  state (hypotheses, evidence, critiques)     │
     │  + renderers.py (state → Markdown)           │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │  orchestrator_tools.py  tools.py             │
     │  (Tool executors for agentic agents)         │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │              providers/                      │
     │  anthropic  openai  google  huggingface      │
     │  (LLMProvider ABC + ProviderResponse)        │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │              validation.py                   │
     │  Post-integration checks (4 checks)          │
     │  Termination gates (can_terminate)            │
     └─────────────────────────────────────────────┘
```

### Key design decisions

- **ResearchState as source of truth.** All research state lives in a structured `ResearchState` object (persisted as `RESEARCH_GRAPH.json`). Agents mutate state via tool calls, then Markdown files (`RESEARCH_STATE.md`, `EVIDENCE_LOG.md`, `CRITIQUE_LOG.md`) are rendered from the state for git snapshots and agent context.
- **Fresh context per call.** Agents are stateless — each call starts from a fresh context built from the current state. No conversation history is carried between iterations.
- **Three specialized work agents.** Researcher (analytical, no code), Computer (code execution), and Reviewer (adversarial review, no code). Each has a focused prompt, tool set, and context view.
- **Evidence inlined on entities.** Rather than a separate `Computation` entity, evidence is inlined as `Evidence` on `Hypothesis` and `ResearchQuestion`, and review is inlined as `ReviewResult` on `Hypothesis`.
- **Mandatory critic passes.** The scaffold forces critic reviews every N iterations, regardless of agent judgment.
- **Tool-based state mutation.** Two agents are agentic — orchestrator (10 tools via `OrchestratorToolExecutor`) and computer (4 tools via `ToolExecutor`, with dynamic tool switching via `active_tools`). Three agents use one-shot structured JSON output — researcher (`_parse_researcher_json()` in `agents/researcher.py`), reviewer (`_parse_review_json()` in `agents/reviewer.py`), and critic (`_parse_critic_json()` in `agents/critic.py`). Tool calls use the `stop_after_round` mechanism to signal completion.
- **Provider-agnostic.** LLM calls go through a `providers/` abstraction layer. Model selection is resolved via `models.yaml` registry (friendly key → provider + model_id + env_key + cost). The `verify.py` script is Anthropic-only.

### Source file map

| File | Lines | Purpose |
|------|------:|---------|
| `main.py` | 82 | CLI entry point, arg parsing, workspace naming (includes model label in dir name) |
| `engine.py` | 758 | `SciRalph` class, `LoopState` dataclass: main loop, dispatch, `_render_files_for_git()`, compression, scaffolding log events |
| `research_state.py` | 480 | `ResearchState` dataclass: authoritative structured state (hypotheses with evidence/review, research_questions, critiques, failed_approaches), query/mutation methods, JSON serialization |
| `renderers.py` | 351 | Snapshot renderers (state → `RESEARCH_STATE.md`, `EVIDENCE_LOG.md`, `CRITIQUE_LOG.md`) + `render_background_survey()` + `render_critic_context()` helpers |
| `orchestrator_tools.py` | 912 | `OrchestratorToolExecutor`: 11 state-mutation tools for orchestrator agent |
| `tools.py` | 478 | `ToolExecutor`, `ToolCall`, `execute_python` + `document_approach` + `submit_result` + `report_progress` tool schemas; `tools_for_task_type()`; `active_tools` property for dynamic tool switching |
| `categories.py` | 10 | `CompensationCategory` enum (call_reliability, state_invariants, loop_control, output_normalization) |
| `validation.py` | 312 | Post-integration checks (4 checks on ResearchState), `can_terminate()` gates, `Violation` dataclass |
| `config.py` | 169 | `Config` dataclass, 3-tier config builder, model resolution from `models.yaml` |
| `task.py` | 103 | `Task` dataclass, `TaskType` enum, `TASK_TYPE_AGENT_MAP`, YAML serialization |
| `llm.py` | 907 | Provider-agnostic LLM wrapper (`call_llm`, `run_agent_loop`), retry, logging, event log entries; checks `tool_executor.active_tools` each round for dynamic tool switching |
| `workspace.py` | 219 | File I/O, git ops, `log_scaffold_event()`, `log_llm_call()` |
| `markdown.py` | 507 | Frontmatter parsing, critique lifecycle, section utilities |
| `sandbox.py` | 49 | `subprocess.run` wrapper with timeout |
| `metrics.py` | 116 | `MetricsTracker`, `METRICS.md` rendering |
| `evaluate.py` | 274 | Answer evaluation for one-shot LLM responses (symbolic + numerical comparison) |
| `one_shot.py` | 417 | One-shot LLM baseline for comparing raw model capability against multi-agent scaffolding |
| `verify.py` | 1045 | Independent verification script (science + process audit) |
| `agents/base.py` | 163 | `BaseAgent` ABC, template method, retry logic, tool-use dispatch |
| `agents/orchestrator.py` | 190 | Agentic: state mutation via `OrchestratorToolExecutor`, emits `CURRENT_TASK.md` |
| `agents/researcher.py` | 98 | One-shot: analytical reasoning via `_parse_researcher_json()`; writes `Evidence` on target entity |
| `agents/computer.py` | 118 | Agentic: code execution via `ToolExecutor` (document_approach + execute_python + submit_result + report_progress); writes `Evidence` on target entity |
| `agents/reviewer.py` | 157 | One-shot: adversarial review via `_parse_review_json()`; writes `ReviewResult` on target hypothesis |
| `agents/critic.py` | 174 | One-shot: strategy/direction review via `_parse_critic_json()`, writes `Critique` objects to `ResearchState` |
| `agents/compressor.py` | 27 | One-shot: file size management |
| `agents/formatter.py` | 43 | One-shot: produces `ANSWER.md` from final research state |
| `agents/surveyor.py` | 45 | One-shot: produces background survey notes |
| `agents/planner.py` | — | Planner agent: produces initial research strategy from problem + background survey (one-shot) |
| `providers/__init__.py` | 24 | `create_provider()` factory + re-exports |
| `providers/base.py` | 83 | `LLMProvider` ABC + `ProviderResponse` dataclass |
| `providers/anthropic.py` | 159 | Anthropic Claude adapter |
| `providers/openai.py` | 109 | OpenAI adapter |
| `providers/google.py` | 155 | Google Gemini adapter |
| `providers/huggingface.py` | 367 | HuggingFace Inference Providers adapter |
| `models.yaml` | ~100 | Model registry (friendly keys → provider, model_id, env_key, cost) |
| **Total** | **~9,435** | |

---

## 2. The Main Loop

**File:** `engine.py` — `SciRalph.run()`

Before the main loop begins, two agents run at iteration 0: the **surveyor** produces background survey notes, and then the **planner** produces the initial research strategy from the problem statement and background survey. The loop then runs `while self.iteration < self.config.max_iterations`, incrementing `self.iteration` at the **top** of each pass (so iteration 1 is the first real turn). Each iteration follows this sequence:

```
┌─── Iteration N ──────────────────────────────────────────────────────┐
│                                                                      │
│  1. UPDATE ITERATION COUNTER (scaffolding-maintained, not LLM)       │
│     └─ _update_research_iteration(): update iteration on ResearchState│
│                                                                      │
│  2. FORCED CRITIC OR ORCHESTRATOR PASS                               │
│     └─ If critic overdue → skip orchestrator, go straight to critic  │
│        (saves an LLM call vs. old override chain)                    │
│     └─ Otherwise: orchestrator pass                                  │
│        └─ context_prefix: violations/blockers/verdicts/agent failures│
│        └─ Reads all state, integrates evidence results               │
│        └─ Emits CURRENT_TASK.md                                      │
│                                                                      │
│  3. POST-INTEGRATION VALIDATION                                      │
│     └─ validate_post_integration(): 4 checks                        │
│     └─ Violations queued for next orchestrator pass                  │
│                                                                      │
│  4. COMPUTE TASK ENRICHMENT (inline, before dispatch)                │
│     └─ If COMPUTE task + prior failures on same claim → append       │
│        failure excerpts to CURRENT_TASK.md body                      │
│                                                                      │
│  5. TERMINATION GATE                                                 │
│     └─ TERMINATE → can_terminate() gate                              │
│        └─ Allowed → run formatter → set status completed → break     │
│        └─ Blocked → continue (blockers shown next pass)              │
│                                                                      │
│  6. DISPATCH to researcher / computer / reviewer / critic / formatter│
│     └─ Wrapped in try/except for transient API errors                │
│     └─ _record_agent_failures(): capture max_tokens/max_rounds/etc.  │
│                                                                      │
│  7. POST-DISPATCH CHECKS                                             │
│     └─ _track_agent_result(): for RESEARCH/COMPUTE checks evidence   │
│        on target entity → pending_explore_results;                   │
│        for REVIEW checks h.review →                            │
│        VERIFIED → pending_verified_results,                          │
│        non-VERIFIED → pending_compute_verdicts                       │
│     └─ _record_agent_failures(): max_tokens/max_rounds detection     │
│                                                                      │
│  8. RENDER + COMPRESSION + METRICS + STATE SYNC + GIT COMMIT         │
│     └─ _render_files_for_git(): render MD files from state for git   │
│     └─ _sync_research_state(): normalize references, save JSON       │
│                                                                      │
│  ── EVENT LOG (cross-cutting) ────────────────────────────────────── │
│     Steps 1–8 emit events to EVENT_LOG.jsonl whenever a              │
│     compensation mechanism fires or an LLM call completes (see §7).  │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Pre-dispatch hooks

Two hooks run inline in `run()` before dispatch, replacing the old override chain:

| Hook | Condition | Action |
|------|-----------|--------|
| Forced critic | `_critic_overdue()` — more than `critic_every_n` since last critic AND new content exists | Skip orchestrator entirely, dispatch critic directly (saves an LLM call) |
| Compute enrichment | `_enrich_compute_task_with_prior_failures()` — COMPUTE task with prior failures on same claim | Append failure excerpts to task body |

Non-VERIFIED review verdicts are no longer auto-recomputed. Instead they are stored in `pending_compute_verdicts` (with `notes` and `failure_detail`) and rendered as a REVIEW RESULTS banner in the orchestrator's next context. VERIFIED verdicts go to `pending_verified_results` and render as a VERIFIED HYPOTHESES banner. Evidence results from researcher/computer go to `pending_explore_results` and render as an EVIDENCE RESULTS banner. The orchestrator decides what to do (re-review, re-derive, promote, or accept provisionally). Stall warnings appear when attempts reach `stall_recompute_limit`.

### Termination paths

| Path | Where | Condition |
|------|-------|-----------|
| Explicit terminate | Step 5 | Orchestrator emits `terminate` → `can_terminate()` gate passes |
| Status field | Step 9 | Agent wrote `status: completed/abandoned/partially_complete` |
| Max iterations | Loop condition | `self.iteration >= self.config.max_iterations` |
| Budget-aware synthesis | Orchestrator | Orchestrator sees budget pressure via `_completion_analysis()` context banner and chooses to synthesize/terminate |

The `can_terminate()` gate requires: at least one VERIFIED hypothesis triggers a mandatory critic pass, all RQs resolved or abandoned, and all WHs either verified and promoted or abandoned. If blocked, blockers are fed back to orchestrator.

### Dispatch routing

Dispatch follows `TASK_TYPE_AGENT_MAP` (in `task.py`):

| TaskType | Agent | Notes |
|----------|-------|-------|
| `research` | `researcher` | Analytical exploration and derivation; stores `Evidence` on target entity |
| `compute` | `computer` | Computational work via code; stores `Evidence` on target entity |
| `review` | `reviewer` | Adversarial review; stores `ReviewResult` on target hypothesis |
| `critique` | `deep_critic` | Strategy/direction review; post-dispatch: `_no_critiques_filed` detection |
| `format` | `formatter` | Dispatched automatically on successful termination |
| `terminate` | `orchestrator` | Handled by engine termination gate |
| `survey` | `surveyor` | Background survey / mid-loop resurvey |
| `plan` | `planner` | Initial research strategy; runs once at iteration 0 after surveyor |

---

## 3. Agent System

### BaseAgent template method (`agents/base.py`)

All agents inherit from `BaseAgent` and implement two abstract methods:

```python
class BaseAgent(ABC):
    name: str = "base"
    prompt_file: str = ""
    tools: ClassVar[list[dict]] = []   # empty → one-shot, non-empty → agentic

    def build_context(self, task: Task, iteration: int) -> str
    def process_response(self, response: LLMResponse | AgentResult, task: Task, iteration: int)
```

The `run()` template method:
1. Calls `build_context()` (subclass fills in)
2. Branches on `self.tools`:
   - **Empty** → `_call_with_retry()` → `call_llm()` → returns `LLMResponse`
   - **Non-empty** → `_call_with_tools()` → `run_agent_loop()` → returns `AgentResult`
3. Calls `process_response()` (subclass writes files)

The `tools` class attribute is the **single switch** between one-shot and agentic behavior. Two agents are agentic: orchestrator (13 tools via `OrchestratorToolExecutor`: 9 mutation + 4 dispatch) and computer (4 tools via `ToolExecutor`, with `active_tools` dynamic switching). Seven agents are one-shot: researcher (structured JSON via `_parse_researcher_json()`), reviewer (structured JSON via `_parse_review_json()`), critic (structured JSON via `_parse_critic_json()`), surveyor, planner, compressor, and formatter.

All agentic tool executors use the `stop_after_round` mechanism: a terminal tool (dispatch tools for the orchestrator, `submit_result` for the computer) sets `stop_after_round = True`, which the agent loop detects and returns with `stop_reason="executor_stop"`. The orchestrator has four dispatch tools (`dispatch_researcher`, `dispatch_computer`, `dispatch_reviewer`, `request_termination`), each with tailored parameters. (Only the computer agent now uses `submit_result` via this mechanism; the researcher uses one-shot JSON output instead.)

**No retry on truncation:** `_call_with_retry` returns immediately when `stop_reason == "max_tokens"` (no retries). The engine's `_record_agent_failures()` detects the truncation and injects a CAPACITY EXCEEDED banner into the orchestrator's next context via `_build_context_prefix()`, prompting task decomposition.

### ResearchState (`research_state.py`)

The authoritative source of truth for all research state. Agents mutate it via tools; Markdown files are rendered from it.

**Entity dataclasses:**
- `Evidence` — `type` ("research" or "compute"), `reasoning`, `approach` (computer's document_approach output), `scripts` (list of script filenames), `output` (code execution output summary), `method`, `result`, `confidence` (exact/approximate/partial), `iteration`. Inlined on `Hypothesis` and `ResearchQuestion`.
- `ReviewResult` — `verdict` (VERIFIED/REFUTED/INCONCLUSIVE), `summary`, `details`, `iteration`. Inlined on `Hypothesis`.
- `Hypothesis` — `id`, `statement`, `status` (`HypothesisStatus`: WORKING/ESTABLISHED/REFUTED/ABANDONED), `derivation`, `critiques` (list of CRIT IDs), `iteration_created`, `iteration_modified`, `depends_on` (list of hypothesis IDs), `promotion_justification`, `evidence: Evidence | None`, `review: ReviewResult | None`
- `ResearchQuestion` — `id` (RQ-NNN), `question`, `context`, `resolved_to` (list of hypothesis IDs), `status` (`RQStatus`: OPEN/RESOLVED/ABANDONED), `iteration_created`, `iteration_resolved`, `resolution_reason`, `evidence: Evidence | None`
- `Critique` — `id`, `targets`, `severity` (`Severity`: HIGH/MEDIUM/LOW — kept for backward compat with existing JSON, no longer used for gating), `argument`, `status` (`CritiqueStatus`: ACTIVE/RESOLVED/WITHDRAWN), `resolution`, `iteration_filed`, `iteration_resolved`
- `FailedApproach` — `description`, `reason`, `related_entities`, `iteration`, `derivation_excerpt`

**ResearchState fields:** `hypotheses` (dict by ID), `research_questions` (dict by ID), `critiques` (dict by ID), `failed_approaches` (list), `critic_clean_reviews` (list), `iteration`, `problem_statement`, `conventions`, `strategy`, `situation_assessment`, `research_notes` (list of dicts), `status`, `title`, `background_survey` (BackgroundSurvey | None)

**Key query methods:** `has_verified_evidence()`, `hypotheses_with_evidence()`, `active_critiques_for()`, `established_hypotheses()`, `working_hypotheses()`, `abandoned_hypotheses()`, `failures_for_hypothesis()`, `open_research_questions()`

**Mutation methods:** `promote_hypothesis(wh_id)` → renames WH-NNN → ER-NNN, updates status, calls `normalize_references()`; `demote_hypothesis(er_id)` → reverse (used by validation demotion safety)

**Serialization:** `to_json()`/`from_json()` → persisted as `RESEARCH_GRAPH.json`

### Renderers (`renderers.py`)

**Snapshot renderers** (produce full Markdown files from state):
- `render_research_state_md(state)` → `RESEARCH_STATE.md` (problem statement, conventions, WH/ER sections, dead ends, open questions)
- `render_evidence_log_md(state)` → `EVIDENCE_LOG.md` (evidence and review on hypotheses, sorted by iteration)
- `render_critique_log_md(state)` → `CRITIQUE_LOG.md` (active/resolved/withdrawn sections with frontmatter counts)

**Helpers:** `render_background_survey(state)` — returns the background survey section as a string. `render_critic_context(state, iteration)` — returns a high-level strategic view for the deep critic (strategy, conventions, situation assessment, research notes, RQ list, hypothesis summaries with evidence/review one-liners, dead ends, background survey, previous critiques — no derivations, scripts, reasoning, or approach text).

### Agent-by-agent summary

#### Orchestrator (`agents/orchestrator.py` + `orchestrator_tools.py`)

**Role:** Planning and state mutation. Mutates `ResearchState` via tools, emits `CURRENT_TASK.md`. (MD files are rendered centrally by the engine.)

**Tools** (11, via `OrchestratorToolExecutor`):
- `add_hypothesis` — creates new WH-NNN in state, auto-assigns ID; optional `from_rq` param links to originating RQ (copies evidence from RQ)
- `update_hypothesis` — updates statement/derivation for existing WH/ER
- `abandon_hypothesis` — marks as ABANDONED, records in `failed_approaches`
- `promote_hypothesis` — promotes WH → ER with guardrails (requires `h.review.verdict == "VERIFIED"`, blocks on unestablished `depends_on`)
- `dismiss_critique` — dismisses a critique as wrong/inapplicable, marks as RESOLVED
- `accept_critique` — accepts a critique as valid, marks as RESOLVED; optionally creates an RQ from the findings with carried-over evidence
- `update_section` — replaces content of Conventions, Situation Assessment, or Strategy
- `append_note` — appends a research note to `research_notes` list
- `add_research_question` — creates new RQ-NNN for open-ended exploration targets
- `abandon_research_question` — marks RQ as abandoned (dead end); reason required
- `dispatch_researcher` — dispatches researcher with target_claim (required), description, background, method_hints, assumptions, relevant_results; triggers `stop_after_round`
- `dispatch_computer` — dispatches computer with same params as researcher; triggers `stop_after_round`
- `dispatch_reviewer` — dispatches reviewer with target_claim (WH-only, required), description, background; triggers `stop_after_round`
- `request_termination` — requests loop termination with optional reason; triggers `stop_after_round`

**Context (largest in the system):**
- `context_prefix` from engine — violations, termination blockers, evidence results, verified hypotheses, review results, agent failures (6 consumed-once banners)
- Completion analysis banner (if ER count sufficient, or budget pressure) — includes synthesis instruction
- Full research state, critique log, and metrics — all rendered from `self.research_state` via renderers (not from file reads)

**Output processing:** Only processes if `_tool_executor.mutations_applied` is true. Writes `CURRENT_TASK.md` from dispatch tool data. (MD files are rendered centrally by the engine's `_render_files_for_git()`, not by individual agents.)

**Prompt rules (key):** COMPUTE-FIRST (new hypotheses get review before critique); converged derivation → move to review; stall loops → escalate or downgrade; critiques don't block promotion (severity is informational only).

#### Researcher (`agents/researcher.py`)

**Role:** Analytical reasoning, derivation, and critique resolution. No code execution.

**Tools:** None (`tools = []`). One-shot structured JSON output.

**Context:** `CURRENT_TASK.md` + light research context (conventions, strategy, established results, open questions) rendered from `self.research_state`.

**Output processing:** Calls `_parse_researcher_json(text)` to extract `{result, method, confidence, summary}` JSON from response text. Falls back to a minimal Evidence entry on parse failure. Builds `Evidence` (type="research") with `reasoning` from the full response text. Stores evidence on the target entity (RQ or WH) in `research_state`.

#### Computer (`agents/computer.py`)

**Role:** Computational work via code execution.

**Tools** (4, via `ToolExecutor` with dynamic tool switching):
- `document_approach` — records computational plan before coding (approach, assumptions, expected_outcome). Available **only on round 1** — removed from tool set after first call via `active_tools` property.
- `execute_python` — runs Python script in sandbox. Requires `purpose` and `code` params.
- `submit_result` — structured exit with target_id, description, method, result, confidence, notes. Sets `stop_after_round = True`.
- `report_progress` — progress check. Does NOT stop.

**Context:** `CURRENT_TASK.md` + light research context (conventions, strategy, established results, open questions) rendered from `self.research_state`.

**Dynamic tool switching:** The `ToolExecutor.active_tools` property returns `COMPUTER_TOOLS_POST_APPROACH` (without `document_approach`) after the approach is documented. `run_agent_loop` checks `tool_executor.active_tools` each round to pick up the change.

**Output processing:** Builds `Evidence` (type="compute") from `document_approach` + `submit_result` tool params, including collected script names and execution outputs. Stores evidence on the target entity (RQ or WH) in `research_state`.

**Prompt rules (critical):**
- Numerical spot-checks always required (5+ parameter values, `np.isclose` with `rtol=1e-6`)
- Never use `assert` (crashes waste a tool call)
- Independence: never hardcode the tested formula on both sides
- Never widen tolerance on failure → result must be INCONCLUSIVE
- Execution errors → INCONCLUSIVE, never REFUTED

#### Reviewer (`agents/reviewer.py`)

**Role:** Adversarial review of hypotheses. One-shot agent (no tools). Gets **focused context** (WH + evidence + light state), not the full research state.

**Tools:** None (`tools = []`). One-shot structured JSON output.

**Context (focused, not full state):** `CURRENT_TASK.md` + claim under review (statement, derivation) + evidence (type, approach, method, result, per-script `<computation>` blocks with purpose/code/output, reasoning, confidence) + originating RQ (if any) + light established context (ER list) + conventions.

**Output processing:** Calls `_parse_review_json(text)` to extract `{verdict, summary, details}` JSON from response text. Falls back to INCONCLUSIVE on parse failure. Stores `ReviewResult` on the target hypothesis in `research_state`.

**3-valued verdict system:** VERIFIED / REFUTED / INCONCLUSIVE

#### Deep Critic (`agents/critic.py`)

**Role:** Strategy and direction review via one-shot structured JSON output. Refocused on **strategy/direction only**, not per-claim review (which is handled by the reviewer). Files structured critiques, never suggests fixes.

**Tools:** None (`tools = []`). One-shot structured JSON output.

**Context:** `render_critic_context(self.research_state, iteration)` from `renderers.py` — a high-level strategic view: strategy, conventions, situation assessment, research notes, RQ list, hypothesis summaries (evidence/review one-liners), dead ends, background survey, and previous critiques. No derivations, scripts, reasoning, or approach text.

**Output processing:** Calls `_parse_critic_json(text)` to extract `{summary, details, critiques}` JSON from response text (tries fenced ```json blocks first, falls back to brace-counting for bare JSON with nested objects). Each critique entry has `target_id` and `argument` fields (plus `severity` for backward compat, no longer used for gating).
- CRIT-NNN numbering via `self.research_state.next_critique_num()`
- Invalid severity defaults to MEDIUM
- If critiques present: creates `Critique` objects with `CritiqueStatus.ACTIVE` in `research_state.critiques`, links to target hypotheses
- If no critiques present: records clean review in `research_state.critic_clean_reviews`, sets `_no_critiques_filed` flag (clean review signal to orchestrator via engine)
- Parse failure: treats as clean review, logs `critic_json_parse_failure` scaffold event

**Severity rules (informational only, severity no longer gates promotion or termination):**
- HIGH: only for specific wrong steps (sign error, dropped term)
- MEDIUM: forced cap when objection rests on intuition, or when only INCONCLUSIVE evidence exists, or when a VERIFIED result exists
- LOW: stylistic

#### Compressor (`agents/compressor.py`)

**Role:** Shrink files exceeding size thresholds. LLM output IS the compressed file. One-shot (no tools).

**Context:** The target file's content with a one-line header.

**Processing:** Archives original (timestamped copy in `archive/`), writes compressed version back.

**Rules:** Preserve ERs and unresolved critiques verbatim. Collapse resolved critiques to one-line summaries. Drop abandoned hypotheses. Never discard "what didn't work" information.

#### Formatter (`agents/formatter.py`)

**Role:** Produces clean `ANSWER.md` from final research state. One-shot (no tools). Dispatched automatically on successful termination.

**Context:** Research state + evidence log (rendered from `self.research_state`, which is set by the engine before dispatch) + optional answer template from problem YAML.

**Output:** Entire response → `ANSWER.md`.

---

## 4. Infrastructure Layer

### Provider abstraction (`providers/`)

All LLM calls go through a provider-agnostic interface:

```python
class LLMProvider(ABC):
    def call(self, model, system, messages, max_tokens, tools=None) -> ProviderResponse

@dataclass
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    tool_calls: list[dict]   # [{id, name, input}]
```

Four adapters: `AnthropicProvider`, `OpenAIProvider`, `GoogleProvider`, `HuggingFaceProvider`. Each handles:
- Tool format transformation (OpenAI canonical → provider-specific)
- Message format conversion (role/content structures differ)
- Stop reason normalization to a common set (`end_turn`, `max_tokens`, `tool_use`)
- Provider-specific features (e.g., reasoning budget for DeepSeek via OpenAI adapter, thinking for Gemini)

**Model registry** (`models.yaml`): Maps friendly names (e.g., `claude-sonnet`, `gpt-4o`, `gemini-2.5-pro`, `deepseek-r1`) to `{provider, model_id, env_key, input_cost, output_cost, reasoning_*}`. The `Config.__post_init__` method resolves the model key and sets `provider`, `api_key`, and cost fields.

**Provider caching:** `_get_provider()` in `llm.py` caches providers by `(provider_name, api_key)` tuple to avoid recreating clients.

### LLM interface (`llm.py`)

Two calling patterns:

**`call_llm`** — stateless one-shot. Uses `_call_provider_with_retry()` for API resilience. Returns `LLMResponse(text, input_tokens, output_tokens, stop_reason, duration)`. Used by reviewer, critic, surveyor, compressor, formatter.

**`run_agent_loop`** — stateful multi-turn. Maintains a growing `messages` list across rounds. Each round: LLM response → tool extraction → tool executor's `execute()` → tool result fed back. Checks `tool_executor.active_tools` each round for dynamic tool switching (used by computer agent to remove `document_approach` after first call). Returns `AgentResult(text, tool_calls, total_input/output_tokens, rounds, truncated, duration, stop_reason)`. Used by orchestrator and computer. On `max_rounds` exhaustion or `tool_call_failure`, forces a single text-only final call via agent-agnostic user message (system prompt unchanged); empty text is honest failure (no synthesis fallback). Agent-agnostic warnings at `max_rounds-2` and `max_rounds-1` (no agent-specific format references).

Both paths go through `_call_provider_with_retry()` which wraps every provider call in an exponential-backoff retry loop (see §7 for details).

**Logging:** Every LLM call produces:
- JSONL event entry in `EVENT_LOG.jsonl` via `log_llm_call()` (metadata only, no prompts; includes round number for tool-use and per-call cost; `kind: "llm_call"`)
- Full conversation log in `logs/iter{NNN}_{agent}_{seq}.md` (system prompt + context + response)

### Tool execution (`tools.py`, `orchestrator_tools.py`)

Two tool executors, one per agentic agent type:

**`ToolExecutor`** (computer only) — tool set for `COMPUTE`:

For `COMPUTE`:
1. **`document_approach`** — records plan before coding. Params: `approach`, `assumptions`, `expected_outcome`. Available only on round 1, then removed via `active_tools`. Does NOT stop.
2. **`execute_python`** — requires `purpose` and `code`. `purpose` preserved in logs; only `code` executed.
3. **`submit_result`** — structured exit. Params: `target_id`, `description`, `method`, `result`, `confidence` (exact/approximate/partial), `notes`. Sets `stop_after_round = True`.
4. **`report_progress`** — progress check. Params: `findings_so_far`, `remaining_questions`, `ready_to_conclude`. Does NOT stop.

**`OrchestratorToolExecutor`** (orchestrator) — 10 state-mutation tools:
1. **`add_hypothesis`** — creates new WH-NNN in `ResearchState`; optional `from_rq` links to originating RQ (copies evidence)
2. **`update_hypothesis`** — updates statement/derivation
3. **`abandon_hypothesis`** — marks ABANDONED, records `FailedApproach`
4. **`promote_hypothesis`** — WH → ER with guardrails (requires `h.review.verdict == "VERIFIED"`, dependency checks)
5. **`dismiss_critique`** — dismisses a critique as wrong/inapplicable
6. **`accept_critique`** — accepts a critique as valid; optionally creates an RQ with carried-over evidence
7. **`update_section`** — replaces Conventions, Situation Assessment, or Strategy content
8. **`append_note`** — appends to `research_notes` list
9. **`add_research_question`** — creates new RQ-NNN for open-ended exploration targets
10. **`abandon_research_question`** — marks RQ as abandoned (dead end); reason required
11. **`dispatch_researcher`** — dispatches researcher; sets `stop_after_round = True`
12. **`dispatch_computer`** — dispatches computer; sets `stop_after_round = True`
13. **`dispatch_reviewer`** — dispatches reviewer (WH-only target); sets `stop_after_round = True`
14. **`request_termination`** — requests loop termination; sets `stop_after_round = True`

Tracks `mutations_applied` (bool) and `resolved_critique_ids` (set) for `process_response` to use.

The `ToolExecutor` class:
- Writes each script to `computations/tool_exec_NNN.py` (monotonic counter per instance)
- Calls `sandbox.execute_python()` with timeout
- Returns structured `ToolCall` records with output, error status, duration
- Truncates large outputs (head + tail, default 10K chars)
- Tool schema description embeds banned-API rules (prompt engineering in the schema itself)

### Sandbox (`sandbox.py`)

Minimal `subprocess.run` wrapper. The "sandbox" is really just a timeout — no OS-level isolation, no seccomp, no cgroups. Scripts inherit the full parent environment (including `ANTHROPIC_API_KEY`). Sets `MPLBACKEND=Agg` to prevent matplotlib display calls.

### Workspace management (`workspace.py`)

All file I/O goes through `WorkspaceManager`. Agents never touch the filesystem directly.

- `init(problem)` — creates workspace structure, initial Markdown files with frontmatter, `git init`
- `read_file` / `write_file` / `append_file` — defensive (missing files return `""`, never raise)
- `archive_file` — timestamped copy to `archive/` before compression
- `git_commit(message)` — `git add -A` + `git commit --allow-empty` every iteration
- `log_scaffold_event(workspace_dir, iteration, category, event, detail)` — free function; appends one JSONL line to `EVENT_LOG.jsonl` with `kind: "scaffold"` (see §7). Never raises (bare `except OSError: pass`). Called from engine, agents, validation, and llm modules
- `log_llm_call(workspace_dir, ...)` — free function; appends one JSONL line to `EVENT_LOG.jsonl` with `kind: "llm_call"` (metadata: agent, model, tokens, cost, round number). Never raises. Called from `llm.py` after every provider call

### Markdown parsing (`markdown.py`)

The richest infrastructure file. Handles:

**Frontmatter:** `parse_frontmatter` strips code fences, tries `yaml.safe_load()`, falls back to line-by-line regex on `YAMLError`. Non-dict results → `{}`. Never crashes.

**Section utilities:** `tail_entries` (last N `## ` sections), `extract_section_by_id` (find section by ID pattern).

**Critique lifecycle:**
- `count_unresolved_critiques` — counts unresolved critiques via regex
- `insert_into_active_critiques` — inserts between Active/Resolved headings
- `resolve_critique` — moves a critique block from Active to Resolved, rewrites `[UNRESOLVED]` → `[RESOLVED]`, appends resolution note
- `extract_resolved_critique_ids` — four-pattern extraction (YAML list, YAML mapping, forward prose, reverse prose)
- `filter_self_retracted_critiques` — marks LOW/MEDIUM critiques with retraction signals as `[WITHDRAWN]`
- `recount_critique_metadata` — re-derives frontmatter counts from body content

**Normalisation:**
- `normalize_er_wh_headers` — converts bold-line ER/WH to proper `## ` headers
- `flatten_unverified_brackets` — collapses nested `[[[ID:unverified]...]]` artifacts
- All critique regexes accept both `CRIT-NNN` and `CRITIQUE-NNN` (LLM drift tolerance)

### Metrics (`metrics.py`)

In-memory `MetricsTracker` with `CallRecord` entries. Tracks: per-call tokens, tool calls, rounds, truncation flags, alerts, cost. Renders to `METRICS.md` with adaptive columns. Not persisted between process restarts — `METRICS.md` is the durable artifact.

---

## 5. Workspace Files & Data Flow

Each run creates a timestamped workspace directory (e.g., `workspaces/20260313_142530_hawking_temperature_claude-sonnet-4-6/`):

```
workspaces/<run>/
  RESEARCH_GRAPH.json    ← Authoritative structured state (ResearchState serialized as JSON)
  RESEARCH_STATE.md      ← Rendered from ResearchState by engine's _render_files_for_git()
  CURRENT_TASK.md        ← Orchestrator writes via dispatch tools; consumed by dispatched agent
  EVIDENCE_LOG.md        ← Rendered from ResearchState by engine's _render_files_for_git()
  CRITIQUE_LOG.md        ← Rendered from ResearchState by engine's _render_files_for_git()
  ANSWER.md              ← Formatter writes on successful termination
  METRICS.md             ← Engine writes every iteration
  EVENT_LOG.jsonl        ← Append-only events: scaffolding interventions (kind: "scaffold") + LLM call metadata (kind: "llm_call")
  computations/          ← Python scripts from tool execution (tool_exec_NNN.py)
  archive/               ← Pre-compression file copies
  logs/                  ← Full conversation logs (iter{NNN}_{agent}_{seq}.md)
  .git/                  ← One commit per iteration
```

### Data flow per iteration

```
     ResearchState (in-memory, authoritative)
           │
           ├──► Orchestrator mutates via tools ──► writes CURRENT_TASK.md
           │
           ├──► Researcher/Computer store Evidence on entities
           │
           ├──► Reviewer stores ReviewResult on hypotheses
           │
           ├──► Critic adds Critique objects
           │
           ├──► _render_files_for_git() ──► renders → RESEARCH_STATE.md
           │                                           EVIDENCE_LOG.md
           │                                           CRITIQUE_LOG.md
           │
           └──► _sync_research_state() ──► saves → RESEARCH_GRAPH.json
```

### Promotion pipeline

A claim advances through this lifecycle:
1. **Research Question (RQ-NNN)** — orchestrator creates via `add_research_question` for open-ended exploration
2. **Explore** — `researcher` or `computer` investigates the question → `Evidence` stored on RQ (researcher via one-shot JSON output; computer via `submit_result` tool)
3. **Working Hypothesis (WH-NNN)** — orchestrator calls `add_hypothesis` (optionally with `from_rq` to link to originating RQ and copy evidence), creates `Hypothesis` with status WORKING. Direct WH creation (skipping RQ) is allowed when the claim is already concrete.
4. **Review** — `reviewer` examines WH + evidence → one-shot structured JSON → `ReviewResult` stored on hypothesis (VERIFIED / REFUTED / INCONCLUSIVE)
5. **Critique** — deep critic reviews strategy/direction via one-shot structured JSON, files objections
6. **Established Result (ER-NNN)** — orchestrator calls `promote_hypothesis` tool with guardrails: (a) `h.review.verdict == "VERIFIED"`, (b) all `depends_on` entries are already established
7. **Termination** — orchestrator emits `terminate` task → `can_terminate()` gates → formatter produces `ANSWER.md`

---

## 6. Configuration

**File:** `config.default.yaml` — single source of truth for all defaults. No values are hardcoded in Python.

**3-tier merge** in `build_config(args)`:
1. Package defaults (from `config.default.yaml`, loaded at module import)
2. User YAML config (via `--config`, only allowed keys)
3. CLI args (override everything)

| Field | Default | Purpose |
|-------|---------|---------|
| `model` | `claude-sonnet-4-6` | Agent model (resolved via `models.yaml`) |
| `verify_model` | `claude-opus-4-6` | Verification script only |
| `max_tokens` | 16384 | Per-call output cap |
| `max_iterations` | 200 | Loop hard ceiling |
| `critic_every_n` | 4 | Forced critic interval |
| `sympy_timeout_seconds` | 60 | Sandbox per-script timeout |
| `max_tool_rounds` | 10 | Agent tool loop depth |
| `tool_output_limit` | 10000 | Chars per tool output before truncation |
| `progress_check_interval` | 3 | Consecutive `execute_python` rounds before progress check injection |
| `computation_token_alert` | 150000 | Cumulative input tokens before firing alert |
| `stall_recompute_limit` | 2 | Max consecutive non-VERIFIED verdicts before orchestrator sees "STALLED" warning |
| `min_er_for_completion` | 3 | ERs needed before orchestrator sees completion analysis hints |
| `compress_threshold` | RS: 50K, CL: 30K, EL: 40K | File size thresholds (chars) |
| `api_retry_max` | 3 | Max retry attempts on transient API errors |
| `api_retry_initial_delay` | 2.0 | Initial retry delay (seconds) |
| `api_retry_max_delay` | 30.0 | Max retry delay (seconds) |
| `api_timeout` | 120.0 | Per-API-call timeout (seconds) |

Compression: alert at 1x, compress at `compress_soft_multiplier` (default 1.5x).

---

## 7. LLM Failure Compensation

This section catalogues every mechanism that compensates for LLM misbehaviour at the scaffolding level. LLMs routinely fail in specific, predictable ways — promoting unverified results, hallucinating IDs, emitting malformed YAML, ignoring instructions, failing to terminate. The scaffolding corrects these failures across four concern-based categories (defined in `categories.py` as `CompensationCategory`):

- **`call_reliability`** — making each LLM call succeed: transport retry, tool-call fallback, agent loop bailouts, tool execution guards
- **`state_invariants`** — keeping ResearchState consistent: post-integration validation pipeline (4 checks)
- **`loop_control`** — steering the main loop: forced critic, dispatch guards, verdict tracking, compute enrichment, termination gates
- **`output_normalization`** — cleaning agent output: per-agent response corrections, markdown parsing tolerance

### Event log (instrumentation)

All scaffolding interventions and LLM call metadata are recorded in a single file, `EVENT_LOG.jsonl`, via two functions defined in `workspace.py`:

- **`log_scaffold_event()`** — emits events with `kind: "scaffold"` whenever a compensation mechanism fires
- **`log_llm_call()`** — emits events with `kind: "llm_call"` after every LLM provider call (metadata: agent, model, tokens, cost, round number)

Each entry is a single JSON line with a `kind` field discriminating the event type:

```json
{"kind": "scaffold", "ts": "2026-03-13T14:22:01+00:00", "iter": 5, "category": "state_invariants", "event": "er_demotion", "detail": "ER-003 → WH-003 (REFUTED review)"}
{"kind": "llm_call", "ts": "2026-03-13T14:22:03+00:00", "iter": 5, "agent": "computer", "model": "claude-sonnet-4-6", "input_tokens": 12340, "output_tokens": 1890, "cost": 0.042, "round": 3}
```

Common fields: `kind` (`"scaffold"` or `"llm_call"`), `ts` (UTC ISO-8601), `iter` (scaffolding iteration). Scaffold events additionally have `category`, `event`, `detail`. LLM call events additionally have `agent`, `model`, `input_tokens`, `output_tokens`, `cost`, `round`. Both functions never raise — failures are silently swallowed (`except OSError: pass`).

**Event keys by category:**

| Category | Event keys |
|----------|-----------|
| `call_reliability` | `api_retry`, `tool_call_failure_fallback`, `progress_check`, `forced_final_call`, `forced_final_call_failed`, `forced_exit_tool_retry`, `empty_end_turn_recovery`, `tool_timeout`, `tool_output_truncation` |
| `state_invariants` | All `Violation.check` values from validation checks (e.g. `er_demotion_safety`, `phantom_labels`, `stale_unverified_labels`, `critique_resolution_consistency`) |
| `loop_control` | `forced_critic`, `compute_enrichment`, `termination_blocked`, `dispatch_failure`, `routing_conflict_corrected`, `no_critiques_filed`, `status_field_exit`, `compute_verdict_failed`, `agent_failure_max_tokens`, `agent_failure_max_rounds`, `max_tokens_no_retry` |
| `output_normalization` | `problem_statement_enforced`, `header_normalized`, `critique_resolved`, `bracket_flattened`, `preamble_stripped`, `critique_self_retracted`, `empty_response_stub`, `header_injected`, `claim_id_injected`, `submit_review_text_extracted` |

**Quick analysis:**

```bash
# Count scaffold events per mechanism across a run
jq -r 'select(.kind == "scaffold") | .event' workspaces/<run>/EVENT_LOG.jsonl | sort | uniq -c | sort -rn

# Count scaffold events per category
jq -r 'select(.kind == "scaffold") | .category' workspaces/<run>/EVENT_LOG.jsonl | sort | uniq -c | sort -rn

# Filter to a specific category
jq 'select(.kind == "scaffold" and .category == "state_invariants")' workspaces/<run>/EVENT_LOG.jsonl

# Timeline of overrides
jq 'select(.kind == "scaffold" and .category == "loop_control") | "\(.iter) \(.event) \(.detail)"' workspaces/<run>/EVENT_LOG.jsonl

# LLM call cost summary per agent
jq -r 'select(.kind == "llm_call") | .agent' workspaces/<run>/EVENT_LOG.jsonl | sort | uniq -c | sort -rn
```

### call_reliability — Transport, retry, and agent loop resilience (`llm.py`, `tools.py`, `sandbox.py`)

#### API and transport

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Transient error classification | `_is_transient()`, `_is_tool_call_failure()` | Classifies exceptions as retryable by HTTP status (429, 500–504), exception class (ConnectionError, TimeoutError, etc.), and tool-call JSON failures | API hiccups, rate limits, malformed tool JSON |
| Exponential-backoff retry | `_call_provider_with_retry()` | Wraps every `provider.call()` in a loop: up to `api_retry_max` attempts, doubling delay between `api_retry_initial_delay` and `api_retry_max_delay` | Rate limits, 5xx server errors, transient network failures |
| Tool-call failure fallback | `run_agent_loop` | If retry exhaustion is due to a tool-call failure, sets `tool_call_failure=True`, breaks the loop, and forces a text-only final call telling the model "The tool-calling interface is unavailable" | Models emitting invalid JSON for tool calls |

#### Agent loop bailouts

These mechanisms prevent agentic agents from wasting rounds or producing empty output:

| Mechanism | Trigger | Action |
|-----------|---------|--------|
| Progress check injection | N consecutive `execute_python` rounds without `report_progress` (`progress_check_interval`, default 3) | Inject user-turn message requiring `report_progress` tool call; fires again at 2N, 3N, etc. if model ignores |
| Two-round escalating warning | At `max_rounds - 2`: warning; at `max_rounds - 1`: CRITICAL | Inject agent-agnostic user-turn messages with escalating urgency (no agent-specific format references) |
| Forced final call with exit tool | Loop exits via max rounds, empty end_turn exhaustion, or tool-call failure | Forced call with exit tool available (up to 3 attempts with retry if model ignores exit tool); falls back to text-only when exit tool not in tool set or provider failure; system prompt unchanged; stop_reason `max_rounds_forced` or `executor_stop` |
| `submit_result` structured exit | Model calls exit tool | Structured data bypasses free-text generation; sets `stop_after_round` → `executor_stop`; `process_response` creates `Evidence` from tool parameters. Plays WITH tool-calling tendency instead of against it |
| `report_progress` tool | Model calls `report_progress` tool (prompted by progress check injection) | Captures structured reasoning (`findings_so_far`, `remaining_questions`, `ready_to_conclude`); enriches conversation history with explicit reasoning; if `ready_to_conclude`, response guides model to exit tool. Works WITH tool-calling tendency |
| `execute_python` purpose parameter | Model calls `execute_python` | Required `purpose` field forces model to articulate WHY before each run; preserved in logs for audit; not enforced at execution time (schema-level only) |
| `document_approach` round-1-only | Computer agent calls `document_approach` | Forces model to articulate plan before coding; `active_tools` removes it from tool set after first call so model cannot re-document instead of coding |

#### Tool execution guards

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Subprocess timeout | `sandbox.execute_python()` | Hard timeout per script via `subprocess.run(timeout=...)` | Infinite loops, exponentially expensive code |
| Structured timeout feedback | `ToolExecutor._execute_python()` | Returns "TIMEOUT: Script exceeded Ns limit" with 4 actionable suggestions | Raw timeout exception would be meaningless to LLM |
| Structured error feedback | `ToolExecutor._execute_python()` | Concatenates stdout+stderr, marks `is_error=True` | Runtime errors in generated code |
| Output truncation | `ToolExecutor._truncate_output()` | Head/tail preservation with `[...truncated...]` marker, default 10K chars | Print-heavy scripts exploding context window |
| Banned API documentation | `TOOL_DEFINITIONS` | Lists removed/renamed APIs (e.g., `scipy.misc.derivative`, `numpy.trapz`) with correct replacements | Known-bad API usage causing reproducible crashes |
| Unknown tool guard | `ToolExecutor.execute()` | `ValueError` on hallucinated tool names | Model inventing nonexistent tools |

### state_invariants — Post-integration validation pipeline (`validation.py`)

All 4 checks run after every orchestrator pass via `validate_post_integration(research_state, *, iteration=0, workspace=None)`. They are pure functions that take `research_state: ResearchState` and return `Violation` objects injected into the orchestrator's next context.

| Check | Function | What it does | Failure compensated |
|-------|----------|--------------|---------------------|
| **ER demotion** | `check_er_demotion_safety()` | Demotes ER-NNN when `h.review.verdict == "REFUTED"` → silently rewrites state (not injected into orchestrator context to prevent re-promotion churn) | LLM promoting WH to ER despite REFUTED review |
| **Phantom label stripping** | `check_phantom_labels()` | Builds verified set from `h.review.verdict == "VERIFIED"`; finds "VERIFIED" labels in derivations without backing → returns violation | LLM copying "VERIFIED" from existing text without evidence |
| **Stale-unverified label promotion** | `check_stale_unverified_labels()` | Finds hypotheses with stale unverified labels that now have review → returns violation | Labels stuck as [unverified] after late review |
| **Critique resolution consistency** | `check_critique_resolution_consistency()` | Checks that resolved critiques actually had their fixes applied: target hypothesis still exists, no inconsistencies | LLM marking critiques "resolved" without applying the fix |
| **Termination gate** | `can_terminate()` | Blocks termination unless: (1) critic pass occurred when VERIFIED hypotheses exist, (2) all RQs resolved/abandoned, (3) all WHs either verified+promoted or abandoned | LLM trying to terminate prematurely |

### loop_control — Pre-dispatch hooks and dispatch guards (`engine.py`)

#### Pre-dispatch hooks

| Mechanism | Function | Condition | Action | Failure compensated |
|-----------|----------|-----------|--------|---------------------|
| Forced critic | `_critic_overdue()` + `_make_forced_critic_task()` | Overdue AND new content exists | Skip orchestrator, dispatch critic directly (saves an LLM call) | Orchestrator skipping critic indefinitely |
| Compute enrichment | `_enrich_compute_task_with_prior_failures()` | COMPUTE task with prior failures on same claim | Append failure excerpts to task body | Model repeating identical failing code |

Non-VERIFIED review verdicts go to `pending_compute_verdicts` in `LoopState` (with `notes` and `failure_detail`), rendered as a REVIEW RESULTS banner in `_build_context_prefix()`. VERIFIED verdicts go to `pending_verified_results`, rendered as a VERIFIED HYPOTHESES banner. Evidence results from researcher/computer go to `pending_explore_results`, rendered as an EVIDENCE RESULTS banner. The orchestrator decides how to respond (re-review, re-derive, promote, or accept). No auto-recompute.

#### Engine-level guards

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Transient-error catch at dispatch | `run()` try/except | If a transient error escapes retry logic, skip iteration with `continue` and file a `dispatch_failure` violation | API errors crashing the entire session |
| Routing conflict auto-correction | `_dispatch()` | Checks `assigned_to` against `TASK_TYPE_AGENT_MAP`; corrects empty/invalid values; routes by task type if `assigned_to` disagrees | Orchestrator assigning wrong agent |
| Unknown task type fallback | `_dispatch()` | Routes unknown types to researcher | LLM hallucinating task types |
| Scaffolding-maintained iteration counter | `_update_research_iteration()` | Updates `iteration` on ResearchState unconditionally at the top of each iteration | LLM forgetting or corrupting iteration count |
| Status field safety-net exit | `_check_status_field()` | Reads ResearchState for `status: completed/abandoned/partially_complete` → exits loop | Loop continuing past a declared terminal state |
| NO_CRITIQUES_FILED handling | `_dispatch()` | Detects `_no_critiques_filed` flag on critic agent → files `critic_clean` violation telling orchestrator to proceed to synthesize | Empty critic looping indefinitely |
| Dispatch-level result tracking | `_track_agent_result()` | For RESEARCH/COMPUTE: checks evidence on target entity → `pending_explore_results`; for REVIEW: checks h.review → VERIFIED → `pending_verified_results` (clears failure count), non-VERIFIED → `pending_compute_verdicts` with notes/failure_detail | Orchestrator unaware of agent results |
| Agent failure routing | `_record_agent_failures()` + `_build_context_prefix()` | Records max_tokens truncation, max_rounds exhaustion, and non-VERIFIED verdicts; shows "AGENT FAILURES" banner to orchestrator on next pass | Orchestrator re-issuing identical failing tasks without awareness of prior failures |
| Violations/blockers/evidence/verified/verdicts/failures as context prefix | `_build_context_prefix()` | 6 sections: violations, termination blockers, evidence results, verified hypotheses, review results (with notes/failure_detail and stall warnings at limit), agent failures — serialised into orchestrator's next user message; all consumed-once (cleared after read) | Orchestrator ignoring validation failures or agent results |
| Compression at soft threshold | `_check_compression()` | Compresses files exceeding `compress_soft_multiplier` × threshold (single tier) | Runaway file growth crashing context window |

### output_normalization — Agent-level corrections and parsing tolerance

#### Orchestrator corrections (`agents/orchestrator.py`, `orchestrator_tools.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Budget-aware completion analysis | `_completion_analysis()` | Injects "COMPLETION CHECK" or "BUDGET SYNTHESIS REQUIRED" banners based on ER/WH/critique counts and remaining budget | Orchestrator failing to terminate when done |
| Phantom marker cleaning | `build_context()` | Flattens nested `[[[ID:unverified]...]]` brackets and replaces `[ID:unverified]` with `ID (unverified)` before the LLM sees the state | LLM copying bracket syntax into new outputs |
| `promote_hypothesis` guardrails | `OrchestratorToolExecutor._promote_hypothesis()` | Rejects promotion if review verdict is not VERIFIED | LLM promoting unreviewed hypotheses |
| Conventions section staleness reminder | `build_context()` | From iteration 3+, injects banner if Conventions still says "To be populated" | LLM skipping conventions population |

#### Critic corrections (`agents/critic.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| JSON parse failure → clean review | `process_response()` | Falls back to clean review (no critiques filed) when `_parse_critic_json()` returns `None`; logs `critic_json_parse_failure` scaffold event | LLM producing unparseable critique output |
| `_no_critiques_filed` flag | `process_response()` | Sets flag when critiques list is empty → engine signals orchestrator to proceed to synthesize | Empty critic output |
| Auto-increment CRIT-NNN | `process_response()` | Auto-assigns next CRIT-NNN via `research_state.next_critique_num()` | LLM using wrong or duplicate critique IDs |
| Invalid severity default | `process_response()` | Defaults to MEDIUM when severity value is not a valid `Severity` enum member | LLM using non-standard severity strings |

#### Researcher corrections (`agents/researcher.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| JSON parse → Evidence object | `process_response()` | Calls `_parse_researcher_json(text)` to extract `{result, method, confidence, summary}` JSON; builds `Evidence` with full response text as `reasoning` | Ensures structured data reaches state regardless of LLM output format |
| Fallback evidence on parse failure | `process_response()` | Creates minimal `Evidence` from response text when JSON parsing fails | LLM producing unparseable output |

#### Computer corrections (`agents/computer.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Structured tool exit → Evidence object | `process_response()` | Routes `submit_result` tool data to `Evidence` object stored on target entity in `ResearchState` | Ensures structured data reaches state regardless of LLM output format |
| Fallback evidence on no exit tool | `process_response()` | Creates minimal `Evidence` with "Agent produced no exit tool call" when no `submit_result` called | Agent producing no output despite forced final call |
| `active_tools` dynamic switching | `ToolExecutor.active_tools` property | After `document_approach` called, removes it from tool set so model cannot re-document instead of coding | Model avoiding code execution by repeatedly documenting approach |
| NameError recovery hint | `_execute_python()` in `tools.py` | Detects NameError in stderr and appends "FRESH Python process" reminder | Models (esp. Kimi K2.5) treating execute_python as persistent REPL, causing NameErrors from referencing prior-script variables |
| Structured output header | `_execute_python()` in `tools.py` | Prepends `=== script_name ===` header with purpose and exit status to every script output | Model loses track of which script produced which output across multi-call sessions |

#### Reviewer corrections (`agents/reviewer.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| JSON parse → ReviewResult | `process_response()` | Calls `_parse_review_json(text)` to extract `{verdict, summary, details}` JSON; stores `ReviewResult` on target hypothesis | Ensures structured review data reaches state |
| Fallback INCONCLUSIVE on parse failure | `process_response()` | Creates `ReviewResult` with verdict="INCONCLUSIVE" when JSON parsing fails or verdict missing | LLM producing unparseable review output |
| Invalid verdict normalization | `process_response()` | Normalizes unrecognized verdict values to "INCONCLUSIVE" | LLM using non-standard verdict strings |

#### Markdown parsing tolerance (`markdown.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Code-fence stripping | `parse_frontmatter()` | Strips `` ```yaml `` / `` ``` `` wrapping YAML frontmatter | LLM wrapping YAML in code fences |
| YAML parse fallback to regex | `parse_frontmatter()` + `_fallback_parse()` | On `YAMLError`, extracts simple `key: value` pairs line-by-line | LLM producing invalid YAML (unquoted colons, trailing commas) |
| Non-dict YAML fallback | `parse_frontmatter()` | If `yaml.safe_load` returns non-dict (string, list), substitutes `{}` | LLM writing bare value instead of YAML mapping |
| CRITIQUE-NNN alias tolerance | `CRIT_ID_RE`, `CRIT_HEADER_RE`, etc. | All regexes use `CRIT(?:IQUE)?-\d+` | LLM writing "CRITIQUE-010" instead of "CRIT-010" |
| Bold-format section detection | `_ER_SECTION_RE`, `_WH_SECTION_RE` | Match both `## ER-NNN` and `**ER-NNN` patterns | LLM using bold-line-start as heading shorthand |
| Nested bracket flattening | `flatten_unverified_brackets()` | Collapses `[[[COMP-001:unverified]:unverified]]` → `[COMP-001:unverified]` | Validators re-wrapping previously wrapped brackets |

---

## 8. Verification

**File:** `verify.py` — runs as `python -m sciralph.verify <workspace_dir>`

A fully independent post-hoc evaluation. Two LLM passes using Claude Opus with streaming:

**Pass 1: Science verification** — evaluates correctness of each ER (derivation validity, evidence support, critique resolution). Verdict scale: VALID / PARTIALLY_VALID / INVALID / INCONCLUSIVE. Produces per-ER assessments and chain coherence check. Optionally re-runs computation scripts.

**Pass 2: Process audit** — evaluates multi-agent process quality (error-correction cycles, evidence effectiveness, orchestrator decisions, budget management). Verdict scale: EFFECTIVE / PARTIALLY_EFFECTIVE / INEFFECTIVE. Lists process events with classifications.

Output: `VERIFICATION.md` written to workspace (when `--write-report`).

---

## 9. Testing

**827 tests** across 28 test files. Run with `uv run python -m pytest -v`.

| Test file | Lines | What it covers |
|-----------|------:|----------------|
| `test_engine.py` | 1490 | Main loop, termination gates, compression, budget, stalls, dispatch, context prefix, result tracking |
| `test_orchestrator_tools.py` | 1150 | OrchestratorToolExecutor: add/update/abandon/promote hypothesis, resolve critique, dispatch tools, append_note, update_section |
| `test_tools.py` | 1060 | ToolExecutor, submit_result, document_approach, run_agent_loop, truncation, token accumulation, active_tools |
| `test_markdown.py` | 972 | Frontmatter, sections, critique lifecycle, header normalisation |
| `test_research_state.py` | 929 | ResearchState dataclass, query methods, promote/demote, normalize_references, Evidence/ReviewResult, serialization |
| `test_llm_retry.py` | 895 | Retry logic, transient error classification, backoff, tool-call failure fallback |
| `test_verify.py` | 852 | Workspace loading, verdict parsing, prompts, process audit, report patching |
| `test_report_recommendations.py` | 682 | Report generation, recommendation analysis |
| `test_renderers.py` | 657 | Snapshot renderers (research_state, evidence_log, critique_log) |
| `test_validation.py` | 465 | All 4 post-integration checks, can_terminate gates, violation types, critique resolution consistency |
| `test_huggingface_repair.py` | 423 | HuggingFace provider edge cases and repair logic |
| `test_evaluate.py` | 373 | Answer evaluation (symbolic + numerical comparison) |
| `test_conversation_log.py` | 311 | File naming, sections, sequence counter |
| `test_orchestrator.py` | 262 | Completion analysis, budget, stalls, context building |
| `test_config.py` | 234 | Defaults, YAML/CLI override, merge priority, model resolution from models.yaml |
| `test_task.py` | 188 | TaskType enum, to_markdown, from_frontmatter, round-trip |
| `test_reasoning_tokens.py` | 156 | Reasoning token handling across providers |
| `test_metrics.py` | 152 | CallRecord, critic tracking, alerts, Markdown rendering |
| `test_scaffold_log.py` | 137 | `log_scaffold_event` and `log_llm_call` JSONL output to `EVENT_LOG.jsonl` |
| `test_critic_tools.py` | 129 | Critic one-shot JSON: `_parse_critic_json()` parsing + `process_response` critique creation, auto-numbering, severity defaults |
| `test_computationalist.py` | 125 | Researcher/computer agent: process_response, Evidence creation, tool sets |
| `test_provider_smoke.py` | 124 | Provider adapters: tool format, message format, stop reason normalisation |
| `test_surveyor.py` | 117 | Surveyor agent: context building, background survey |
| `test_sandbox.py` | 66 | Script execution, timeout, MPLBACKEND |
| `test_workspace.py` | 29 | init structure |

**Testing approach:** pytest with `tmp_path` fixtures. All LLM calls are mocked (no real API calls). `SimpleNamespace` objects mock SDK responses. Fixture Markdown files for complex document parsing.

**Notable coverage gaps:**
- `call_llm` one-shot path (only `run_agent_loop` is tested via `test_tools.py`)
- BaseAgent retry logic directly
- Compressor, formatter `process_response` methods
- Workspace git operations
- End-to-end `main.py` run path
- Reviewer `_parse_review_json()` edge cases (partial JSON, deeply nested)

---

## 10. Known Issues

### `Task.from_frontmatter` iteration-0 gotcha

```python
meta.get("iteration", fallback_iteration) or fallback_iteration
```

The `or` treats `0` as falsy. A task explicitly written with `iteration: 0` silently falls back. Unlikely in practice.

### Context accumulation in `run_agent_loop`

The `messages` list grows unboundedly across rounds. Large tool outputs can push past the model's context limit with no trimming mechanism. The `max_tool_rounds` limit is the only guard.

---

## 11. Documentation Status

All documentation was synced with the codebase on 2026-03-19.

- **ResearchState as source of truth** — `research_state.py` (480 lines) provides authoritative structured state; agents mutate via tools, Markdown rendered from state for git snapshots only
- **Two agentic agents** — orchestrator (10 tools via `OrchestratorToolExecutor`), computer (4 tools via `ToolExecutor` with `active_tools` dynamic switching); both use `stop_after_round` mechanism. **Three one-shot structured JSON agents** — researcher (`_parse_researcher_json()`), reviewer (`_parse_review_json()`), and critic (`_parse_critic_json()` + `render_critic_context()`)
- **Three specialized work agents** — researcher (analytical), computer (code), reviewer (adversarial review); each has focused prompt, tool set, and context view; evidence inlined on entities as `Evidence` and `ReviewResult`
- **Renderers** — `renderers.py` produces Markdown snapshot files from ResearchState; agents render context from `self.research_state` via renderers; MD files rendered centrally by engine's `_render_files_for_git()`
- **Formatter agent** — `agents/formatter.py` produces `ANSWER.md` on successful termination
- Multi-provider support (Anthropic, OpenAI, Google Gemini, HuggingFace) fully implemented with `models.yaml` registry
- API retry with exponential backoff implemented (transient errors + tool-call failures)
- 4 post-integration validation checks (er_demotion_safety, phantom_labels, stale_unverified_labels, critique_resolution_consistency); all take `research_state: ResearchState`; checks use `h.review.verdict` rather than computation-based lookups
- Scaffolding-maintained iteration counter (no longer LLM-dependent)
- `verify.py` remains Anthropic-only
- Event log (`EVENT_LOG.jsonl`) instrumentation implemented across all 4 categories
- Workspace directory names include model label (e.g. `20260313_142530_hawking_temperature_claude-sonnet-4-6`)
- Problems organized into `problems/tier1/` (10 core), `problems/tier2/` (12 advanced), and `problems/critpt/` (quantum error correction decomposition)
- `evaluate.py` provides answer evaluation for one-shot LLM responses; `one_shot.py` provides a one-shot baseline for comparison
