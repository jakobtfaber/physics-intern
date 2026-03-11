# SciRalph — Codebase Reference

> **Purpose of this document:** A developer-oriented map of the codebase as it exists today (March 2026). Written to serve as a reference for understanding the system before implementing the Phase 2 architectural changes described in [PLAN.md](PLAN.md).

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
```

### Key design decisions

- **Fresh context per call.** Agents are stateless — they read from disk, call the LLM, and write back to disk. No conversation history is carried between iterations. This prevents context degradation but means each call rebuilds its window from scratch.
- **Staging discipline.** The researcher writes to `PROPOSED_CHANGES.md`, never to `RESEARCH_STATE.md`. The orchestrator reviews and integrates on its next pass. No agent can self-certify its own results.
- **Mandatory critic passes.** The scaffold forces critic reviews every N iterations, regardless of agent judgment.
- **All state in Markdown.** Every piece of research state, computation result, and critique is persisted in version-controlled Markdown files. The workspace is its own git repo.

### Source file map

| File | Lines | Purpose |
|------|------:|---------|
| `main.py` | 70 | CLI entry point, arg parsing, workspace naming |
| `engine.py` | 355 | `SciRalph` class: main loop, dispatch, termination, overrides |
| `config.py` | 97 | `Config` dataclass, 3-tier config builder |
| `task.py` | 71 | `Task` dataclass, `TaskType` enum, YAML serialization |
| `llm.py` | 256 | `call_llm` (one-shot), `run_agent_loop` (tool-use), logging |
| `tools.py` | 126 | `ToolExecutor`, `ToolCall`, `execute_python` tool schema |
| `workspace.py` | 184 | File I/O, git ops, phantom reference validation |
| `markdown.py` | 411 | Frontmatter parsing, critique lifecycle, stall detection |
| `sandbox.py` | 49 | `subprocess.run` wrapper with timeout |
| `metrics.py` | 110 | `MetricsTracker`, `METRICS.md` rendering |
| `verify.py` | 760 | Independent verification script (science + process audit) |
| `agents/base.py` | 138 | `BaseAgent` ABC, template method, retry logic |
| `agents/orchestrator.py` | 192 | Planning, integration, critique resolution |
| `agents/researcher.py` | 40 | Derivations, writes `PROPOSED_CHANGES.md` |
| `agents/computationalist.py` | 64 | Agentic tool-use, verdict writing |
| `agents/critic.py` | 70 | Adversarial review, self-retraction filter |
| `agents/compressor.py` | 27 | File size management |
| **Total** | **~3,020** | |

---

## 2. The Main Loop

**File:** `engine.py` — `SciRalph.run()`

The loop runs `while self.iteration < self.config.max_iterations`, incrementing `self.iteration` at the **top** of each pass (so iteration 1 is the first real turn). Each iteration follows this sequence:

```
┌─── Iteration N ──────────────────────────────────────────────────────┐
│                                                                      │
│  1. ORCHESTRATOR PASS                                                │
│     └─ Reads all state → emits CURRENT_TASK.md                       │
│        (+ integrates PROPOSED_CHANGES.md into RESEARCH_STATE.md)     │
│                                                                      │
│  2. PHANTOM REFERENCE VALIDATION                                     │
│     └─ Strips hallucinated COMP-NNN/TASK-NNN refs from RESEARCH_STATE│
│                                                                      │
│  3. TASK OVERRIDES (in priority order)                               │
│     a. Pending recompute after REFUTED verdict → force COMPUTE       │
│     b. Budget enforcement (≤1 iter left) → force SYNTHESIZE          │
│     c. Forced critic (overdue by N iterations) → force CRITIQUE      │
│                                                                      │
│  4. TERMINATION CHECK                                                │
│     └─ TERMINATE → break                                             │
│     └─ Stale-loop backstop (2 consecutive iters with ≥3 ERs, 0 WHs) │
│                                                                      │
│  5. DISPATCH to researcher / computationalist / critic               │
│     └─ Prior failure enrichment for COMPUTE tasks                    │
│     └─ Critic underflow retry (< 200 output tokens)                  │
│     └─ REFUTED verdict detection after COMPUTE                       │
│                                                                      │
│  6. COMPRESSION CHECK (per file, at 1.5x / 2x threshold)            │
│                                                                      │
│  7. METRICS + GIT COMMIT                                             │
│                                                                      │
│  8. POST-DISPATCH TERMINATION CHECK                                  │
│     └─ Reads status from RESEARCH_STATE frontmatter                  │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Task override ordering

Multiple mechanisms can override the orchestrator's chosen task within a single iteration. They execute in this order:

1. **Pending recompute** — if previous iteration's computation returned REFUTED
2. **Budget enforcement** — if ≤ 1 iteration remaining, hard override to SYNTHESIZE
3. **Forced critic** — if > `critic_every_n` iterations since last critic pass

If both (1) and (2) apply, budget wins (runs later). The forced critic check runs after both but respects already-terminal task types.

### Termination paths

There are five distinct ways the loop can end:

| Path | Where | Condition |
|------|-------|-----------|
| Explicit terminate | Step 4 | Orchestrator emits `task_type: terminate` |
| Stale-loop backstop | Step 4 | ≥ 2 consecutive non-synthesize iters with ER ≥ 3 and WH = 0 |
| Status check | Step 8 | Any agent wrote `status: completed/abandoned/partially_complete` |
| Budget exhaustion | Step 3b | Forces SYNTHESIZE; next iteration's status check catches completion |
| Max iterations | Loop condition | `self.iteration >= self.config.max_iterations` |

### Dispatch routing

| TaskType | Agent | Notes |
|----------|-------|-------|
| `research`, `derive`, `resolve`, `synthesize` | Researcher | All go to the same agent |
| `compute` | Computationalist | Post-dispatch: check for REFUTED verdict |
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
- Completion analysis banner (if ER count sufficient, or budget ≤ 3)
- Computation stall warnings (≥ 3 consecutive non-VERIFIED on same claim)
- Full `RESEARCH_STATE.md`, `CRITIQUE_LOG.md`, tail of `COMPUTATION_LOG.md`, `METRICS.md`
- `PROPOSED_CHANGES.md` (when present)

**Output parsing:** Splits on literal delimiter strings `=== RESEARCH_STATE.md ===` and `=== CURRENT_TASK.md ===`. If both present, writes state update and deletes PROPOSED_CHANGES. If only task delimiter, writes task only. No delimiters at all → entire output becomes CURRENT_TASK.

**Key behaviors:**
- `_enforce_problem_statement` — regex-replaces the Problem Statement section after every state integration to prevent LLM paraphrasing
- `_resolve_critiques` — scans its own output for resolved critique IDs via three regex patterns (YAML list, forward prose, reverse prose), then physically moves critique blocks from Active to Resolved
- `_completion_analysis` — injects completion/budget-pressure banners into context
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
6. **FINAL_SYNTHESIS.md** — researcher synthesizes all ERs into the final answer

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

**172 tests** across 12 test files (~3,020 lines). Run with `uv run python -m pytest -v`.

| Test file | Lines | What it covers |
|-----------|------:|----------------|
| `test_markdown.py` | 542 | Frontmatter, sections, critique lifecycle, stall detection, comp parsing |
| `test_verify.py` | 582 | Workspace loading, verdict parsing, prompts, process audit, report patching |
| `test_orchestrator.py` | 462 | Response splitting, integration, completion analysis, budget, stalls, critiques |
| `test_engine.py` | 394 | Compression, budget enforcement, refuted recompute, critic retry, status |
| `test_config.py` | 224 | Defaults, YAML/CLI override, merge priority |
| `test_tools.py` | 236 | ToolExecutor, run_agent_loop, truncation, token accumulation |
| `test_computationalist.py` | 142 | Soft-check pattern, tools attribute, process_response, INCONCLUSIVE fallback |
| `test_metrics.py` | 110 | CallRecord, critic tracking, alerts, Markdown rendering |
| `test_task.py` | 92 | TaskType enum, to_markdown, from_frontmatter, round-trip |
| `test_workspace.py` | 90 | init structure, validate_comp_references |
| `test_conversation_log.py` | 80 | File naming, sections, sequence counter |
| `test_sandbox.py` | 66 | Script execution, timeout, MPLBACKEND |

**Testing approach:** pytest with `tmp_path` fixtures. All LLM calls are mocked (no real API calls). `SimpleNamespace` objects mock Anthropic SDK responses. Fixture Markdown files for complex document parsing.

**Notable coverage gaps:**
- `call_llm` one-shot path (only `run_agent_loop` is tested)
- BaseAgent retry logic directly
- Researcher, critic, compressor `process_response` methods
- Full `engine.py` `run()` loop (only individual methods tested via mocks)
- Workspace git operations
- End-to-end `main.py` run path

---

## 9. Known Issues & Ad-hoc Fixes

This section catalogs the workarounds and structural problems in the current codebase. Many of these are exactly what PLAN.md's three-layer architecture aims to resolve.

### The iteration contract problem (PLAN.md Step 1)

**The most impactful issue.** Observed in 5/8 test runs. The problem has two facets:

**1. Stale backstop exits before dispatch.** The stale-loop backstop (`_stale_iterations >= 2`) fires between the orchestrator pass and `_dispatch()`. When the orchestrator emits a non-synthesize/non-terminate task (e.g. COMPUTE or RESOLVE) but the stale backstop triggers (ER ≥ 3, WH = 0 for 2 consecutive iterations), the loop `break`s and the task never executes:

```python
# engine.py — current structure (simplified)
while self.iteration < self.config.max_iterations:
    self.iteration += 1
    task = orchestrator.run(...)       # orchestrator emits task

    if task.task_type == TaskType.TERMINATE:
        break                          # ← exits before dispatch (by design)

    # Stale backstop
    if er_count >= 3 and wh_count == 0:
        self._stale_iterations += 1
        if self._stale_iterations >= 2:
            break                      # ← exits before dispatch (BUG)

    self._dispatch(task)               # task only runs if we get here
```

**2. Budget accounting is off by one.** The counter increments at the top of the loop, so `budget_remaining = max_iterations - self.iteration` is calculated after the increment. The orchestrator's multi-step plans get cut short because the budget appears one iteration shorter than it actually is, and budget enforcement (`budget_remaining <= 1 → force SYNTHESIZE`) fires one iteration too early.

### Scattered engine overrides

The engine has accumulated multiple independent override mechanisms that interact in non-obvious ways:

| Override | Location in loop | Can override | Overridden by |
|----------|-----------------|--------------|---------------|
| Pending recompute | After orchestrator | Orchestrator's task | Budget enforcement |
| Budget enforcement | After recompute | Any non-terminal task | Nothing |
| Prior failure enrichment | After budget | Modifies COMPUTE tasks | N/A (additive) |
| Forced critic | After enrichment | Any non-CRITIQUE task | Nothing |
| Stale-loop backstop | Step 4 | Forces termination | N/A |

These are independent if-statements with implicit priority via execution order. There is no unified validation layer — each check was added to solve a specific observed failure.

### Phantom reference validation

`workspace.validate_comp_references()` modifies `RESEARCH_STATE.md` in-place, replacing hallucinated `COMP-NNN` references with `[COMP-NNN:unverified]`. This is irreversible within a run and uses regex replacement that could theoretically match valid references in prose context.

### Compression 1.5x vs 2x are identical

In `_check_compression()`, the 1.5x and 2x threshold branches execute identical code (same `Task` construction, same `compressor.run()` call). Only the console message differs. If the intent was more aggressive compression at 2x, it is not implemented.

### Critic underflow retry

When the critic produces < 200 output tokens, the engine retries once. Both the original and retry responses are processed (both append to CRITIQUE_LOG), so a retry can produce duplicate entries if the first run generated some content.

### `_should_terminate` uses string matching

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

### Dual-layer budget enforcement

Budget pressure exists at two layers with different thresholds and no shared logic:

1. **Orchestrator soft banner** (`orchestrator.py`, `_completion_analysis`) — when ≤ 3 iterations remain, injects a `BUDGET SYNTHESIS REQUIRED` prompt banner nudging the LLM to emit `task_type: synthesize`. This is advisory — the LLM can ignore it.
2. **Engine hard override** (`engine.py`, main loop) — when ≤ 1 iteration remains, forcibly replaces the orchestrator's chosen task with a SYNTHESIZE task. This is unconditional.

The two mechanisms were added independently. There is no shared threshold constant or unified budget-awareness interface — the orchestrator doesn't know about the engine's hard cutoff, and the engine doesn't know about the orchestrator's soft banner.

### Computation stall detection is context-injected

`detect_computation_stalls()` in `markdown.py` groups COMPUTATION_LOG entries by claim and finds streaks of ≥ 3 consecutive non-VERIFIED verdicts. The orchestrator calls this in `build_context()` and injects warning banners into its own prompt. However, this is purely advisory — the orchestrator LLM may ignore the stall warning. There is no engine-level enforcement (no hard override to skip or downgrade a stalled claim). The stall detection also lives entirely in the orchestrator's context builder rather than in a shared validation layer.

### Context accumulation in `run_agent_loop`

The `messages` list in `run_agent_loop` grows unboundedly across rounds. Large tool outputs can push past the model's context limit with no trimming mechanism. The `max_tool_rounds` limit is the only guard.

---

## 10. Planned Architecture (PLAN.md)

PLAN.md proposes restructuring the ad-hoc fixes into three architectural layers. This section summarizes the plan and maps it to current code.

### The three layers

**Layer A — Iteration contract** (`engine.py` loop structure)

Redefine one iteration as the full cycle: `orchestrator → dispatch → post-dispatch checks`. The `max_iterations` check fires only after the dispatched agent has completed. This addresses the most common failure mode (5/8 runs). A `can_terminate()` function replaces the scattered termination checks with a single gate:

- At least one critic pass has occurred
- No ERs promoted since last critic without review
- No unresolved HIGH critiques
- Problem coverage: all sub-objectives addressed
- Computation count > 0 when numerics are required

**Currently:** Termination logic is split across `_should_terminate()` (status string matching), the stale-loop backstop (ER/WH counting), the TERMINATE task type check, and budget enforcement. These are independent mechanisms with no shared interface.

**Layer B — Post-integration validation** (`validation.py`, new module)

A single validation pipeline that runs after every orchestrator integration pass:

```python
def validate_post_integration(workspace, config) -> list[Violation]:
    checks = [
        check_er_promotion_gate,      # new ER without VERIFIED COMP → demote
        check_task_agent_routing,      # compute task to wrong agent → reroute
        check_phantom_labels,          # researcher-written VERIFIED → strip
        check_id_consistency,          # COMP/CRIT counter gaps → fix
    ]
    return [v for check in checks for v in check(workspace)]
```

**Currently:** These concerns are handled by: `validate_comp_references()` in workspace.py (phantom stripping), the orchestrator prompt (promotion criteria), implicit dispatch routing in `_dispatch()` (agent routing), and frontmatter counter updates scattered across agent `process_response` methods. There is no unified validation interface.

**Layer C — Agent loop resilience** (`llm.py` inner loop)

Two improvements to `run_agent_loop`:
1. **Forced partial output on truncation** — when hitting `max_rounds`, make one final text-only LLM call to extract a partial INCONCLUSIVE verdict instead of empty output
2. **Stall detection** — track repeated failures and escalate after 2 consecutive INCONCLUSIVE verdicts on the same claim

**Currently:** Truncated runs produce `"Agent produced no text output"` stubs (handled by computationalist's fallback INCONCLUSIVE entry). Stall detection exists only at the orchestrator level via `detect_computation_stalls()` with threshold 3 — there is no stall tracking in the engine itself, only context injection into the orchestrator's prompt.

### Implementation order and dependencies

| Step | Layer | What | Absorbs current code | Impact |
|------|-------|------|---------------------|--------|
| 1 | A | Iteration contract + termination gates | Scattered termination checks, stale backstop | 7/8 runs |
| 2 | C | Forced partial output on truncation | INCONCLUSIVE fallback in computationalist | 4/8 runs |
| 3 | B | Post-integration validation pipeline | `validate_comp_references`, phantom handling | 5/8 runs |
| 4 | C | Stall detection (builds on 2+3) | `detect_computation_stalls` context injection | 3/8 runs |
| 5 | — | Critique preamble stripping | N/A (cosmetic) | Cosmetic |
| 6 | — | Verdict validator (deferred) | Not currently implemented | 0/8 runs |

### Key benefits of the three-layer approach

1. **Testability.** Each layer is a single module with a clear interface. `can_terminate()` and `validate_post_integration()` are pure functions that take workspace state and return structured results — easy to unit test with fixture files.

2. **Replaces implicit priority ordering.** Currently, engine overrides interact via execution order. The layered architecture makes the contract explicit: Layer B checks run at a defined hook point, Layer A gates run at the iteration boundary, Layer C improvements live inside the agent loop.

3. **Extensibility.** Adding a new invariant check means adding one function to the Layer B pipeline. Currently it means adding another if-statement to `engine.py` and reasoning about its interaction with every other override.

4. **Consolidation.** The plan absorbs 6 current mechanisms (phantom validation, stale backstop, stall detection context injection, INCONCLUSIVE fallback, termination string matching, budget enforcement) into 3 modules with unified interfaces.

---

## 11. Documentation Status

### README.md
- Updated March 2026: model names, agent reads columns, current status, design doc links

### DESIGN.md
- `read_file` tool for orchestrator/researcher/critic is listed as planned — confirmed not implemented
- External reference files (`files:` YAML key) is planned — not implemented
- Main loop pseudocode (§5.1) shows the current iteration contract (the one PLAN.md proposes fixing)
- Computationalist "Reads" column updated to reflect that prior failure context is injected by the engine, not read directly
- Otherwise structurally accurate

### Problem YAML format
- PLAN.md Step 1 mentions a planned `requires_numerical: true` flag for termination gate enforcement — not implemented in any problem file
- DESIGN.md mentions a planned `files:` key for external references — not implemented
- Currently all problems use only the `problem:` key
