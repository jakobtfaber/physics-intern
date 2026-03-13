# SciRalph — Architecture Diagnosis

> **Date:** 2026-03-13
> **Scope:** Sections 1–2 (Architecture & Main Loop) and Section 7 (LLM Failure Compensation) of CODEBASE.md, cross-referenced against actual code in `engine.py`, `validation.py`, `llm.py`, and agent modules.

---

## 1. Overall Verdict

**It's not spaghetti.** The architecture is well-structured — the main loop has a clear sequential pipeline, responsibilities are mostly well-separated, and the override chain has explicit priorities. But with 50+ mechanisms across 10 layers, there are **interaction effects** that are hard to reason about statically, and a handful of places where mechanisms partially duplicate or subtly conflict with each other.

---

## 2. Architecture & Main Loop (Sections 1–2)

The core design is sound. Fresh-context-per-call, staging discipline (researcher → PROPOSED_CHANGES → orchestrator integrates), typed tasks, all state in Markdown — these are good choices that make the system debuggable and recoverable.

The main loop in `engine.py` is a clean 9-step pipeline. The override chain (`_apply_overrides`) is the most complex piece, but the explicit P1–P6 priority ordering with early returns is the right way to handle competing concerns. One priority fires, the rest are skipped. No ambiguity there.

### Structural concern: violation accumulator fragility

`_build_context_prefix()` clears `_pending_violations` *before* the orchestrator LLM call happens. If that call raises a non-transient exception, those violations are gone forever. Low probability, but the fix is trivial (clear after success, not before).

---

## 3. LLM Failure Compensation (Section 7) — Layer-by-Layer Assessment

### 3.1 Clearly load-bearing (don't touch)

| Mechanism | Layer | Why it's essential |
|-----------|-------|--------------------|
| API retry with exponential backoff | L1 | Table stakes infrastructure |
| ER demotion/promotion gate | L4 | Single most important safety mechanism — LLMs *will* prematurely promote WH→ER |
| Phantom reference replacement | L4 | LLMs hallucinate COMP-NNN IDs constantly |
| P1 budget enforcement | L5 | Without this, runs never terminate |
| P3 forced critic | L5 | Without this, orchestrator skips critique forever |
| P5 stall blocking | L5 | Without this, infinite loops on broken claims |
| Problem statement enforcement | L7 | LLMs rewrite problem statements (scope drift) |
| YAML parse fallback to regex | L10 | LLMs produce invalid YAML constantly |

### 3.2 Defensive but fine (low cost, leave them)

| Mechanism | Layer | Notes |
|-----------|-------|-------|
| Zero-text-streak and low-cumulative-text bailouts | L2 | Cheap safety nets, worth keeping even if rare |
| Tool execution guards (timeout, truncation, banned APIs) | L3 | Standard infrastructure |
| Critic preamble stripping, self-retraction filtering | L8 | Fire in practice; real observed failure mode |
| Computationalist empty-response stub, header injection | L9 | Cheap guards |
| Code-fence stripping, CRITIQUE-NNN alias tolerance | L10 | Near-zero cost tolerance mechanisms |

### 3.3 Suspicious interactions and potential redundancies

#### Issue 1 — Three phantom reference checks per iteration, two code paths

- `workspace.validate_comp_references()` runs before the orchestrator (workspace-level, no Violation emitted)
- `check_phantom_references()` runs in `validate_post_integration` after the orchestrator (emits Violations)
- `check_phantom_references()` runs *again* post-dispatch (emits more Violations)

The first is silent cleanup; the second and third produce Violations seen by the orchestrator next iteration. Probably harmless (idempotent), but confusing to trace.

**Profiling target:** Log which of the three actually finds phantoms. Hypothesis: the workspace-level one does most of the work and the post-dispatch one almost never fires.

#### Issue 2 — Validation checks 3 and 4 are near-inverses with asymmetric evidence thresholds

Check 3 (`check_phantom_labels`) strips "VERIFIED" from prose when the ID isn't in a computation *claim*. Check 4 (`check_stale_unverified_labels`) restores "VERIFIED" when the ID appears in a computation *claim or body*. Because check 4 searches more broadly (claim + body), there's a narrow edge case where check 3 strips a label and check 4 immediately re-adds it in the same pass — generating both an ERROR and a WARNING violation that net to zero change.

**Profiling target:** Count how often checks 3 and 4 fire on the same ID in the same iteration.

#### Issue 3 — Checks 2 and 5 both maintain `verified_results` frontmatter

Check 2 (ER promotion gate) normalizes the frontmatter list as a side effect of its rename logic. Check 5 (verified frontmatter backfill) does it as its primary job. If check 2 fires, check 5 is usually a no-op. **This is a real redundancy** — not harmful, but adds code surface for no gain. Check 5 could be the sole owner of frontmatter maintenance.

**Profiling target:** Count how often check 5 actually adds IDs that check 2 didn't already handle.

#### Issue 4 — P4 (REFUTED recompute) silently drops on SYNTHESIZE/TERMINATE

`_pending_recompute_claim` is cleared without action or logging when the orchestrator independently chose SYNTHESIZE or TERMINATE. This is an intentional escape valve, but the silent drop means you can't tell from logs whether a REFUTED result was addressed or forgotten.

**Profiling target:** Log when P4 is suppressed by SYNTHESIZE/TERMINATE. Check if any REFUTED claims were left unaddressed in final RESEARCH_STATE.

#### Issue 5 — Forced final call duplicates format instructions from user-turn nudges

At `max_rounds - 1`, the loop injects a COMP format template as a user message. Then the forced final call rebuilds the system prompt with nearly identical format instructions. The model sees the template twice. This could cause doubled COMP entries in the output, both of which would be appended to COMPUTATION_LOG.

**Profiling target:** Check if forced-final-call outputs ever contain two `## COMP-` headers.

#### Issue 6 — `_stalled_claims` grows monotonically with no recovery path

Once a claim enters the stalled set, it stays forever. If the orchestrator rephrases the claim slightly, `_normalize_claim_key()` may or may not match, creating an inconsistent block. **This is the mechanism most likely to cause surprising behavior in long runs.** A claim could be correctly re-derived under a slightly different name and the stall block would be irrelevant but still sitting there.

**Profiling target:** Track stalled claims across a full run. Check if any stalled claims were later successfully verified under a different name.

---

## 4. The Deeper Architectural Observation

The 10 layers fall into three conceptually distinct concerns that are currently interleaved:

| Concern | Layers | Where it lives |
|---------|--------|---------------|
| **API/transport resilience** | 1, 2, 3 | `llm.py`, `tools.py`, `sandbox.py` |
| **State invariant enforcement** | 4, 7, 8, 9, 10 | `validation.py`, agent `process_response`, `markdown.py` |
| **Loop flow control** | 5, 6 | `engine.py` |

The first and third are cleanly separated. The second concern — state invariant enforcement — is spread across too many places. The same conceptual invariant ("ER status must match computation backing") is enforced by:

1. `validation.py` check 2 — demote/promote ER headers
2. `validation.py` check 3 — strip phantom VERIFIED labels
3. `validation.py` check 4 — restore stripped labels
4. `validation.py` check 5 — frontmatter backfill
5. `orchestrator.process_response` — header normalization
6. `workspace.validate_comp_references` — phantom ref stripping
7. Post-dispatch phantom check — another pass

That's 7 touch points for one invariant. Each is individually reasonable, but together they make it hard to predict what state RESEARCH_STATE.md is in at any given point in the iteration.

---

## 5. Recommendations Before Profiling

### 5.1 Instrument every mechanism in §7

Add a counter/log to every mechanism. Even a simple `console.print(f"[dim]{mechanism_name} fired[/dim]")` would show which ones are doing work over a 10-iteration run. Hypothesis: many fire 0 times on well-behaved models (Claude Sonnet).

### 5.2 Separate "fix" from "report"

Several validation checks both mutate files *and* emit Violations. Consider having them return the Violation without writing, letting a single writer apply all changes. This would eliminate ordering dependencies between checks 1–5.

### 5.3 Unify the phantom reference paths

Three separate phantom checks (workspace-level, validation pipeline, post-dispatch) doing overlapping work via different code is the single most confusing part of the codebase.

### 5.4 Add a P4-suppression log

Silent drops of REFUTED recomputes are a correctness concern worth tracking.

### 5.5 Consider a stall recovery mechanism

Even something simple: "clear stall for claim X if a VERIFIED computation arrives for anything mentioning X."

### 5.6 Fix the violation accumulator timing

Clear `_pending_violations` after the orchestrator call succeeds, not before. Trivial fix, eliminates the data-loss path.

---

## 6. Profiling Plan

The right next step is empirical. Run a few problems (e.g., `hawking_temperature`, `thomas_fermi`) with instrumentation that logs every time a §7 mechanism fires. Key questions:

| Question | How to measure |
|----------|---------------|
| Which mechanisms fire most? | Counter per mechanism per iteration |
| Do checks 3 and 4 fight each other? | Count same-ID fire in same iteration |
| Does check 5 ever add IDs that check 2 missed? | Diff check 5's additions against check 2's |
| How often does P4 silently drop? | Log P4 suppression events |
| Does the forced final call produce doubled entries? | Scan COMPUTATION_LOG for consecutive same-round COMP headers |
| Do stalled claims get re-verified under new names? | Track `_stalled_claims` vs final VERIFICATION.md results |
| Which phantom check layer catches most? | Counter per phantom check site |

Expectation: 60–70% of mechanisms fire < 1% of the time with Claude Sonnet. The interesting finding will be which of the remaining ones are *fighting each other*.
