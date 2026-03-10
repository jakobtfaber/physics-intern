# SciRalph: A Ralph Wiggum–Inspired Scaffolding System for Autonomous Scientific Research

## Design Document v2.0

---

## 1. Overview

### 1.1 Purpose

SciRalph is a multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics. It adapts the "Ralph Wiggum" approach (iterative fresh-context LLM calls with externally persisted progress) to scientific research, replacing test-suite backpressure with a layered "soft verification" stack including adversarial critique, symbolic computation, and structured sanity checks.

### 1.2 Design Principles

1. **Fresh context per iteration.** Each agent call starts with a clean context window, reads shared state from files, performs one focused task, writes back, and exits. This prevents context degradation over long research sessions.
2. **Progress lives in files, not in context.** All knowledge, results, critiques, and computations are persisted in structured Markdown files under version control (git). The LLM is stateless; the repository is the memory.
3. **Soft verification as backpressure.** Without a formal proof engine, we build layered verification: hard-ish (dimensional analysis, numerical checks, limiting cases via SymPy), medium (cross-derivation consistency), and soft (adversarial LLM critique). Unresolved critiques act as "failing tests" that block forward progress.
4. **Overexcitement prevention by design.** The system architecturally separates proposing from evaluating. No agent self-certifies its own work. Mandatory critic passes are enforced by the loop, not left to agent judgment.
5. **Extensibility.** Tool integration is abstracted behind a tool-call interface. SymPy is the initial backend; the design accommodates MCP-based integration with Cadabra, xAct, Mathematica, or simulation codes.

### 1.3 Scope (MVP)

- Single-threaded execution (no parallel subagents). The orchestrator may *plan* branches but executes them sequentially.
- SymPy as the sole computational backend (via Python execution).
- Anthropic Claude as the LLM (via API). Model-agnostic design, but prompts tuned for Claude.
- Target domains: derivations in theoretical physics, mathematical proofs (informal), symbolic computation chains.

---

## 2. Architecture

### 2.1 High-Level Loop

```
┌─────────────────────────────────────────────────────┐
│                    MAIN LOOP                        │
│                                                     │
│  while not done:                                    │
│    1. Orchestrator reads state → emits task          │
│    2. Dispatch task to appropriate agent             │
│    3. Agent reads state + task → writes results      │
│    4. Post-step: update metrics, git commit          │
│    5. Every N steps: force Deep Critic pass          │
│    6. Every M steps: force State Compression         │
│    7. Check termination conditions                   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 2.2 Agent Roles

| Role | Purpose | Reads | Writes | Frequency |
|---|---|---|---|---|
| **Orchestrator** | Planning, task selection, integration of proposed changes | All files | `CURRENT_TASK.md`, `RESEARCH_STATE.md` | Every iteration |
| **Researcher** | Derivations, hypothesis generation, conceptual reasoning | `RESEARCH_STATE.md`, `CURRENT_TASK.md`, `CRITIQUE_LOG.md` | `RESEARCH_STATE.md` (proposed changes in `PROPOSED_CHANGES.md`) | On demand |
| **Computationalist** | Symbolic/numerical verification, code execution | `CURRENT_TASK.md`, `COMPUTATION_LOG.md`, relevant sections of `RESEARCH_STATE.md` | `COMPUTATION_LOG.md`, code files in `computations/` | On demand |
| **Deep Critic** | Adversarial review, flaw detection | `RESEARCH_STATE.md`, `COMPUTATION_LOG.md` | `CRITIQUE_LOG.md` | Forced every N iterations + on demand |
| **Compressor** | Summarize and compress growing files | Any file above size threshold | Compressed version of that file | Triggered by size threshold |

### 2.3 Information Flow

```
                    ┌──────────────┐
                    │ Orchestrator │
                    └──────┬───────┘
                           │ writes CURRENT_TASK.md
                           │ reads all state files
                           ▼
              ┌────────────┼────────────┐
              │            │            │
              ▼            ▼            ▼
        ┌───────────┐ ┌──────────┐ ┌──────────┐
        │ Researcher│ │Computat- │ │Deep      │
        │           │ │ionalist  │ │Critic    │
        └─────┬─────┘ └────┬─────┘ └────┬─────┘
              │             │            │
              ▼             ▼            ▼
        PROPOSED_      COMPUTATION_  CRITIQUE_
        CHANGES.md     LOG.md        LOG.md
              │             │            │
              └─────────────┼────────────┘
                            │
                     ┌──────▼───────┐
                     │ Orchestrator │ (next iteration:
                     │ integrates   │  accept/reject/
                     └──────────────┘  modify proposed
                                       changes)
```

Key: The Researcher does NOT directly modify `RESEARCH_STATE.md`. It writes to `PROPOSED_CHANGES.md`. The Orchestrator, on its next pass, decides whether to integrate those changes into `RESEARCH_STATE.md` — but only if there are no blocking critiques. This prevents unverified claims from silently entering the established state.

---

## 3. File Formats

All files are structured Markdown with YAML frontmatter for machine-parseable metadata.

### 3.1 `RESEARCH_STATE.md`

This is the central "knowledge base" — the evolving document of established results.

```markdown
---
problem_id: "hawking-temperature-derivation"
title: "Derive Hawking temperature from Euclidean path integral"
status: "in_progress"  # not_started | in_progress | blocked | completed | abandoned
last_updated: "2026-03-07T14:23:00Z"
iteration: 42
---

# Problem Statement

Derive the Hawking temperature T_H = ℏc³/(8πGMk_B) for a Schwarzschild
black hole using the Euclidean path integral approach. Start from the
Schwarzschild metric, perform Wick rotation, identify the conical singularity
condition, and extract the temperature from the required periodicity of
Euclidean time.

# Established Results

Results here have survived critique and computational verification.

## ER-1: Wick-rotated Schwarzschild metric
- **Statement:** Under τ → -iτ_E, the Schwarzschild metric becomes
  ds² = (1-2GM/rc²)dτ_E² + (1-2GM/rc²)⁻¹dr² + r²dΩ²
- **Confidence:** HIGH
- **Verified by:** COMP-012 (symbolic sign check), CRIT-008 (approved)
- **Depends on:** nothing (starting point)

## ER-2: Near-horizon expansion
- **Statement:** Substituting r = r_s + ε with r_s = 2GM/c², to leading order
  in ε the metric becomes ds² ≈ (ε/r_s)dτ_E² + (r_s/ε)dε² + r_s²dΩ²
- **Confidence:** HIGH
- **Verified by:** COMP-014 (symbolic Taylor expansion)
- **Depends on:** ER-1
- **Open critique:** CRIT-010 (factor of c² in ε coefficient — pending resolution)

# Working Hypotheses

Not yet fully verified. Subject to critique.

## WH-1: Coordinate transformation to polar form
- **Statement:** Defining ρ = 2√(r_s·ε) transforms the (τ_E, ε) part of the
  near-horizon metric into dρ² + (ρ²/4r_s²)dτ_E², which is flat ℝ² in polar
  coordinates iff τ_E has period 4πr_s.
- **Confidence:** MEDIUM
- **Needs verification:** symbolic computation of the Jacobian, explicit
  conical singularity argument
- **Proposed by:** iteration 38
- **Blocking critiques:** none yet (awaiting critic pass)

# Dead Ends

## DE-1: Direct thermal partition function approach [ABANDONED iter. 22]
- **Why abandoned:** Requires full path integral measure specification which
  is beyond scope. The Euclidean periodicity approach is more direct.
- **Key lesson:** Don't try to compute Z directly; use geometric argument.

# Open Questions

- OQ-1: How to rigorously justify ignoring the angular part dΩ² in the
  conical singularity argument?
- OQ-2: What is the role of the path integral measure in the final result?
```

### 3.2 `CRITIQUE_LOG.md`

```markdown
---
total_critiques: 12
unresolved_high: 1
unresolved_medium: 2
unresolved_low: 0
last_critic_pass: "2026-03-07T14:20:00Z"
---

# Active Critiques

## CRIT-010 [HIGH] [UNRESOLVED]
- **Target:** ER-2 (near-horizon expansion)
- **Filed:** iteration 35
- **Critique:** The expansion of (1 - 2GM/rc²) around r = r_s + ε gives
  ε/(r_s + ε) ≈ ε/r_s to leading order. But the coefficient should carry
  a factor involving c². Specifically, in geometrized units (G=c=1) the
  expression is clean, but the document mixes SI and geometrized units.
  The unit system must be made consistent before proceeding.
- **Severity:** HIGH — unit inconsistency could propagate a wrong factor
  into the final temperature.
- **Suggested verification:** Have computationalist redo expansion in
  explicitly geometrized units AND in SI units separately, compare.
- **Resolution:** PENDING

## CRIT-011 [MEDIUM] [UNRESOLVED]
- **Target:** WH-1 (coordinate transformation)
- **Filed:** iteration 40
- **Critique:** The claim that ρ = 2√(r_s·ε) produces the standard polar
  form assumes dε = dρ·(dε/dρ) substitution is clean. But ε itself depends
  on r, and r > r_s is required for the Euclidean section to be real. Is
  the domain of ρ correctly (0, ∞)? What happens at ρ = 0 exactly?
- **Severity:** MEDIUM — likely fine but needs explicit domain statement.

# Resolved Critiques

## CRIT-008 [MEDIUM] [RESOLVED ✓]
- **Target:** ER-1 (Wick-rotated metric)
- **Filed:** iteration 28
- **Critique:** Is the sign of dτ_E² correct? Wick rotation t = -iτ_E
  gives dt² = -dτ_E², so the (1-2GM/rc²)dt² term should become
  -(1-2GM/rc²)dτ_E². But we want a positive-definite Euclidean metric,
  and (1-2GM/rc²) < 0 inside the horizon. Need to clarify that the
  Euclidean section is only defined for r > r_s.
- **Resolution:** Researcher clarified in iteration 30 that the Euclidean
  metric is only defined for r > r_s where (1-2GM/rc²) > 0, making
  the signature (+,+,+,+) as required. COMP-012 verified signs.
  Resolved iteration 31.
```

### 3.3 `COMPUTATION_LOG.md`

```markdown
---
total_computations: 14
last_computation: "2026-03-07T14:15:00Z"
---

# Computations

## COMP-014: Taylor expansion of Schwarzschild metric near horizon
- **Requested by:** Orchestrator, iteration 36, to verify ER-2
- **CLAIM:** Near-horizon expansion gives leading order ε/r_s
- **METHOD:** Expand (1-r_s/r) around r = r_s + ε in geometrized units (G=c=1)
- **Code:** `computations/comp_task-036.py`
- **RESULT:**
  ```
  (1 - r_s/r) at r = r_s + ε:
    = 1 - r_s/(r_s + ε)
    = ε/(r_s + ε)
    = ε/r_s - ε²/r_s² + O(ε³)
  ALL NUMERICAL CHECKS PASSED
  ```
- **VERDICT:** VERIFIED
- **NOTES:** ER-2 confirmed in geometrized units. CRIT-010 raises unit-system concern.
- **Iteration:** 36

## COMP-012: Sign verification of Wick-rotated metric
- **Requested by:** Orchestrator, iteration 29, to verify ER-1
- **CLAIM:** Wick rotation gives positive-definite Euclidean metric for r > r_s
- **METHOD:** Verify sign of g_tt dt² under t = -iτ_E
- **Code:** `computations/comp_task-029.py`
- **RESULT:**
  ```
  g_tt dt² = -(1-r_s/r)·(-dτ_E²) = (1-r_s/r)dτ_E²  (positive for r > r_s)
  ALL NUMERICAL CHECKS PASSED
  ```
- **VERDICT:** VERIFIED
- **NOTES:** ER-1 signs confirmed.
- **Iteration:** 29
```

### 3.4 `CURRENT_TASK.md`

```markdown
---
task_id: "TASK-043"
task_type: "compute"  # research | derive | compute | critique | resolve | synthesize | terminate
assigned_to: "computationalist"
priority: "high"
iteration: 43
parent_task: null
blocking_critiques: ["CRIT-010"]
---

# Task Description

Redo the near-horizon expansion of the Schwarzschild metric (ER-2) in full
SI units (keeping all factors of G, c, ℏ, k_B explicit). Compare with the
geometrized-unit result from COMP-014. This resolves CRIT-010.

# Specific Deliverables

1. SymPy script that performs the expansion in SI units.
2. Explicit mapping between SI result and geometrized result, showing where
   each factor of c² enters.
3. Verdict: does ER-2 as currently stated need correction? If so, what is
   the corrected expression?

# Context

See ER-2 in RESEARCH_STATE.md and CRIT-010 in CRITIQUE_LOG.md.
```

### 3.5 `PROPOSED_CHANGES.md`

Written by the Researcher; consumed by the Orchestrator on its next pass.

```markdown
---
proposed_by: "researcher"
iteration: 38
status: "pending_review"  # pending_review | accepted | rejected | needs_revision
---

# Proposed Addition: WH-1

## Target Section
Working Hypotheses

## Proposed Content
[The content to add or modify — full text as it should appear]

## Justification
[Brief reasoning for why this should be added]

## Requested Verifications
- [ ] Symbolic: compute Jacobian of (ε, τ_E) → (ρ, θ) transformation
- [ ] Numerical: spot-check metric coefficients at ε = 0.01·r_s
- [ ] Critic: review domain and regularity argument

## Dependencies
Depends on ER-1, ER-2 being established.
```

### 3.6 `METRICS.md`

```markdown
---
total_iterations: 43
total_llm_calls: 67
total_input_tokens: 1_847_320
total_output_tokens: 412_890
max_tokens_reached_count: 2
retries: 3
---

# Per-Iteration Metrics

| Iter | Agent | Input Tokens | Output Tokens | Max Tokens Hit | Duration (s) |
|------|-------|-------------|---------------|----------------|-------------|
| 43   | comp  | 38420       | 4210          | no             | 12.3        |
| 42   | orch  | 41200       | 1890          | no             | 8.1         |
| 41   | crit  | 39800       | 6340          | no             | 15.7        |
| ...  | ...   | ...         | ...           | ...            | ...         |

# File Size Tracking

| File | Current Size (chars) | Threshold | Compression Needed |
|------|---------------------|-----------|-------------------|
| RESEARCH_STATE.md | 12400 | 50000 | no |
| CRITIQUE_LOG.md | 8200 | 30000 | no |
| COMPUTATION_LOG.md | 15600 | 40000 | no |

# Alerts
- [iter 23] max_tokens_reached on researcher call — retried with truncated context, succeeded.
- [iter 31] COMPUTATION_LOG.md approaching 50% of threshold.
```

---

## 4. Agent System Prompts

### 4.1 Orchestrator

```
You are the Orchestrator of a scientific research system. Your role is
PLANNING AND COORDINATION ONLY. You do not derive, compute, or critique.
You decide what should happen next.

You will be given the current state of a research project via several files.
Your job is to:

1. Assess the current state: What is established? What is pending? What
   critiques are unresolved?
2. If PROPOSED_CHANGES.md is present, evaluate and integrate accepted
   changes into RESEARCH_STATE.md (see INTEGRATION DUTY below).
3. Decide the single most valuable next action.
4. Write a focused task description.

PROMOTION RULES:
- You MUST NOT mark a Working Hypothesis as an Established Result unless
  ALL of the following are true:
  (a) At least one computational VERIFIED verdict supports it (INCONCLUSIVE
      does NOT count as support, but also does NOT block promotion if other
      evidence supports the claim)
  (b) A Deep Critic pass has reviewed it with no unresolved HIGH critiques
  (c) Its dependencies are all Established Results
- HIGH critiques are not infallible. If a disputed claim has a VERIFIED
  computation verdict, the critique may itself be wrong. Assess before
  blindly resolving.

MOMENTUM RULE — PROMOTE EAGERLY AND ADVANCE:
- When a Working Hypothesis satisfies ALL promotion criteria, promote it
  in the SAME pass and immediately plan the next derivation step.
- Remaining LOW critiques should NOT block promotion.

COMPUTE-FIRST RULE:
- When a new Working Hypothesis has NO computation verdict yet, your FIRST
  action MUST be a "compute" task, not a "critique" task. Numerical
  verification is faster and more decisive.

STALL DETECTION:
- If the researcher has produced the same derivation in 2+ consecutive
  iterations, the line of reasoning has CONVERGED. Note convergence and
  proceed to verification or promotion.
- If stuck in a resolve → critique → resolve loop for the same critique,
  escalate: send to "compute" for a numerical test, or downgrade the
  critique and move on.

VALID TASK TYPES:
  research — new derivation, hypothesis, or conceptual reasoning
  derive — derivation of a specific formula or result
  compute — symbolic/numerical verification via code execution
  critique — adversarial review of research state
  resolve — address a specific unresolved critique
  synthesize — produce final write-up when all results established
  terminate — signal that research is complete or should stop

INTEGRATION DUTY:
When PROPOSED_CHANGES.md is present, evaluate each proposed change against
promotion criteria. Integrate accepted changes into RESEARCH_STATE.md.

CRITIQUE RESOLUTION:
When integrating changes that address critiques, list resolved critique IDs
in YAML frontmatter: `resolved_critiques: [CRIT-001, CRIT-003]`. The system
automatically moves them from Active to Resolved in CRITIQUE_LOG.md.

VERDICT INTERPRETATION (from COMPUTATION_LOG.md):
- VERIFIED — numerically confirmed. Counts as support for promotion.
- REFUTED — claim is computationally disproved. Blocks promotion.
- INCONCLUSIVE — tooling could not verify. NOT evidence against the claim.
  After 2+ INCONCLUSIVE for the same claim, do not retry. Move on.

OUTPUT FORMAT:
When PROPOSED_CHANGES.md is present, output TWO sections:

=== RESEARCH_STATE.md ===
(Full updated file including YAML frontmatter and all sections.)

=== CURRENT_TASK.md ===
(YAML frontmatter + Markdown body.)

When NO proposed changes are present, output only:

=== CURRENT_TASK.md ===
(YAML frontmatter + Markdown body.)
```

### 4.2 Researcher

```
You are a Researcher in a scientific research system. Your role is to do
the intellectual work: derivations, proofs, hypothesis generation,
conceptual reasoning.

You will be given:
- CURRENT_TASK.md describing what to work on
- RESEARCH_STATE.md with the current state of knowledge
- Relevant sections of CRITIQUE_LOG.md (if resolving a critique)

RULES:
- You do NOT write directly to RESEARCH_STATE.md. You write to
  PROPOSED_CHANGES.md. Your proposals will be reviewed before integration.
- For every claim you make, you MUST assign a confidence tag:
  HIGH = follows from established results by straightforward algebra/logic
  MEDIUM = requires non-trivial argument, plausible but needs verification
  LOW = speculative, heuristic, or involves an unverified assumption
- For every claim at MEDIUM or LOW confidence, you MUST specify what
  verification would raise your confidence. Choose from:
  - "symbolic_check" = verify with SymPy
  - "numerical_spot_check" = plug in specific values
  - "dimensional_analysis" = verify units
  - "limiting_case" = check known limit
  - "independent_rederivation" = derive the same result a different way
  - "critic_review" = request adversarial review
- Be explicit about every step. Do not skip "obvious" algebra. Write out
  the chain of reasoning so that a critic can examine each link.
- If the task is a "resolve" task (addressing a critique), you must either:
  (a) Fix the issue and explain the fix, or
  (b) Argue rigorously why the critique is invalid, or
  (c) Acknowledge the critique reveals a fundamental problem and suggest
      an alternative approach.
- If you get stuck or believe the approach is flawed, say so explicitly.
  Propose marking the current line as a Dead End and suggest alternatives.

OUTPUT FORMAT:
You must output ONLY a valid PROPOSED_CHANGES.md file (with YAML
frontmatter and Markdown body) as specified in the design document.
```

### 4.3 Computationalist (Agentic Tool-Use Design)

The computationalist uses the Anthropic tool-use API with an `execute_python` tool. The agent writes code, calls the tool, sees the output (including tracebacks), can fix errors and iterate, and eventually produces the final COMPUTATION_LOG entry with VERDICT as text. This replaces the previous two-pass design where bugs wasted entire iterations.

**Tool-use loop:** The scaffold runs `run_agent_loop()` which loops until the LLM returns `stop_reason="end_turn"`, hits `max_tool_rounds` (default 10), or `max_tokens`. Each round that involves a tool call: the LLM emits a `tool_use` block, the scaffold executes it via `ToolExecutor`, and the result is fed back as a `tool_result` message. Typical computations need 1-3 tool calls.

**Legacy fallback:** The old two-pass flow (generate code → scaffold executes → separate review LLM call) is preserved in `_process_legacy_response`. Setting `tools = []` on the agent class reverts to this path.

The system prompt instructs the agent on verification strategy, comparison rules, numerical pitfalls, and verdict criteria. See `prompts/computationalist.md` for the full prompt. Key elements:

- **Verification strategy:** Numerical spot-checks (Tier 1, mandatory) → Symbolic verification (Tier 2, optional) → Series expansion (Tier 3, when inconclusive).
- **Soft-check pattern:** Never use `assert`; use `np.isclose()` with try/except and a `CHECKS: N/M PASSED` summary.
- **Verdict values:** VERIFIED, REFUTED (requires convergent evidence), INCONCLUSIVE (execution errors, insufficient evidence). Execution failure → always INCONCLUSIVE.
- **Tolerance rules:** Default `rtol=1e-6`, no tolerance widening, quantity validation before comparison.

### 4.4 Deep Critic

```
You are the Deep Critic of a scientific research system. Your SOLE PURPOSE
is to find flaws, gaps, unjustified steps, and potential errors in the
current research state.

You are not helpful. You do not suggest fixes. You do not praise good work.
You ONLY identify problems.

You will be given:
- RESEARCH_STATE.md (the claims to scrutinize)
- COMPUTATION_LOG.md (the evidence supporting those claims)
- Your previous critiques in CRITIQUE_LOG.md (so you don't repeat yourself)

FOR EVERY CLAIM, systematically check:
- LOGICAL: step justification, implicit assumptions, gaps, non sequiturs
- MATHEMATICAL: sign errors, missing factors, index structure, limits,
  boundary conditions, order of operations
- PHYSICAL: units/dimensions, known limits, order of magnitude, symmetries,
  conservation laws
- META: unit system consistency, notation, dependency tracking

COMPUTATION EVIDENCE CHECKS:
- VERIFIED — claim has computational support. You may still critique the
  derivation logic, but note that numerical checks passed.
- REFUTED — claim was computationally disproved. Warrants HIGH severity.
- INCONCLUSIVE — NOT evidence against the claim. INCONCLUSIVE verdicts
  MUST NOT be the sole basis for a HIGH critique. Cap at MEDIUM.
  Execution failures reflect code quality, not mathematical validity.

SEVERITY LEVELS:
- HIGH: Could invalidate the result. Must be resolved before promotion.
- MEDIUM: Gap or concern, likely doesn't invalidate the result.
- LOW: Stylistic, notational, or minor clarity issue.

TWO-PHASE OUTPUT FORMAT:
For EACH claim examined, use this exact structure:

## CRIT-NNN [SEVERITY] [UNRESOLVED]
- **Target:** [claim ID]
- **Filed:** iteration [N]

### Phase 1: Reproduce
Restate the claim's argument step by step IN YOUR OWN WORDS. Do NOT
critique yet. Faithfully reproduce the logical chain. If you cannot
reproduce the argument, note exactly WHERE you get stuck.

### Phase 2: Objection
Having reproduced the argument, state the objection:
- **What is wrong:** [specific flaw]
- **Why it matters:** [could it change the result?]
- **Suggested verification:** [symbolic_check / numerical_spot_check / etc.]

CRITICAL RULES:
- Keep Phase 1 and Phase 2 STRICTLY separate.
- If Phase 1 reproduction arrives at the same result and you find no flaw,
  file a LOW critique noting "Reproduction succeeded, no issues found."

EPISTEMIC CALIBRATION:
- If your objection rests on competing intuition rather than a concrete
  algebraic error, cap severity at MEDIUM.
- If a claim has a VERIFIED computation verdict and your objection is
  purely analytical, cap at MEDIUM.

NON-REPETITION:
- Check CRITIQUE_LOG.md for existing equivalent critiques. Do not duplicate.

You MUST file at least one critique. Do not approve by silence.
```

### 4.5 Compressor

```
You are the Compressor of a scientific research system. Your role is to
reduce the size of a state file that has grown too large, while preserving
all essential information.

You will be given one file that has exceeded its size threshold.

RULES:
- Preserve ALL Established Results verbatim. Never summarize or compress
  these — they are the verified foundation.
- Preserve ALL unresolved critiques verbatim.
- For resolved critiques: collapse to a single-line summary with ID and
  resolution.
- For computations that support Established Results: keep the verdict and
  key result, remove intermediate steps and full code (the code files in
  computations/ directory are the source of truth).
- For Dead Ends: keep the key lesson learned, compress the details.
- For Working Hypotheses that have been superseded or abandoned: remove.
- Never discard information about what DIDN'T work — this prevents the
  system from re-exploring dead ends.

OUTPUT FORMAT:
Output the compressed version of the file, preserving the same structure
and YAML frontmatter format.
```

---

## 5. Main Loop Implementation

### 5.1 Pseudocode

```python
class SciRalph:
    def __init__(self, problem: str, config: Config):
        self.config = config
        self.metrics = MetricsTracker()
        self.workspace = WorkspaceManager(config)
        self.workspace.init(problem)  # creates workspace dir, initial files, git init
        self.config.audit_log = str(self.workspace.root / "AUDIT_LOG.jsonl")
        self.config.logs_dir = str(self.workspace.logs_dir)
        self.iteration = 0
        self._stale_iterations = 0

        # Initialize all five agents
        self.orchestrator = OrchestratorAgent(config, self.workspace, self.metrics)
        self.researcher = ResearcherAgent(config, self.workspace, self.metrics)
        self.computationalist = ComputationalistAgent(config, self.workspace, self.metrics)
        self.critic = CriticAgent(config, self.workspace, self.metrics)
        self.compressor = CompressorAgent(config, self.workspace, self.metrics)

    def run(self):
        """Main loop."""
        while self.iteration < self.config.max_iterations:
            self.iteration += 1

            # --- Step 1: Orchestrator decides next task ---
            # (also integrates PROPOSED_CHANGES.md if present)
            orch_response = self.orchestrator.run({}, self.iteration)
            task = self.orchestrator.parse_task(orch_response.text, iteration=self.iteration)

            # Check for termination signal
            if task["task_type"] == "terminate":
                break
            if task["task_type"] != "synthesize":
                # Backstop: detect stale loops when research looks complete
                # If 3+ ERs, 0 WHs, and orchestrator hasn't terminated for 2 iterations
                er_count, wh_count = self._count_results()
                if er_count >= 3 and wh_count == 0:
                    self._stale_iterations += 1
                    if self._stale_iterations >= 2:
                        break  # force exit
                else:
                    self._stale_iterations = 0

            # --- Step 2: Force critic if overdue ---
            if self._critic_overdue() and task["task_type"] != "critique":
                task = self._make_forced_critic_task()

            # --- Step 3: Dispatch to appropriate agent ---
            agent_name = self._dispatch(task)

            # --- Step 4: File size check & compression ---
            self._check_compression()

            # --- Step 5: Metrics & git commit ---
            self._update_metrics()
            self.workspace.git_commit(f"Iteration {self.iteration}: {agent_name}")

            # --- Step 6: Check termination conditions ---
            if self._should_terminate():  # checks status: completed/abandoned
                break

        self._final_report()  # flushes metrics, prints summary

    def _dispatch(self, task: dict) -> str:
        """Route task to the correct agent."""
        task_type = task["task_type"]

        if task_type in ("research", "derive", "resolve", "synthesize"):
            self.researcher.run(task, self.iteration)
            return "researcher"
        elif task_type == "compute":
            self.computationalist.run(task, self.iteration)
            return "computationalist"
        elif task_type == "critique":
            self.critic.run(task, self.iteration)
            return "deep_critic"
        elif task_type == "compress":
            self.compressor.run(task, self.iteration)
            return "compressor"
        else:
            # Unknown type: fall through to researcher
            self.researcher.run(task, self.iteration)
            return "researcher"
```

### 5.2 Entry Point

```bash
uv run python -m sciralph.main <problem.yaml> [--model MODEL] [--max-iterations N] [--workspace-dir DIR]
```

Problems are defined in YAML files:

```yaml
problem: |
  Derive the Hawking temperature T_H = ℏc³/(8πGMk_B) for a Schwarzschild
  black hole using the Euclidean path integral approach...
```

Each run creates a timestamped workspace directory under `workspaces/`.

---

## 6. Verification Stack

### 6.1 Hierarchy (hardest to softest)

```
HARD SIGNALS (automated, near-binary)
├── Dimensional analysis (SymPy units module)
├── Known limiting cases (compute & compare to known answer)
├── Numerical spot-checks (evaluate at specific parameter values)
├── Symmetry verification (if result should be symmetric, verify)
└── Conservation law checks (probability, charge, energy)

MEDIUM SIGNALS (automated, require judgment)
├── Symbolic computation agreement (SymPy derivation vs. claim)
├── Independent re-derivation (different method, same result?)
├── Cross-consistency (does result A + result B give expected C?)

SOFT SIGNALS (LLM judgment, adversarial)
├── Deep Critic logical scrutiny
├── Deep Critic physical reasonableness check
├── Order-of-magnitude sanity
└── "Is there a simpler way?" test (complexity smell)
```

### 6.2 Promotion Rules

A claim moves from Working Hypothesis → Established Result when:

1. **At least one computation with VERIFIED verdict** supports it. (INCONCLUSIVE does not count as support, but also does not block promotion if other evidence exists.)
2. **A Deep Critic pass has reviewed it** with no unresolved HIGH critiques. (LOW critiques do not block promotion.)
3. **All dependencies are themselves Established Results.**

The Orchestrator enforces these rules. The Researcher cannot self-promote.

---

## 7. Extension Points

### 7.1 MCP Tool Integration (future)

The Computationalist's tool access is abstracted behind method calls:

```python
class ToolBackend:
    """Abstract interface for computational tools."""

    def execute_sympy(self, code: str) -> str: ...
    def execute_cadabra(self, code: str) -> str: ...    # future
    def execute_xact(self, code: str) -> str: ...       # future
    def execute_wolfram(self, query: str) -> str: ...   # future
    def execute_simulation(self, config: dict) -> str:  # future
        ...
```

When MCP integration is added, the Computationalist's system prompt gains
a tool-use section describing available MCP tools and their capabilities.

### 7.2 Parallel Subagents (future)

The Orchestrator already plans sequentially. To enable parallelism:

1. The Orchestrator emits multiple tasks in a single pass, tagged with
   dependency relationships.
2. A `TaskQueue` manages execution, running independent tasks in parallel.
3. A `MergeAgent` reconciles results from parallel branches before the
   next Orchestrator pass.
4. Conflict resolution: if two parallel researchers produce contradictory
   results, the Orchestrator spawns a "debate" task where each result is
   critiqued in light of the other.

### 7.3 Literature Integration (future)

A `Librarian` agent with web search access could:
- Verify results against known literature
- Find relevant papers when the system gets stuck
- Check whether a "novel" result is actually already known

### 7.4 Audit Logging

Two complementary logging systems capture all system activity:

**JSONL Audit Log** (`AUDIT_LOG.jsonl`): One JSON object per LLM call containing metadata (agent name, iteration, token counts, duration, stop reason, character counts). This log is **not** consumed by any agent — it exists purely for the operator. Flushed immediately so it survives crashes.

**Conversation Logs** (`logs/`): Every LLM call produces a Markdown file containing the full system prompt, user content, and assistant response verbatim. Files are named `iter{NNN}_{agent}_{seq}.md` where `seq` is a per-iteration sequence number (handling retries and the computationalist's two-pass flow within one iteration). These files enable inspection and replay of any individual call.

Both live in the workspace directory (gitignored from the source repo, tracked in the workspace's own git).

### 7.5 Agentic Tool Use

The scaffold supports **tool-use agents** via the Anthropic tool-use API. Instead of a single `messages.create` call, `run_agent_loop()` in `llm.py` runs a multi-round loop: the agent calls a tool, sees the output, can fix errors and iterate, and eventually produces a final text response.

**Implementation:** `tools.py` defines `ToolExecutor` (dispatches tool calls) and `ToolCall` (records each invocation). `BaseAgent` has a `tools` class attribute — if non-empty, `run()` uses `_call_with_tools()` → `run_agent_loop()` instead of the one-shot `_call_with_retry()`. Non-tool agents are completely unaffected.

| Agent | Tools | Status |
|-------|-------|--------|
| Computationalist | `execute_python(code)` | **Implemented** — runs code, sees output, iterates on bugs |
| Orchestrator | `read_file(path)` | Planned |
| Researcher | `read_file(path)` | Planned |
| Deep Critic | `read_file(path)` | Planned |
| Compressor | _(none)_ | N/A — simple transformation, one-shot is fine |

**Security:** The LLM never chooses file paths — `execute_python` takes code as a string, and `ToolExecutor` writes it to `computations/tool_exec_NNN.py`. Scripts run in subprocess with timeout (default 60s). Max tool rounds configurable via `max_tool_rounds` (default 10). Output truncated to `tool_output_limit` (default 10K chars).

**Audit logging:** Each round in the loop gets its own audit entry (with a `round` field) and conversation log file. `AgentResult` accumulates tokens across rounds, carries the tool_calls log, and tracks rounds/truncated status.

### 7.6 External Reference Files (Planned)

Problem definitions could include external files (papers, notes, LaTeX documents) that agents can access via tool use. Requires the `read_file` tool from §7.5.

```yaml
problem: |
  Derive the Hawking temperature...

files:
  - path: "references/birrell_davies_ch3.md"
    description: "Chapter 3 of Birrell & Davies on quantum fields in curved spacetime"
```

At workspace initialization, reference files would be copied into `workspace/references/`. The file descriptions would be included in the orchestrator's context so it can direct agents to relevant references.

### 7.7 Workspace Git Strategy

Each workspace (`workspaces/<YYYYMMDD_HHMMSS_problem>/`) is an **independent git repository**, gitignored from the SciRalph source repo. This separation is deliberate:

- The SciRalph source code evolves independently of any research session.
- Multiple research sessions can coexist (each with its own workspace directory).
- The workspace git history is a complete, replayable record of the research.

**Commit strategy:** The scaffolding loop (not any agent) manages git. One commit per iteration, after all file writes are complete. The commit message includes iteration number, agent name, and task ID. Uses `--allow-empty` so every iteration is recorded even if no files changed.

**Session isolation:** Each `sciralph.main` invocation creates a fresh timestamped workspace. The `--workspace-dir` flag overrides the default naming.

---

## 8. Known Limitations & Risks

1. **No formal verification.** All verification is "soft." A consistent
   but wrong derivation could pass all checks. Mitigation: numerical-first
   verification, adversarial critique, multiple derivation paths, independent
   verification script (Claude Opus), human review of final output.
2. **Orchestrator quality is critical.** A bad orchestrator can waste
   iterations on dead ends or skip necessary verification. Mitigation:
   hard-coded rules (forced critic passes, promotion rules, stale-iteration
   backstop) limit orchestrator discretion.
3. **Compressor information loss.** Compression may lose nuances.
   Mitigation: git history preserves originals; archive copies made before
   compression; only resolved critiques and superseded computations are
   compressed.
4. **LLM cost.** Each iteration involves 1-2 full-context LLM calls
   (orchestrator + dispatched agent; computationalist makes a second review
   call). At ~40K input tokens per call and 200 iterations, expect ~8M+
   input tokens per research session.
5. **Symbolic computation limitations.** SymPy cannot handle all
   computations (e.g., tensor algebra, group theory, advanced differential
   geometry). This is the primary motivation for MCP extension.
6. **Tool-use token cost.** The agentic computationalist (§7.5) can use
   5-10x more tokens per compute task than the old one-shot design, since
   it may run multiple rounds of code execution. Mitigation: `max_tool_rounds`
   (default 10) caps iteration count; metrics track rounds and tool calls
   per agent invocation; prompt instructs "1-3 tool calls typical."

---

## 9. Implementation Status & Roadmap

### Implemented
- [x] File initialization and git setup (workspace per run, workspace-local git)
- [x] LLM API wrapper with token tracking, retry logic, and JSONL audit logging
- [x] Full conversation logging (system prompt + context + response per LLM call)
- [x] Orchestrator agent (task emission, promotion rules, integration duty, critique resolution, stall detection, stale-iteration backstop)
- [x] Researcher agent (proposed changes workflow)
- [x] Computationalist agent (agentic tool-use with `execute_python`, numerical-first verification, 3-valued verdict system, legacy two-pass fallback)
- [x] Deep Critic agent (two-phase format, INCONCLUSIVE severity cap, epistemic calibration)
- [x] Compressor agent (archival + compression, forced at 2x threshold)
- [x] Metrics tracking and METRICS.md generation (flush on termination)
- [x] Main loop with forced critic passes, termination detection, stale-iteration backstop
- [x] Independent verification script (Claude Opus, streaming, optional computation re-run)
- [x] 10 problem definitions (Hawking temperature, QHO thermodynamics, 1D Ising, hydrogen fine structure, Casimir effect, perihelion precession, Berry phase, Chandrasekhar limit, path integral HO, phi-4 renormalisation)
- [x] Tool-use loop in llm.py (§7.5) — `run_agent_loop()`, `AgentResult`, `ToolExecutor`
- [x] Agentic computationalist with `execute_python` tool
- [x] Tool-use metrics (rounds, tool calls, truncated flag in METRICS.md)
- [x] Test suite (151 tests covering all modules)

### Planned
- [ ] Agentic researcher/orchestrator/critic with `read_file` tool
- [ ] External reference file support in problem YAML (§7.6)
- [ ] Workspace session resume

### Future Extensions
- [ ] MCP tool integration (Cadabra, xAct, Mathematica)
- [ ] Parallel subagent support
- [ ] Literature search agent
- [ ] Human-in-the-loop breakpoints
- [ ] Audit log replay utility
