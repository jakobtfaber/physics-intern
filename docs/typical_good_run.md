# Typical Good Run: Quantum Error Correction (Main)

Reference document describing what a successful run looks like on `quantum_error_correction_main.yaml`, derived from successful runs with Gemini 3 Flash Preview (March 2026). Use this as a baseline to diagnose where weaker models diverge.

## Problem Summary

Derive the exact rational function F(p) for the logical state fidelity of a [[4,2,2]] code state preparation circuit with 5 depolarizing CNOT gates. The answer is a ratio of two degree-5 polynomials in p, with F(0)=1 and leading infidelity 16p²/25, confirming fault-tolerance. Requires exhaustive enumeration of all 16⁵ = 1,048,576 two-qubit Pauli error configurations.

## Expected Outcome

- **Iterations:** 5 (consistently; occasionally 4 if the orchestrator proactively dispatches critic)
- **Total tokens:** 149K–175K (including reasoning tokens)
- **Science verdict:** VALID, HIGH confidence
- **Established results:** 1 (the F(p) expression); occasionally 2 if the model also promotes noiseless circuit correctness as a separate ER

## Run Anatomy

A good run follows this general structure. The arc is highly consistent across runs.

### Iteration 0: Surveyor

**Agent:** surveyor (one-shot)
**Duration:** 14–18s | **Tokens:** 2K–3K output
**Purpose:** Produce background reference notes before the main loop. The surveyor maps the landscape — the orchestrator decides the route.

Good background notes:
- Identify the Pauli error propagation approach (enumerate all 16⁵ error combinations through the 5-gate Clifford circuit)
- Note post-selection criteria: ancilla M₄ = |0⟩ AND code stabilizers XXXX, ZZZZ preserved
- Warn about gate ordering (matrix multiplication order vs circuit execution order — right-to-left)
- Warn about the denominator (F(p) = N(p)/D(p), not just N(p))
- Flag that Z-errors on |00⟩_L are benign (ZZ...Z stabilizer preserves the logical state)
- Describe known methods (stabilizer formalism, syndrome mapping, weight analysis) without recommending which to pursue
- Do NOT produce code, numerical predictions, or recommend an approach order — this causes harmful anchoring

**Red flag if:** The notes contain Python code, candidate numerical expressions, or attempt to solve the problem. This anchors the orchestrator on wrong values.

### Iteration 1: Strategy Formulation + Computation

**Agent sequence:** orchestrator → computer
**Orchestrator actions:**
- Reads background survey and formulates an initial strategy via `update_section(section="Strategy")` — typically a 3–4 step plan (enumerate errors → filter → compute F(p) → verify)
- Creates 1–2 research questions (main F(p) expression; occasionally also noiseless circuit verification)
- Dispatches `compute` targeting the main RQ

Note: the two-phase dispatch gate may reject `set_next_task` in the same response as `add_research_question` — this is normal (forces dispatch into a separate response so `target_claim` uses actual entity IDs).

**Computer behavior:**
- 5–8 rounds of `document_approach` + `execute_python` + `submit_result`
- Round 1 is always `document_approach` (removed from tool set after first call)
- Builds a Pauli propagation framework (either symbolic via SymPy or direct integer/bitmask representation)
- Enumerates all 16⁵ error configurations, applies post-selection filters, classifies accepted states as correct or logical-error
- Produces the exact rational function F(p) = N(p)/D(p)
- Confidence: "exact"

**Scaffold events:**
- `progress_check` may fire after 3–4 consecutive `execute_python` calls — this is normal and healthy
- `empty_end_turn_recovery` fires if the model produces an empty response (Gemini-specific, ~30–60% of runs, 1–2 occurrences) — the scaffold re-prompts and the agent usually recovers

**What can go wrong:**
- The computer only does weight-1 and weight-2 analysis (first-order truncation) instead of the full 16⁵ enumeration — produces an incomplete result that will need a second computation pass
- The computer produces code that errors or times out on the full enumeration — may need to simplify the implementation approach
- The computer produces an expression with a spurious common factor (e.g., 3×N(p)/D'(p) instead of N(p)/D(p)) — the review cycle will catch this

**Token budget:** 30K–40K output for computer. If it exceeds ~50K, the agent is likely stuck in a loop.

### Iteration 2: Integration + Review

**Agent sequence:** orchestrator → reviewer
**Orchestrator actions:**
- Integrates the compute result (visible via EVIDENCE RESULTS banner in context)
- Creates WH from the compute result via `add_hypothesis(from_rq=...)` — evidence is auto-copied from RQ to WH
- Resolves the corresponding RQ
- Updates strategy to focus on review
- Dispatches `review` targeting the WH

**Reviewer behavior (one-shot, no code execution):**
- Receives focused context: WH statement + evidence (including per-script `<computation>` blocks with purpose/code/output) + light state
- Examines the code logic, post-selection criteria, error propagation methodology
- Checks physical sanity: F(0)=1, leading term O(p²), rational function structure
- Outputs structured JSON: `{verdict, summary, details}`
- Verdict: VERIFIED

The reviewer does NOT run code or write an independent implementation — it reviews the existing evidence and code analytically. This is a key difference from earlier versions of the system that used an agentic compute-based verifier.

**Scaffold events:**
- None typically (reviewer is one-shot, no tool calls)

**What can go wrong:**
- **False refutation:** The reviewer misreads the code or misunderstands the post-selection logic and issues REFUTED despite the claim being correct. This sends the run into a detour re-deriving the same result.
- **INCONCLUSIVE verdict:** The reviewer cannot determine correctness from the evidence alone. This requires the orchestrator to dispatch another review or gather more evidence.

**Token budget:** 4K–10K output for reviewer. Reviewer calls are cheap.

### Iteration 3: Promotion + Termination Attempt

**Agent:** orchestrator
**Orchestrator actions:**
- Integrates review result (visible via VERIFIED HYPOTHESES banner in context)
- Promotes WH → ER with a substantive `promotion_justification` citing the VERIFIED review
- Updates strategy to reflect promotion
- Attempts `terminate`

**What typically happens:**

The orchestrator attempts `terminate`, but the scaffold blocks with "No critic pass has occurred yet." The `termination_blocked` event is logged, and the scaffold sets the `forced_critic` flag for the next iteration.

In rare cases (~10% of runs), the orchestrator proactively dispatches `critique` before attempting termination, saving one iteration. But the dominant path is termination blocked → forced critic.

**Variant — two WHs:** If the orchestrator created a second WH (e.g., noiseless circuit correctness), it may dispatch a second `review` at this iteration for that WH, then promote it later. This adds 1 WH and 1 review call but doesn't change the overall arc.

### Iteration 4: Forced Critic Pass

**Agent:** deep_critic (one-shot structured JSON)
**Duration:** 50–63s | **Tokens:** 10K–13K output (mostly reasoning)

A good critic on this problem:
- Reviews the ER's derivation chain (computation → review → promotion)
- Checks fault-tolerance (first-order error suppression)
- May note the weight-1 error count (7/75 accepted, all correct) as a sanity check
- Reviews the Strategy section for consistency with evidence (strategy critique capability)
- Typically files **zero critiques** — the result is correct and well-supported

**Strategy critiques:** The critic can file critiques with `target_id: "STRATEGY"` when the strategy conflicts with accumulated evidence. This is most valuable when:
- The strategy still references a refuted hypothesis
- The strategy recommends an approach contradicted by results
- There is a disconnect between the stated plan and actual work

In ~30% of runs, the critic files a MEDIUM critique on STRATEGY (e.g., about asymptotic behavior misinterpretation in the strategy text). These are resolved by the orchestrator in the next iteration and do not block termination.

**Scaffold events:**
- `forced_critic` — logged at the start of this iteration
- `no_critiques_filed` — if clean review, recorded as `critic_clean` signal to orchestrator

**What can go wrong:**
- **False-alarm HIGH critique:** The critic files a HIGH-severity critique about a non-issue (e.g., questioning the Z-error classification when Z-errors genuinely don't affect |00⟩_L). This forces the orchestrator to resolve the critique, adding 1–3 iterations.
- **Redundant second critic:** The orchestrator dispatches another critic after a clean review. This wastes ~15K tokens. The `critic_clean` warning in context should prevent this.

### Iteration 5: Termination + Formatting

**Agent:** orchestrator → formatter
**Orchestrator actions:**
- If MEDIUM critique was filed: resolves it with substantive text (e.g., Taylor expansion verification)
- If second WH needs promotion: promotes it now
- Emits `set_next_task(task_type: terminate)`

**Scaffold:** `can_terminate()` checks all gates:
- At least one critic pass has occurred
- No unresolved HIGH critiques
- All RQs resolved or abandoned
- All WHs either verified+promoted or abandoned

**Formatter:** One-shot agent, produces ANSWER.md with the polynomial expression. Very cheap (~300–900 output tokens).

**What can go wrong:**
- **Legitimate termination block:** If the orchestrator created a second WH (e.g., noiseless circuit correctness) and didn't verify it, the scaffold blocks with unverified WH. This adds 1–2 iterations for a review pass. This is the scaffold correctly enforcing verification completeness.

## Strategy Evolution

A healthy run shows the strategy evolving through 3–4 updates:

1. **Initial formulation** (iter 1): Orchestrator reads background survey, writes a plan (typically: enumerate → filter → compute F(p) → verify)
2. **Post-computation update** (iter 2): Strategy shifts to review focus after compute result arrives
3. **Post-review update** (iter 3): Strategy shifts to promotion + termination
4. **Post-critique update** (if critiques filed): Strategy revised to address critique findings

**Known weakness — strategy lag after refutations:** The orchestrator sometimes fails to update the strategy immediately after integrating a REFUTED verdict, leaving the strategy referencing a hypothesis that is already abandoned. The critic's strategy critique mechanism catches this (typically one iteration later), but it introduces a latency cost. When diagnosing a detour, check whether the strategy was stale at the time.

**Note:** `update_section` calls are not currently logged in EVENT_LOG.jsonl — strategy evolution must be traced through the orchestrator conversation logs or RESEARCH_GRAPH.json snapshots.

## Entity Lifecycle Reference

The typical entity flow for this problem:

```
Iter 1:  RQ-001 created ──────────────────────────────────────────►
         Computer dispatched on RQ-001 → Evidence(type=compute) stored on RQ-001

Iter 2:  WH-001 created from RQ-001 (evidence auto-copied) ──────►
         RQ-001 resolved → WH-001
         Reviewer dispatched on WH-001 → ReviewResult(verdict=VERIFIED) stored on WH-001

Iter 3:  WH-001 promoted → ER-001 (with promotion_justification)
         Termination blocked (no critic pass yet)

Iter 4:  Forced critic pass → 0 critiques (clean review)

Iter 5:  Termination allowed → Formatter → ANSWER.md
```

**Variant with 2 entities** (run 2 pattern, ~30% of runs):
```
Iter 1:  RQ-001, RQ-002 created
         Computer dispatched on RQ-002 → Evidence on RQ-002

Iter 2:  WH-001 from RQ-001 (no evidence), WH-002 from RQ-002 (evidence copied)
         Reviewer verifies WH-002 → VERIFIED

Iter 3:  WH-002 promoted → ER-002
         Reviewer verifies WH-001 → VERIFIED

Iter 4:  Forced critic → CRIT-001 MEDIUM on STRATEGY (asymptotic misinterpretation)

Iter 5:  WH-001 promoted → ER-001, CRIT-001 resolved
         Termination → Formatter → ANSWER.md
```

## Scaffold Events Reference

Events that should appear in a good run:

| Event | When | Meaning |
|-------|------|---------|
| `add_research_question` | Iter 1 | Orchestrator decomposes the problem |
| `add_hypothesis` | Iter 2 | Orchestrator formulates WH from compute evidence |
| `promote_hypothesis` | Iter 3 | WH→ER after VERIFIED review |
| `progress_check` | During computer | Fires after 3–4 consecutive `execute_python` calls — normal |
| `no_critiques_filed` | Critic pass | Clean review — signals the orchestrator can terminate |
| `termination_blocked` | Iter 3 | "No critic pass yet" — triggers forced critic |
| `forced_critic` | Iter 4 | Scaffold forces a critic pass |

Events that are acceptable but indicate minor friction:

| Event | When | Meaning |
|-------|------|---------|
| `empty_end_turn_recovery` | During computer | Model produced empty response, scaffold re-prompted — Gemini-specific, 1–2 per run |
| `termination_blocked` (WH unverified) | Iter 5+ | Legitimate block — the model over-scoped and created an extra WH that needs review |
| `file_critique` (MEDIUM on STRATEGY) | Critic pass | Strategy text inconsistency caught by critic — resolved next iteration |

Events that signal problems:

| Event | Meaning |
|-------|---------|
| `agent_failure_max_rounds` | Agent exhausted all rounds without calling exit tool — wasted iteration |
| `agent_failure_max_tokens` | Agent hit token limit — likely context bloat or stuck loop |
| `er_demotion_safety` | ER was demoted back to WH — indicates premature promotion |
| `explore_result_suppressed` | Evidence result was dropped (zero_output or missing target) |
| `phantom_labels` | References to non-existent hypotheses — state corruption |

## Token Budget Reference

Expected token distribution for a 5-iteration run (~160K total):

| Agent | % of Total | Absolute Range |
|-------|-----------|----------------|
| computer | 35–40% | 50K–66K |
| orchestrator | 30–38% | 45K–67K |
| deep_critic | 8–10% | 14K–16K |
| reviewer | 8–10% | 10K–16K |
| surveyor | 2–3% | 3K–4K |
| formatter | 1–3% | 3K–7K |

Reasoning tokens are 75–85% of all output for Gemini Flash. Actual answer content is only 15–25%.

## Diagnosing Failures by Comparison

When analyzing a failed or inefficient run, compare against this reference:

**If the run stalls at computation (iteration 1):**
- Did the computer attempt the full 16⁵ enumeration, or only first-order analysis?
- Did the code error/timeout? Check for `tool_timeout` events.
- Did the computer call `submit_result`, or did it end without the exit tool?
- How many `empty_end_turn_recovery` events fired? More than 2 suggests the model is struggling.

**If review goes wrong (iteration 2):**
- Did the reviewer receive focused context (WH + evidence + per-script computation blocks)?
- If REFUTED: does the reviewer's reasoning identify a genuine error, or did it misread the code?
- If INCONCLUSIVE: was the evidence sufficient for the reviewer to make a determination?

**If the run enters a multi-iteration detour (iterations 4+):**
- Was a correct result falsely refuted? Compare the abandoned hypothesis expression to later-derived expressions — if they match, the refutation was wrong.
- Is the orchestrator re-dispatching the same type of task repeatedly? Check for semantic duplication.
- Are critiques being filed and then requiring resolution? Check if the critiques are legitimate.
- Is the strategy stale? Check whether it still references a refuted or abandoned hypothesis — if so, the critic should have filed a strategy critique.
- Is the orchestrator slow to abandon a redundant subsidiary hypothesis? Look for repeated INCONCLUSIVE verdicts on the same WH.

**If termination is delayed:**
- Check `termination_blocked` events — are the blockers legitimate or stale?
- Is the orchestrator dispatching redundant critics after a clean review?
- Did the orchestrator create unnecessary hypotheses that now need review?
- Is a HIGH strategy critique unresolved? Check if the orchestrator updated the strategy and resolved it.

**If the science is wrong:**
- Check which computation produced the wrong result and what kind of code it used.
- Check whether the error propagation correctly handles: gate ordering, ancilla post-selection, Z-error classification, denominator normalization.
- Check whether the surveyor's background notes anchored the orchestrator on a wrong candidate answer (should not happen if the surveyor followed its prompt, but check for code or numerical predictions in the notes).

**If strategy is mismanaged:**
- Did the orchestrator formulate an initial strategy in iteration 1? If not, the placeholder "(No strategy set...)" should have prompted it.
- After a refutation, did the orchestrator update the strategy? If not, check whether the critic caught this via a strategy critique.
- Is the strategy generic/shallow? On this problem, a good strategy should reference the specific computational approach (16⁵ enumeration), not just "explore and verify."
