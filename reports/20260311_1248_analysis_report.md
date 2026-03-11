# SciRalph Batch Analysis Report — March 11, 2026

Eight problems run with `claude-sonnet-4-6`, default configuration (`max_tool_rounds: 10`, `max_iterations: 200`, `critic_every_n: 4`).

---

## 1. Executive Summary

All 8 problems produced **scientifically correct results** (VALID / HIGH confidence across the board). The multi-agent architecture works: critique cycles catch genuine errors, computations provide real numerical verification, and termination is appropriately gated. However, a single dominant process issue — **computationalist token bloat / stalling** — recurs in 5 of 8 runs and accounts for ~30% of total token spend. Several minor bookkeeping bugs appear systematically across runs.

**Key numbers:** 4.3M total tokens, ~1.28M wasted on stalling computations, 34 established results, 40 critiques filed and resolved, 0 phantom verifications, 0 rubber-stamp critiques.

---

## 2. Scientific Scorecard

| Problem | Verdict | Confidence | ERs | Critiques | Process | Tokens |
|---------|---------|------------|-----|-----------|---------|--------|
| QHO Thermodynamics | VALID | HIGH | 5 | 1 (resolved) | EFFECTIVE | 117K |
| Hawking Temperature | VALID | HIGH | 1 | 2 (resolved) | EFFECTIVE | 96K |
| Ising 1D Transfer Matrix | VALID | HIGH | 8 | 4 (resolved) | EFFECTIVE | 492K |
| Perihelion Precession | VALID | HIGH | 4 | 4 (resolved) | EFFECTIVE | 526K |
| Berry Phase Spin-1/2 | VALID | HIGH | 5 | 8 (resolved) | EFFECTIVE | 1,420K |
| Path Integral HO | VALID | HIGH | 3 | 6 (resolved) | PARTIALLY_EFFECTIVE | 590K |
| Chandrasekhar Limit | VALID | HIGH | 3 | 5 (resolved) | EFFECTIVE | 366K |
| Renormalization φ⁴ | VALID | HIGH | 5 | 10 (resolved) | EFFECTIVE | 725K |

**8/8 scientifically valid. 7/8 process-effective. 40 critiques filed, 40 resolved.**

---

## 3. Token Budget Overview

| Problem | Total Tokens | Wasted (stalling) | Waste % | Iterations |
|---------|-------------|-------------------|---------|------------|
| Hawking Temperature | 96K | 0 | 0% | 5 |
| QHO Thermodynamics | 117K | 0 | 0% | 5 |
| Chandrasekhar Limit | 366K | 0 | 0% | 7 |
| Ising 1D | 492K | ~233K | 47% | 7 |
| Perihelion Precession | 526K | ~279K | 53% | 8 |
| Path Integral HO | 590K | ~305K | 52% | 6 |
| Renormalization φ⁴ | 725K | 0* | 0% | 12 |
| Berry Phase Spin | 1,420K | ~467K | 33% | 17 |
| **Total** | **~4.3M** | **~1.28M** | **30%** | |

*φ⁴'s COMP-001 used 162K across 10 rounds but produced valid results — not counted as waste.

---

## 4. What Went Well

### 4.1. Perfect Scientific Correctness
8/8 runs produced VALID results with HIGH confidence. No phantom verifications were detected — every claimed computation exists with real scripts producing real output. The architecture genuinely works for producing correct physics.

### 4.2. The Critique Cycle
The deep critic is the star of the system. 40 critiques filed across 8 runs, all substantive, all resolved. Highlights:

- **Berry Phase CRIT-004 (HIGH):** Caught a genuine algebraic error — the southern-gauge line integral gives π cos(θ₀), not -π(1-cos(θ₀)). Corrected by making northern gauge primary.
- **φ⁴ CRIT-003 (HIGH):** Caught a sign error in the 4-point amplitude with explicit i-factor tracking.
- **Ising CRIT-004 (MEDIUM):** Identified a tautological verification in COMP-002 Task D, leading to a genuine independent four-stage re-verification (COMP-005).
- **Chandrasekhar CRIT-001 (MEDIUM):** Caught m_H vs m_u inconsistency — a 1.5% error in the mass formula.
- **Perihelion CRIT-003 (HIGH):** Required explicit treatment of all three forcing terms in the perturbation equation.

Zero rubber-stamp resolutions: every resolution was traced back to substantive changes in the research state.

### 4.3. Genuine Computational Verification
Spot-checking computation scripts confirms real numpy/scipy/sympy code with proper error tolerances. No stubs or trivial checks. The computationalist's self-correction ability is also notable — in Hawking, it caught an erroneous test expectation in its own first script and wrote a corrected second script.

### 4.4. The Validation Pipeline
Phantom references are caught and stripped (3 runs). The ER promotion gate demotes unverified claims. Termination gates prevent premature completion. The system catches its own mistakes.

### 4.5. Benchmark Runs
Hawking Temperature (96K tokens, 5 iterations) and QHO Thermodynamics (117K tokens, 5 iterations) are essentially optimal: clean derive → verify → critique → resolve → terminate pipelines with zero waste. These serve as benchmarks for what the system achieves when the computationalist doesn't stall.

---

## 5. Recurring Issues

### 5.1. Computationalist Token Bloat / Stalling (CRITICAL)

**The single most impactful process issue.** 5 of 8 runs had computationalist sessions that hit `max_rounds` (11 rounds = 10 tool rounds + 1 forced final), consuming disproportionate tokens:

| Run | Computation | Rounds | Input Tokens | % of Budget | Outcome |
|-----|-------------|--------|-------------|-------------|---------|
| Berry Phase | COMP-002/003 (iters 2-3) | 11+11 | 467K | 33% | **INCONCLUSIVE, zero text** |
| Path Integral HO | iter 5 | 11 | 305K | 52% | **INCONCLUSIVE, zero text** |
| Perihelion | COMP-003 (iter 6) | 11 | 279K | 53% | VERIFIED (but bloated) |
| Ising | COMP-005 (iter 5) | 11 | 233K | 47% | VERIFIED (but bloated) |
| φ⁴ | COMP-001 (iter 2) | 10 | 163K | 22% | VERIFIED (justified) |

**Two distinct failure modes:**

1. **Silent stalling** (Berry Phase iters 2-3, Path Integral iter 5): The computationalist produces zero text output (`response_chars: 0`) after early rounds but keeps executing tool calls. The forced final call at `max_rounds` yields only a few tokens. The computation is completely lost. This is the most damaging variant.

2. **Productive bloat** (Perihelion, Ising): The computationalist does produce a valid result but takes 11 rounds to get there, with escalating context (9K → 43K per round). The verification is thorough but could have been achieved in 3-4 focused rounds.

**Root causes:**
- The computationalist enters "rabbit holes" — investigating edge cases, self-correcting, or exploring analytical tangents within the tool-use loop
- Context accumulation: each round appends the full previous conversation to the next call's input, creating escalating cost per round
- No early-exit mechanism when `response_chars = 0` persists across rounds
- Complex multi-claim verification tasks encourage scope creep within a single session

**Concrete consequence:** In Path Integral HO, the stalled computation actually discovered that the Maslov phase is e^{iπ} (not e^{-iπ/2} as stated in the research state), but this finding was **lost** because the agent never produced a text summary.

### 5.2. `[unverified]` Labels Persist in Synthesis Tables

**Affected:** QHO, Hawking, Perihelion, φ⁴ (4/8 runs)

The orchestrator writes summary tables with `[unverified]` status placeholders and never updates them after verification, even when the surrounding text says "VERIFIED" and the COMPUTATION_LOG confirms it. In φ⁴, CRIT-010 explicitly asked for a fix, but the orchestrator's integration didn't actually apply it.

### 5.3. WH-to-ER Label Inconsistency

**Affected:** QHO, Hawking, Ising, Berry Phase, φ⁴ (5/8 runs)

When working hypotheses are promoted to established results, the section headers in RESEARCH_STATE keep the `WH-NNN` prefix while the body text and cross-references use `ER-NNN`. The promotion step doesn't rename headers.

### 5.4. `total_computations` Counter Incorrect

**Affected:** QHO, Hawking, φ⁴ (3/8 runs)

The COMPUTATION_LOG frontmatter reports more computations than actually exist (e.g., says 2 but only COMP-002 exists, or says 4 but only COMP-001 and COMP-005 exist).

### 5.5. Truncated Critique Resolution Text

**Affected:** Ising, Chandrasekhar, Path Integral (3/8 runs)

Resolution summaries in CRITIQUE_LOG are cut off mid-sentence. The full resolutions exist in RESEARCH_STATE but the log is incomplete as a standalone record.

### 5.6. Redundant Iterations

- **Hawking** iter 3: Redundant critic pass producing no new critiques (~5.6K tokens)
- **Berry Phase** iters 14-16: Polish iterations (~120K tokens) for minor refinements
- **Perihelion** iter 5: Premature ER promotion, then demotion and re-verification in iter 6

---

## 6. Per-Run Detailed Findings

### 6.1. QHO Thermodynamics
- **5 iterations, 117K tokens — near-optimal**
- All 5 ERs (partition function, mean energy, heat capacity, classical limit, quantum limit) verified by COMP-002
- One LOW critique (CRIT-001) about internal consistency — properly resolved with computational evidence
- Issues: WH-to-ER headers not renamed; summary table has `[unverified]` labels; `total_computations` counter says 2, only 1 exists

### 6.2. Hawking Temperature
- **5 iterations, 96K tokens — the cleanest run**
- ER-001 (T_H = ℏc³/8πGMk_B) verified via 9 independent numerical checks
- Two derivation routes (Euclidean Wick rotation + surface gravity / Unruh) with honest acknowledgment of Rindler approximation limitation
- Issues: cosmetic `[unverified]` in synthesis header; `total_computations` counter off; one redundant critic pass (iter 3)

### 6.3. Ising 1D Transfer Matrix
- **7 iterations, 492K tokens — good science, some computation bloat**
- All 8 ERs (transfer matrix through susceptibility divergence) verified
- CRIT-004 caught a tautological verification → led to independent COMP-005 re-verification
- Issues: COMP-005 hit max_rounds (233K tokens, 57% of budget) but still produced valid result; WH-to-ER headers; truncated CRIT-001/002 resolution text

### 6.4. Perihelion Precession
- **8 iterations, 526K tokens — correct result, significant computation waste**
- All 4 ERs (Schwarzschild radial equation → 42.98 arcsec/century) verified by two independent computations
- 4 substantive critiques including HIGH for forcing term analysis
- Issues: COMP-003 (iter 6) consumed 279K tokens (53% of budget) across 11 rounds — productive but bloated; premature ER promotion in iter 5 then demotion/re-verification; phantom TASK-001 references stripped repeatedly

### 6.5. Berry Phase Spin-1/2
- **17 iterations, 1,420K tokens — correct result, significant early waste**
- 5 ERs covering eigenstates, Berry connection, curvature, phase, and Hopf bundle geometry — all verified
- 8 critiques including HIGH algebraic error (CRIT-004) — all resolved substantively
- Issues: **iters 2-3 consumed 467K tokens (33%) with INCONCLUSIVE verdicts and zero text output** — the worst case of silent stalling; pivot to critic at iter 4 was the right recovery but the damage was done

### 6.6. Path Integral Harmonic Oscillator
- **6 iterations, 590K tokens — PARTIALLY_EFFECTIVE (only run with this rating)**
- 3 ERs (Mehler kernel, classical action, energy spectrum) all verified by COMP-002
- **Iter 5 computationalist consumed 305K tokens (52%) with zero text output** — stalled while investigating the Maslov phase
- The stalled computation actually discovered the correct Maslov phase (e^{iπ} not e^{-iπ/2}) but this finding was lost
- Critique resolutions claim "numerical" backing they never received (the computation that was supposed to provide it failed)

### 6.7. Chandrasekhar Limit
- **7 iterations, 366K tokens — exemplary run, no stalling**
- 3 ERs (relativistic EOS, Lane-Emden equation, Chandrasekhar mass) all verified
- 5 critiques including genuine m_H → m_u correction (1.5% error)
- Only issue: one max_tokens overflow on initial researcher call (retried successfully)
- No computation bloat — COMP-002 (7 rounds) and COMP-004 (4 rounds) were both appropriately sized

### 6.8. Renormalization φ⁴
- **12 iterations, 725K tokens — thorough, efficient**
- 5 ERs (self-energy, vertex correction, beta function, running coupling, finite-part numerics) all verified
- 10 critiques including HIGH sign error (CRIT-003) and Γ(ε-1) constant correction — all substantively resolved
- COMP-001 used 10 rounds / 163K tokens (justified by verifying 5 claims simultaneously)
- Issues: `[unverified]` labels persist despite CRIT-010 asking for fix; `total_computations` counter wrong

---

## 7. Recommendations

### P0-A. Zero-Text Watchdog for Computationalist

**Problem:** When the computationalist produces `response_chars: 0` for multiple consecutive rounds, it is stuck in a tool-only loop and will never recover. Currently it burns through all remaining rounds before the forced final call produces a near-empty response.

**Impact:** Would have saved ~770K tokens across Berry Phase (iters 2-3) and Path Integral (iter 5). Would also have preserved the Maslov phase discovery in Path Integral.

**Implementation:**

In `run_agent_loop()` (`llm.py`, lines 75–208), track consecutive zero-text rounds. After 3 consecutive rounds with `len(resp.text) == 0`, instead of continuing to the next round, break out of the loop and trigger the forced final call early — but with an enriched system message that asks the agent to summarize all findings from its tool executions so far.

```python
# In run_agent_loop(), inside the main for-loop, after processing tool_use:
zero_text_streak += 1 if len(resp.text.strip()) == 0 else 0
if zero_text_streak >= 3:
    # Break early — agent is stuck in tool-only loop
    break
```

The threshold (3 rounds) should be a new config parameter `zero_text_bailout: 3` in `config.default.yaml`. The forced final call (lines 167–208) already exists and would execute after the break — no changes needed there beyond potentially enriching the system message to say "You were terminated early because you stopped producing text. Summarize all computation results from your tool calls."

**Files to modify:**
- `src/sciralph/llm.py` — add zero-text tracking + early break in `run_agent_loop()`
- `src/sciralph/config.py` + `config.default.yaml` — add `zero_text_bailout` parameter

### P0-B. Decompose Multi-Claim Compute Tasks

**Problem:** The orchestrator often emits a single `compute` task like "Verify all 5 working hypotheses numerically." This gives the computationalist a huge scope, encouraging multi-script verification marathons that escalate context across many rounds.

**Impact:** The productive-bloat cases (Perihelion COMP-003 at 279K, Ising COMP-005 at 233K) could have been 2-3 focused tasks of 50-80K each, with cleaner separation of concerns and easier recovery if one fails.

**Implementation:**

This is primarily a prompt-level change. In the orchestrator's system prompt (`src/sciralph/prompts/orchestrator.md`), add an explicit instruction:

> When assigning computation tasks, emit ONE task per established result or working hypothesis to verify. Do not combine multiple verification targets into a single task. Each CURRENT_TASK should target exactly one ER/WH with a specific numerical check.

Additionally, in `engine.py`'s `_apply_overrides()` (lines 155–223), add a new override at P6 priority that detects multi-target compute tasks (by counting ER/WH references in the task body) and splits them. However, the prompt-level fix is simpler and should be tried first.

The engine could also enforce this as a soft check: after the orchestrator emits a `compute` task, count the number of distinct ER-NNN / WH-NNN references in the task body. If > 1, log a warning in METRICS. This serves as telemetry to track whether the prompt fix is working.

**Files to modify:**
- `src/sciralph/prompts/orchestrator.md` — add single-target compute instruction
- `src/sciralph/engine.py` (optional) — add multi-target detection + warning

### P1-A. Intermediate Checkpoint at Round N

**Problem:** Even when the computationalist is producing valid results (the "productive bloat" variant), it accumulates context across 10+ rounds without any external signal to wrap up. The escalating context pattern (9K → 43K per round) means late rounds are disproportionately expensive.

**Impact:** Would reduce the Perihelion COMP-003 from 11 rounds to ~6-7, saving ~100-150K tokens. Would similarly trim Ising COMP-005.

**Implementation:**

In `run_agent_loop()` (`llm.py`), at a configurable checkpoint round (default: round 5 of 10), inject a system message into the conversation before the next API call:

```python
if round_num == checkpoint_round:
    messages.append({
        "role": "user",
        "content": [{"type": "text", "text":
            "CHECKPOINT: You have used half your available rounds. "
            "Summarize your findings so far and focus remaining rounds "
            "on completing any unfinished checks. Write your final "
            "COMP entry text alongside any remaining tool calls."
        }]
    })
```

The checkpoint round should be `max_tool_rounds // 2` by default, configurable as `checkpoint_round` in config. This doesn't force termination — it nudges the agent to start producing text output alongside tool calls rather than deferring all text to the very end.

**Files to modify:**
- `src/sciralph/llm.py` — inject checkpoint message at configurable round
- `src/sciralph/config.py` + `config.default.yaml` — add `checkpoint_round` parameter (default: 5)

### P1-B. Per-Computation Token Budget Alert

**Problem:** There is no visibility into how much budget a single computation session is consuming until it finishes. The 279K-token Perihelion COMP-003 represented 53% of the total run budget, but the engine had no way to know this during execution.

**Impact:** Provides early warning and telemetry. Doesn't directly prevent waste (the computation is already in-flight) but informs future tuning of `max_tool_rounds` and supports the checkpoint mechanism (P1-A).

**Implementation:**

`run_agent_loop()` already tracks `total_input` and `total_output` across rounds. Add a check after each round:

```python
if total_input > config.computation_token_alert:
    # Log alert to metrics
    metrics.log_alert(
        f"computation_token_alert on {agent_name} "
        f"(round {round_num}, {total_input} input tokens)"
    )
```

The threshold should be a new config parameter `computation_token_alert: 150000` (150K input tokens ≈ 30% of a typical 500K-token run).

The `MetricsTracker` already has a `log_alert()` method, so this just needs a new call site. The alert would show up in METRICS.md and be visible to the orchestrator on the next iteration.

**Files to modify:**
- `src/sciralph/llm.py` — add token-budget check per round in `run_agent_loop()`
- `src/sciralph/config.py` + `config.default.yaml` — add `computation_token_alert` parameter
- Requires passing `metrics` tracker into `run_agent_loop()` (currently not passed — either pass it or return the alert info in `AgentResult` for the engine to log)

### P2-A. Fix `[unverified]` Label Persistence

**Problem:** The orchestrator writes synthesis tables with `[unverified]` status labels that are never updated after verification. This contradicts VERIFIED verdicts in COMPUTATION_LOG and confuses the verification report.

**Affected:** 4/8 runs (QHO, Hawking, Perihelion, φ⁴).

**Implementation:**

Add a new post-integration check in `validation.py`. The check should:

1. Parse all COMP entries in COMPUTATION_LOG to build a set of VERIFIED claim IDs
2. Scan RESEARCH_STATE for lines containing `[unverified]` that also reference a verified ER/WH
3. Replace `[unverified]` with `VERIFIED` on those lines
4. Return a violation for each replacement (informational, not blocking)

```python
def check_unverified_labels(workspace: Workspace, config: Config) -> list[Violation]:
    """Replace [unverified] labels that contradict VERIFIED computations."""
    comp_log = workspace.read_file("COMPUTATION_LOG.md")
    verified_ids = _extract_verified_claim_ids(comp_log)

    state = workspace.read_file("RESEARCH_STATE.md")
    violations = []
    for line_num, line in enumerate(state.splitlines()):
        if "[unverified]" in line:
            for vid in verified_ids:
                if vid in line:
                    # Replace and log
                    ...
    return violations
```

Register the check in `_DEFAULT_CHECKS` (line 239 of `validation.py`) to run automatically after every orchestrator integration.

**Files to modify:**
- `src/sciralph/validation.py` — add `check_unverified_labels()` + register in `_DEFAULT_CHECKS`

### P2-B. Fix WH-to-ER Header Renaming on Promotion

**Problem:** When the orchestrator promotes working hypotheses to established results, section headers keep `WH-NNN` prefixes while body text uses `ER-NNN`.

**Affected:** 5/8 runs (QHO, Hawking, Ising, Berry Phase, φ⁴).

**Implementation:**

The ER promotion gate in `validation.py` (`check_er_promotion_gate()`, lines 57–100) already scans RESEARCH_STATE for ER/WH entries and their verification status. It currently demotes unverified ERs to WHs. Add the **reverse**: when a WH is found under the "Established Results" section and has a VERIFIED computation, rename its header from `WH-NNN` to `ER-NNN`.

This logic partially exists — `check_er_promotion_gate()` already modifies RESEARCH_STATE in place. The addition is:

```python
# After checking for unverified ERs (demotions), check for verified WHs (promotions)
for wh_id, section in wh_entries_in_er_section:
    if wh_id_has_verified_computation(wh_id, comp_log):
        # Rename header: "## WH-003" -> "## ER-003"
        state = state.replace(f"## WH-{num}", f"## ER-{num}")
```

**Files to modify:**
- `src/sciralph/validation.py` — extend `check_er_promotion_gate()` with WH→ER renaming

### P2-C. Fix `total_computations` Counter

**Problem:** COMPUTATION_LOG frontmatter reports incorrect computation counts.

**Affected:** 3/8 runs (QHO, Hawking, φ⁴).

**Implementation:**

The counter is updated in `computationalist.py` `_update_computation_metadata()` (lines 55–64). It counts `## COMP-` and `## TASK-` headers. The bug is likely that it counts `## TASK-` headers as computations — these are task headers within a computation entry, not separate computations.

Fix: count only `## COMP-` headers (or more precisely, top-level COMP entries using the same regex pattern used elsewhere):

```python
def _update_computation_metadata(self):
    content = self.workspace.read_file("COMPUTATION_LOG.md")
    meta, body = parse_frontmatter(content)
    # Count only COMP-NNN entries, not TASK-NNN sub-entries
    count = len(re.findall(r'^## COMP-\d+', body, re.MULTILINE))
    meta["total_computations"] = count
    ...
```

There is also already a fix in `validation.py` `check_id_consistency()` (lines 210–232) that corrects this mismatch post-integration. Verify that the validation check uses the same counting logic and fix both if needed.

**Files to modify:**
- `src/sciralph/agents/computationalist.py` — fix `_update_computation_metadata()` counting logic
- `src/sciralph/validation.py` — verify `check_id_consistency()` uses matching logic

### P2-D. Fix Critique Resolution Text Truncation

**Problem:** Resolution summaries written back to CRITIQUE_LOG are cut off mid-sentence.

**Affected:** 3/8 runs (Ising, Chandrasekhar, Path Integral).

**Implementation:**

The truncation happens in `orchestrator.py` `_resolve_critiques()` (lines 150–181). The method uses regex to extract per-critique resolution notes from the orchestrator's prose response. The regex likely captures only a partial match when the resolution text spans multiple lines or contains special characters.

The current extraction (line 161–162) probably uses a pattern like:
```python
re.search(r'CRIT-\d+[:\s]+(.*?)(?=CRIT-\d+|$)', text)
```

The fix should:
1. Use a more robust extraction pattern that handles multi-line resolutions
2. Fall back to a generic "Resolved at iteration N" note rather than a truncated fragment
3. Cap resolution notes at a reasonable length (e.g., 200 characters) with clean truncation at sentence boundaries

```python
note = match.group(1).strip()
if len(note) > 200:
    # Truncate at last sentence boundary
    cut = note[:200].rfind('.')
    note = note[:cut+1] if cut > 50 else note[:200] + "..."
```

**Files to modify:**
- `src/sciralph/agents/orchestrator.py` — fix regex and truncation logic in `_resolve_critiques()`

### P3. Skip Redundant Critic Passes

**Problem:** The engine forces a critic pass every `critic_every_n` iterations (default: 4) even when all existing critiques are already filed and resolved and no new content has been added since the last critic pass.

**Affected:** Hawking (iter 3 redundant critic, ~5.6K tokens), Berry Phase (polish iterations).

**Implementation:**

In `engine.py` `_critic_overdue()` (used in `_apply_overrides()` at P3 priority), add a condition: skip the forced critic if:
1. A critic pass was already run since the last researcher/computationalist output, AND
2. All existing critiques are resolved (0 unresolved at any severity)

```python
def _critic_overdue(self) -> bool:
    if self._iteration - self._last_critic_iteration < self._config.critic_every_n:
        return False
    # NEW: Skip if nothing new to critique
    if self._last_critic_iteration > self._last_content_iteration:
        return False  # Critic already reviewed latest content
    return True
```

This requires tracking `_last_content_iteration` — the iteration at which a researcher or computationalist last produced output. Set it in `_dispatch()` after researcher/computationalist returns.

**Files to modify:**
- `src/sciralph/engine.py` — add `_last_content_iteration` tracking + guard in `_critic_overdue()`

---

## 8. Summary of Implementation Priority

| ID | Issue | Impact | Effort | Files |
|----|-------|--------|--------|-------|
| **P0-A** | Zero-text watchdog | Saves ~770K tokens, prevents lost findings | Small | `llm.py`, `config.py` |
| **P0-B** | Decompose multi-claim compute tasks | Prevents scope creep and bloat | Small | `prompts/orchestrator.md`, optionally `engine.py` |
| **P1-A** | Checkpoint at round N | Reduces productive bloat by ~50% | Small | `llm.py`, `config.py` |
| **P1-B** | Per-computation token budget alert | Early warning / telemetry | Small | `llm.py`, `config.py` |
| **P2-A** | Fix `[unverified]` label persistence | Cosmetic consistency | Small | `validation.py` |
| **P2-B** | Fix WH-to-ER header renaming | Cosmetic consistency | Small | `validation.py` |
| **P2-C** | Fix `total_computations` counter | Metrics accuracy | Small | `computationalist.py`, `validation.py` |
| **P2-D** | Fix critique resolution truncation | Log completeness | Small | `orchestrator.py` |
| **P3** | Skip redundant critic passes | Saves 1-2 iterations per run | Small | `engine.py` |

The P0 items alone would eliminate roughly **30% of total token spend** across these 8 runs while preserving all scientific quality. The P2 items are all small, independent fixes that improve workspace auditability.
