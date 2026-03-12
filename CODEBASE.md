# SciRalph — Codebase Reference

> **Purpose of this document:** A developer-oriented map of the codebase as it exists today (March 2026). Phase 2 engine hardening is complete — this document reflects the current system state.

## Table of Contents

- [1. Architecture Overview](#1-architecture-overview)
- [2. The Main Loop](#2-the-main-loop)
- [3. Agent System](#3-agent-system)
- [4. Infrastructure Layer](#4-infrastructure-layer)
- [5. Workspace Files & Data Flow](#5-workspace-files--data-flow)
- [6. Configuration](#6-configuration)
- [7. Verification](#7-verification)
- [8. Testing](#8-testing)
- [9. Known Issues & Ad-hoc Fixes](#9-known-issues--ad-hoc-fixes)
- [10. Planned Architecture (PLAN.md)](#10-planned-architecture-planmd)
- [11. Documentation Discrepancies](#11-documentation-discrepancies)

---

## 1. Architecture Overview

SciRalph is a multi-agent scaffolding system for autonomous scientific research in theoretical physics. Five agents take turns in a main loop, communicating exclusively through Markdown files with YAML frontmatter.

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
     │  (template      │ │  (API calls,│ │  (file I/O, git)   │
     │   method)       │ │   logging)  │ │                    │
     └────────┬────────┘ └──────┬──────┘ └────────────────────┘
              │                 │
     ┌────────▼────────────────▼───────────────────┐
     │                   Agents                     │
     │  orchestrator  researcher  computationalist  │
     │  critic        compressor                    │
     └─────────────────────────────────────────────┘

     ┌─────────────────────────────────────────────┐
     │              validation.py                   │
     │  Post-integration checks (Layer B)           │
     │  Termination gates (can_terminate)            │
     └─────────────────────────────────────────────┘
```

### Key design decisions

- **Fresh context per call.** Agents are stateless — they read from disk, call the LLM, and write back to disk. No conversation history is carried between iterations. This prevents context degradation but means each call rebuilds its window from scratch.
- **Staging discipline.** The researcher writes to `PROPOSED_CHANGES.md`, never to `RESEARCH_STATE.md`. The orchestrator reviews and integrates on its next pass. No agent can self-certify its own results.
- **Mandatory critic passes.** The scaffold forces critic reviews every N iterations, regardless of agent judgment.
- **All state in Markdown.** Every piece of research state, computation result, and critique is persisted in version-controlled Markdown files. The workspace is its own git repo.

### Source file map

| File | Lines | Purpose |
|------|------:|---------|
| `main.py` | 76 | CLI entry point, arg parsing, workspace naming |
| `engine.py` | 544 | `SciRalph` class: main loop, dispatch, overrides, termination gates |
| `validation.py` | 474 | Post-integration checks (7 checks), `can_terminate()` gates, `Violation` dataclass |
| `config.py` | 103 | `Config` dataclass, 3-tier config builder |
| `task.py` | 82 | `Task` dataclass, `TaskType` enum, YAML serialization |
| `llm.py` | 351 | `call_llm` (one-shot), `run_agent_loop` (tool-use), logging |
| `tools.py` | 126 | `ToolExecutor`, `ToolCall`, `execute_python` tool schema |
| `workspace.py` | 201 | File I/O, git ops, phantom reference validation |
| `markdown.py` | 493 | Frontmatter parsing, critique lifecycle, stall detection |
| `sandbox.py` | 49 | `subprocess.run` wrapper with timeout |
| `metrics.py` | 110 | `MetricsTracker`, `METRICS.md` rendering |
| `verify.py` | 760 | Independent verification script (science + process audit) |
| `agents/base.py` | 158 | `BaseAgent` ABC, template method, retry logic |
| `agents/orchestrator.py` | 226 | Planning, integration, critique resolution, inline synthesis |
| `agents/researcher.py` | 40 | Derivations, writes `PROPOSED_CHANGES.md` |
| `agents/computationalist.py` | 71 | Agentic tool-use, verdict writing |
| `agents/critic.py` | 79 | Adversarial review, self-retraction filter |
| `agents/compressor.py` | 27 | File size management |
| **Total** | **~3,970** | |

---

## 2. The Main Loop

**File:** `engine.py` — `SciRalph.run()`

The loop runs `while self.iteration < self.config.max_iterations`, incrementing `self.iteration` at the **top** of each pass (so iteration 1 is the first real turn). Each iteration follows this sequence:

```
┌─── Iteration N ──────────────────────────────────────────────────────┐
│                                                                      │
│  1. ORCHESTRATOR PASS                                                │
│     └─ context_prefix with violations/blockers/displaced tasks       │
│     └─ Reads all state → integrates PROPOSED_CHANGES.md              │
│     └─ Emits CURRENT_TASK.md                                         │
│                                                                      │
│  2. POST-INTEGRATION VALIDATION (Layer B)                            │
│     └─ validate_post_integration(): 7 checks                        │
│     └─ Violations queued for next orchestrator pass                  │
│                                                                      │
│  3. _apply_overrides() — consolidated priority chain                 │
│     P1. Budget enforcement (≤1 iter left) → synthesize               │
│     P2. Stale-loop backstop (≥2 stale iters) → synthesize            │
│     P3. Forced critic (overdue) → critique                           │
│     P4. REFUTED recompute → compute                                  │
│     P5. Stall block (stalled claim) → research                       │
│     P6. Enrichment (prior failure context, additive)                 │
│                                                                      │
│  4. TERMINATION GATE                                                 │
│     └─ TERMINATE → can_terminate() gate                              │
│        └─ Allowed → break                                            │
│        └─ Blocked → continue (blockers shown next pass)              │
│                                                                      │
│  5. DISPATCH to researcher / computationalist / critic               │
│                                                                      │
│  6. POST-DISPATCH CHECKS                                             │
│     └─ REFUTED verdict detection after COMPUTE                       │
│     └─ Stall tracking update                                         │
│     └─ Phantom reference check on agent output                       │
│                                                                      │
│  7. COMPRESSION + METRICS + GIT COMMIT                               │
│                                                                      │
│  8. STATUS FIELD SAFETY NET                                          │
│     └─ Reads status from RESEARCH_STATE frontmatter                  │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Task override ordering

All overrides are consolidated in `_apply_overrides()` with explicit priority:

| Priority | Override | Condition | Result |
|----------|----------|-----------|--------|
| P1 | Budget enforcement | ≤ 1 iteration remaining | → synthesize |
| P2 | Stale-loop backstop | ≥ 2 consecutive stale iters (ER ≥ min, WH = 0) | → synthesize |
| P3 | Forced critic | > `critic_every_n` since last critic | → critique |
| P4 | REFUTED recompute | Previous computation returned REFUTED | → compute |
| P5 | Stall block | COMPUTE task targets a stalled claim | → research |
| P6 | Enrichment | COMPUTE task with prior failures on same claim | Mutates task body (additive) |

Higher priority wins. Displaced tasks are logged and shown to orchestrator on next pass via `context_prefix`.

### Termination paths

| Path | Where | Condition |
|------|-------|-----------|
| Explicit terminate | Step 4 | Orchestrator emits `terminate` → `can_terminate()` gate passes |
| Stale-loop backstop | Step 3 (P2) | Forces synthesize → next pass terminates |
| Status field | Step 8 | Agent wrote `status: completed/abandoned/partially_complete` |
| Budget exhaustion | Step 3 (P1) | Forces synthesize → next pass terminates |
| Max iterations | Loop condition | `self.iteration >= self.config.max_iterations` |

The `can_terminate()` gate requires: at least one ER, a critic pass has occurred, no unresolved HIGH critiques, and numerical verification when `requires_numerical: true` in problem YAML. If blocked, blockers are fed back to orchestrator.

### Dispatch routing

| TaskType | Agent | Notes |
|----------|-------|-------|
| `research`, `derive`, `resolve`, `synthesize` | Researcher | Synthesize rarely dispatched; orchestrator typically writes synthesis inline |
| `compute` | Computationalist | Post-dispatch: REFUTED detection + stall tracking |
| `critique` | Critic | Post-dispatch: underflow retry if < 200 tokens |
| Unknown | Researcher | Fallback |

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
- `context_prefix` from engine — violations, termination blockers, displaced tasks
- Completion analysis banner (if ER count sufficient, or budget ≤ 3) — includes inline synthesis instruction
- Computation stall warnings (≥ threshold consecutive non-VERIFIED on same claim)
- Full `RESEARCH_STATE.md`, `CRITIQUE_LOG.md`, tail of `COMPUTATION_LOG.md`, `METRICS.md`
- `PROPOSED_CHANGES.md` (when present)

**Output parsing:** Splits on literal delimiter strings `=== RESEARCH_STATE.md ===` and `=== CURRENT_TASK.md ===`. If both present, writes state update and deletes PROPOSED_CHANGES. If only task delimiter, writes task only. No delimiters at all → entire output becomes CURRENT_TASK.

**Key behaviors:**
- `_enforce_problem_statement` — regex-replaces the Problem Statement section after every state integration to prevent LLM paraphrasing
- `_resolve_critiques` — scans its own output for resolved critique IDs via three regex patterns (YAML list, forward prose, reverse prose), then physically moves critique blocks from Active to Resolved
- `_completion_analysis` — injects completion/budget-pressure/inline-synthesis banners into context
- **Inline synthesis** — when all problem steps are established, the orchestrator writes a `## Synthesis` section directly into RESEARCH_STATE.md and emits `terminate` (bypassing the separate synthesize → researcher path)
- `detect_computation_stalls` — groups COMPUTATION_LOG entries by claim, reports streaks of ≥ 3 non-VERIFIED

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

**Self-retraction filter:** After the LLM responds, the scaffold scans LOW-severity critiques for retraction signals in Phase 2 (e.g., "reproduction succeeded, no issues found") and drops them before writing to `CRITIQUE_LOG.md`. Retractions are logged as HTML comments.

**`NO_CRITIQUES_FILED`:** When the critic finds nothing, it outputs this marker. The scaffold treats it as an empty critic pass.

#### Compressor (`agents/compressor.py`)

**Role:** Shrink files exceeding size thresholds. LLM output IS the compressed file.

**Context:** The target file's content with a one-line header.

**Processing:** Archives original (timestamped copy in `archive/`), writes compressed version back.

**Rules:** Preserve ERs and unresolved critiques verbatim. Collapse resolved critiques to one-line summaries. Drop abandoned hypotheses. Never discard "what didn't work" information.

---

## 4. Infrastructure Layer

### LLM interface (`llm.py`)

Two calling patterns:

**`call_llm`** — stateless one-shot. Creates a fresh Anthropic client per call. Returns `LLMResponse(text, input_tokens, output_tokens, stop_reason, duration)`. Used by orchestrator, researcher, critic, compressor.

**`run_agent_loop`** — stateful multi-turn. Maintains a growing `messages` list across rounds. Each round: LLM response → tool extraction → `ToolExecutor.execute()` → tool result fed back. Returns `AgentResult(text, tool_calls, total_input/output_tokens, rounds, truncated, duration, stop_reason)`. Terminates on `end_turn`, `max_tokens`, or `max_rounds` exhaustion. Used exclusively by computationalist.

**Logging:** Every LLM call produces:
- JSONL audit entry in `AUDIT_LOG.jsonl` (metadata only, no prompts)
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

### Markdown parsing (`markdown.py`)

The richest infrastructure file. Handles:

**Frontmatter:** `parse_frontmatter` tries `yaml.safe_load()`, falls back to line-by-line regex on `YAMLError`. Never crashes — the "never crash the loop" principle.

**Section utilities:** `tail_entries` (last N `## ` sections), `extract_section_by_id` (find section by ID pattern).

**Critique lifecycle:**
- `count_unresolved_critiques` — counts by severity via regex
- `insert_into_active_critiques` — inserts between Active/Resolved headings
- `resolve_critique` — moves a critique block from Active to Resolved, rewrites `[UNRESOLVED]` → `[RESOLVED]`, appends resolution note
- `extract_resolved_critique_ids` — three-pattern extraction (YAML list, forward prose, reverse prose)
- `filter_self_retracted_critiques` — drops LOW critiques with retraction signals
- `recount_critique_metadata` — re-derives frontmatter counts from body content

**Computation analysis:**
- `_parse_comp_entries` — extracts structured dicts from COMPUTATION_LOG (id, claim, verdict, result)
- `detect_computation_stalls` — groups by claim, finds streaks of ≥ N consecutive non-VERIFIED
- `find_prior_failures_for_claim` — finds RESULT blocks from previous failed attempts at the same claim

**LLM drift tolerance:** All critique regexes accept both `CRIT-NNN` and `CRITIQUE-NNN`.

### Metrics (`metrics.py`)

In-memory `MetricsTracker` with `CallRecord` entries. Tracks: per-call tokens, tool calls, rounds, truncation flags, alerts. Renders to `METRICS.md` with adaptive columns (tool-use columns appear only when relevant). Not persisted between process restarts — `METRICS.md` is the durable artifact.

Special case: when `agent == "deep_critic"`, updates `last_critic_iteration` — this field is read by the engine to enforce the `critic_every_n` policy.

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
  AUDIT_LOG.jsonl        ← Append-only LLM call metadata
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
| `model` | `claude-sonnet-4-6` | Agent model |
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
| `min_er_for_completion` | 3 | ERs needed before stale-loop backstop fires |
| `compress_threshold` | RS: 50K, CL: 30K, CompL: 40K | File size thresholds (chars) |

Compression tiers: alert at 1x, compress at 1.5x, force-compress at 2x (though 1.5x and 2x currently execute identical code).

---

## 7. Verification

**File:** `verify.py` — runs as `python -m sciralph.verify <workspace_dir>`

A fully independent post-hoc evaluation. Two LLM passes using Claude Opus with streaming:

**Pass 1: Science verification** — evaluates correctness of each ER (derivation validity, computational support, critique resolution). Verdict scale: VALID / PARTIALLY_VALID / INVALID / INCONCLUSIVE. Produces per-ER assessments and chain coherence check. Optionally re-runs computation scripts.

**Pass 2: Process audit** — evaluates multi-agent process quality (error-correction cycles, computation effectiveness, orchestrator decisions, budget management). Verdict scale: EFFECTIVE / PARTIALLY_EFFECTIVE / INEFFECTIVE. Lists process events with classifications.

Output: `VERIFICATION.md` written to workspace (when `--write-report`).

---

## 8. Testing

**365 tests** across 14 test files (~6,312 lines). Run with `uv run python -m pytest -v`.

| Test file | Lines | What it covers |
|-----------|------:|----------------|
| `test_engine.py` | 1144 | Main loop, overrides, termination gates, compression, budget, stalls, status |
| `test_validation.py` | 1076 | All 7 post-integration checks, can_terminate gates, violation types |
| `test_markdown.py` | 920 | Frontmatter, sections, critique lifecycle, stall detection, comp parsing |
| `test_report_recommendations.py` | 850 | Report generation, recommendation analysis |
| `test_verify.py` | 582 | Workspace loading, verdict parsing, prompts, process audit, report patching |
| `test_orchestrator.py` | 488 | Response splitting, integration, completion analysis, budget, stalls, critiques, inline synthesis |
| `test_tools.py` | 349 | ToolExecutor, run_agent_loop, truncation, token accumulation |
| `test_config.py` | 224 | Defaults, YAML/CLI override, merge priority |
| `test_computationalist.py` | 166 | Soft-check pattern, tools attribute, process_response, INCONCLUSIVE fallback |
| `test_workspace.py` | 132 | init structure, validate_comp_references |
| `test_task.py` | 125 | TaskType enum, to_markdown, from_frontmatter, round-trip |
| `test_metrics.py` | 110 | CallRecord, critic tracking, alerts, Markdown rendering |
| `test_conversation_log.py` | 80 | File naming, sections, sequence counter |
| `test_sandbox.py` | 66 | Script execution, timeout, MPLBACKEND |

**Testing approach:** pytest with `tmp_path` fixtures. All LLM calls are mocked (no real API calls). `SimpleNamespace` objects mock Anthropic SDK responses. Fixture Markdown files for complex document parsing.

**Notable coverage gaps:**
- `call_llm` one-shot path (only `run_agent_loop` is tested)
- BaseAgent retry logic directly
- Researcher, critic, compressor `process_response` methods
- Workspace git operations
- End-to-end `main.py` run path

---

## 9. Known Issues & Ad-hoc Fixes

This section catalogs remaining structural issues. Phase 2 resolved the most impactful ones (iteration contract, scattered overrides, phantom references, stall detection enforcement, budget coordination).

### Resolved in Phase 2

- **Iteration contract problem** — stale backstop no longer breaks before dispatch; it forces synthesize via `_apply_overrides()` P2. Budget accounting uses `budget_remaining = max_iterations - iteration` after override chain.
- **Scattered engine overrides** — consolidated into `_apply_overrides()` with explicit P1-P6 priority (see §2).
- **Phantom reference validation** — now part of the validation pipeline (`check_phantom_references()` in `validation.py`), runs both post-integration and post-dispatch.
- **Computation stall detection** — now enforced at engine level via `_update_stall_tracking()` + P5 stall blocking in `_apply_overrides()`, in addition to orchestrator context injection.
- **Dual-layer budget enforcement** — coordinated: orchestrator soft banner at ≤ 3 iterations (`_completion_analysis`), engine hard override at ≤ 1 iteration (P1 in `_apply_overrides`). Displaced tasks are logged and fed back to orchestrator.

### Still Present

### Compression 1.5x vs 2x are identical

In `_check_compression()`, the 1.5x and 2x threshold branches execute identical code (same `Task` construction, same `compressor.run()` call). Only the console message differs. If the intent was more aggressive compression at 2x, it is not implemented.

### Critic underflow retry

When the critic produces < 200 output tokens, the engine retries once. Both the original and retry responses are processed (both append to CRITIQUE_LOG), so a retry can produce duplicate entries if the first run generated some content.

### `_check_status_field` uses string matching

```python
if f'status: "{status}"' in state or f"status: {status}" in state:
```

This checks for both quoted and unquoted YAML values by substring match on the raw file text. It works but is fragile — a comment containing `status: completed` would trigger false termination.

### `Task.from_frontmatter` iteration-0 gotcha

```python
meta.get("iteration", fallback_iteration) or fallback_iteration
```

The `or` treats `0` as falsy. A task explicitly written with `iteration: 0` silently falls back. Unlikely in practice but a latent bug.

### `_enforce_problem_statement` edge case

Uses a DOTALL lookahead `(?=\n# )` to find the next top-level heading. If Problem Statement is the last section (no following `# ` heading), the regex won't match and the problem statement won't be enforced.

### Context accumulation in `run_agent_loop`

The `messages` list in `run_agent_loop` grows unboundedly across rounds. Large tool outputs can push past the model's context limit with no trimming mechanism. The `max_tool_rounds` limit is the only guard.

---

## 10. Implemented Architecture (Phase 2)

Phase 2 restructured the ad-hoc fixes into three architectural layers, all now implemented.

### Layer A — Iteration contract (`engine.py`)

One iteration is the full cycle: `orchestrator → validate → overrides → terminate gate → dispatch → post-dispatch → commit`. The `max_iterations` check fires only after the dispatched agent has completed. `can_terminate()` in `validation.py` replaces scattered termination checks with a single gate:

- At least one ER exists
- At least one critic pass has occurred
- No unresolved HIGH critiques
- Numerical verification required when `requires_numerical: true` in problem YAML

Termination blockers are fed back to the orchestrator via `context_prefix`, which also carries validation violations and displaced task records.

### Layer B — Post-integration validation (`validation.py`)

`validate_post_integration()` runs 7 checks after every orchestrator pass:

```python
checks = [
    check_phantom_references,           # hallucinated COMP-NNN/TASK-NNN refs
    check_er_promotion_gate,            # new ER without VERIFIED COMP → demote
    check_phantom_labels,               # researcher-written VERIFIED → strip
    check_stale_unverified_labels,      # unverified labels stale for 6+ iterations
    check_verified_frontmatter_backfill,# verified_by in frontmatter matches log
    check_task_agent_routing,           # compute task to wrong agent → reroute
    check_id_consistency,               # COMP/CRIT counter gaps → fix
]
```

Each check returns `list[Violation]`. Violations are queued and injected into the orchestrator's context on the next pass.

### Layer C — Agent loop resilience (`llm.py`)

- **Forced text-only final call** on `max_rounds` exhaustion — extracts partial verdict instead of empty output; `stop_reason="max_rounds_forced"`
- **Zero-text bailout** — `zero_text_bailout` (default 3) consecutive rounds with no text trigger early exit
- **Low cumulative text bailout** — exits at halfway point if total text is very low
- **Checkpoint message** — injected at `checkpoint_round` (default 2) to nudge the agent to produce output
- **Token alert** — fires when cumulative input tokens exceed `computation_token_alert` threshold
- **Engine-level stall tracking** — `_update_stall_tracking()` tracks repeated failures per claim; P5 in `_apply_overrides()` blocks dispatch to stalled claims

---

## 11. Documentation Status

All documentation was synced with the codebase in March 2026.

- `requires_numerical: true/false` is implemented in all 22 problem YAML files and consumed by `can_terminate()` gate
- `read_file` tool for orchestrator/researcher/critic remains planned (not implemented)
- External reference files (`files:` YAML key) remains planned (not implemented)
- Problems organized into `problems/tier1/` (10 core) and `problems/tier2/` (12 advanced)
