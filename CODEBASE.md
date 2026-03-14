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

SciRalph is a multi-agent scaffolding system for autonomous scientific research in theoretical physics. Five agents take turns in a main loop, communicating exclusively through Markdown files with YAML frontmatter. LLM calls go through a provider abstraction layer supporting Anthropic, OpenAI, Google Gemini, and HuggingFace.

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
     │  orchestrator  researcher  computationalist  │
     │  critic        compressor                    │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │              providers/                      │
     │  anthropic  openai  google  huggingface      │
     │  (LLMProvider ABC + ProviderResponse)        │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │              validation.py                   │
     │  Post-integration checks (8 checks)          │
     │  Termination gates (can_terminate)            │
     └─────────────────────────────────────────────┘
```

### Key design decisions

- **Fresh context per call.** Agents are stateless — they read from disk, call the LLM, and write back to disk. No conversation history is carried between iterations.
- **Staging discipline.** The researcher writes to `PROPOSED_CHANGES.md`, never to `RESEARCH_STATE.md`. The orchestrator reviews and integrates on its next pass. No agent can self-certify its own results.
- **Mandatory critic passes.** The scaffold forces critic reviews every N iterations, regardless of agent judgment.
- **All state in Markdown.** Every piece of research state, computation result, and critique is persisted in version-controlled Markdown files.
- **Provider-agnostic.** LLM calls go through a `providers/` abstraction layer. Model selection is resolved via `models.yaml` registry (friendly key → provider + model_id + env_key + cost). The `verify.py` script is Anthropic-only.

### Source file map

| File | Lines | Purpose |
|------|------:|---------|
| `main.py` | 91 | CLI entry point, arg parsing, workspace naming (includes model label in dir name) |
| `engine.py` | 670 | `SciRalph` class: main loop, dispatch, overrides, compression, scaffolding log events |
| `validation.py` | 597 | Post-integration checks (8 checks), `can_terminate()` gates, `Violation` dataclass |
| `config.py` | 155 | `Config` dataclass, 3-tier config builder, model resolution from `models.yaml` |
| `task.py` | 82 | `Task` dataclass, `TaskType` enum, YAML serialization |
| `llm.py` | 526 | Provider-agnostic LLM wrapper (`call_llm`, `run_agent_loop`), retry, logging, scaffolding log events |
| `tools.py` | 129 | `ToolExecutor`, `ToolCall`, `execute_python` tool schema |
| `workspace.py` | 224 | File I/O, git ops, phantom reference validation, `log_scaffold_event()` |
| `markdown.py` | 502 | Frontmatter parsing, critique lifecycle, stall detection, comp parsing |
| `sandbox.py` | 49 | `subprocess.run` wrapper with timeout |
| `metrics.py` | 110 | `MetricsTracker`, `METRICS.md` rendering |
| `verify.py` | 760 | Independent verification script (science + process audit) |
| `agents/base.py` | 158 | `BaseAgent` ABC, template method, retry logic |
| `agents/orchestrator.py` | 252 | Planning, integration, critique resolution, inline synthesis, scaffolding log events |
| `agents/researcher.py` | 40 | Derivations, writes `PROPOSED_CHANGES.md` |
| `agents/computationalist.py` | 75 | Agentic tool-use, verdict writing, scaffolding log events |
| `agents/critic.py` | 84 | Adversarial review, self-retraction filter, scaffolding log events |
| `agents/compressor.py` | 27 | File size management |
| `providers/__init__.py` | 24 | `create_provider()` factory + re-exports |
| `providers/base.py` | 42 | `LLMProvider` ABC + `ProviderResponse` dataclass |
| `providers/anthropic.py` | 112 | Anthropic Claude adapter |
| `providers/openai.py` | 97 | OpenAI adapter |
| `providers/google.py` | 148 | Google Gemini adapter |
| `providers/huggingface.py` | 108 | HuggingFace Inference Providers adapter |
| `models.yaml` | 101 | Model registry (friendly keys → provider, model_id, env_key, cost) |
| **Total** | **~5,044** | |

---

## 2. The Main Loop

**File:** `engine.py` — `SciRalph.run()`

The loop runs `while self.iteration < self.config.max_iterations`, incrementing `self.iteration` at the **top** of each pass (so iteration 1 is the first real turn). Each iteration follows this sequence:

```
┌─── Iteration N ──────────────────────────────────────────────────────┐
│                                                                      │
│  1. UPDATE ITERATION COUNTER (scaffolding-maintained, not LLM)       │
│     └─ _update_research_iteration(): write iteration: N to frontmatter│
│                                                                      │
│  2. ORCHESTRATOR PASS                                                │
│     └─ context_prefix: violations/blockers/displaced tasks/agent failures│
│     └─ Reads all state → integrates PROPOSED_CHANGES.md              │
│     └─ Emits CURRENT_TASK.md                                         │
│                                                                      │
│  3. POST-INTEGRATION VALIDATION                                      │
│     └─ validate_post_integration(): 8 checks                        │
│     └─ Violations queued for next orchestrator pass                  │
│                                                                      │
│  4. _apply_overrides() — consolidated priority chain                 │
│     P1. Budget enforcement (≤1 iter left) → synthesize               │
│     P2. Stale-loop backstop (≥2 stale iters) → synthesize            │
│     P3. Forced critic (overdue) → critique                           │
│     P3b. Redundant critic suppression → synthesize                   │
│     P5. Stall block (stalled claim) → research                       │
│     P4. REFUTED/INCONCLUSIVE recompute (gated, +P6 enrichment) → compute │
│     P6. Enrichment (prior failure context, additive)                 │
│                                                                      │
│  5. TERMINATION GATE                                                 │
│     └─ TERMINATE → can_terminate() gate                              │
│        └─ Allowed → break                                            │
│        └─ Blocked → continue (blockers shown next pass)              │
│                                                                      │
│  6. DISPATCH to researcher / computationalist / critic               │
│     └─ Wrapped in try/except for transient API errors                │
│     └─ _record_agent_failures(): capture max_tokens/max_rounds/etc.  │
│                                                                      │
│  7. POST-DISPATCH CHECKS                                             │
│     └─ Verdict tracking with failure counter after COMPUTE           │
│     └─ Stall tracking update                                         │
│     └─ Phantom reference check on agent output                       │
│     └─ NO_CRITIQUES_FILED detection after CRITIQUE                   │
│                                                                      │
│  8. COMPRESSION + METRICS + GIT COMMIT                               │
│                                                                      │
│  9. STATUS FIELD SAFETY NET                                          │
│     └─ Reads status from RESEARCH_STATE frontmatter                  │
│                                                                      │
│  ── SCAFFOLDING LOG (cross-cutting) ──────────────────────────────── │
│     Steps 1–9 emit events to SCAFFOLDING_LOG.jsonl whenever a        │
│     compensation mechanism fires (see §7).                           │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Task override ordering

All overrides are consolidated in `_apply_overrides()` with explicit priority:

| Priority | Override | Condition | Result |
|----------|----------|-----------|--------|
| P1 | Budget enforcement | ≤ 1 iteration remaining | → synthesize |
| P2 | Stale-loop backstop | ≥ 2 consecutive stale iters (ER ≥ min, WH = 0) | → synthesize |
| P3 | Forced critic | > `critic_every_n` since last critic AND new content since last critic | → critique |
| P3b | Redundant critic suppression | Scheduled critique but no new content since last critic | → synthesize |
| P5 | Stall block | COMPUTE task targets a stalled claim (≥ threshold consecutive failures) | → research |
| P4 | REFUTED/INCONCLUSIVE recompute | Previous REFUTED/INCONCLUSIVE verdict, count < `stall_recompute_limit` | → compute (P6 enrichment applied before return) |
| P6 | Enrichment | COMPUTE task with prior failures on same claim | Mutates task body (additive) |

Higher priority wins. Displaced tasks are logged and shown to orchestrator on next pass via `context_prefix`.
P5 is checked before P4 so stalled claims cannot be force-recomputed.

### Termination paths

| Path | Where | Condition |
|------|-------|-----------|
| Explicit terminate | Step 5 | Orchestrator emits `terminate` → `can_terminate()` gate passes |
| Stale-loop backstop | Step 4 (P2) | Forces synthesize → next pass terminates |
| Status field | Step 9 | Agent wrote `status: completed/abandoned/partially_complete` |
| Budget exhaustion | Step 4 (P1) | Forces synthesize → next pass terminates |
| Max iterations | Loop condition | `self.iteration >= self.config.max_iterations` |

The `can_terminate()` gate requires: at least one VERIFIED computation triggers a mandatory critic pass, no unresolved HIGH critiques, and numerical verification when `requires_numerical: true` in problem YAML. If blocked, blockers are fed back to orchestrator.

### Dispatch routing

| TaskType | Agent | Notes |
|----------|-------|-------|
| `research`, `derive`, `resolve`, `synthesize` | Researcher | Synthesize rarely dispatched; orchestrator typically writes synthesis inline |
| `compute` | Computationalist | Post-dispatch: REFUTED detection + stall tracking |
| `critique` | Critic | Post-dispatch: NO_CRITIQUES_FILED detection |
| Unknown | Researcher | Fallback with console warning |

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

The `tools` class attribute is the **single switch** between one-shot and agentic behavior. Currently only the computationalist sets `tools = ToolExecutor.TOOL_DEFINITIONS`.

**Retry on truncation:** `_call_with_retry` retries up to `max_retries_on_max_tokens` times when `stop_reason == "max_tokens"`. On retry, it truncates the context (keeps first 20% + last 60%) to reduce prompt size.

### Agent-by-agent summary

#### Orchestrator (`agents/orchestrator.py`)

**Role:** Planning only. Reads all state, emits `CURRENT_TASK.md`, and integrates `PROPOSED_CHANGES.md` into `RESEARCH_STATE.md`.

**Context (largest in the system):**
- `context_prefix` from engine — violations, termination blockers, displaced tasks, agent failures
- Completion analysis banner (if ER count sufficient, or budget ≤ 3) — includes inline synthesis instruction
- Computation stall warnings (≥ threshold consecutive non-VERIFIED on same claim)
- Full `RESEARCH_STATE.md`, `CRITIQUE_LOG.md`, tail of `COMPUTATION_LOG.md`, `METRICS.md`
- `PROPOSED_CHANGES.md` (when present)

**Output parsing:** Splits on literal delimiter strings `=== RESEARCH_STATE.md ===` and `=== CURRENT_TASK.md ===`. If both present, writes state update and deletes PROPOSED_CHANGES. If only task delimiter, writes task only. No delimiters at all → entire output becomes CURRENT_TASK.

**Key behaviors:**
- `_enforce_problem_statement` — regex-replaces the Problem Statement section after every state integration to prevent LLM paraphrasing
- `_resolve_critiques` — scans its own output for resolved critique IDs via four patterns (YAML list, YAML mapping, forward prose, reverse prose), then physically moves critique blocks from Active to Resolved
- `_completion_analysis` — injects completion/budget-pressure/inline-synthesis banners into context
- **Inline synthesis** — when all problem steps are established, the orchestrator writes a `## Synthesis` section directly into RESEARCH_STATE.md and emits `terminate`
- `detect_computation_stalls` — groups COMPUTATION_LOG entries by claim, reports streaks of ≥ N non-VERIFIED

**Prompt rules (key):** COMPUTE-FIRST (new hypotheses get verification before critique); converged derivation → move to verification; stall loops → escalate or downgrade; LOW critiques don't block promotion.

#### Researcher (`agents/researcher.py`)

**Role:** Derivations, proofs, hypothesis generation. Writes only to `PROPOSED_CHANGES.md`.

**Context (leanest):** `CURRENT_TASK.md` + `RESEARCH_STATE.md`. For RESOLVE tasks, adds relevant critique sections from `CRITIQUE_LOG.md`.

**Output:** Entire response → `PROPOSED_CHANGES.md`. The simplest agent mechanically.

**Prompt rules:** Mandatory confidence tags (HIGH/MEDIUM/LOW) on every claim. MEDIUM/LOW claims must specify a verification method.

#### Computationalist (`agents/computationalist.py`)

**Role:** Code-based verification via `execute_python` tool. The only agentic (tool-use) agent.

**Context:** `CURRENT_TASK.md` + full `RESEARCH_STATE.md`. Prior failure context is injected into the task by the engine, not by this agent.

**Tool-use loop:** LLM writes Python → `ToolExecutor` runs it in sandbox → output fed back → LLM iterates or writes final verdict. Up to `max_tool_rounds` (default 10) rounds.

**Output processing:**
- Empty text → synthesizes fallback INCONCLUSIVE entry
- Ensures `##` header present
- Appends iteration/tool-call metadata
- Appends to `COMPUTATION_LOG.md` (log grows, never overwrites)
- Updates frontmatter computation count

**Prompt rules (critical):**
- Numerical spot-checks always required (5+ parameter values, `np.isclose` with `rtol=1e-6`)
- Never use `assert` (crashes waste a tool call)
- Independence: never hardcode the tested formula on both sides
- Never widen tolerance on failure → verdict must be INCONCLUSIVE
- REFUTED requires convergent failures at ≥ 2 test points + both numerical and symbolic disagree
- Execution errors → INCONCLUSIVE, never REFUTED

**3-valued verdict system:** VERIFIED / REFUTED / INCONCLUSIVE

#### Critic (`agents/critic.py`)

**Role:** Adversarial review. Finds flaws, never suggests fixes.

**Context (heaviest one-shot):** Full `RESEARCH_STATE.md` + `COMPUTATION_LOG.md` + `CRITIQUE_LOG.md`.

**Two-phase format:**
- Phase 1: Reproduce — restate the argument in own words; no critique yet
- Phase 2: Objection — what is wrong, why it matters, suggested verification
- If Phase 1 arrives at the same result → do NOT file a critique

**Severity rules:**
- HIGH: only for specific wrong steps (sign error, dropped term)
- MEDIUM: forced cap when objection rests on intuition, or when only INCONCLUSIVE evidence exists, or when a VERIFIED computation exists
- LOW: stylistic

**Self-retraction filter:** After the LLM responds, the scaffold scans LOW/MEDIUM-severity critiques for retraction signals in Phase 2 (9 regex patterns: "reproduction succeeded", "no issues found", "not filing", etc.) and marks them `[WITHDRAWN]`. Withdrawals are kept in the log as HTML comments.

**`NO_CRITIQUES_FILED`:** When the critic finds nothing, it outputs this marker. The scaffold treats it as an empty critic pass and signals the orchestrator to proceed to synthesize.

#### Compressor (`agents/compressor.py`)

**Role:** Shrink files exceeding size thresholds. LLM output IS the compressed file.

**Context:** The target file's content with a one-line header.

**Processing:** Archives original (timestamped copy in `archive/`), writes compressed version back.

**Rules:** Preserve ERs and unresolved critiques verbatim. Collapse resolved critiques to one-line summaries. Drop abandoned hypotheses. Never discard "what didn't work" information.

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

**`call_llm`** — stateless one-shot. Uses `_call_provider_with_retry()` for API resilience. Returns `LLMResponse(text, input_tokens, output_tokens, stop_reason, duration)`. Used by orchestrator, researcher, critic, compressor.

**`run_agent_loop`** — stateful multi-turn. Maintains a growing `messages` list across rounds. Each round: LLM response → tool extraction → `ToolExecutor.execute()` → tool result fed back. Returns `AgentResult(text, tool_calls, total_input/output_tokens, rounds, truncated, duration, stop_reason)`. Used exclusively by computationalist.

Both paths go through `_call_provider_with_retry()` which wraps every provider call in an exponential-backoff retry loop (see §7 for details).

**Logging:** Every LLM call produces:
- JSONL audit entry in `AUDIT_LOG.jsonl` (metadata only, no prompts; includes round number for tool-use and per-call cost)
- Full conversation log in `logs/iter{NNN}_{agent}_{seq}.md` (system prompt + context + response)

### Tool execution (`tools.py`)

One tool: `execute_python`. The `ToolExecutor` class:
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
- `validate_comp_references()` — scans RESEARCH_STATE for `COMP-NNN`/`TASK-NNN` refs, cross-checks against COMPUTATION_LOG entries, replaces orphaned refs with `[COMP-NNN:unverified]`
- `git_commit(message)` — `git add -A` + `git commit --allow-empty` every iteration
- `log_scaffold_event(workspace_dir, iteration, layer, event, detail)` — free function; appends one JSONL line to `SCAFFOLDING_LOG.jsonl` (see §7). Never raises (bare `except OSError: pass`). Called from engine, agents, validation, and llm modules

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

**Computation analysis:**
- `_parse_comp_entries` — extracts structured dicts from COMPUTATION_LOG (id, claim, verdict, result)
- `detect_computation_stalls` — groups by claim, finds streaks of ≥ N consecutive non-VERIFIED
- `find_prior_failures_for_claim` — finds RESULT blocks from previous failed attempts at the same claim

**Normalisation:**
- `normalize_er_wh_headers` — converts bold-line ER/WH to proper `## ` headers
- `flatten_unverified_brackets` — collapses nested `[[[ID:unverified]...]]` artifacts
- All critique regexes accept both `CRIT-NNN` and `CRITIQUE-NNN` (LLM drift tolerance)

### Metrics (`metrics.py`)

In-memory `MetricsTracker` with `CallRecord` entries. Tracks: per-call tokens, tool calls, rounds, truncation flags, alerts, cost. Renders to `METRICS.md` with adaptive columns. Not persisted between process restarts — `METRICS.md` is the durable artifact.

---

## 5. Workspace Files & Data Flow

Each run creates a timestamped workspace directory (e.g., `workspaces/20260310_142530_hawking_temperature/`):

```
workspaces/<run>/
  RESEARCH_STATE.md      ← Orchestrator writes; the canonical research document
  PROPOSED_CHANGES.md    ← Researcher writes; orchestrator integrates on next pass
  CURRENT_TASK.md        ← Orchestrator writes; consumed by dispatched agent
  COMPUTATION_LOG.md     ← Computationalist appends; grows over the run
  CRITIQUE_LOG.md        ← Critic appends; scaffold manages Active/Resolved sections
  METRICS.md             ← Engine writes every iteration
  AUDIT_LOG.jsonl        ← Append-only LLM call metadata (per-LLM-call tokens, cost)
  SCAFFOLDING_LOG.jsonl  ← Append-only scaffolding intervention events (see §7)
  computations/          ← Python scripts from tool execution (tool_exec_NNN.py)
  archive/               ← Pre-compression file copies
  logs/                  ← Full conversation logs (iter{NNN}_{agent}_{seq}.md)
  .git/                  ← One commit per iteration
```

### Data flow per iteration

```
                       RESEARCH_STATE.md ◄──────── Orchestrator (integrates)
                              │                         ▲
                              ▼                         │
                      PROPOSED_CHANGES.md ◄──── Researcher (writes)
                              │
                              │ (deleted after integration)
                              ▼
     CURRENT_TASK.md ◄──────── Orchestrator (emits)
           │
           ├──► Researcher ──► PROPOSED_CHANGES.md
           ├──► Computationalist ──► COMPUTATION_LOG.md (append)
           └──► Critic ──► CRITIQUE_LOG.md (append to Active section)
```

### Promotion pipeline

A hypothesis advances through this lifecycle:
1. **Working Hypothesis (WH-NNN)** — researcher proposes in PROPOSED_CHANGES
2. **Orchestrator integrates** — adds to RESEARCH_STATE as `## WH-NNN`
3. **Computation** — computationalist runs verification → VERIFIED / REFUTED / INCONCLUSIVE
4. **Critique** — critic reviews, files objections
5. **Established Result (ER-NNN)** — orchestrator promotes when: (a) ≥ 1 VERIFIED computation, (b) no unresolved HIGH critiques, (c) all dependencies are themselves ERs
6. **Inline synthesis** — orchestrator writes `## Synthesis` section directly into RESEARCH_STATE.md, then emits `terminate`

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
| `max_retries_on_max_tokens` | 2 | BaseAgent retry count |
| `sympy_timeout_seconds` | 60 | Sandbox per-script timeout |
| `max_tool_rounds` | 10 | Computationalist tool loop depth |
| `tool_output_limit` | 10000 | Chars per tool output before truncation |
| `zero_text_bailout` | 3 | Consecutive zero-text rounds before tool-use bailout |
| `checkpoint_round` | 2 | Tool-use round that triggers a checkpoint nudge |
| `computation_token_alert` | 150000 | Cumulative input tokens before firing alert |
| `stall_threshold` | 2 | Repeat failures on same claim before stall block |
| `stall_recompute_limit` | 2 | Max consecutive non-VERIFIED verdicts before P4 recompute blocked |
| `min_er_for_completion` | 3 | ERs needed before stale-loop backstop fires |
| `compress_threshold` | RS: 50K, CL: 30K, CompL: 40K | File size thresholds (chars) |
| `api_retry_max` | 3 | Max retry attempts on transient API errors |
| `api_retry_initial_delay` | 2.0 | Initial retry delay (seconds) |
| `api_retry_max_delay` | 30.0 | Max retry delay (seconds) |
| `api_timeout` | 120.0 | Per-API-call timeout (seconds) |

Compression tiers: alert at 1x, compress at 1.5x, force-compress at 2x (though 1.5x and 2x currently execute identical code).

---

## 7. LLM Failure Compensation

This section catalogues every mechanism that compensates for LLM misbehaviour at the scaffolding level. LLMs routinely fail in specific, predictable ways — promoting unverified results, hallucinating IDs, emitting malformed YAML, ignoring instructions, failing to terminate. The scaffolding corrects these failures across ten layers.

### Scaffolding log (instrumentation)

Every mechanism in layers 1–10 emits a structured event to `SCAFFOLDING_LOG.jsonl` via `log_scaffold_event()` (defined in `workspace.py`). Each entry is a single JSON line:

```json
{"ts": "2026-03-13T14:22:01+00:00", "iter": 5, "layer": 4, "event": "er_demotion", "detail": "ER-003 → WH-003 (no VERIFIED backing)"}
```

Fields: `ts` (UTC ISO-8601), `iter` (scaffolding iteration), `layer` (§7 layer number 1–10), `event` (mechanism key), `detail` (free-text context). The function never raises — failures are silently swallowed (`except OSError: pass`).

**Event keys by layer:**

| Layer | Event keys |
|-------|-----------|
| 1 | `api_retry` |
| 2 | `tool_call_failure_fallback`, `zero_text_bailout`, `low_text_bailout`, `forced_final_call` |
| 3 | `tool_timeout`, `tool_output_truncation` |
| 4 | All `Violation.check` values from validation checks (e.g. `er_promotion_gate`, `phantom_references`, `phantom_labels`, `stale_unverified_labels`, `verified_frontmatter_backfill`, `task_agent_routing`, `id_consistency`, `critique_resolution_consistency`) |
| 5 | `p1_budget_override`, `p2_stale_loop_override`, `p3_forced_critic`, `p3b_redundant_critic_suppressed`, `p4_refuted_recompute`, `p4_recompute_enriched`, `p4_refuted_suppressed`, `p5_stall_block`, `p6_enrichment` |
| 6 | `termination_blocked`, `dispatch_failure`, `post_dispatch_phantom`, `routing_conflict_corrected`, `no_critiques_filed`, `status_field_exit`, `compute_verdict_failed`, `compute_verdict_stall_escalation` |
| 7 | `problem_statement_enforced`, `header_normalized`, `critique_resolved`, `bracket_flattened` |
| 8 | `preamble_stripped`, `critique_self_retracted` |
| 9 | `empty_response_stub`, `header_injected` |
| 10 | (YAML parse fallback fires inside `markdown.py` which does not currently have workspace context — not yet instrumented) |

**Quick analysis:**

```bash
# Count events per mechanism across a run
jq -r '.event' workspaces/<run>/SCAFFOLDING_LOG.jsonl | sort | uniq -c | sort -rn

# Count events per layer
jq -r '.layer' workspaces/<run>/SCAFFOLDING_LOG.jsonl | sort | uniq -c | sort -rn

# Filter to a specific layer
jq 'select(.layer == 4)' workspaces/<run>/SCAFFOLDING_LOG.jsonl

# Timeline of overrides
jq 'select(.layer == 5) | "\(.iter) \(.event) \(.detail)"' workspaces/<run>/SCAFFOLDING_LOG.jsonl
```

### Layer 1 — Transport / API retry (`llm.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Transient error classification | `_is_transient()`, `_is_tool_call_failure()` | Classifies exceptions as retryable by HTTP status (429, 500–504), exception class (ConnectionError, TimeoutError, etc.), and tool-call JSON failures | API hiccups, rate limits, malformed tool JSON |
| Exponential-backoff retry | `_call_provider_with_retry()` | Wraps every `provider.call()` in a loop: up to `api_retry_max` attempts, doubling delay between `api_retry_initial_delay` and `api_retry_max_delay` | Rate limits, 5xx server errors, transient network failures |
| Tool-call failure fallback | `run_agent_loop` | If retry exhaustion is due to a tool-call failure, sets `tool_call_failure=True`, breaks the loop, and forces a text-only final call telling the model "The tool-calling interface is unavailable" | Models emitting invalid JSON for tool calls |

### Layer 2 — Agent loop resilience (`llm.py` → `run_agent_loop`)

These mechanisms prevent the computationalist from wasting rounds or producing empty output:

| Mechanism | Trigger | Action |
|-----------|---------|--------|
| Zero-text streak bailout | N consecutive tool-use rounds with no text (`zero_text_bailout`, default 3) | Break loop → forced final call |
| Low-cumulative-text bailout | < 100 chars total text at halfway point (and halfway ≥ 3) | Break loop → forced final call |
| Checkpoint message | At `checkpoint_round` (default 2) | Inject user-turn message: "CHECKPOINT: Write your COMP entry text now" |
| Two-round escalating warning | At `max_rounds - 2`: warning; at `max_rounds - 1`: CRITICAL with exact format template | Inject user-turn messages with escalating urgency and an INCONCLUSIVE fallback template |
| Interleaved text checkpoint | `text_checkpoint_interval` consecutive zero-text rounds (default 2, must be < `zero_text_bailout`) | Text-only LLM call via `_make_text_checkpoint_call()`; on success resets streak and injects assistant+user messages to resume tool use |
| Tool history synthesis | Both forced final call and retry produce empty text | `_synthesize_from_tool_history()` builds COMP-000 entry from actual tool execution history (code + output excerpts) |
| Forced text-only final call | Loop exits for any reason (max rounds, bailout, tool-call failure) | Final LLM call with `tools` omitted, strongly-worded system prompt with full COMP-NNN format, stop_reason set to `max_rounds_forced` |

### Layer 3 — Tool execution guards (`tools.py`, `sandbox.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Subprocess timeout | `sandbox.execute_python()` | Hard timeout per script via `subprocess.run(timeout=...)` | Infinite loops, exponentially expensive code |
| Structured timeout feedback | `ToolExecutor._execute_python()` | Returns "TIMEOUT: Script exceeded Ns limit" with 4 actionable suggestions | Raw timeout exception would be meaningless to LLM |
| Structured error feedback | `ToolExecutor._execute_python()` | Concatenates stdout+stderr, marks `is_error=True` | Runtime errors in generated code |
| Output truncation | `ToolExecutor._truncate_output()` | Head/tail preservation with `[...truncated...]` marker, default 10K chars | Print-heavy scripts exploding context window |
| Banned API documentation | `TOOL_DEFINITIONS` | Lists removed/renamed APIs (e.g., `scipy.misc.derivative`, `numpy.trapz`) with correct replacements | Known-bad API usage causing reproducible crashes |
| Unknown tool guard | `ToolExecutor.execute()` | `ValueError` on hallucinated tool names | Model inventing nonexistent tools |

### Layer 4 — Post-integration validation pipeline (`validation.py`)

All 8 checks run after every orchestrator pass. They are pure functions that mutate workspace files directly and return `Violation` objects injected into the orchestrator's next context.

| Check | Function | What it does | Failure compensated |
|-------|----------|--------------|---------------------|
| **ER demotion** | `check_er_promotion_gate()` Pass 1 | Scans for `## ER-NNN` headers without VERIFIED backing in COMPUTATION_LOG → silently rewrites to `## WH-NNN` + updates all prose references (not injected into orchestrator context to prevent re-promotion churn) | LLM promoting WH to ER without computation |
| **WH promotion** | `check_er_promotion_gate()` Pass 2 | Scans for `## WH-NNN` headers that DO have VERIFIED backing → promotes to `## ER-NNN` | LLM failing to promote after verification |
| **Agent routing fix** | `check_task_agent_routing()` | Corrects known aliases in `assigned_to` frontmatter ("compute"→"computationalist", "critique"→"deep_critic", etc.) | LLM using shortform agent names |
| **Phantom label stripping** | `check_phantom_labels()` | Finds "VERIFIED" in prose near ER/WH IDs without computation backing → replaces with `[unverified]` | LLM copying "VERIFIED" from existing text without evidence |
| **Stale-unverified label promotion** | `check_stale_unverified_labels()` | Finds `[unverified]` labels on IDs that now HAVE verification → restores "VERIFIED"; also applies WH→ER rename on same line | Labels stuck as [unverified] after late computation |
| **Phantom reference replacement** | `check_phantom_references()` | Scans for COMP-NNN/TASK-NNN references not in COMPUTATION_LOG → replaces with `[ID:unverified]`; flattens nested bracket artifacts | LLM hallucinating computation IDs |
| **Verified frontmatter backfill** | `check_verified_frontmatter_backfill()` | Ensures `verified_results` list in RESEARCH_STATE frontmatter includes all IDs with VERIFIED entries; normalises WH↔ER form | LLM forgetting to update frontmatter |
| **ID consistency** | `check_id_consistency()` | Corrects `total_computations` in COMPUTATION_LOG frontmatter to match actual `## COMP-NNN` header count | LLM writing wrong counter value |
| **Critique resolution consistency** | `check_critique_resolution_consistency()` | Checks that resolved critiques actually had their fixes applied: target ER/WH still exists in RESEARCH_STATE, no leftover dual WH/ER labels | LLM marking critiques "resolved" without applying the fix |
| **Termination gate** | `can_terminate()` | Blocks termination unless: (1) critic pass occurred when VERIFIED computations exist, (2) zero unresolved HIGH critiques, (3) computation exists when `requires_numerical` | LLM trying to terminate prematurely |

### Layer 5 — Engine override chain (`engine.py` → `_apply_overrides`)

| Override | Priority | Condition | Action | Failure compensated |
|----------|----------|-----------|--------|---------------------|
| Budget enforcement | P1 | ≤ 1 iter remaining | Force synthesize | Orchestrator scheduling research indefinitely |
| Stale-loop backstop | P2 | ≥ 2 stale iters (ER ≥ min, WH = 0) | Force synthesize | Loop continuing after problem is solved |
| Forced critic | P3 | Overdue AND new content exists | Force critique | Orchestrator skipping critic indefinitely |
| Redundant critic suppression | P3b | Critique scheduled but no new content | Force synthesize | Infinite critic-no-critique loop |
| Stall blocking | P5 | Compute targets claim with ≥ threshold failures | Force research with alternative-approach request | Infinite retries on same broken claim |
| REFUTED recompute | P4 | Previous REFUTED/INCONCLUSIVE verdict, count < `stall_recompute_limit` | Force compute on refuted claim | Orchestrator ignoring a REFUTED result |
| Prior-failure enrichment | P6 | Compute task with prior failures on same claim | Append last failure's METHOD+RESULT+NOTES excerpt to task body; zero-output stall gets special "ZERO-OUTPUT STALL DETECTED" warning; also applied to P4 recompute tasks | Model repeating identical failing code |

### Layer 6 — Engine-level guards (`engine.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Transient-error catch at dispatch | `run()` try/except | If a transient error escapes retry logic, skip iteration with `continue` and file a `dispatch_failure` violation | API errors crashing the entire session |
| Routing conflict auto-correction | `_dispatch()` | Checks `assigned_to` against `TASK_TYPE_AGENT_MAP`; corrects empty/invalid values; routes by task type if `assigned_to` disagrees | Orchestrator assigning wrong agent |
| Unknown task type fallback | `_dispatch()` | Routes unknown types to researcher | LLM hallucinating task types |
| Scaffolding-maintained iteration counter | `_update_research_iteration()` | Writes `iteration: N` into RESEARCH_STATE frontmatter unconditionally at the top of each iteration | LLM forgetting or corrupting iteration count |
| Status field safety-net exit | `_check_status_field()` | Reads RESEARCH_STATE for `status: completed/abandoned/partially_complete` → exits loop | Loop continuing past a declared terminal state |
| Post-dispatch phantom check | `run()` | Runs `check_phantom_references()` after every agent dispatch | Phantoms introduced by non-orchestrator agents |
| NO_CRITIQUES_FILED handling | `_dispatch()` | Detects `NO_CRITIQUES_FILED` in critic response → files `critic_clean` violation telling orchestrator to proceed to synthesize | Empty critic looping indefinitely |
| Displaced-task transparency | `_log_displacement()` + `_build_context_prefix()` | Logs every overridden task and feeds the list to the orchestrator's next context: "Consider re-scheduling if still needed" | Orchestrator unaware that its planned task was overridden |
| Dispatch-level verdict tracking | `_track_compute_verdict()` | Counts consecutive non-VERIFIED verdicts per claim; below `stall_recompute_limit` sets `_pending_recompute_claim` and `_pending_recompute_verdict` (actual verdict), at/above limit escalates to `_stalled_claims` with violation | Infinite recompute loops on persistently failing claims |
| Agent failure routing | `_record_agent_failures()` + `_build_context_prefix()` | Records max_tokens truncation, max_rounds exhaustion, and non-VERIFIED compute verdicts; shows "AGENT FAILURES" banner to orchestrator on next pass | Orchestrator re-issuing identical failing tasks without awareness of prior failures |
| Violations/blockers as context prefix | `_build_context_prefix()` | All pending violations (except ER promotion gate, which is enforced silently by state rewrite) and termination blockers serialised into orchestrator's next user message with explicit "Do NOT emit terminate again" instruction | Orchestrator ignoring validation failures |
| Forced compression at 2x threshold | `_check_compression()` | Force-compresses files exceeding 2× threshold | Runaway file growth crashing context window |

### Layer 7 — Orchestrator-level corrections (`agents/orchestrator.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Budget-aware completion analysis | `_completion_analysis()` | Injects "COMPLETION CHECK" or "BUDGET SYNTHESIS REQUIRED" banners based on ER/WH/critique counts and remaining budget | Orchestrator failing to terminate when done |
| Phantom marker cleaning | `build_context()` | Flattens nested `[[[ID:unverified]...]]` brackets and replaces `[ID:unverified]` with `ID (unverified)` before the LLM sees the state | LLM copying bracket syntax into new outputs |
| Computation stall warnings | `build_context()` | Injects "COMPUTATION STALL: N consecutive failures on claim: ..." banners | Orchestrator re-scheduling same failing compute |
| Problem statement enforcement | `_enforce_problem_statement()` | Regex-replaces the Problem Statement section with the original from problem YAML | LLM rewriting/paraphrasing the problem (scope drift) |
| Bold-to-header normalisation | `process_response()` via `normalize_er_wh_headers()` | Converts `**ER-NNN title**` bold lines to `## ER-NNN title` headers | Bold shorthand breaking all regex-based ER/WH detection |
| Multi-strategy critique resolution | `_resolve_critiques()` via `extract_resolved_critique_ids()` | Four independent patterns to extract resolved critique IDs from various LLM formats | Each LLM uses a different format for claiming resolution |
| Resolution note quality validation | `_validate_resolution_note()` | Rejects notes < 20 chars or containing system markers (`[error]`, `phantom`, `:unverified]`, `>>>`, `<<<`) | LLM emitting garbage or system artifacts as resolution notes |
| Critique metadata recount | `_resolve_critiques()` via `recount_critique_metadata()` | Recomputes unresolved_high/medium/low and total_critiques from actual file contents | LLM updating counters incorrectly |
| Conventions section staleness reminder | `build_context()` | From iteration 3+, injects banner if Conventions still says "To be populated" | LLM skipping conventions population |

### Layer 8 — Critic-level corrections (`agents/critic.py`, `markdown.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Preamble stripping | `_strip_preamble()` | Removes everything before first `## CRIT-` header | Critic producing narrative preamble before actual critiques |
| Self-retraction filtering | `filter_self_retracted_critiques()` | Scans LOW/MEDIUM critiques for 9 retraction patterns; marks as `[WITHDRAWN]`, kept as HTML comments | Critic filing critiques then immediately retracting in Phase 2 |
| Critique metadata recount | `_update_critique_metadata()` | Recounts all statistics from file and updates frontmatter, including `last_critic_pass` timestamp | Counter drift across multiple critic passes |

### Layer 9 — Computationalist-level corrections (`agents/computationalist.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Empty-response INCONCLUSIVE stub | `process_response()` | Synthesizes minimal COMP entry with VERDICT: INCONCLUSIVE when response is empty | Agent producing no text despite forced final call |
| Missing header injection | `process_response()` | Prepends `## {task_id}: Computation` if output doesn't start with `##` | Headerless entry invisible to downstream parsers |
| Computation metadata recount | `_update_computation_metadata()` | Recounts `## COMP-NNN` headers and updates `total_computations` frontmatter | LLM using TASK-NNN headers instead of COMP-NNN |

### Layer 10 — Markdown parsing tolerance (`markdown.py`)

| Mechanism | Function | What it does | Failure compensated |
|-----------|----------|--------------|---------------------|
| Code-fence stripping | `parse_frontmatter()` | Strips `` ```yaml `` / `` ``` `` wrapping YAML frontmatter | LLM wrapping YAML in code fences |
| YAML parse fallback to regex | `parse_frontmatter()` + `_fallback_parse()` | On `YAMLError`, extracts simple `key: value` pairs line-by-line | LLM producing invalid YAML (unquoted colons, trailing commas) |
| Non-dict YAML fallback | `parse_frontmatter()` | If `yaml.safe_load` returns non-dict (string, list), substitutes `{}` | LLM writing bare value instead of YAML mapping |
| CRITIQUE-NNN alias tolerance | `CRIT_ID_RE`, `CRIT_HEADER_RE`, etc. | All regexes use `CRIT(?:IQUE)?-\d+` | LLM writing "CRITIQUE-010" instead of "CRIT-010" |
| Verdict field format tolerance | `_parse_comp_entries()` | Loose regexes for CLAIM/VERDICT/RESULT (`**VERDICT:** X`, `**VERDICT**: X`, `**VERDICT** X` all match) | LLM varying punctuation in bold field markers |
| Bold-format section detection | `_ER_SECTION_RE`, `_WH_SECTION_RE` | Match both `## ER-NNN` and `**ER-NNN` patterns | LLM using bold-line-start as heading shorthand |
| Nested bracket flattening | `flatten_unverified_brackets()` | Collapses `[[[COMP-001:unverified]:unverified]]` → `[COMP-001:unverified]` | Validators re-wrapping previously wrapped brackets |

---

## 8. Verification

**File:** `verify.py` — runs as `python -m sciralph.verify <workspace_dir>`

A fully independent post-hoc evaluation. Two LLM passes using Claude Opus with streaming:

**Pass 1: Science verification** — evaluates correctness of each ER (derivation validity, computational support, critique resolution). Verdict scale: VALID / PARTIALLY_VALID / INVALID / INCONCLUSIVE. Produces per-ER assessments and chain coherence check. Optionally re-runs computation scripts.

**Pass 2: Process audit** — evaluates multi-agent process quality (error-correction cycles, computation effectiveness, orchestrator decisions, budget management). Verdict scale: EFFECTIVE / PARTIALLY_EFFECTIVE / INEFFECTIVE. Lists process events with classifications.

Output: `VERIFICATION.md` written to workspace (when `--write-report`).

---

## 9. Testing

**450 tests** across 17 test files (~7,777 lines). Run with `uv run python -m pytest -v`.

| Test file | Lines | What it covers |
|-----------|------:|----------------|
| `test_engine.py` | 1405 | Main loop, overrides, termination gates, compression, budget, stalls, status, dispatch errors |
| `test_validation.py` | 1346 | All 8 post-integration checks, can_terminate gates, violation types, critique resolution consistency |
| `test_markdown.py` | 958 | Frontmatter, sections, critique lifecycle, stall detection, comp parsing, header normalisation |
| `test_report_recommendations.py` | 931 | Report generation, recommendation analysis |
| `test_verify.py` | 582 | Workspace loading, verdict parsing, prompts, process audit, report patching |
| `test_llm_retry.py` | 530 | Retry logic, transient error classification, backoff, tool-call failure fallback |
| `test_orchestrator.py` | 515 | Response splitting, integration, completion analysis, budget, stalls, critiques, inline synthesis |
| `test_tools.py` | 364 | ToolExecutor, run_agent_loop, truncation, token accumulation |
| `test_config.py` | 224 | Defaults, YAML/CLI override, merge priority, model resolution from models.yaml |
| `test_computationalist.py` | 166 | Soft-check pattern, tools attribute, process_response, INCONCLUSIVE fallback |
| `test_workspace.py` | 132 | init structure, validate_comp_references |
| `test_provider_smoke.py` | 118 | Provider adapters: tool format, message format, stop reason normalisation |
| `test_task.py` | 125 | TaskType enum, to_markdown, from_frontmatter, round-trip |
| `test_metrics.py` | 110 | CallRecord, critic tracking, alerts, Markdown rendering |
| `test_conversation_log.py` | 80 | File naming, sections, sequence counter |
| `test_sandbox.py` | 66 | Script execution, timeout, MPLBACKEND |
| `test_scaffold_log.py` | 120 | `log_scaffold_event` JSONL output, validation integration, budget override integration |

**Testing approach:** pytest with `tmp_path` fixtures. All LLM calls are mocked (no real API calls). `SimpleNamespace` objects mock SDK responses. Fixture Markdown files for complex document parsing.

**Notable coverage gaps:**
- `call_llm` one-shot path (only `run_agent_loop` is tested via `test_tools.py`)
- BaseAgent retry logic directly
- Researcher, critic, compressor `process_response` methods
- Workspace git operations
- End-to-end `main.py` run path

---

## 10. Known Issues

### Compression 1.5x vs 2x are identical

In `_check_compression()`, the 1.5x and 2x threshold branches execute identical code (same `Task` construction, same `compressor.run()` call). Only the console message differs. If the intent was more aggressive compression at 2x, it is not implemented.

### `_check_status_field` uses string matching

```python
if f'status: "{status}"' in state or f"status: {status}" in state:
```

Checks for both quoted and unquoted YAML values by substring match on raw file text. A comment containing `status: completed` would trigger false termination.

### `Task.from_frontmatter` iteration-0 gotcha

```python
meta.get("iteration", fallback_iteration) or fallback_iteration
```

The `or` treats `0` as falsy. A task explicitly written with `iteration: 0` silently falls back. Unlikely in practice.

### `_enforce_problem_statement` edge case

Uses a DOTALL lookahead `(?=\n# )` to find the next top-level heading. If Problem Statement is the last section (no following `# ` heading), the regex won't match and the problem statement won't be enforced.

### Context accumulation in `run_agent_loop`

The `messages` list grows unboundedly across rounds. Large tool outputs can push past the model's context limit with no trimming mechanism. The `max_tool_rounds` limit is the only guard.

---

## 11. Documentation Status

All documentation was synced with the codebase in March 2026.

- Multi-provider support (Anthropic, OpenAI, Google Gemini, HuggingFace) fully implemented with `models.yaml` registry
- API retry with exponential backoff implemented (transient errors + tool-call failures)
- 8 post-integration validation checks (up from 7; added critique resolution consistency)
- Scaffolding-maintained iteration counter (no longer LLM-dependent)
- `verify.py` remains Anthropic-only
- Scaffolding log (`SCAFFOLDING_LOG.jsonl`) instrumentation implemented across layers 1–9 (layer 10 partial — `markdown.py` parse fallback not yet instrumented due to lacking workspace context)
- Workspace directory names now include model label (e.g. `20260313_142530_hawking_temperature_claude-sonnet-4-6`)
- `read_file` tool for orchestrator/researcher/critic remains planned (not implemented)
- External reference files (`files:` YAML key) remains planned (not implemented)
- Problems organized into `problems/tier1/` (10 core) and `problems/tier2/` (12 advanced)
