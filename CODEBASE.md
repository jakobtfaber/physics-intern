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

SciRalph is a multi-agent scaffolding system for autonomous scientific research in theoretical physics. Seven agent roles take turns in a main loop: researcher (analytical reasoning), computer (code execution), verifier (adversarial verification), deep critic (strategy review), surveyor, compressor, and formatter. All research state lives in a structured `ResearchState` object — agents mutate it via tools, and Markdown files are rendered from it for git snapshots and agent context. LLM calls go through a provider abstraction layer supporting Anthropic, OpenAI, Google Gemini, and HuggingFace.

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
     │  computer        verifier                    │
     │  critic          compressor                  │
     │  formatter       surveyor                    │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │         research_state.py                    │
     │  ResearchState: authoritative structured     │
     │  state (hypotheses, evidence, critiques)     │
     │  + renderers.py (state → Markdown)           │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │  orchestrator_tools.py  tools.py             │
     │  verifier_tools.py  critic_tools.py          │
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
- **Three specialized work agents.** Researcher (analytical, no code), Computer (code execution), and Verifier (adversarial verification, no code). Each has a focused prompt, tool set, and context view.
- **Evidence inlined on entities.** Rather than a separate `Computation` entity, evidence is inlined as `Evidence` on `Hypothesis` and `ResearchQuestion`, and verification is inlined as `VerificationResult` on `Hypothesis`.
- **Mandatory critic passes.** The scaffold forces critic reviews every N iterations, regardless of agent judgment.
- **Tool-based state mutation.** Five agents are agentic — orchestrator (11 tools via `OrchestratorToolExecutor`), researcher (2 tools via `ToolExecutor`), computer (4 tools via `ToolExecutor`, with dynamic tool switching via `active_tools`), verifier (3 tools via `VerifierToolExecutor`), and critic (2 tools via `CriticToolExecutor`). Tool calls use the `stop_after_round` mechanism to signal completion.
- **Provider-agnostic.** LLM calls go through a `providers/` abstraction layer. Model selection is resolved via `models.yaml` registry (friendly key → provider + model_id + env_key + cost). The `verify.py` script is Anthropic-only.

### Source file map

| File | Lines | Purpose |
|------|------:|---------|
| `main.py` | 82 | CLI entry point, arg parsing, workspace naming (includes model label in dir name) |
| `engine.py` | 758 | `SciRalph` class, `LoopState` dataclass: main loop, dispatch, `_render_files_for_git()`, compression, scaffolding log events |
| `research_state.py` | 480 | `ResearchState` dataclass: authoritative structured state (hypotheses with evidence/verification, research_questions, critiques, failed_approaches), query/mutation methods, JSON serialization |
| `renderers.py` | 351 | Snapshot renderers (state → `RESEARCH_STATE.md`, `EVIDENCE_LOG.md`, `CRITIQUE_LOG.md`) + `render_background_survey()` helper |
| `orchestrator_tools.py` | 912 | `OrchestratorToolExecutor`: 11 state-mutation tools for orchestrator agent |
| `verifier_tools.py` | 207 | `VerifierToolExecutor`: `submit_verdict` + `submit_critique` + `report_progress` tools for verifier agent |
| `critic_tools.py` | 154 | `CriticToolExecutor`: `submit_critique` + `finish_review` tools for critic agent |
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
| `agents/researcher.py` | 98 | Agentic: analytical reasoning via `ToolExecutor` (submit_result + report_progress); writes `Evidence` on target entity |
| `agents/computer.py` | 118 | Agentic: code execution via `ToolExecutor` (document_approach + execute_python + submit_result + report_progress); writes `Evidence` on target entity |
| `agents/verifier.py` | 156 | Agentic: adversarial verification via `VerifierToolExecutor` (submit_verdict + submit_critique + report_progress); writes `VerificationResult` on target hypothesis |
| `agents/critic.py` | 147 | Agentic: strategy/direction review via `CriticToolExecutor`, writes `Critique` objects to `ResearchState` |
| `agents/compressor.py` | 27 | One-shot: file size management |
| `agents/formatter.py` | 43 | One-shot: produces `ANSWER.md` from final research state |
| `agents/surveyor.py` | 45 | One-shot: produces background survey notes |
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

The loop runs `while self.iteration < self.config.max_iterations`, incrementing `self.iteration` at the **top** of each pass (so iteration 1 is the first real turn). Each iteration follows this sequence:

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
│  6. DISPATCH to researcher / computer / verifier / critic / formatter│
│     └─ Wrapped in try/except for transient API errors                │
│     └─ _record_agent_failures(): capture max_tokens/max_rounds/etc.  │
│                                                                      │
│  7. POST-DISPATCH CHECKS                                             │
│     └─ _track_agent_result(): for RESEARCH/COMPUTE checks evidence   │
│        on target entity → pending_explore_results;                   │
│        for VERIFY checks h.verification →                            │
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

Non-VERIFIED verification verdicts are no longer auto-recomputed. Instead they are stored in `pending_compute_verdicts` (with `notes` and `failure_detail`) and rendered as a VERIFICATION RESULTS banner in the orchestrator's next context. VERIFIED verdicts go to `pending_verified_results` and render as a VERIFIED HYPOTHESES banner. Evidence results from researcher/computer go to `pending_explore_results` and render as an EVIDENCE RESULTS banner. The orchestrator decides what to do (re-verify, re-derive, promote, or accept provisionally). Stall warnings appear when attempts reach `stall_recompute_limit`.

### Termination paths

| Path | Where | Condition |
|------|-------|-----------|
| Explicit terminate | Step 5 | Orchestrator emits `terminate` → `can_terminate()` gate passes |
| Status field | Step 9 | Agent wrote `status: completed/abandoned/partially_complete` |
| Max iterations | Loop condition | `self.iteration >= self.config.max_iterations` |
| Budget-aware synthesis | Orchestrator | Orchestrator sees budget pressure via `_completion_analysis()` context banner and chooses to synthesize/terminate |

The `can_terminate()` gate requires: at least one VERIFIED hypothesis triggers a mandatory critic pass, no unresolved HIGH critiques, computational evidence when `requires_numerical: true` in problem YAML, all RQs resolved or abandoned, and all WHs either verified and promoted or abandoned. If blocked, blockers are fed back to orchestrator.

### Dispatch routing

Dispatch follows `TASK_TYPE_AGENT_MAP` (in `task.py`):

| TaskType | Agent | Notes |
|----------|-------|-------|
| `research` | `researcher` | Analytical exploration and derivation; stores `Evidence` on target entity |
| `compute` | `computer` | Computational work via code; stores `Evidence` on target entity |
| `verify` | `verifier` | Adversarial verification; stores `VerificationResult` on target hypothesis |
| `critique` | `deep_critic` | Strategy/direction review; post-dispatch: `_no_critiques_filed` detection |
| `format` | `formatter` | Dispatched automatically on successful termination |
| `terminate` | `orchestrator` | Handled by engine termination gate |
| `survey` | `surveyor` | Background survey / mid-loop resurvey |

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

The `tools` class attribute is the **single switch** between one-shot and agentic behavior. Five agents are agentic: orchestrator (11 tools via `OrchestratorToolExecutor`), researcher (2 tools via `ToolExecutor`), computer (4 tools via `ToolExecutor`, with `active_tools` dynamic switching), verifier (3 tools via `VerifierToolExecutor`), and critic (2 tools via `CriticToolExecutor`). Three agents are one-shot: surveyor, compressor, and formatter.

All agentic tool executors use the `stop_after_round` mechanism: a terminal tool (`set_next_task`, `submit_verdict`, `submit_result`, `finish_review`) sets `stop_after_round = True`, which the agent loop detects and returns with `stop_reason="executor_stop"`.

**No retry on truncation:** `_call_with_retry` returns immediately when `stop_reason == "max_tokens"` (no retries). The engine's `_record_agent_failures()` detects the truncation and injects a CAPACITY EXCEEDED banner into the orchestrator's next context via `_build_context_prefix()`, prompting task decomposition.

### ResearchState (`research_state.py`)

The authoritative source of truth for all research state. Agents mutate it via tools; Markdown files are rendered from it.

**Entity dataclasses:**
- `Evidence` — `type` ("research" or "compute"), `reasoning`, `approach` (computer's document_approach output), `scripts` (list of script filenames), `output` (code execution output summary), `method`, `result`, `confidence` (exact/approximate/partial), `iteration`. Inlined on `Hypothesis` and `ResearchQuestion`.
- `VerificationResult` — `verdict` (VERIFIED/REFUTED/INCONCLUSIVE), `reasoning`, `critiques` (list of dicts from verifier's submit_critique calls), `iteration`. Inlined on `Hypothesis`.
- `Hypothesis` — `id`, `statement`, `status` (`HypothesisStatus`: WORKING/ESTABLISHED/REFUTED/ABANDONED), `derivation`, `critiques` (list of CRIT IDs), `iteration_created`, `iteration_modified`, `depends_on` (list of hypothesis IDs), `promotion_justification`, `evidence: Evidence | None`, `verification: VerificationResult | None`
- `ResearchQuestion` — `id` (RQ-NNN), `question`, `context`, `resolved_to` (list of hypothesis IDs), `status` (`RQStatus`: OPEN/RESOLVED/ABANDONED), `iteration_created`, `iteration_resolved`, `resolution_reason`, `evidence: Evidence | None`
- `Critique` — `id`, `targets`, `severity` (`Severity`: HIGH/MEDIUM/LOW), `argument`, `status` (`CritiqueStatus`: ACTIVE/RESOLVED/WITHDRAWN), `resolution`, `iteration_filed`, `iteration_resolved`
- `FailedApproach` — `description`, `reason`, `related_entities`, `iteration`, `derivation_excerpt`

**ResearchState fields:** `hypotheses` (dict by ID), `research_questions` (dict by ID), `critiques` (dict by ID), `failed_approaches` (list), `critic_clean_reviews` (list), `iteration`, `problem_statement`, `conventions`, `strategy`, `short_term_plan`, `research_notes` (list of dicts), `status`, `title`, `background_survey` (BackgroundSurvey | None)

**Key query methods:** `has_verified_evidence()`, `hypotheses_with_evidence()`, `active_critiques_for()`, `unresolved_high_critiques()`, `established_hypotheses()`, `working_hypotheses()`, `abandoned_hypotheses()`, `failures_for_hypothesis()`, `open_research_questions()`

**Mutation methods:** `promote_hypothesis(wh_id)` → renames WH-NNN → ER-NNN, updates status, calls `normalize_references()`; `demote_hypothesis(er_id)` → reverse (used by validation demotion safety)

**Serialization:** `to_json()`/`from_json()` → persisted as `RESEARCH_GRAPH.json`

### Renderers (`renderers.py`)

**Snapshot renderers** (produce full Markdown files from state):
- `render_research_state_md(state)` → `RESEARCH_STATE.md` (problem statement, conventions, WH/ER sections, dead ends, open questions)
- `render_evidence_log_md(state)` → `EVIDENCE_LOG.md` (evidence and verification on hypotheses, sorted by iteration)
- `render_critique_log_md(state)` → `CRITIQUE_LOG.md` (active/resolved/withdrawn sections with frontmatter counts)

**Helpers:** `render_background_survey(state)` — returns the background survey section as a string.

### Agent-by-agent summary

#### Orchestrator (`agents/orchestrator.py` + `orchestrator_tools.py`)

**Role:** Planning and state mutation. Mutates `ResearchState` via tools, emits `CURRENT_TASK.md`. (MD files are rendered centrally by the engine.)

**Tools** (11, via `OrchestratorToolExecutor`):
- `add_hypothesis` — creates new WH-NNN in state, auto-assigns ID; optional `from_rq` param links to originating RQ (copies evidence from RQ)
- `update_hypothesis` — updates statement/derivation for existing WH/ER
- `abandon_hypothesis` — marks as ABANDONED, records in `failed_approaches`
- `promote_hypothesis` — promotes WH → ER with guardrails (requires `h.verification.verdict == "VERIFIED"`, no HIGH-severity verifier critiques, no unresolved HIGH deep critic critiques, blocks on unestablished `depends_on`)
- `resolve_critique` — marks critique as RESOLVED with resolution text
- `update_section` — replaces content of Conventions, Strategy, or Short-term Plan
- `append_note` — appends a research note to `research_notes` list
- `add_research_question` — creates new RQ-NNN for open-ended exploration targets
- `resolve_research_question` — marks RQ as resolved, links to resulting hypothesis IDs
- `record_dead_end` — records a dead-end approach directly without creating a hypothesis
- `set_next_task` — emits next task with structured dispatch params (background, method_hints, assumptions, relevant_results); triggers `stop_after_round` to end agent loop

**Context (largest in the system):**
- `context_prefix` from engine — violations, termination blockers, evidence results, verified hypotheses, verification results, agent failures (6 consumed-once banners)
- Completion analysis banner (if ER count sufficient, or budget pressure) — includes synthesis instruction
- Full research state, critique log, and metrics — all rendered from `self.research_state` via renderers (not from file reads)

**Output processing:** Only processes if `_tool_executor.mutations_applied` is true. Writes `CURRENT_TASK.md` from `set_next_task` tool data. (MD files are rendered centrally by the engine's `_render_files_for_git()`, not by individual agents.)

**Prompt rules (key):** COMPUTE-FIRST (new hypotheses get verification before critique); converged derivation → move to verification; stall loops → escalate or downgrade; LOW critiques don't block promotion.

#### Researcher (`agents/researcher.py`)

**Role:** Analytical reasoning, derivation, and critique resolution. No code execution.

**Tools** (2, via `ToolExecutor`):
- `submit_result` — structured exit with target_id, description, method, result, confidence, notes. Sets `stop_after_round = True`.
- `report_progress` — progress check with findings_so_far, remaining_questions, ready_to_conclude. Does NOT stop.

**Context:** `CURRENT_TASK.md` + light research context (conventions, strategy, established results, open questions) rendered from `self.research_state`.

**Output processing:** Builds `Evidence` (type="research") from `submit_result` tool params (or fallback from response text). Stores evidence on the target entity (RQ or WH) in `research_state`.

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

#### Verifier (`agents/verifier.py` + `verifier_tools.py`)

**Role:** Adversarial verification of hypotheses. Gets **focused context** (WH + evidence + light state), not the full research state.

**Tools** (3, via `VerifierToolExecutor`):
- `submit_verdict` — final verification verdict with target_id, verdict (VERIFIED/REFUTED/INCONCLUSIVE), reasoning, notes. Sets `stop_after_round = True`. Verifier critique IDs use `VCRIT-NNN` prefix.
- `submit_critique` — files a critique with severity/argument during verification. Does NOT stop (can file multiple critiques before verdict). Auto-increments VCRIT-NNN.
- `report_progress` — progress check. Does NOT stop.

**Context (focused, not full state):** `CURRENT_TASK.md` + claim under verification (statement, derivation) + evidence (type, approach, method, result, scripts, output, reasoning, confidence) + originating RQ (if any) + light established context (ER list) + conventions.

**Output processing:** Builds `VerificationResult` from `submit_verdict` tool data (or fallback INCONCLUSIVE). Stores verification on the target hypothesis in `research_state`. Filed critiques are included in the `VerificationResult.critiques` list.

**3-valued verdict system:** VERIFIED / REFUTED / INCONCLUSIVE

#### Deep Critic (`agents/critic.py` + `critic_tools.py`)

**Role:** Strategy and direction review via agentic tool-use. Refocused on **strategy/direction only**, not per-claim verification (which is handled by the verifier). Files structured critiques, never suggests fixes.

**Tools** (2, via `CriticToolExecutor`):
- `submit_critique` — files a critique with severity/target_id/argument; auto-increments CRIT-NNN; does NOT stop the loop (can file multiple critiques)
- `finish_review` — completes review with summary; triggers `stop_after_round`

**Context:** Full research state, background survey, and previous critiques — all rendered from `self.research_state` via renderers (not from file reads).

**Output processing:**
- If critiques filed: creates `Critique` objects with `CritiqueStatus.ACTIVE` in `research_state.critiques`, links to target hypotheses
- If no critiques filed: records clean review in `research_state.critic_clean_reviews`, sets `_no_critiques_filed` flag (clean review signal to orchestrator via engine)

**Severity rules:**
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

**`call_llm`** — stateless one-shot. Uses `_call_provider_with_retry()` for API resilience. Returns `LLMResponse(text, input_tokens, output_tokens, stop_reason, duration)`. Used by surveyor, compressor, formatter.

**`run_agent_loop`** — stateful multi-turn. Maintains a growing `messages` list across rounds. Each round: LLM response → tool extraction → tool executor's `execute()` → tool result fed back. Checks `tool_executor.active_tools` each round for dynamic tool switching (used by computer agent to remove `document_approach` after first call). Returns `AgentResult(text, tool_calls, total_input/output_tokens, rounds, truncated, duration, stop_reason)`. Used by orchestrator, researcher, computer, verifier, and critic. On `max_rounds` exhaustion or `tool_call_failure`, forces a single text-only final call via agent-agnostic user message (system prompt unchanged); empty text is honest failure (no synthesis fallback). Agent-agnostic warnings at `max_rounds-2` and `max_rounds-1` (no agent-specific format references).

Both paths go through `_call_provider_with_retry()` which wraps every provider call in an exponential-backoff retry loop (see §7 for details).

**Logging:** Every LLM call produces:
- JSONL event entry in `EVENT_LOG.jsonl` via `log_llm_call()` (metadata only, no prompts; includes round number for tool-use and per-call cost; `kind: "llm_call"`)
- Full conversation log in `logs/iter{NNN}_{agent}_{seq}.md` (system prompt + context + response)

### Tool execution (`tools.py`, `orchestrator_tools.py`, `verifier_tools.py`, `critic_tools.py`)

Four tool executors, one per agentic agent type:

**`ToolExecutor`** (researcher and computer) — tool set determined by agent type:

For `RESEARCH`:
1. **`submit_result`** — structured exit. Params: `target_id`, `description`, `method`, `result`, `confidence` (exact/approximate/partial), `notes`. Sets `stop_after_round = True`.
2. **`report_progress`** — progress check. Params: `findings_so_far`, `remaining_questions`, `ready_to_conclude`. Does NOT stop.

For `COMPUTE`:
1. **`document_approach`** — records plan before coding. Params: `approach`, `assumptions`, `expected_outcome`. Available only on round 1, then removed via `active_tools`. Does NOT stop.
2. **`execute_python`** — requires `purpose` and `code`. `purpose` preserved in logs; only `code` executed.
3. **`submit_result`** — same as researcher.
4. **`report_progress`** — same as researcher.

**`OrchestratorToolExecutor`** (orchestrator) — 11 state-mutation tools:
1. **`add_hypothesis`** — creates new WH-NNN in `ResearchState`; optional `from_rq` links to originating RQ (copies evidence)
2. **`update_hypothesis`** — updates statement/derivation
3. **`abandon_hypothesis`** — marks ABANDONED, records `FailedApproach`
4. **`promote_hypothesis`** — WH → ER with guardrails (requires `h.verification.verdict == "VERIFIED"`, HIGH critique checks from both verifier and deep critic, dependency checks)
5. **`resolve_critique`** — marks CRIT-NNN as RESOLVED with resolution text
6. **`update_section`** — replaces Conventions, Strategy, or Short-term Plan content
7. **`append_note`** — appends to `research_notes` list
8. **`add_research_question`** — creates new RQ-NNN for open-ended exploration targets
9. **`resolve_research_question`** — marks RQ as resolved, links to resulting hypothesis IDs
10. **`record_dead_end`** — records dead-end approach directly
11. **`set_next_task`** — emits task with structured dispatch params (background, method_hints, assumptions, relevant_results); sets `stop_after_round = True`

Tracks `mutations_applied` (bool) and `resolved_critique_ids` (set) for `process_response` to use.

**`VerifierToolExecutor`** (verifier) — 3 verification tools:
1. **`submit_verdict`** — final verdict with target_id, verdict, reasoning, notes; sets `stop_after_round = True`
2. **`submit_critique`** — files critique with severity/argument during verification; auto-increments VCRIT-NNN; does NOT stop
3. **`report_progress`** — progress check; does NOT stop

Accumulates `verdict_data` (dict) and `filed_critiques` (list) for `process_response` to convert into `VerificationResult`.

**`CriticToolExecutor`** (critic) — 2 review tools:
1. **`submit_critique`** — files critique with severity/target_id/argument; auto-increments CRIT-NNN; does NOT stop
2. **`finish_review`** — captures summary; sets `stop_after_round = True`

Accumulates `filed_critiques` (list) for `process_response` to convert into `Critique` objects.

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
- `count_unresolved_critiques` — counts by severity via regex
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
  CURRENT_TASK.md        ← Orchestrator writes via set_next_task tool; consumed by dispatched agent
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
           ├──► Verifier stores VerificationResult on hypotheses
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
2. **Explore** — `researcher` or `computer` investigates the question → `submit_result` → `Evidence` stored on RQ
3. **Working Hypothesis (WH-NNN)** — orchestrator calls `add_hypothesis` (optionally with `from_rq` to link to originating RQ and copy evidence), creates `Hypothesis` with status WORKING. Direct WH creation (skipping RQ) is allowed when the claim is already concrete.
4. **Verify** — `verifier` examines WH + evidence → `submit_verdict` → `VerificationResult` stored on hypothesis (VERIFIED / REFUTED / INCONCLUSIVE); may also file critiques via `submit_critique`
5. **Critique** — deep critic reviews strategy/direction via `submit_critique` tool, files objections
6. **Established Result (ER-NNN)** — orchestrator calls `promote_hypothesis` tool with guardrails: (a) `h.verification.verdict == "VERIFIED"`, (b) no HIGH-severity verifier critiques, (c) no unresolved HIGH deep critic critiques, (d) all `depends_on` entries are already established
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
{"kind": "scaffold", "ts": "2026-03-13T14:22:01+00:00", "iter": 5, "category": "state_invariants", "event": "er_demotion", "detail": "ER-003 → WH-003 (REFUTED verification)"}
{"kind": "llm_call", "ts": "2026-03-13T14:22:03+00:00", "iter": 5, "agent": "computer", "model": "claude-sonnet-4-6", "input_tokens": 12340, "output_tokens": 1890, "cost": 0.042, "round": 3}
```

Common fields: `kind` (`"scaffold"` or `"llm_call"`), `ts` (UTC ISO-8601), `iter` (scaffolding iteration). Scaffold events additionally have `category`, `event`, `detail`. LLM call events additionally have `agent`, `model`, `input_tokens`, `output_tokens`, `cost`, `round`. Both functions never raise — failures are silently swallowed (`except OSError: pass`).

**Event keys by category:**

| Category | Event keys |
|----------|-----------|
| `call_reliability` | `api_retry`, `tool_call_failure_fallback`, `progress_check`, `forced_final_call`, `forced_final_call_failed`, `empty_end_turn_fallthrough`, `tool_timeout`, `tool_output_truncation` |
| `state_invariants` | All `Violation.check` values from validation checks (e.g. `er_demotion_safety`, `phantom_labels`, `stale_unverified_labels`, `critique_resolution_consistency`) |
| `loop_control` | `forced_critic`, `compute_enrichment`, `termination_blocked`, `dispatch_failure`, `routing_conflict_corrected`, `no_critiques_filed`, `status_field_exit`, `compute_verdict_failed`, `agent_failure_max_tokens`, `agent_failure_max_rounds`, `max_tokens_no_retry` |
| `output_normalization` | `problem_statement_enforced`, `header_normalized`, `critique_resolved`, `bracket_flattened`, `preamble_stripped`, `critique_self_retracted`, `empty_response_stub`, `header_injected`, `claim_id_injected`, `submit_verdict_text_extracted` |

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
| Two-round escalating warning | At `max_rounds - 2`: warning; at `max_rounds - 1`: CRITICAL | Inject agent-agnostic user-turn messages with escalating urgency (no agent-specific format references); mentions `submit_verdict` as preferred exit |
| Forced text-only final call | Loop exits via max rounds or tool-call failure | Single forced text-only call via agent-agnostic user message (system prompt unchanged), `tools` omitted, stop_reason set to `max_rounds_forced`; empty text is honest failure (no synthesis fallback) |
| `submit_verdict` / `submit_result` structured exit | Model calls exit tool | Structured data bypasses free-text generation; sets `stop_after_round` → `executor_stop`; `process_response` creates `Evidence` or `VerificationResult` from tool parameters. Plays WITH tool-calling tendency instead of against it |
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
| **ER demotion** | `check_er_demotion_safety()` | Demotes ER-NNN when `h.verification.verdict == "REFUTED"` → silently rewrites state (not injected into orchestrator context to prevent re-promotion churn) | LLM promoting WH to ER despite REFUTED verification |
| **Phantom label stripping** | `check_phantom_labels()` | Builds verified set from `h.verification.verdict == "VERIFIED"`; finds "VERIFIED" labels in derivations without backing → returns violation | LLM copying "VERIFIED" from existing text without evidence |
| **Stale-unverified label promotion** | `check_stale_unverified_labels()` | Finds hypotheses with stale unverified labels that now have verification → returns violation | Labels stuck as [unverified] after late verification |
| **Critique resolution consistency** | `check_critique_resolution_consistency()` | Checks that resolved critiques actually had their fixes applied: target hypothesis still exists, no inconsistencies | LLM marking critiques "resolved" without applying the fix |
| **Termination gate** | `can_terminate()` | Blocks termination unless: (1) critic pass occurred when VERIFIED hypotheses exist, (2) zero unresolved HIGH critiques, (3) computational evidence exists when `requires_numerical`, (4) all RQs resolved/abandoned, (5) all WHs either verified+promoted or abandoned | LLM trying to terminate prematurely |

### loop_control — Pre-dispatch hooks and dispatch guards (`engine.py`)

#### Pre-dispatch hooks

| Mechanism | Function | Condition | Action | Failure compensated |
|-----------|----------|-----------|--------|---------------------|
| Forced critic | `_critic_overdue()` + `_make_forced_critic_task()` | Overdue AND new content exists | Skip orchestrator, dispatch critic directly (saves an LLM call) | Orchestrator skipping critic indefinitely |
| Compute enrichment | `_enrich_compute_task_with_prior_failures()` | COMPUTE task with prior failures on same claim | Append failure excerpts to task body | Model repeating identical failing code |

Non-VERIFIED verification verdicts go to `pending_compute_verdicts` in `LoopState` (with `notes` and `failure_detail`), rendered as a VERIFICATION RESULTS banner in `_build_context_prefix()`. VERIFIED verdicts go to `pending_verified_results`, rendered as a VERIFIED HYPOTHESES banner. Evidence results from researcher/computer go to `pending_explore_results`, rendered as an EVIDENCE RESULTS banner. The orchestrator decides how to respond (re-verify, re-derive, promote, or accept). No auto-recompute.

#### Engine-level guards

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Transient-error catch at dispatch | `run()` try/except | If a transient error escapes retry logic, skip iteration with `continue` and file a `dispatch_failure` violation | API errors crashing the entire session |
| Routing conflict auto-correction | `_dispatch()` | Checks `assigned_to` against `TASK_TYPE_AGENT_MAP`; corrects empty/invalid values; routes by task type if `assigned_to` disagrees | Orchestrator assigning wrong agent |
| Unknown task type fallback | `_dispatch()` | Routes unknown types to researcher | LLM hallucinating task types |
| Scaffolding-maintained iteration counter | `_update_research_iteration()` | Updates `iteration` on ResearchState unconditionally at the top of each iteration | LLM forgetting or corrupting iteration count |
| Status field safety-net exit | `_check_status_field()` | Reads ResearchState for `status: completed/abandoned/partially_complete` → exits loop | Loop continuing past a declared terminal state |
| NO_CRITIQUES_FILED handling | `_dispatch()` | Detects `_no_critiques_filed` flag on critic agent → files `critic_clean` violation telling orchestrator to proceed to synthesize | Empty critic looping indefinitely |
| Dispatch-level result tracking | `_track_agent_result()` | For RESEARCH/COMPUTE: checks evidence on target entity → `pending_explore_results`; for VERIFY: checks h.verification → VERIFIED → `pending_verified_results` (clears failure count), non-VERIFIED → `pending_compute_verdicts` with notes/failure_detail | Orchestrator unaware of agent results |
| Agent failure routing | `_record_agent_failures()` + `_build_context_prefix()` | Records max_tokens truncation, max_rounds exhaustion, and non-VERIFIED verdicts; shows "AGENT FAILURES" banner to orchestrator on next pass | Orchestrator re-issuing identical failing tasks without awareness of prior failures |
| Violations/blockers/evidence/verified/verdicts/failures as context prefix | `_build_context_prefix()` | 6 sections: violations, termination blockers, evidence results, verified hypotheses, verification results (with notes/failure_detail and stall warnings at limit), agent failures — serialised into orchestrator's next user message; all consumed-once (cleared after read) | Orchestrator ignoring validation failures or agent results |
| Compression at soft threshold | `_check_compression()` | Compresses files exceeding `compress_soft_multiplier` × threshold (single tier) | Runaway file growth crashing context window |

### output_normalization — Agent-level corrections and parsing tolerance

#### Orchestrator corrections (`agents/orchestrator.py`, `orchestrator_tools.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Budget-aware completion analysis | `_completion_analysis()` | Injects "COMPLETION CHECK" or "BUDGET SYNTHESIS REQUIRED" banners based on ER/WH/critique counts and remaining budget | Orchestrator failing to terminate when done |
| Phantom marker cleaning | `build_context()` | Flattens nested `[[[ID:unverified]...]]` brackets and replaces `[ID:unverified]` with `ID (unverified)` before the LLM sees the state | LLM copying bracket syntax into new outputs |
| `promote_hypothesis` guardrails | `OrchestratorToolExecutor._promote_hypothesis()` | Rejects promotion if verification verdict is not VERIFIED, or if unresolved HIGH critiques exist on target | LLM promoting unverified or contested hypotheses |
| Conventions section staleness reminder | `build_context()` | From iteration 3+, injects banner if Conventions still says "To be populated" | LLM skipping conventions population |

#### Critic corrections (`agents/critic.py`, `critic_tools.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| `_no_critiques_filed` flag | `process_response()` | Sets flag when `CriticToolExecutor.filed_critiques` is empty → engine signals orchestrator to proceed to synthesize | Empty critic looping indefinitely |
| Auto-increment CRIT-NNN | `CriticToolExecutor._submit_critique()` | Auto-assigns next CRIT-NNN based on existing critique count | LLM using wrong or duplicate critique IDs |

#### Researcher/Computer corrections (`agents/researcher.py`, `agents/computer.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Structured tool exit → Evidence object | `process_response()` | Routes `submit_result` tool data to `Evidence` object stored on target entity in `ResearchState` | Ensures structured data reaches state regardless of LLM output format |
| Fallback evidence on no exit tool | `process_response()` | Creates minimal `Evidence` with "Agent produced no exit tool call" when no `submit_result` called | Agent producing no output despite forced final call |
| Dynamic tool set | `ToolExecutor.tools_for_task_type()` | Sets researcher tools (submit_result + report_progress) vs computer tools (document_approach + execute_python + submit_result + report_progress) based on task type | Prevent model from accessing wrong tools for the agent type |
| `active_tools` dynamic switching | `ToolExecutor.active_tools` property | After `document_approach` called, removes it from tool set so model cannot re-document instead of coding | Model avoiding code execution by repeatedly documenting approach |
| NameError recovery hint | `_execute_python()` in `tools.py` | Detects NameError in stderr and appends "FRESH Python process" reminder | Models (esp. Kimi K2.5) treating execute_python as persistent REPL, causing NameErrors from referencing prior-script variables |
| Structured output header | `_execute_python()` in `tools.py` | Prepends `=== script_name ===` header with purpose and exit status to every script output | Model loses track of which script produced which output across multi-call sessions |

#### Verifier corrections (`agents/verifier.py`, `verifier_tools.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Structured tool exit → VerificationResult | `process_response()` | Routes `submit_verdict` tool data + filed critiques to `VerificationResult` stored on target hypothesis | Ensures structured verification data reaches state |
| Fallback INCONCLUSIVE on no exit tool | `process_response()` | Creates `VerificationResult` with verdict="INCONCLUSIVE" when no `submit_verdict` called | Agent producing no verdict despite forced final call |
| Auto-increment VCRIT-NNN | `VerifierToolExecutor._submit_critique()` | Auto-assigns next VCRIT-NNN based on existing critique count | LLM using wrong or duplicate critique IDs |

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
| `test_orchestrator_tools.py` | 1150 | OrchestratorToolExecutor: add/update/abandon/promote hypothesis, resolve critique, set_next_task, append_note, update_section |
| `test_tools.py` | 1060 | ToolExecutor, submit_result, document_approach, run_agent_loop, truncation, token accumulation, active_tools |
| `test_markdown.py` | 972 | Frontmatter, sections, critique lifecycle, header normalisation |
| `test_research_state.py` | 929 | ResearchState dataclass, query methods, promote/demote, normalize_references, Evidence/VerificationResult, serialization |
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
| `test_critic_tools.py` | 129 | CriticToolExecutor: submit_critique, finish_review, auto-numbering |
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
- VerifierToolExecutor integration (verifier agent process_response)

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

All documentation was synced with the codebase on 2026-03-18.

- **ResearchState as source of truth** — `research_state.py` (480 lines) provides authoritative structured state; agents mutate via tools, Markdown rendered from state for git snapshots only
- **Five agentic agents** — orchestrator (11 tools via `OrchestratorToolExecutor`), researcher (2 tools via `ToolExecutor`), computer (4 tools via `ToolExecutor` with `active_tools` dynamic switching), verifier (3 tools via `VerifierToolExecutor`), critic (2 tools via `CriticToolExecutor`); all use `stop_after_round` mechanism
- **Three specialized work agents** — researcher (analytical), computer (code), verifier (adversarial verification); each has focused prompt, tool set, and context view; evidence inlined on entities as `Evidence` and `VerificationResult`
- **Renderers** — `renderers.py` produces Markdown snapshot files from ResearchState; agents render context from `self.research_state` via renderers; MD files rendered centrally by engine's `_render_files_for_git()`
- **Formatter agent** — `agents/formatter.py` produces `ANSWER.md` on successful termination
- Multi-provider support (Anthropic, OpenAI, Google Gemini, HuggingFace) fully implemented with `models.yaml` registry
- API retry with exponential backoff implemented (transient errors + tool-call failures)
- 4 post-integration validation checks (er_demotion_safety, phantom_labels, stale_unverified_labels, critique_resolution_consistency); all take `research_state: ResearchState`; checks use `h.verification.verdict` rather than computation-based lookups
- Scaffolding-maintained iteration counter (no longer LLM-dependent)
- `verify.py` remains Anthropic-only
- Event log (`EVENT_LOG.jsonl`) instrumentation implemented across all 4 categories
- Workspace directory names include model label (e.g. `20260313_142530_hawking_temperature_claude-sonnet-4-6`)
- Problems organized into `problems/tier1/` (10 core), `problems/tier2/` (12 advanced), and `problems/critpt/` (quantum error correction decomposition)
- `evaluate.py` provides answer evaluation for one-shot LLM responses; `one_shot.py` provides a one-shot baseline for comparison
