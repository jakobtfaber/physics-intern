# SciRalph — Task List

## Phase 1



## Phase 2: Engine Hardening

Findings from two rounds of 8 test runs each (March 10 2026): QHO, Ising, Hawking, Berry Phase, Chandrasekhar, Path Integral HO, Perihelion, Renormalisation. All 8 runs produce correct science (VALID/HIGH). Process verdict: only 2/8 EFFECTIVE; the other 6 share a small set of recurring failure modes.

### Architecture: Three Layers, Not Eight Patches

The original P1–P8 list identified real problems, but implementing them as independent if-statements scattered through `engine.py` would produce spaghetti. The failures actually fall into three architectural layers — each layer is a single, testable module with a clear responsibility:

**Layer A — Iteration contract** (`engine.py` loop structure)

The engine loop currently has an ambiguous definition of "iteration." Sometimes it's just the orchestrator pass; sometimes it's orchestrator + dispatched agent. This ambiguity is the root cause of the most common failure (5/8 runs): the orchestrator creates a task, the loop increments the counter, hits `max_iterations`, and exits before the task runs. Fixing the loop contract also provides natural insertion points for pre-termination checks (critic review, coverage verification) — these become steps in the loop, not bolted-on conditionals.

**Layer B — Post-integration validation** (`validation.py`, new module)

After each orchestrator integration pass, a single validation function checks a list of invariants against the workspace files. This replaces four separate fixes (old P1, P6, P8, phantom label stripping) with one extensible pipeline:

```python
def validate_post_integration(workspace, config) -> list[Violation]:
    """Run all invariant checks. Returns violations to inject into next orchestrator context."""
    checks = [
        check_er_promotion_gate,      # new ER without VERIFIED COMP → demote
        check_task_agent_routing,      # compute task sent to wrong agent → reroute
        check_phantom_labels,          # researcher-written VERIFIED → strip
        check_id_consistency,          # COMP/CRIT counter gaps → fix
    ]
    return [v for check in checks for v in check(workspace)]
```

Each check is a small pure function — easy to add, test, and disable individually. Violations are injected as structured warnings into the orchestrator's next context.

**Layer C — Agent loop resilience** (`llm.py` inner loop)

The `run_agent_loop` function needs two improvements that are independent of the engine: forced partial output on truncation (so truncated runs produce usable verdicts instead of empty stubs), and stall detection (so the engine can track repeated failures and escalate). These live in the agent loop itself, not in engine-level iteration logic.

### Implementation order

The layers have a dependency chain: Layer C (forced partial output) gives Layer B (stall detection) actual verdict data to work with; Layer A (loop contract) provides the structural hooks where Layer B's checks and Layer C's escalation signals are consumed. But Layer A's loop fix is the highest-impact standalone change (5/8 runs), so it comes first:

| Step | Layer | What | Absorbs | Impact |
|------|-------|------|---------|--------|
| 1 | A | Fix iteration contract + termination gates | New (not in old P1–P8), old P5 | 7/8 runs |
| 2 | C | Forced partial output on truncation | Old P4 | 4/8 runs |
| 3 | B | Post-integration validation pipeline | Old P1, P6, P8 | 5/8 runs |
| 4 | C | Stall detection (builds on steps 2+3) | Old P2 | 3/8 runs |
| 5 | — | Output cleanup (critique preamble strip) | Old P7 | Cosmetic |
| 6 | — | Verdict validator (deferred) | Old P3 | 0/8 recent runs |

---

### Step 1 — Fix the iteration contract (Layer A)

**Problem:** The orchestrator emits a non-`terminate` task (resolve, compute, synthesize), the loop increments the iteration counter, hits `max_iterations`, and exits — leaving the task unexecuted with budget remaining. Observed in **5/8 runs** (QHO: TASK-004, Berry Phase: TASK-009, Path Integral HO: TASK-005, Chandrasekhar: TASK-016, Ising: early exit via completion check). This is the single most common process failure and was not identified in the original P1–P8 list.

**Root cause:** An "iteration" is currently counted as one orchestrator pass. The dispatched agent runs in the same iteration but the counter has already incremented, so the boundary check fires between orchestrator and agent.

**Fix:** Redefine one iteration as the full cycle: `orchestrator → agent dispatch → (optional forced critic/compressor)`. The `max_iterations` check fires only after the dispatched agent has completed. Concretely in `engine.py`:

```
while iteration <= max_iterations:
    # 1. Orchestrator pass → reads state, emits CURRENT_TASK
    task = run_orchestrator(...)

    # 2. Pre-dispatch validation (Layer B hook, step 3)
    violations = validate_post_integration(workspace, config)
    # inject violations into next context if any

    # 3. Termination gate (absorbs old P5)
    if task.type == "terminate":
        allowed, reasons = can_terminate(workspace, config)
        if not allowed:
            inject_blocking_reasons(reasons)
            continue  # force orchestrator to address blockers
        break

    # 4. Dispatch agent (researcher/computationalist/critic)
    run_agent(task, ...)

    # 5. Post-dispatch checks (stall detection hook, step 4)

    iteration += 1  # count AFTER the full cycle
```

**Termination gates** (`can_terminate`): a single function checking preconditions before allowing exit. Absorbs old P5 and adds new checks:

- At least one critic pass has occurred (or no ERs exist yet)
- No ERs promoted since last critic pass without review → insert final critic pass
- No unresolved HIGH critiques
- Problem statement coverage: all sub-objectives in the YAML `steps` list are addressed by at least one ER (absorbs the Path Integral HO gap where 2/3 sub-objectives were unaddressed)
- `total_computations > 0` when the problem requires numerical verification (flag in problem YAML: `requires_numerical: true`)

If any gate fails, inject the blocking reasons as structured warnings and let the orchestrator try again. The orchestrator can still force termination via `budget_override` if it's genuinely stuck.

**Evidence from runs:**
- QHO: TASK-004 (resolve 3 critiques) would have executed → 3 critiques resolved
- Berry Phase: TASK-009 (resolve CRIT-007/008) would have executed → sign convention fixed
- Path Integral HO: TASK-005 (resolve CRIT-015/016/017) would have executed → phase check done
- Chandrasekhar: TASK-016 (final synthesis) would have executed → FINAL_SYNTHESIS.md written
- Hawking, Ising: `can_terminate` would have blocked exit (0 critic passes, 0 computations)

### Step 2 — Forced partial output on truncation (Layer C)

**Problem:** When the computationalist hits `max_rounds` in the tool-use loop, COMPUTATION_LOG gets an empty stub ("Agent produced no text output") and the entire iteration is wasted. Observed in **4/8 runs** (Chandrasekhar ×8 truncations, Perihelion ×3, Path Integral HO ×2, Berry Phase ×1). Chandrasekhar alone wasted ~400K tokens (50.4% of budget) on truncated runs with zero usable output.

**Fix:** In `llm.py` `run_agent_loop`, when hitting `max_rounds`:

1. Append a system message: "You have reached the maximum number of tool-use rounds. You MUST now write your COMP-NNN entry with whatever results you have so far. Use verdict INCONCLUSIVE if incomplete. Summarize what worked and what remains."
2. Make one final LLM call with tools disabled (text-only) to extract a partial result.
3. Return this as the agent's output — the scaffold writes it to COMPUTATION_LOG as usual.

This ensures every truncation produces a usable INCONCLUSIVE verdict with partial results, rather than an empty stub. Step 4 (stall detection) depends on this: it needs actual verdict data to count consecutive failures.

**Also consider:** Summarize prior tool outputs in the conversation to reduce quadratic context growth. When the conversation exceeds N tokens, collapse earlier rounds into a summary before the next tool call. This addresses the root cause of some truncations (context blowup), not just the symptom.

### Step 3 — Post-integration validation pipeline (Layer B)

**Problem:** The orchestrator's LLM output violates scaffold invariants that the prompt requests but the model ignores. Rather than scattering per-violation if-statements through `engine.py`, implement a single validation module that runs after every orchestrator integration pass.

**Fix:** Create `validation.py` with a pipeline of check functions. Each check reads workspace state and returns a (possibly empty) list of `Violation` objects. The engine calls `validate_post_integration()` once per iteration at the Layer A hook point (step 1, between orchestrator and dispatch).

**Check functions:**

**(a) ER promotion gate** (absorbs old P1)

Scan RESEARCH_STATE for any ER-NNN entries added since last iteration. For each new ER, verify it references a COMP-NNN that exists in COMPUTATION_LOG with verdict VERIFIED. If not, demote the ER back to Working Hypothesis (rewrite the section header from `## ER-NNN` to `## WH-NNN`) and emit a violation warning.

Observed in 5/8 runs: Ising promoted 10 ERs with 0 computations; Berry Phase promoted 4 ERs with 0 computations; Path Integral HO promoted 6 ERs with 0 computations; Chandrasekhar promoted researcher-written numerical results when every computation was truncated; Perihelion promoted with phantom "COMP-A VERIFIED" label.

**(b) Task-agent routing** (absorbs old P6)

After parsing CURRENT_TASK, validate that the `assigned_to` field maps to a real agent. Alias resolution: `compute` → `computationalist`, `research` → `researcher`, `critique` / `review` → `deep_critic`. If a `compute`-type task is assigned to `researcher`, reroute to `computationalist` with a log warning.

Observed in 1/8 runs (Ising: `assigned_to: compute` silently fell back to researcher). Prompt rewrites reduced frequency but the alias gap remains a latent bug.

**(c) Phantom label stripping** (new, complements old P1)

Scan PROPOSED_CHANGES.md and RESEARCH_STATE.md for "VERIFIED" labels not backed by a real COMPUTATION_LOG entry. Strip the label (not just the COMP-NNN reference — the word "VERIFIED" itself). Only the computationalist writing to COMPUTATION_LOG can create VERIFIED verdicts.

Observed in 5/8 runs: researcher-written "[VERIFIED — HIGH confidence]" labels in Ising, Berry Phase, Path Integral HO; phantom COMP-001 through COMP-009 regenerated every iteration in Chandrasekhar; phantom "COMP-A VERIFIED" in Perihelion.

**(d) ID consistency** (absorbs old P8)

Assign COMP and CRIT IDs in the scaffold, not in LLM output. Pass `next_comp_id` and `next_crit_id` into agent context. After agent output, rewrite any LLM-chosen IDs to the scaffold-assigned ones. Fix the `total_computations` frontmatter counter to count COMP entries in the file body, not tool-call rounds.

Observed in 3/8 runs: QHO and Hawking had `total_computations: 2` with only 1 COMP entry; Berry Phase had duplicate CRIT-002.

### Step 4 — Stall detection for repeated computation failures (Layer C + engine)

**Problem:** The orchestrator re-dispatches the same computation task after repeated failures without decomposing scope or trying alternatives. Observed in **3/8 runs** (Chandrasekhar: 8 truncations on the same ODE, 50.4% waste; Perihelion: 3 attempts at the same numerical integration, 42.1% waste; Path Integral HO: 2 retries of the same broad task, 33% waste).

**Depends on:** Step 2 (forced partial output) — stall detection needs actual INCONCLUSIVE verdicts, not empty stubs, to count consecutive failures. Step 3 (validation pipeline) — stall state is tracked in the same framework.

**Fix:** In the engine, track consecutive INCONCLUSIVE/truncated COMPs targeting the same claim (match by ER-NNN/WH-NNN reference in CURRENT_TASK). After 2 consecutive failures on the same claim:

1. Inject a stall alert into the orchestrator context: "COMPUTATION STALL on [claim]: 2 consecutive failures (verdicts: INCONCLUSIVE, INCONCLUSIVE). You MUST either (a) reduce scope to a simpler sub-check, (b) assign to researcher for an alternative analytical approach, or (c) mark claim as UNVERIFIED and move on."
2. If the orchestrator re-dispatches the same task unchanged after a stall alert, block the dispatch and force option (c).

This can be implemented as another check in the validation pipeline (step 3), triggered at the post-dispatch hook in the iteration loop (step 1).

### Step 5 — Output cleanup (cosmetic)

**Critique log preamble stripping** (old P7): The critic's first-person preamble ("I will examine both claims systematically...") gets appended verbatim to CRITIQUE_LOG.md. Present in all 8 runs. Cosmetic only.

**Fix:** In `workspace.py` (or the critic output handler), strip all text before the first `## CRIT-` heading when appending to the critique log. Implement opportunistically.

### Step 6 — Verdict validator (deferred)

Old P3. The LLM declares VERIFIED on computations that partially or fully failed. Not observed in 8 post-Phase-1 runs — prompt rewrites appear to have resolved this. Defer unless it resurfaces in future test runs.

If needed, implement as another check in the validation pipeline (step 3): after the computationalist writes a COMP entry, scan script stdout for failure keywords (FAIL, ERROR, discrepancy) and reject a VERIFIED verdict if found. This slots naturally into the Layer B architecture without requiring a separate module.

---

## Future work

### Misc ideas?
- Use a more structured output format for agent responses (e.g., JSON with separate fields for "verdict", "summary", "next_steps") to reduce ambiguity and parsing errors.
- Use AgentType enum instead of string literals for agent routing and validation.
- Add a linting step for computation scripts to avoid running obviously broken code (syntax errors, missing imports). This could be a lightweight static check before execution.
- Add a more open ended "brainstorm" task and maybe a dedicated section in the research state for ideas, possible routes, alternatives, etc.

### Compression and context management

- **Priority-based compression** — compressor falls behind on large COMPUTATION_LOG files. Compress the largest file first instead of round-robin. Old VERIFIED computations: archive everything except the verdict line.

- **read_file tool for orchestrator/researcher/critic** — currently these agents get full context via `build_context()`. Adding `read_file` lets them drill into large files or inspect computation scripts. Implement when file sizes regularly approach compression thresholds.

### Multi-model support

- **More models** — add support for more models (GPT, Gemini, Open models via Hugging Face inference providers). Should we define a unified format for tool calls in prompts that works across providers? (e.g. JSON with "tool_name" and "args" fields)

### Problem YAML features

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

### Workspace management

- **Workspace resume** — `--resume <workspace-dir>` to continue a previous run. Skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state (corrupted state, version mismatches).