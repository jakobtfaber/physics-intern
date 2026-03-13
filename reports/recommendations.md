# Scaffolding Recommendations

Consolidated from two independent cross-model analysis rounds on the Thomas-Fermi problem:
- **Round 1** (6 models: Opus, GPT, Gemini, DeepSeek, Kimi K2.5, GLM 5) — `reports/20260313_0721_thomas_fermi_synthesis.md`
- **Round 2** (9 models: claude-opus-4.6, gpt-5.4, gemini-3.1-pro, deepseek-v3.2, kimi-k2.5, glm-5, gpt-oss-120b, minimax-m2.5, qwen-3.5-397B) — analysis conducted 2026-03-13

Items are ordered by priority. Each entry describes the problem, the evidence across models, the root cause, and the recommended fix.

---

## P0-1: Dispatch-level stall tracking for repeated computation failures

DONE
---

## P0-2: Route failure context to orchestrator via `context_prefix`

DONE

---

## P0-3: Guarantee text output from agentic loops

DONE

---

## P1-1: ER promotion gate redesign

**Problem:** The orchestrator eagerly promotes working hypotheses to "established results" (ER) status. The ER promotion gate then demotes them because no VERIFIED computation backs the promotion. On the next pass the orchestrator promotes again, the gate demotes again, creating pointless churn that wastes context tokens and adds noise to the state.

**Evidence:**
- Round 2: 9/9 models exhibited this pattern.
  - minimax-m2.5: ER-001 demoted 3 times (iter 3, 7, 9)
  - deepseek-v3.2: 8 demotions total
  - gpt-5.4: ER-002 and ER-003 demoted/re-promoted multiple times
  - claude-opus-4.6: ER-004 demoted at iter 11, re-promoted after COMP-012
  - qwen-3.5: ER-001 demoted at iter 6, re-promoted at iter 8
  - gpt-oss-120b: All 4 ERs demoted in final iteration
  - All other models showed at least 1-2 demotion events.
- Round 1: Not explicitly flagged (may have been present but not identified as a standalone pattern).

**Root cause:** The orchestrator and the validation gate have conflicting policies. The orchestrator's prompt encourages promotion when analytical derivation is complete. The gate requires VERIFIED computation. Neither side is wrong individually, but the mismatch creates a deterministic churn cycle.

**Recommended fix (two options, pick one):**

**Option A (prevent premature promotion):** Add to the orchestrator prompt: "Do NOT promote a working hypothesis to established result (ER) status unless it has a VERIFIED computation entry. If analytical derivation is complete but unverified, keep it as a working hypothesis and schedule a compute task." This is the simpler fix.

**Option B (advisory demotion):** Change the ER promotion gate from hard demotion to advisory. Instead of rewriting the state to demote ER→WH, inject a warning into `context_prefix`: "ER-NNN lacks VERIFIED computation backing — schedule verification or demote." Let the orchestrator decide. This is more flexible but risks the orchestrator ignoring the warning.

**Recommendation:** Option A. It's simpler and eliminates the churn at source.

**Files:** `prompts/orchestrator.md` (for Option A), or `validation.py` + `engine.py` (for Option B).

---

## P1-2: Max-tokens truncation signal to orchestrator

**Problem:** When a researcher hits the output token ceiling, the engine retries the identical task verbatim. The orchestrator doesn't know truncation occurred, so it never decomposes the task into smaller pieces.

**Evidence:**
- Round 1: 3/6 models affected. Opus: 3 retries on TASK-007 (~99K wasted output tokens). GPT: 11 retries across iterations 15-34. Kimi: 6 retries (~98K wasted tokens).
- Round 2: Less prominent (possibly due to different model versions or settings), but `tool_loop_truncated` alerts appeared in gemini-3.1-pro (iter 2, 11), kimi-k2.5 (iter 2, 5), qwen-3.5 (iter 9), deepseek-v3.2 (29 times).

**Root cause:** The engine tracks `max_tokens_reached_count` in metrics but never injects this information into the orchestrator's context. The retry logic is blind.

**Recommended fix:**

On the first max_tokens event for a given task, do NOT retry. Instead, re-dispatch to the orchestrator with a `context_prefix` message:

```
>>> CAPACITY EXCEEDED <<<
TASK-NNN exceeded output token limit. The task is too large for a single agent call.
Decompose into smaller subtasks, each targeting a single derivation step or sub-claim.
>>> END CAPACITY EXCEEDED <<<
```

This is a specific instance of P0-2 (failure context routing) but called out separately because the fix is straightforward and the waste is large. If P0-2 is implemented as a general mechanism, this becomes a special case of it.

**Files:** `engine.py` (dispatch/retry logic), feeds into `context_prefix`.

---

## P1-3: Critic readiness check before P3 forced_critic

**Problem:** The P3 `forced_critic` override fires on a timer (N iterations since last critic pass) regardless of whether there's anything new to critique. It displaces the currently scheduled task, which is often a high-priority derivation or computation.

**Evidence:**
- Round 2: 7/9 models had tasks displaced by forced critic.
  - glm-5: Displaced TASK-005 and TASK-006 (derive tasks) at iter 5, 6
  - minimax-m2.5: Forced at iter 4, produced zero critiques (complete waste of ~5.7K tokens)
  - deepseek-v3.2: 10 forced critic invocations
  - gpt-oss-120b: 4 forced critic passes
  - kimi-k2.5: Displaced TASK-004 (compute) at iter 4
  - qwen-3.5, gpt-5.4: 2 each
- Round 1: Not flagged as standalone issue, but the override displacement problem was noted under Cluster 4.

**Root cause:** P3 has no awareness of (a) whether new material exists since the last critic pass, or (b) the priority of the task being displaced.

**Recommended fix:**

Add pre-conditions before P3 fires:

1. **New-material check:** Skip forced critic if no new PROPOSED_CHANGES, COMP entries, or researcher output has been added since the last critic pass. The critic will have nothing new to review.

2. **Priority-aware deferral:** If the task being displaced is `compute` or `derive` type, defer the critic by 1 iteration rather than displacing immediately. The critic can run after the higher-priority task completes.

3. **Post-critic validation:** If a forced critic pass produces zero critiques (`no_critiques_filed`), increase the interval before the next forced critic by +2 iterations to avoid repeated empty passes.

**Files:** `engine.py` (`_apply_overrides`, P3 logic).

---

## P2-1: Circuit breaker for transient provider failures

**Problem:** When API calls fail with ReadTimeout or 5xx errors, the engine runs the full orchestrator cycle (expensive) only to arrive at the same task that fails again for the same infrastructure reason. No circuit-breaker recognizes that workspace state hasn't changed.

**Evidence:**
- Round 1: 1/6 models (GLM)
- Round 2: 3/9 models. GLM-5: 16 ReadTimeout retries, 4 dispatch failures, ~40% of iterations lost to infrastructure. DeepSeek-v3.2: 26 ReadTimeout failures. Qwen-3.5: 4 ReadTimeout retries (recovered cleanly via backoff).

**Root cause:** The engine always runs the full orchestrator → dispatch cycle even when the previous iteration failed due to a transient error and workspace state hasn't changed. The orchestrator call itself is expensive and produces the same task.

**Recommended fix:**

If the previous iteration's dispatch failed with a transient error (timeout, 5xx, `HfHubHTTPError`) and no workspace files changed since the last successful iteration:

1. Skip the orchestrator call entirely.
2. Retry the previously queued task directly.
3. Use a longer inter-iteration cooldown (60-120s instead of the normal delay) to let provider issues resolve.
4. After 3 consecutive transient failures on the same task, fall through to the normal orchestrator cycle (the provider may be down for an extended period).

**Files:** `engine.py` (main loop, needs a "last dispatch status" flag).

---

## P2-2: Phantom reference cross-referencing

**Problem:** The validation pipeline replaces unverified references with `[X:unverified]` tags. But these tags sometimes persist even after the referenced computation is marked VERIFIED in COMPUTATION_LOG.md, corrupting the research state with false warnings.

**Evidence:**
- Round 2: 4/9 models.
  - minimax-m2.5: TASK-004 flagged across 5 iterations
  - glm-5: COMP-003 tagged `[COMP-003:unverified]` even after VERIFIED verdict; persisted in ER-001 and ER-002 frontmatter
  - qwen-3.5: TASK-004 and COMP-009 flagged
  - deepseek-v3.2: Multiple phantom references
- Round 1: Not flagged.

**Root cause:** The phantom reference check scans for bare `COMP-NNN` patterns and checks against the `verified_results` frontmatter field. But a computation can be VERIFIED in COMPUTATION_LOG without yet being listed in `verified_results` frontmatter (the backfill happens separately). The check doesn't cross-reference the actual COMPUTATION_LOG verdicts.

**Recommended fix:**

In the phantom reference validation check, also scan COMPUTATION_LOG.md for VERIFIED verdicts. If a `COMP-NNN` reference is tagged VERIFIED in the log, don't flag it as phantom even if it's not yet in the frontmatter. This is a small change to the validation function.

**Files:** `validation.py` (phantom reference check function).
