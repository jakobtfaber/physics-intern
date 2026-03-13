
## Opus

### 1. Researcher max-token triple-retry (EVENT-003, iteration 7) — ~99K wasted output tokens

What happened: TASK-007 asked the researcher to derive the full binding energy E_TF and Z^{7/3} scaling with three detailed deliverables (scaling proof, exact coefficient derivation with all prefactors, and
prefactor clarification). The researcher hit the 33,024 output token ceiling three times:
- Attempt 1: 3040 chars response (truncated)
- Attempt 2: 0 chars response (completely empty — likely truncated before any content was captured)
- Attempt 3: 10,021 chars response (truncated but usable)

Root cause: The orchestrator's TASK-007 was too large — it asked for a fully self-contained, step-by-step derivation covering three distinct sub-proofs with "all factors of b, Z, a₀ explicit in each step." This
is inherently a long output. After the first failure, the engine retried identically rather than decomposing.

Recommendation: The engine should detect max_tokens on the first retry and signal the orchestrator to decompose the task. A simple heuristic: if a researcher hits max_tokens, re-dispatch to the orchestrator
with a context_prefix like "TASK-007 exceeded output capacity — decompose into smaller subtasks." This is already noted in PLAN.md-style recommendations but not implemented.


## GPT

### 1. Researcher max_tokens death spiral (iterations 15-34)
The tasks were NOT as well-scoped as I initially claimed. Looking at the actual content:

1. The orchestrator kept bundling multiple items: TASK-030 (iter 30) asks for promotions WH→ER-002 and ER-003 plus stale bookkeeping cleanup plus a full energy derivation. TASK-015 (iter 15) did the same:      
   promotions + resolve CRIT-004 + derive the energy functional. These should have been separate iterations.
2. It's the same failing task repeated 11 times: Iterations 15, 16, 18, 19, 24, 26, 27, 30, 31, 32, 34 all ask for essentially the same thing — "promote WH-002/WH-003 and derive the Z^{7/3} energy scaling." The
   researcher gets truncated, the proposed changes never land, and the orchestrator re-emits the same task next time because the work is still undone.
3. The mathematical output is genuinely dense: The researcher prompt says "Be explicit about every step. Do not skip obvious algebra." For a Thomas-Fermi energy derivation with integration by parts, virial
   arguments, and boundary terms — that's easily 16K+ tokens of output.

So to your question: simply increasing max_tokens would probably fix this specific case (the derivation is real work, not bloat), but it papers over two deeper problems:

- The orchestrator doesn't decompose on failure. After the researcher fails twice on the same task, the orchestrator should split "do promotions" and "derive energy" into separate iterations. Right now it just
  re-emits the same bundle.
- The engine has no loop detection for this pattern. It tracks max_tokens_reached_count in metrics but never feeds this back to the orchestrator. The stale-loop backstop doesn't trigger because the task
  description changes slightly each time (different iteration number, slightly different wording).

The most impactful fix is probably both: raise max_tokens to ~32K (the derivation genuinely needs the space), and add a mechanism where repeated max_tokens failures get fed back to the orchestrator as a
context_prefix telling it to break the task down.

### 2: No priority escalation for HIGH critiques

What happened: CRIT-010 (a concrete sign error: [eφ-μ]₊ vs [eφ+μ]₊) was filed at iteration 33 with HIGH severity. But the orchestrator didn't immediately redirect work to address it — it continued with the
pre-planned researcher tasks. The error was eventually fixed (iterations 33-36), but the delay is symptomatic of a missing priority override.

Root cause: The orchestrator prompt instructs it to prioritize critiques, but there's no engine-level mechanism to force a HIGH-critique-first task. The orchestrator is just an LLM call — it sees the critique
in context but may not override its current plan.

Recommendation: HIGH CRITIQUE PRIORITY: When an unresolved HIGH critique exists, your next task MUST be either a resolve task (assigned to researcher) or a compute task to test the disputed claim. Do not plan derivation or
research tasks while a HIGH critique is open.

(optional; Add an engine-level override in _apply_overrides(): when an unresolved HIGH critique exists and the orchestrator's emitted task is not a resolve or compute targeting that critique, inject a
blocker that forces the orchestrator to address it first. This is similar to the existing stale-loop and forced-critic overrides.)
So an engine-level override would look like inserting a ~P3.5 step:

```
# P3.5: HIGH critique priority — redirect non-resolve work
critique_log = self.workspace.read_file("CRITIQUE_LOG.md")
if critique_log:
crit_counts = count_unresolved_critiques(critique_log)
if crit_counts.get("HIGH", 0) > 0 and task.task_type not in (
TaskType.RESOLVE, TaskType.COMPUTE, TaskType.CRITIQUE,
TaskType.SYNTHESIZE, TaskType.TERMINATE):
# Inject a context_prefix warning instead of hard override
self._pending_violations.append(Violation(
check="high_critique_priority",
severity=ViolationSeverity.WARNING,
message="Unresolved HIGH critique(s) exist — prioritize resolve/compute",
...
))
```
Is it worth it? I'd say no, start with the prompt fix. 


## GEMINI

### 1: Gemini empty final response (COMP-006, COMP-008, COMP-013)

What happened: Three consecutive attempts to verify WH-002 (energy derivation) all returned INCONCLUSIVE with "Agent produced no text output", wasting ~130K input tokens.

Root cause: The computationalist runs on gemini-3.1-pro-preview. In all three cases, Gemini successfully executed tool calls with inline text across multiple rounds (4-7 tool calls), but on the final turn —
when it should have written its COMP entry — it returned end_turn with 0 output tokens. The conversation logs confirm this pattern:

- iter006_computationalist_6.md: Output tokens: 0, Stop reason: end_turn
- iter008_computationalist_9.md: Output tokens: 0, Stop reason: end_turn

The existing zero_text_bailout mechanism (threshold=3 consecutive zero-text rounds) did not trigger because the streak was only 1 — Gemini produced text during tool-call rounds, resetting the counter each time.
The bailout was designed for models that go silent mid-loop, not for the case where the model's final end_turn response is empty.

Notably, the successful COMP-002 run (iteration 2) used the exact same model and worked fine (837 output tokens on final turn), so this is a stochastic Gemini reliability issue that the scaffolding doesn't
guard against.

Recommendation: Add a specific guard in run_agent_loop: if the model returns end_turn with 0 text tokens after having made tool calls, treat it as a bailout and force a text-only final call (same as the
existing max_rounds forced call). This is a one-line check on the loop exit condition — no need to change zero_text_bailout semantics.

### 2: Late-stage budget waste (iterations 26-30)

What happened: By iteration 25, all results were established (ER-001, ER-003, WH-002, WH-004), all 3 critiques resolved, and 5 computations VERIFIED. Yet the system ran 5 more iterations consuming ~150K+ input
tokens with near-zero productive output:

- Iteration 26: researcher (2046 tokens out), stall_block displaced compute task
- Iteration 27: deep_critic (18 tokens — no critique found), forced_critic displaced compute task
- Iteration 28: researcher (3609 tokens out), stall_block displaced compute task
- Iteration 29: researcher (6255 tokens out), 3 stall_block/max_tokens events on orchestrator
- Iteration 30: orchestrator finally terminates

The stall_block mechanism fired correctly (displacing tasks at iterations 26, 28, 29) but this only prevented wasted compute — it didn't trigger termination. The orchestrator kept trying to find productive work
when there was none left.

The fix is ~10 lines: add a >>> STALLED COMPUTATIONS <<< block to _build_context_prefix listing claims from self._stalled_claims, telling the orchestrator explicitly: "Do NOT assign compute tasks to these      
claims. If the analytical derivation is complete and critiques are resolved, consider proceeding to termination without numerical verification."


## DEEPSEEK

### 1. max_rounds too low for iterative numerical debugging

Evidence: Every single computationalist invocation (24 total) hit max_rounds_forced at round 4 with 3 tool calls. The model (DeepSeek-V3.2) needs to: write code → see error → fix → see result → possibly adjust
again → write COMP entry. That's 5-6 rounds minimum for a non-trivial numerical problem. With only 3 tool calls available, the computationalist could never complete the iterative debug cycle needed for a stiff
singular BVP.

This directly caused the 7 "Agent produced no text output" failures — the LLM was still issuing tool calls at the max_rounds cutoff, so it never got to write its final COMP entry.

The cleanest approach would be to:

1. Add tool_choice as an optional parameter to LLMProvider.call()
2. In run_agent_loop, use tool_choice="auto" for normal rounds
3. For the forced final call, use tool_choice="none" (instead of omitting tools entirely — more explicit and provider-standard)
4. Optionally, after N consecutive tool-only rounds (like the current zero_text_bailout), insert a round with tool_choice="none" to force the model to emit analysis text before continuing with tools — rather
   than killing the loop entirely

Option 4 is the key insight: instead of bailout = termination, bailout = "pause and explain yourself, then you can keep going." This would let DeepSeek produce its 3 tool calls, be forced to write analysis,
then get 3 more tool calls, and so on — effectively interleaving required text checkpoints without cutting the session short.


### 2. No escalation strategy for repeated computational failure on the same claim

The system spent ~60-70% of its token budget (~800K+ tokens) on failed numerical computations, leaving zero budget for 4 of the 5 problem sub-objectives.

Recommendation: Implement a hard cap (e.g., 3-4 consecutive INCONCLUSIVE verdicts on the same claim) that forces the orchestrator to either decompose the task or move to a different objective. The orchestrator
should maintain a checklist of all sub-objectives and distribute budget proportionally.

### 3. Stall detection

Recommendation: Dispatch-Level Stall Tracking

Problem

Current stall detection parses COMPUTATION_LOG entries to count consecutive INCONCLUSIVE verdicts per claim key. This fails because:

1. Invisible entries: When the computationalist is truncated at max_rounds with partial text (no structured **CLAIM:** line), the claim key is empty and the entry is skipped entirely by
   detect_computation_stalls.
2. Key fragmentation: Entries that do have a claim line use inconsistent keys — "unknown", "WH-002", or long free-text prefixes — so failures on the same underlying goal scatter across multiple keys and never
   reach the threshold.
3. Override mismatch: Even when a stall is detected, the blocking check in _apply_overrides extracts the key from task.body (the orchestrator's new task description), which may not match the key stored in
   _stalled_claims if the orchestrator rephrased the task.

All three problems share the same root cause: stall tracking depends on LLM-generated text (the computationalist's COMP entry and the orchestrator's task wording) instead of data the engine already controls.

Fix: Track at dispatch, not at parse

The engine knows two things reliably at dispatch time:
- What claim the task targets — extractable from task.body before dispatch (line 266 already does this)
- Whether the computation succeeded — the verdict in the COMP entry, or the absence of one

Instead of re-parsing the full COMPUTATION_LOG after each dispatch, track stalls using a dispatch-level ledger.

Implementation

1. Add target_claim to Task (task.py)

@dataclass
class Task:
task_id: str
task_type: TaskType
assigned_to: str
priority: str = "medium"
iteration: int = 0
blocking_critiques: list[str] = field(default_factory=list)
target_file: str = ""
target_claim: str = ""   # ← new field
body: str = ""

Populate it in from_frontmatter from a target_claim YAML field if present (orchestrator can emit it), and as a fallback extract WH/ER IDs from the body:

target_claim = meta.get("target_claim", "")
if not target_claim:
ids = _ER_WH_ID_RE.findall(body)
target_claim = " ".join(sorted(set(ids))) if ids else ""

Instruct the orchestrator (in its prompt) to include target_claim: WH-002 in compute task frontmatter. The fallback extraction handles cases where it doesn't.

2. Replace _stalled_claims: set[str] with a dispatch ledger (engine.py)

# Replace line 46:
#   self._stalled_claims: set[str] = set()
# With:
self._compute_outcomes: dict[str, list[str]] = {}  # claim_key -> [verdict, verdict, ...]
self._blocked_claims: set[str] = set()

3. Record outcome after each compute dispatch (engine.py, replacing _update_stall_tracking)

def _update_stall_tracking(self, task: Task):
"""Record compute outcome and update blocked claims."""
claim_key = task.target_claim or _normalize_claim_key(task.body)
if not claim_key:
return

      # Get the verdict from the last COMP entry
      comp_log = self.workspace.read_file("COMPUTATION_LOG.md")
      entries = _parse_comp_entries(comp_log)
      verdict = entries[-1]["verdict"] if entries else ""

      if verdict == "VERIFIED":
          # Success: reset streak, unblock
          self._compute_outcomes[claim_key] = []
          self._blocked_claims.discard(claim_key)
      else:
          self._compute_outcomes.setdefault(claim_key, []).append(verdict or "UNKNOWN")
          if len(self._compute_outcomes[claim_key]) >= self.config.stall_threshold:
              self._blocked_claims.add(claim_key)

Pass the task to the call site (line 121):
self._update_stall_tracking(task)  # was: self._update_stall_tracking()

4. Update the override check (engine.py, P5 block starting at line 264)

# P5: Block dispatch to stalled claim
if task.task_type == TaskType.COMPUTE:
claim_key = task.target_claim or _normalize_claim_key(task.body)
if claim_key and claim_key in self._blocked_claims:
# ... existing displacement logic ...

This now compares the same key source (task-level, not COMP-entry-level) on both sides.

Why this fixes all three sub-problems

┌───────────────────────────────────────────┬──────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────────────────────────────────────────────┐
│                Sub-problem                │                       Current                        │                                             After fix                                             │
├───────────────────────────────────────────┼──────────────────────────────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Invisible entries (no CLAIM line)         │ Entry skipped, failure not counted                   │ Verdict read from last entry regardless of CLAIM line; key comes from the task, not the entry     │
├───────────────────────────────────────────┼──────────────────────────────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Key fragmentation ("unknown" vs "WH-002") │ Failures scatter across keys                         │ Key is always derived from the dispatched task's target_claim field, consistent across iterations │
├───────────────────────────────────────────┼──────────────────────────────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Override mismatch                         │ Stalled key from COMP entry ≠ key from new task body │ Both sides use task.target_claim, which is stable                                                 │
└───────────────────────────────────────────┴──────────────────────────────────────────────────────┴───────────────────────────────────────────────────────────────────────────────────────────────────┘

Scope and risk

- Touches: task.py (one field + extraction), engine.py (stall tracking + override check), orchestrator prompt (add target_claim instruction)
- Backward-compatible: target_claim defaults to "", fallback extraction preserves current behavior for tasks without it
- Tests to update: test_engine.py stall-related tests, test_task.py for new field round-trip
- Does not change: COMPUTATION_LOG format, detect_computation_stalls (can keep as a secondary diagnostic), agent prompts other than orchestrator


## KIMI K2.5

### Key Failure Pattern 1: Computationalist Empty-Output Stall (iters 5-6)

What happened: Two consecutive computationalist iterations produced zero text output, wasting ~77k tokens. The model (Kimi-K2.5) entered a pure tool-call loop —
executing Python code on every round without writing any inline text. When max_rounds was reached and the engine forced a text-only final call, the model produced
nothing usable.

Root cause: The model's tendency to emit only tool calls (no inline text) from round 3 onward. The system prompt's INLINE TEXT RULE is supposed to guard against this,
but the zero-text bailout only fires at max_rounds — by then all rounds are consumed. The orchestrator saw the tool_loop_truncated alert and pivoted to a different
task (good), but dispatched to the same agent without injecting any failure context (bad), so the identical behavior repeated.

Recommendation: Make the zero-text early termination more aggressive — if 2 consecutive rounds have empty text blocks, abort the tool loop immediately rather than
waiting for max_rounds. Additionally, when a tool_loop_truncated alert fires, the orchestrator should inject "Prior Computation Failure Context" into the next
computationalist prompt so the model knows to prioritize writing text alongside tool calls.

### Key Failure Pattern 2: Researcher max_tokens Truncation (iters 3, 10)

What happened: The researcher hit the 16,384 output token ceiling 3 times each in iterations 3 and 10 (6 total), producing ~98k tokens of truncated derivations that
were never integrated. Each time the engine retried the identical task verbatim, and each time the model started the derivation from scratch and hit the same wall.

Root cause: Tasks were too broad for the model's output budget. Iteration 3 bundled 4 sub-deliverables (energy decomposition, prefactor, dimensional analysis, virial
check) into one task. Iteration 10 was narrower but still required a full first-principles derivation. The retry mechanism reissues the identical task without (a)
breaking it into sub-tasks, (b) injecting partial output from the previous attempt, or (c) telling the model to write more concisely.

Recommendation: Two changes would help:
1. Apply the existing SINGLE-TARGET rule to derive tasks, not just compute — break multi-step derivations into sequential sub-tasks.
2. On max_tokens retry, inject the truncated output as context with an instruction like "Continue from where this was cut off" rather than starting from scratch.


###  Key Failure Pattern 3: Density Coefficient — Poisoned Numerical Track (iters 5-9)

What happened: The density formula went through a tortuous 5-iteration saga:
- CRIT-001 correctly identified the original coefficient (1/2π³) was wrong, but suggested an incorrect integral value I = 3π/(4√2) ≈ 1.666 (the true value is I = 1).
- COMP-007 measured I ≈ 0.944 numerically (consistent with I = 1), but compared against the false I = 1.666 target from CRIT-001 → INCONCLUSIVE.
- COMP-009 got I ≈ 1.185 due to insufficient asymptotic tail handling → REFUTED both candidate formulas (including the correct one).
- The orchestrator eventually routed around the failed numerical track via an analytical derivation, which produced the correct coefficient 32/(9π³).
- COMP-015 later "VERIFIED" charge neutrality, but this was likely self-referential — confirming a formula already accepted into RESEARCH_STATE rather than providing
  independent evidence.

Root cause: A single incorrect analytical claim in CRIT-001 (I = 1.666) poisoned the numerical verification target for two subsequent computation attempts. The system
has no mechanism for the computationalist to question the theoretical targets it's given — it blindly compared against the wrong value.

Recommendation: When a computation produces a numerical result that disagrees with the expected value by >10%, the system should flag this as a potential "wrong
target" rather than automatically concluding INCONCLUSIVE/REFUTED. The orchestrator could route to a researcher to verify the expected value analytically before
re-running the computation. More broadly: computationalist tasks should derive expected values from first principles (or cite sources) rather than accepting them
uncritically from critique text.


## GLM 5

### Pattern 1: ReadTimeout Feedback Loop (iters 5-8)

The waste comes from two things:

1. No short-circuit on repeated transient failures — After iteration 5's dispatch fails, iteration 6 re-runs the orchestrator (same state, same plan) and tries the
   same dispatch again. There's no circuit-breaker logic that says "last dispatch failed for the same reason, skip the orchestrator call and retry the task directly."
2. The backoff is too short relative to the timeout — The exponential backoff (2-8s) is negligible compared to the 120s API timeout. If the provider is down for
   several minutes, the backoff between iterations should be longer (e.g., 60-120s between iterations, not just between retries within a single iteration).

A possible improvement would be a dispatch-level circuit breaker: if the previous iteration's dispatch failed with a transient error and the state hasn't changed,
skip the orchestrator call and directly retry the queued task — possibly with a longer inter-iteration cooldown. That would save the orchestrator tokens and avoid
replanning the same task 4 times.


###  Unbounded REFUTED Recompute Cycle (iters 16-18)

Every LLM in the chain did the right thing:
- COMP-016 correctly diagnosed the singularity on the first try
- The orchestrator correctly planned corrective actions (resolve in iter 17, derive in iter 18)
- The researcher, when it finally got the task (iter 20), successfully pivoted to the virial theorem

The override chain sabotaged the orchestrator's own correct decisions. Three specific design flaws:

1. No recompute counter. _check_for_refuted_verdict() fires unconditionally with no memory of how many times it's already recomputed the same claim.
2. Wrong priority ordering. P4 (recompute) runs before P5 (stall blocking), so stall detection can never prevent a recompute — even after correctly detecting the
   stall at threshold=2 after iter 17.
3. Misleading task prompt. _make_recompute_task says "The orchestrator has integrated corrections" — but the corrections were displaced by this very override. The
   computationalist is told to verify a corrected version that doesn't exist.

Cost

~192K input tokens (36% of the run's total budget) spent on COMP-017 and COMP-018, which produced no new information beyond what COMP-016 already established.

Recommended fix

The simplest fix: after the first REFUTED recompute is also REFUTED, stop the override and let the orchestrator's planned task through. Concretely, either:

- Add a counter in _check_for_refuted_verdict: if the last 2+ entries on the same claim are REFUTED, don't set _pending_recompute_claim
- Or swap P4 and P5 priority so stall detection can block the recompute
- Or both
