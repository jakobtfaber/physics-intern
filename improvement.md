# SciRalph Improvement Notes

Analysis based on workspace run: `20260316_133356_quantum_error_correction_main_gemini-3-flash-preview`
Problem: Quantum Error Correction — [[4,2,2]] code logical fidelity F(p)
Model: gemini-3-flash-preview | 5 iterations | Correct answer produced

---

## A. Bugs (fix now)

### A1. `problem_statement` lost after iteration 1

**Where:** `engine.py:61`, `orchestrator.py:183`, `renderers.py:45`

**Root cause:** `engine.py` initializes `self.research_state = ResearchState()` — an empty object with `problem_statement = ""`. The problem text is written to `RESEARCH_STATE.md` by `workspace.init()`, but never loaded into the in-memory `ResearchState`. At the end of iteration 1, `orchestrator.process_response()` overwrites `RESEARCH_STATE.md` via `render_research_state_md(research_state)`, which renders the empty field as `"(No problem statement.)"`. From iteration 2 onward, all agents read back the corrupted file.

**Evidence:** `RESEARCH_GRAPH.json` shows `"problem_statement": ""`. Iteration 2 orchestrator context shows `(No problem statement.)`.

**Fix:** After creating `ResearchState()`, populate it:
```python
self.research_state = ResearchState()
self.research_state.problem_statement = problem.strip()
```
Also populate `answer_template` if applicable. Consider loading from `RESEARCH_GRAPH.json` if resuming a run.

**Impact:** All agents from iteration 2 onward lose the problem statement context. The run still succeeded because the conventions section preserved enough information, but this is a serious context degradation. Especially costly for the critic, which can't assess whether the research answers the right question.

### A2. `total_computations` always 0 in COMPUTATION_LOG frontmatter

**Where:** `renderers.py:101`

**Root cause:** `len([c for c in comps if c.id.startswith("COMP-")])` filters for `COMP-` prefix, but computation IDs in this run use `TASK-` prefix (e.g., `TASK-001`, `TASK-002`). The count is always 0.

**Fix:** Remove the prefix filter, or match the actual ID scheme:
```python
"total_computations": len(comps),
```

### A3. RESEARCH_GRAPH.json not synced after final iteration

**Where:** `engine.py` (main loop sync logic)

**Evidence:** After the run completes, the JSON shows `status: "in_progress"`, `iteration: 4`, and WH-002/WH-003 still have `status: "working"` with their original WH IDs. The iteration 5 promotions (WH-002→ER-002, WH-003→ER-003) and status change to `"completed"` are reflected in RESEARCH_STATE.md but not in the JSON.

**Root cause:** `_sync_research_state()` is likely called before the final orchestrator pass processes its tool results, or not called at all after the termination path (formatter dispatch + status update). The JSON — which is supposed to be the authoritative structured state — ends up stale.

**Fix:** Ensure `_sync_research_state()` is called after the orchestrator's `process_response()` in the termination path, and again after the status is set to `"completed"`.

### A4. `zero_output` not set for failed computations without exit tool

**Where:** `agents/computationalist.py` (process_response fallback path)

**Evidence:** TASK-001 in RESEARCH_GRAPH.json has `zero_output: false` despite the agent producing no exit tool call and all fields being empty. The `notes` field correctly says "Agent produced no exit tool call" but `zero_output` is not set.

**Impact:** Any logic gating on `zero_output` (like the proposed fix in C3) won't work for this failure mode. The fix in C3 should additionally check for empty `result` and empty `target_hypothesis` as fallback indicators.

---

## B. Architecture (design changes)

### B1. Three-tier entity lifecycle: RQ → WH → ER

**Problem:** The current WH category conflates open questions needing exploration with concrete claims needing verification. This leads to vague WHs like "F(p) is a rational function" that are just placeholders.

**Proposal:**
- **Research Questions (RQ):** Open-ended things to explore — "What is F(p)?", "How does the error scale?" Targets for exploration tasks.
- **Working Hypotheses (WH):** Concrete, falsifiable claims with specific values or expressions. Created from RQ exploration results or directly when the claim is already concrete. Targets for verification tasks.
- **Established Results (ER):** Verified WHs. No change from current.

**Flow:** RQ → explore → concrete WH → verify → ER. Direct WH creation (skipping RQ) is allowed when the claim is already concrete.

We should make sure all the tools offered to the orchestrator allow it to manipulate the research questions (add/remove etc.)

### B2. One task = one agent (eliminate task/agent indirection)

**Problem:** Currently 6 agents with static prompts handle multiple task types via conditional logic. The computationalist prompt covers both explore and verify modes ("if verify, use submit_verdict; if explore, use submit_result"). This creates branching instructions where only one branch is relevant per call — exactly the kind of thing LLMs handle poorly. `tools_for_task_type()` already filters tool sets, but the *prompt* still covers all modes.

**Proposal:** Each task type gets its own agent with a focused prompt and tool set. No conditionals, no mode switching.

**New agent roster (8-9 agents, up from 6):**
- `orchestrator` — unchanged (unified role: assess state, plan next task)
- `compute_verify` — execute_python + submit_verdict only. Prompt focused on verification methodology.
- `compute_explore` — execute_python + submit_result only. Prompt focused on exploratory computation.
- `research_verify` — new. Structured reasoning verification with verdict output (VERIFIED/REFUTED/INCONCLUSIVE). Needs a verdict mechanism similar to computationalist's submit_verdict.
- `research_explore` — current researcher (research/derive tasks). May stay one-shot or gain tools.
- `critic` — unchanged
- `compressor` — unchanged
- `formatter` — unchanged

**Benefits:**
- Focused prompts: each describes exactly one job, no conditionals
- Focused tool sets: no irrelevant tools cluttering the context
- Easier to tune: iterate on explore prompt without risking verify regressions
- Clearer orchestrator dispatch: task type *is* the agent, no routing table needed

**Design questions to resolve:**
- Should `research_verify` be agentic (tool-use loop) or one-shot with structured output?
- How does `research_verify` emit its verdict? New tool (submit_reasoning_verdict)? Or structured output format?
- The base class infrastructure (BaseAgent, tools class attribute, prompt_file) already supports this split cleanly.

### B3. Orchestrator dispatch becomes a 2×2 matrix

With the above changes, the orchestrator's task planning maps onto:

|  | Explore (RQ → WH) | Verify (WH → ER) |
|--|---|---|
| **Reasoning** | research_explore | research_verify |
| **Computation** | compute_explore | compute_verify |

The orchestrator prompt would reference this matrix explicitly, making dispatch decisions more systematic.

**Promotion guidance in orchestrator prompt:** The orchestrator should choose the verification method based on the nature of the claim:
- Claims that make numerical/computational predictions → `compute_verify`
- Claims that are analytical/structural (e.g., "the ideal circuit prepares |00>_L") → `research_verify`
A WH should not be promoted without at least one verification pass through the appropriate channel. The current prompt's "COMPUTE-FIRST" instruction should be generalized to "VERIFY-FIRST" with the orchestrator choosing the right column from the 2×2 matrix.

**`verified_results` semantics are wrong — rename or fix:**
Currently `check_verified_frontmatter_backfill` (`validation.py:386`) auto-syncs `verified_results` to equal all ER section IDs. The comment says *"Promotion is now explicit via the orchestrator's promote_hypothesis tool, so verified_results should simply reflect actual ER sections."* This conflates "established" with "verified." In this run, ER-001 was added to `verified_results` despite never being computationally or analytically verified — it was promoted purely on the orchestrator's own derivation.

With the 2×2 matrix, `verified_results` should track actual verification evidence:
- An ER has a `compute_verify` VERIFIED verdict → add to `verified_results`
- An ER has a `research_verify` VERIFIED verdict → add to `verified_results`
- An ER was promoted without any verification → NOT in `verified_results` (and this should be flagged or blocked)

The backfill check should query ResearchState for verification evidence (computations with `kind="verify"` and `verdict=VERIFIED`, or future research_verify records), not just mirror ER section headers. Alternatively, if promotion always requires prior verification (as proposed above), then the backfill becomes redundant — it can be removed entirely.

### B4. Formal justification graph: `depends_on` + `promotion_justification` on Hypothesis

**Problem:** Promotion justifications and hypothesis dependencies are informal and untracked.

- The `justification` string passed to `promote_hypothesis` is logged to EVENT_LOG but **not stored on the Hypothesis object**. After promotion, there's no structured way to answer "why was ER-002 promoted?"
- There is no `depends_on` field. WH-003 (leading-order F(p)) is derived from WH-002 (full rational function), but this dependency is only implicit in the derivation text. The system can't answer "if ER-002 were demoted, which other ERs would be invalidated?"
- Nothing prevents promoting a hypothesis whose premises are still unverified WHs. In this run, WH-002 was promoted before WH-003 only because the LLM happened to order the tool calls correctly — not because the scaffold enforced it.

**Proposal:**

Add two fields to `Hypothesis`:
```python
@dataclass
class Hypothesis:
    ...
    depends_on: list[str] = field(default_factory=list)          # e.g., ["ER-002"]
    promotion_justification: str = ""                             # stored at promotion time
```

**Orchestrator tools changes:**
- `add_hypothesis(statement, derivation, depends_on?)` — optional list of hypothesis IDs this claim depends on.
- `promote_hypothesis(id, justification)` — store `justification` on the Hypothesis object, not just in the event log.

**New guardrail in `_promote_hypothesis`:**
```python
# Check all dependencies are already established
for dep_id in h.depends_on:
    dep_num = dep_id.split("-")[1]
    current_id = f"ER-{dep_num}"
    if current_id not in state.hypotheses or state.hypotheses[current_id].status != HypothesisStatus.ESTABLISHED:
        return f"Error: Cannot promote {wh_id} — dependency {dep_id} is not yet an Established Result."
```

**Benefits:**
- Formal dependency graph: can traverse `depends_on` to find all downstream results affected by a demotion
- Promotion justifications are queryable from ResearchState, not buried in EVENT_LOG
- Cascade safety: prevents promoting derived claims before their premises are established
- The verifier (`verify.py`) can check the justification graph for completeness

**`normalize_references` impact:** The existing method already remaps `WH-NNN → ER-NNN` in computation targets. It should also remap `depends_on` entries when a dependency is promoted (e.g., `depends_on: ["WH-002"]` → `depends_on: ["ER-002"]`).

---

## C. Scaffolding behavior (edge-case handling)

### C1. `empty_end_turn_fallthrough`: inject conclusion message instead of breaking the loop

**Where:** `llm.py:284-290`

**Problem:** When the model returns `end_turn` with no text and no tool calls in the current round (but prior tool calls exist), the scaffold immediately `break`s out of the agent loop and falls through to the text-only forced final call. This is too aggressive — the agent may have many rounds remaining. The forced final call then tells the model "You cannot call any more tools", which prevents the structured exit tool (`submit_result`/`submit_verdict`/`set_next_task`) from being called. Any work done in prior rounds is effectively lost as unstructured output.

**Proposal:** Instead of `break`ing, inject a firm user message demanding conclusion via the agent's specific exit tool, and **continue the loop**. The message must mention only the one exit tool relevant to the current agent/task mode — e.g. `submit_result` for compute_explore, `submit_verdict` for compute_verify, `set_next_task` for orchestrator. Mentioning multiple tools would confuse the model. Something like:

> "You returned an empty response. Your session will end soon. You MUST now call `submit_result` to record your conclusions. If you do not, your work will be lost."

The scaffold can determine which exit tool to name from the `tool_executor` or the task type. Keep the `break`-to-forced-final as a last resort only when `max_rounds` is actually exhausted.

**Implementation notes:**
- Track empty-turn count to avoid infinite nudge loops (break after 2 consecutive empties)
- The injected message should name the specific exit tool(s) available, not just generically say "conclude"
- Log the event as `empty_end_turn_recovery` (distinct from the current `empty_end_turn_fallthrough`)

### C2. All scaffold-injected messages must be context-aware about the exit tool

This is a general principle that applies to multiple injection points:
- The `report_progress` acknowledgment text currently hardcodes "Call `submit_verdict` now" even in explore mode (should say `submit_result`).
- The forced final call message.
- The empty end-turn recovery message (C1 above).

All should derive the correct exit tool name from the task type or tool executor.

### C3. Don't emit EXPLORE RESULTS banner for failed computations

**Where:** `engine.py:448-454` (`_track_computation`)

**Problem:** When the computationalist fails without calling `submit_result` (e.g., max_rounds exhaustion), the scaffold still creates a `Computation` object with empty fields and appends it to `pending_explore_results`. This produces a garbled banner like `- : unknown  [partial]` that contradicts the AGENT FAILURES banner which correctly reports the failure. The orchestrator receives two conflicting signals: "here's a result" and "the agent failed."

**Fix:** Gate `pending_explore_results.append()` on whether the computation actually produced a result. Note: `zero_output` is currently not reliably set (see bug A4), so use a broader check:
```python
if comp.kind == "explore" and comp.result and comp.target_hypothesis:
    self._state.pending_explore_results.append({...})
```
Once A4 is fixed, `not comp.zero_output` can be used as the primary gate.

**Rationale:** The AGENT FAILURES banner is the correct signal for failures. The EXPLORE RESULTS banner should only carry actual results. Emitting both is contradictory and wastes orchestrator attention.

### C4. Reduce noise in computation information delivered to orchestrator

**Two issues:**

**a) EXPLORE RESULTS banner truncates at 200 chars** (`engine.py:451`, `result[:200]`)
The banner is the orchestrator's primary "act on this now" signal for explore results, but 200 chars cuts mathematical expressions mid-formula. Increase to 500-800 chars. The banner is consumed once, so the extra tokens are a one-time cost.

**b) COMPUTATION_LOG includes failed computations with empty fields**
The orchestrator context includes COMPUTATION_LOG.md with all entries, including `zero_output` failures that show all-empty fields (TARGET: blank, DESCRIPTION: unknown, METHOD: blank, etc.). These entries are pure noise — the AGENT FAILURES banner already signals the failure.

**Fix for (b):** The orchestrator context renderer should filter out `zero_output` computations from COMPUTATION_LOG. Either:
- Skip them in `render_computation_log_md()` entirely, or
- Render them as a single collapsed line: `TASK-001: FAILED (no result produced, iteration 1)` instead of the full empty template.

This keeps the log clean and focused on computations that actually produced results.

### C5. `forced_final_call` reason should distinguish empty-turn from max-rounds

**Where:** `llm.py:451-459`

**Problem:** The post-loop forced final call always logs reason `max_rounds` and tells the model "You have reached the maximum number of tool-use rounds" — even when the loop was exited early by `empty_end_turn_fallthrough`. This produces misleading EVENT_LOG entries and incorrect scaffold injection text.

**Proposal:** If improvement C1 is implemented (empty turns no longer break to forced-final), this becomes less critical since the forced-final path would only fire on actual max_rounds exhaustion. But if the break path is kept for any reason, the forced-final code should carry forward the actual exit reason:
- Track why the loop exited (a variable like `loop_exit_reason: "max_rounds" | "empty_end_turn" | "tool_call_failure"`)
- Use the correct reason in both the log event and the injected message
- For `empty_end_turn`: "You returned an empty response and your session is ending." (not "max rounds reached")

---

## D. Prompt improvements

### D1. Orchestrator prompt (`prompts/orchestrator.md`)

- **Role contradiction on derivation:** The prompt says "you do not derive" but `add_hypothesis(statement, derivation)` invites full derivations. Either rename the parameter to `justification` and instruct the orchestrator to write brief rationale only (delegating actual derivation to the researcher), or accept that the orchestrator bootstraps initial hypotheses with lightweight reasoning.
- **Redundant tool list:** Lines 15-25 repeat tool names/signatures already provided via the tool-use API. Remove to save ~200 tokens.
- **Legacy alias noise:** "compute: Legacy alias for compute_verify" is implementation debt leaking into the prompt. Remove — the LLM doesn't need to know about deprecated aliases.
- **Missing budget/efficiency guidance:** The orchestrator sees "iteration N of M" in user content but the prompt says nothing about how to pace work. Add a brief note on iteration awareness.
- **Edge-case guidance is front-loaded:** Convergence detection, resolve-critique loops, dead-end tracking (lines 58-62) are rarely relevant but cost attention on every call. Move to a "ADVANCED" or "EDGE CASES" section at the bottom so the common-path instructions come first.

---

## E. Observability

### E1. Remove always-firing scaffold events from EVENT_LOG

**Where:** `llm.py:370-372` (`executor_stop_signal`), `orchestrator.py:191-193` (`orchestrator_tool_mutations`)

**What:** Remove these `log_scaffold_event` calls (or move them to debug-level tracing). Scaffold events should only log when something *exceptional* happens — a compensation mechanism activating, a fallback path taken, an LLM failure being recovered. The normal happy path should be silent.

**Why:** The EVENT_LOG should be a concise record of "what went wrong and how the scaffolding compensated." Always-firing events dilute signal-to-noise ratio, making it harder to diagnose runs.

**Note:** `orchestrator_tool_mutations` does skip logging when there are no mutations (observed in iter 2), so it's not fully always-firing. But it still fires on every normal orchestrator pass that includes any state change, which is the common case. `executor_stop_signal` fires on every successful agent exit — pure noise.

---

## F. Validation checklist for next run

After implementing the changes above, run the same problem (`quantum_error_correction_main.yaml`) and verify the following:

### Bug fixes (must pass)

- [ ] **A1:** Problem statement visible in all agent contexts from iteration 1 onward (check orchestrator, computationalist, critic user content)
- [ ] **A1:** `RESEARCH_GRAPH.json` has non-empty `problem_statement`
- [ ] **A2:** `total_computations` in COMPUTATION_LOG frontmatter matches actual entry count
- [ ] **A3:** `RESEARCH_GRAPH.json` reflects final state after termination — all promotions applied, `status: "completed"`, correct iteration count
- [ ] **A4:** Failed computations (no exit tool) have `zero_output: true` in RESEARCH_GRAPH.json

### Architecture (if implemented)

- [ ] **B1:** Orchestrator creates RQs for open questions, WHs only for concrete claims. No vague WHs like "F(p) is a rational function"
- [ ] **B1:** Explore tasks target RQs, verify tasks target WHs
- [ ] **B2:** Computationalist prompt contains only the tools relevant to its mode (no `submit_verdict` docs in explore mode, no `submit_result` docs in verify mode)
- [ ] **B3:** Orchestrator uses `research_verify` for analytical claims (e.g., ideal circuit state) and `compute_verify` for numerical claims (e.g., F(p) expression)
- [ ] **B3:** `verified_results` only contains ERs that have actual verification evidence (not auto-backfilled from ER sections)
- [ ] **B4:** `depends_on` populated for derived hypotheses (e.g., WH-003 depends on WH-002)
- [ ] **B4:** `promotion_justification` stored on Hypothesis object, readable from RESEARCH_GRAPH.json
- [ ] **B4:** Attempting to promote a WH whose dependencies are not yet ERs returns an error

### Scaffolding behavior

- [ ] **C1:** Empty end-turn triggers a recovery nudge (naming the correct exit tool), not an immediate break to forced-final. Check that the computationalist calls its exit tool after the nudge
- [ ] **C2:** `report_progress` acknowledgment names the correct exit tool for the current mode
- [ ] **C3:** Failed explorations (no result, no target) don't produce EXPLORE RESULTS banners. Only AGENT FAILURES banner appears
- [ ] **C4a:** EXPLORE RESULTS banner shows full mathematical expressions (not truncated mid-formula)
- [ ] **C4b:** COMPUTATION_LOG in orchestrator context doesn't show all-empty entries for failed computations
- [ ] **C5:** If forced-final fires, the reason distinguishes `empty_end_turn` from `max_rounds` in both the log event and the injected message

### Prompt improvements

- [ ] **D1:** Orchestrator prompt has no redundant tool list (tools come from API only)
- [ ] **D1:** No "compute: Legacy alias" in prompt
- [ ] **D1:** Edge-case guidance (convergence, resolve-critique loops, dead ends) is in a separate section at the bottom, not interleaved with common-path instructions

### Observability

- [ ] **E1:** EVENT_LOG contains no `executor_stop_signal` events
- [ ] **E1:** EVENT_LOG scaffold events are limited to exceptional/compensating actions
- [ ] **E1:** A successful run with no failures produces a near-empty scaffold event log (only `promote_hypothesis`, `forced_critic`, and similar meaningful state transitions)

### Efficiency comparison (baseline from this run)

| Metric | Baseline (this run) | New run |
|--------|-------------------|---------|
| Total iterations | 5 | |
| Total tokens (in+out) | ~139K | |
| Wasted tokens (failed iter + redundant compute) | ~47K (34%) | |
| Computationalist failures (no exit tool) | 1 | |
| Time to correct answer | ~7 min | |
| Critic critiques filed | 0 | |
