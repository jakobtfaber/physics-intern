# Thomas-Fermi Cross-Model Synthesis

Cross-cutting analysis of failure patterns across 6 model runs (Opus, GPT, Gemini, DeepSeek, Kimi K2.5, GLM 5) on the Thomas-Fermi problem. Identifies shared root causes behind differently-phrased symptoms.

## Recurring Failure Clusters

### Cluster 1: Max-tokens truncation with identical retry (3/6 runs)

**Affected runs:** Opus §1, GPT §1, Kimi §2

The orchestrator bundles too many deliverables into one task, the researcher hits the output token ceiling, and the engine retries the identical task verbatim. No decomposition ever happens.

- Opus: 3 retries on TASK-007, ~99K wasted output tokens
- GPT: 11 retries across iterations 15-34, same bundled task each time
- Kimi: 6 retries across iterations 3 and 10, ~98K wasted tokens

**Root cause:** No feedback path from a max_tokens failure back to the orchestrator. The engine tracks `max_tokens_reached_count` in metrics but never injects this into the orchestrator's context.

### Cluster 2: Computationalist produces tool calls but no final text (3/6 runs)

**Affected runs:** Gemini §1, DeepSeek §1, Kimi §1

The computationalist executes tool calls successfully but the COMP entry is never written. Three different surface mechanisms, same outcome:

| Run | Mechanism | Detail |
|-----|-----------|--------|
| Gemini | `end_turn` with 0 output tokens | Model "finishes" without writing anything after last tool call |
| DeepSeek | `max_rounds_forced` at round 4 | Model still issuing tool calls at cutoff, never writes text |
| Kimi | Pure tool-call loop, zero inline text | `zero_text_bailout` fires but model still produces nothing |

**Root cause:** `run_agent_loop` doesn't guarantee text output before loop exit. The existing `zero_text_bailout` and `max_rounds_forced` mechanisms catch some cases but miss the "final turn is empty" (Gemini) and "bailout fires but model still produces nothing" (Kimi) variants.

### Cluster 3: No effective loop-breaking for repeated failures on the same objective

**Affected runs:** GPT §1, DeepSeek §2, DeepSeek §3, Kimi §1, GLM §2

Two distinct sub-problems:

**3a — Detection failures (GPT, DeepSeek):** Stall tracking depends on LLM-generated text (COMP entry claim lines, orchestrator task wording) rather than engine-controlled data. Failures scatter across inconsistent keys, invisible entries get skipped, and the stale-loop backstop doesn't trigger because task descriptions change slightly between iterations.

**3b — Override priority inversion (GLM):** Stall detection works correctly (fires at threshold=2) but P4 (REFUTED recompute) has higher priority than P5 (stall blocking) in `_apply_overrides`, so the recompute override supersedes it. The engine displaces the orchestrator's own correct corrective actions. Additionally, `_check_for_refuted_verdict()` has no recompute counter — it fires unconditionally regardless of how many times recompute has already failed.

### Cluster 4 (meta): Orchestrator is stateless with respect to failures

This cuts across Clusters 1-3. The orchestrator doesn't know that:

- The researcher was truncated on this task (Cluster 1)
- The computationalist failed N times on this claim (Cluster 3)
- A HIGH critique is open (GPT §2)
- The engine's override chain displaced its planned task (GLM §2)

The `context_prefix` mechanism already exists for validation violations — failure signals just aren't routed through it.

### Cluster 5: No circuit-breaker for transient provider failures (1/6 runs)

**Affected run:** GLM §1

When API calls fail with ReadTimeout, the engine re-runs the full orchestrator (expensive) only to arrive at the same task that will fail again for infrastructure reasons. No circuit-breaker logic recognizes that the state hasn't changed and the same dispatch should be retried directly with a longer cooldown.

## Issues unique to a single run

- **GPT §2 — HIGH critique priority:** Unresolved HIGH critique not prioritized by the orchestrator. Prompt-level fix sufficient.
- **Gemini §2 — Late-stage budget waste:** 5 extra iterations after all results established. Stall blocking fired but didn't trigger termination. Fix: inject stalled-claims context so the orchestrator knows to terminate.
- **Kimi §3 — Poisoned verification target:** An incorrect analytical claim in CRIT-001 propagated as the expected value for two subsequent computations. Epistemic/trust issue, not a scaffolding loop bug.

## Prioritized Recommendations

### P0 — High impact, affects 3+ runs

**R1. Force text output before any loop exit (Cluster 2)**

In `run_agent_loop`: if the loop is about to exit for any reason (`end_turn`, `max_rounds`, `zero_text_bailout`) and accumulated text is empty or below a minimum threshold, force one additional text-only call. This is a unified guard that subsumes the current ad-hoc checks.

As a complementary measure for models like DeepSeek that need more rounds: interleave mandatory text checkpoints (via `tool_choice="none"`) every N consecutive tool-only rounds, rather than killing the loop.

**R2. Inject max_tokens failure signal into orchestrator context (Cluster 1)**

On the first max_tokens retry, skip the retry and instead re-dispatch to the orchestrator with a `context_prefix`: "TASK-NNN exceeded output capacity — decompose into smaller subtasks." This leverages the existing `context_prefix` mechanism and requires no new infrastructure.

**R3. Dispatch-level stall tracking (Cluster 3a)**

Replace COMP-entry-based stall detection with a dispatch-level ledger keyed on `task.target_claim`. The engine already knows the target claim at dispatch time and the verdict at completion — tracking these directly eliminates the invisible-entry, key-fragmentation, and override-mismatch problems. See the DeepSeek §3 section of the per-model report for a detailed implementation sketch.

### P1 — Medium impact, affects 1-2 runs

**R4. Add recompute counter + fix P4/P5 priority (Cluster 3b)**

Two changes to `_apply_overrides`:
1. Add a counter to `_check_for_refuted_verdict()`: if the last 2+ COMP entries on the same claim are REFUTED, don't set `_pending_recompute_claim`.
2. Swap P4 (REFUTED recompute) and P5 (stall blocking) priority so stall detection can block a recompute.

**R5. Route failure context through `context_prefix` (Cluster 4)**

Generalize the existing violation-injection pattern: after any agent failure (max_tokens, empty output, INCONCLUSIVE/REFUTED verdict, override displacement), append a structured failure summary to the orchestrator's `context_prefix` on the next iteration. This gives the orchestrator the information it needs to adapt without requiring new override logic.

**R6. Stalled-claims context for termination (Gemini §2)**

Add a `>>> STALLED COMPUTATIONS <<<` block to `_build_context_prefix` listing claims from `_stalled_claims`, telling the orchestrator: "Do NOT assign compute tasks to these claims. If analytical derivation is complete and critiques are resolved, consider termination without numerical verification."

### P2 — Lower impact or narrower scope

**R7. Dispatch-level circuit breaker for transient errors (Cluster 5)**

If the previous iteration's dispatch failed with a transient error (timeout, 5xx) and the workspace state hasn't changed, skip the orchestrator call and retry the queued task directly with a longer inter-iteration cooldown (60-120s instead of 2-8s).

**R8. HIGH critique priority prompt fix (GPT §2)**

Add to the orchestrator prompt: "When an unresolved HIGH critique exists, your next task MUST be either a resolve or compute task targeting that critique." Prompt-level fix is sufficient; engine-level override is optional.

**R9. Computationalist should derive expected values, not accept them from critiques (Kimi §3)**

When a computation disagrees with the expected value by >10%, flag this as a potential wrong-target situation rather than automatically concluding INCONCLUSIVE/REFUTED. Route to a researcher to verify the expected value analytically before re-running.
