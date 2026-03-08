# SciRalph: A Ralph Wiggum–Inspired Scaffolding System for Autonomous Scientific Research

## Design Document v1.0

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
| **Orchestrator** | Planning, task selection, progress tracking | All files | `CURRENT_TASK.md` | Every iteration |
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
- **Task:** Expand (1-r_s/r) around r = r_s + ε to first order in ε,
  using geometrized units (G=c=1).
- **Code:** `computations/comp_014_near_horizon.py`
- **Result:**
  ```
  (1 - r_s/r) at r = r_s + ε:
    = 1 - r_s/(r_s + ε)
    = ε/(r_s + ε)
    = ε/r_s - ε²/r_s² + O(ε³)
  Leading order: ε/r_s  ✓

  Inverse: r_s/ε + 1 + O(ε)  
  Leading order: r_s/ε  ✓
  ```
- **Agrees with claim:** YES (ER-2 confirmed in geometrized units)
- **Note:** CRIT-010 raises unit-system concern. SI-unit version TODO.
- **Iteration:** 36

## COMP-012: Sign verification of Wick-rotated metric
- **Requested by:** Orchestrator, iteration 29, to verify ER-1
- **Task:** Verify that t = -iτ_E applied to Schwarzschild metric yields
  correct signs for Euclidean signature when r > r_s.
- **Code:** `computations/comp_012_wick_signs.py`
- **Result:**
  ```
  dt = -i dτ_E  →  dt² = -dτ_E²
  g_tt dt² = -(1-r_s/r)dτ_E²  
  But original g_tt = -(1-r_s/r), so:
  g_tt dt² = -(1-r_s/r)·(-dτ_E²) = (1-r_s/r)dτ_E²  ✓ (positive for r > r_s)
  ```
- **Agrees with claim:** YES (ER-1 signs confirmed)
- **Iteration:** 29
```

### 3.4 `CURRENT_TASK.md`

```markdown
---
task_id: "TASK-043"
task_type: "compute"  # research | compute | critique | resolve | compress | synthesize
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

Written by the Researcher or Computationalist; consumed by the Orchestrator.

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
2. Decide the single most valuable next action.
3. Write a focused task description.

RULES:
- You MUST NOT mark a Working Hypothesis as an Established Result unless
  ALL of the following are true:
  (a) At least one computational verification supports it
  (b) A Deep Critic pass has reviewed it with no unresolved HIGH critiques
  (c) Its dependencies are all Established Results
- If there are unresolved HIGH critiques, your FIRST priority is to create
  a task that resolves them (usually a "compute" or "resolve" task).
- If no critiques are pending and the last critic pass was more than 3
  iterations ago, your next task MUST be a "critique" task.
- When the problem is complex, identify prerequisite sub-problems or simpler
  analogues whose solutions inform the main derivation. Tackle these first as
  "derive" tasks before attempting the full problem.
- Track dead ends. If a line of reasoning has been attempted twice and
  critiqued both times, consider marking it as a Dead End and trying an
  alternative approach.
- If you believe the research goal has been achieved (all steps from
  problem statement to final result are Established Results forming a
  complete logical chain), set task_type to "synthesize" to produce the
  final write-up.

OUTPUT FORMAT:
You must output ONLY a valid CURRENT_TASK.md file (with YAML frontmatter
and Markdown body) as specified in the design document. Nothing else.
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

### 4.3 Computationalist

```
You are a Computationalist in a scientific research system. Your role is
to perform symbolic and numerical computations that verify, support, or
refute claims made by the Researcher.

You will be given:
- CURRENT_TASK.md describing what to compute
- Relevant context from RESEARCH_STATE.md and COMPUTATION_LOG.md

You have access to a Python environment with SymPy, NumPy, SciPy, and
matplotlib. You write and execute code to perform exact symbolic
manipulations and numerical checks.

RULES:
- Every computation must be self-contained and reproducible. Write a
  complete Python script that can be run independently.
- Always print intermediate steps, not just final results. If you are
  expanding an expression, show each stage of the expansion.
- When verifying a claim, structure your output as:
  CLAIM: [restate the claim being verified]
  METHOD: [what computation you're performing]
  CODE: [the Python script]
  RESULT: [filled automatically from execution]
- After code execution, a second lightweight LLM call reviews the actual
  output and writes the final VERDICT and NOTES. This two-pass design
  ensures verdicts are grounded in real execution results, not pre-execution
  predictions. The review call adds ~10-15% overhead per computation step.
  VERDICT: AGREES / DISAGREES / PARTIALLY AGREES / FAILED
  NOTES: [any caveats, edge cases, or surprises]
- If a computation DISAGREES with a claim, flag this prominently. Include
  the expected result and the actual result side by side.
- For numerical spot-checks, use at least 3 different parameter values
  spanning different regimes (small, medium, large; or specific physically
  meaningful values).
- Always verify units/dimensions when applicable. Use SymPy's unit system
  or explicit dimensional tracking.
- If the task requires a tool you don't have access to (e.g., Cadabra for
  tensor algebra), say so explicitly and describe what the computation
  would be, so the system can be extended later.

OUTPUT FORMAT:
You must output:
1. A COMPUTATION_LOG entry (Markdown, to be appended to COMPUTATION_LOG.md)
2. The Python script (to be saved in computations/ directory)
```

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

FOR EVERY CLAIM in the Working Hypotheses and Established Results sections,
systematically ask:

LOGICAL CHECKS:
- Is each step justified? What is the logical warrant for each inference?
- What assumptions are made implicitly? Are they stated?
- Is there a gap between what is claimed and what is actually shown?
- Does the conclusion follow from the premises, or is there a non sequitur?

MATHEMATICAL CHECKS:
- Could there be a sign error?
- Could there be a missing factor (of 2, π, 2π, etc.)?
- Is the index structure consistent (for tensors)?
- Are limits of integration / boundary conditions correct?
- Is the order of operations / order of limits correct?

PHYSICAL CHECKS:
- Do the units/dimensions work out?
- Does the result reduce to known results in appropriate limits?
- Is the result physically reasonable in order of magnitude?
- Are symmetries respected?
- Are conservation laws satisfied?

META CHECKS:
- Is the unit system consistent throughout?
- Are notation conventions consistent?
- Is there a simpler argument that would make a complex one unnecessary?
  (If so, why is the complex one being used? Possible sign of error.)
- Are the dependencies between results correctly tracked?

SEVERITY LEVELS:
- HIGH: This could invalidate the result. Must be resolved before the
  claim can be promoted to Established.
- MEDIUM: This is a gap or concern that should be addressed but likely
  doesn't invalidate the result.
- LOW: Stylistic, notational, or minor clarity issue.

OUTPUT FORMAT:
You must output new CRITIQUE_LOG entries (Markdown, to be appended to
CRITIQUE_LOG.md). Each critique must have: ID, severity, target claim,
the critique itself, and a suggested verification method.

You MUST file at least one critique. If you genuinely cannot find any
issues, file a LOW critique noting what you checked and that it passed.
Do not approve by silence — the system needs an explicit record that you
looked.
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
import subprocess, json, time, os
from datetime import datetime
from pathlib import Path

# --- Configuration ---
CONFIG = {
    "model": "claude-sonnet-4-20250514",  # or claude-opus-4-6
    "max_tokens": 16384,
    "max_iterations": 200,
    "critic_every_n": 4,          # force critic pass every N iterations
    "compress_threshold": {       # char count thresholds
        "RESEARCH_STATE.md": 50_000,
        "CRITIQUE_LOG.md": 30_000,
        "COMPUTATION_LOG.md": 40_000,
    },
    "max_retries_on_max_tokens": 2,
    "sympy_timeout_seconds": 60,
}

WORKSPACE = Path("./workspace")
COMPUTATIONS_DIR = WORKSPACE / "computations"
PROMPTS_DIR = Path("./prompts")   # stores system prompts for each agent

class SciRalph:
    def __init__(self, problem_description: str):
        """
        problem_description: Natural language description of the research goal.
        """
        self.iteration = 0
        self.metrics = MetricsTracker()
        self.workspace = WORKSPACE
        self._init_workspace(problem_description)

    def _init_workspace(self, problem_description):
        """Create initial files from templates."""
        os.makedirs(COMPUTATIONS_DIR, exist_ok=True)
        # Initialize RESEARCH_STATE.md with problem statement
        # Initialize empty CRITIQUE_LOG.md, COMPUTATION_LOG.md, METRICS.md
        # Initialize git repo
        ...

    def run(self):
        """Main loop."""
        while self.iteration < CONFIG["max_iterations"]:
            self.iteration += 1
            print(f"\n{'='*60}")
            print(f"  ITERATION {self.iteration}")
            print(f"{'='*60}")

            # --- Step 1: Orchestrator decides next task ---
            task = self._run_orchestrator()

            if task["task_type"] == "terminate":
                print("Orchestrator signaled completion.")
                break

            # --- Step 2: Force critic if overdue ---
            if self._critic_overdue() and task["task_type"] != "critique":
                print("Forcing critic pass (overdue).")
                task = self._make_forced_critic_task()

            # --- Step 3: Dispatch to appropriate agent ---
            agent_name = self._dispatch(task)

            # --- Step 4: File size check & compression ---
            self._check_compression()

            # --- Step 5: Metrics & git commit ---
            self.metrics.record_iteration(self.iteration, agent_name)
            self._git_commit(f"Iteration {self.iteration}: {agent_name} — {task['task_id']}")

            # --- Step 6: Check termination conditions ---
            if self._should_terminate():
                break

        self._final_report()

    def _run_orchestrator(self) -> dict:
        """Run orchestrator agent, return parsed task."""
        system_prompt = self._load_prompt("orchestrator")
        user_content = self._build_orchestrator_context()
        response = self._call_llm(system_prompt, user_content, agent="orchestrator")
        task = self._parse_task(response)
        self._write_file("CURRENT_TASK.md", response)
        return task

    def _dispatch(self, task: dict) -> str:
        """Route task to the correct agent."""
        task_type = task["task_type"]

        if task_type in ("research", "resolve", "synthesize"):
            return self._run_researcher(task)
        elif task_type == "compute":
            return self._run_computationalist(task)
        elif task_type == "critique":
            return self._run_deep_critic(task)
        elif task_type == "compress":
            return self._run_compressor(task)
        else:
            raise ValueError(f"Unknown task type: {task_type}")

    def _run_researcher(self, task) -> str:
        system_prompt = self._load_prompt("researcher")
        context = self._build_researcher_context(task)
        response = self._call_llm(system_prompt, context, agent="researcher")
        self._write_file("PROPOSED_CHANGES.md", response)
        return "researcher"

    def _run_computationalist(self, task) -> str:
        system_prompt = self._load_prompt("computationalist")
        context = self._build_computationalist_context(task)
        response = self._call_llm(system_prompt, context, agent="computationalist")
        # Parse response into log entry + code
        log_entry, code = self._parse_computation_response(response)
        self._append_file("COMPUTATION_LOG.md", log_entry)
        # Save and execute the code
        code_path = COMPUTATIONS_DIR / f"comp_{task['task_id']}.py"
        self._write_file(code_path, code)
        exec_result = self._execute_python(code_path)
        # Append execution result to log
        self._append_file("COMPUTATION_LOG.md", f"\n**Execution output:**\n```\n{exec_result}\n```\n")
        return "computationalist"

    def _run_deep_critic(self, task) -> str:
        system_prompt = self._load_prompt("deep_critic")
        context = self._build_critic_context()
        response = self._call_llm(system_prompt, context, agent="deep_critic")
        self._append_file("CRITIQUE_LOG.md", response)
        # Update critique counts in frontmatter
        self._update_critique_metadata()
        return "deep_critic"

    def _run_compressor(self, task) -> str:
        target_file = task.get("target_file")
        system_prompt = self._load_prompt("compressor")
        content = self._read_file(target_file)
        response = self._call_llm(system_prompt, content, agent="compressor")
        # Archive original, write compressed version
        self._archive_file(target_file)
        self._write_file(target_file, response)
        return "compressor"

    # --- LLM Interface ---

    def _call_llm(self, system_prompt: str, user_content: str,
                  agent: str, retry_count: int = 0) -> str:
        """
        Call the LLM API. Track tokens. Retry on max_tokens_reached.
        """
        start_time = time.time()

        response = call_anthropic_api(
            model=CONFIG["model"],
            max_tokens=CONFIG["max_tokens"],
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

        duration = time.time() - start_time
        input_tokens = response["usage"]["input_tokens"]
        output_tokens = response["usage"]["output_tokens"]
        stop_reason = response["stop_reason"]

        self.metrics.record_call(
            iteration=self.iteration,
            agent=agent,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration=duration,
            max_tokens_hit=(stop_reason == "max_tokens"),
        )

        if stop_reason == "max_tokens":
            self.metrics.alert(
                self.iteration,
                f"max_tokens_reached on {agent} call "
                f"(input={input_tokens}, output={output_tokens})"
            )
            if retry_count < CONFIG["max_retries_on_max_tokens"]:
                # Retry with truncated context
                truncated = self._truncate_context(user_content)
                return self._call_llm(
                    system_prompt, truncated, agent, retry_count + 1
                )

        return response["content"][0]["text"]

    # --- Context Builders ---

    def _build_orchestrator_context(self) -> str:
        """Assemble all state files into orchestrator's context."""
        parts = [
            f"# Current Iteration: {self.iteration}\n",
            "## RESEARCH_STATE.md\n",
            self._read_file("RESEARCH_STATE.md"),
            "\n## CRITIQUE_LOG.md\n",
            self._read_file("CRITIQUE_LOG.md"),
            "\n## COMPUTATION_LOG.md (last 5 entries)\n",
            self._read_file_tail("COMPUTATION_LOG.md", n_entries=5),
            "\n## METRICS.md (summary)\n",
            self._read_file("METRICS.md"),
        ]
        if os.path.exists(self.workspace / "PROPOSED_CHANGES.md"):
            parts.append("\n## PROPOSED_CHANGES.md (pending review)\n")
            parts.append(self._read_file("PROPOSED_CHANGES.md"))
        return "\n".join(parts)

    def _build_researcher_context(self, task) -> str:
        parts = [
            "## CURRENT_TASK.md\n",
            self._read_file("CURRENT_TASK.md"),
            "\n## RESEARCH_STATE.md\n",
            self._read_file("RESEARCH_STATE.md"),
        ]
        if task["task_type"] == "resolve":
            parts.append("\n## Relevant Critiques\n")
            parts.append(self._get_relevant_critiques(task))
        return "\n".join(parts)

    def _build_computationalist_context(self, task) -> str:
        parts = [
            "## CURRENT_TASK.md\n",
            self._read_file("CURRENT_TASK.md"),
            "\n## Relevant Research State (excerpts)\n",
            self._get_relevant_research_sections(task),
            "\n## Recent Computations (for reference)\n",
            self._read_file_tail("COMPUTATION_LOG.md", n_entries=3),
        ]
        return "\n".join(parts)

    def _build_critic_context(self) -> str:
        parts = [
            "## RESEARCH_STATE.md\n",
            self._read_file("RESEARCH_STATE.md"),
            "\n## COMPUTATION_LOG.md\n",
            self._read_file("COMPUTATION_LOG.md"),
            "\n## Your Previous Critiques (do not repeat)\n",
            self._read_file("CRITIQUE_LOG.md"),
        ]
        return "\n".join(parts)

    # --- Verification & Execution ---

    def _execute_python(self, script_path: Path) -> str:
        """Execute a Python script in a sandboxed environment with timeout."""
        try:
            result = subprocess.run(
                ["python", str(script_path)],
                capture_output=True,
                text=True,
                timeout=CONFIG["sympy_timeout_seconds"],
                cwd=str(COMPUTATIONS_DIR),
            )
            output = result.stdout
            if result.returncode != 0:
                output += f"\n\nSTDERR:\n{result.stderr}"
            return output
        except subprocess.TimeoutExpired:
            return f"TIMEOUT: Script exceeded {CONFIG['sympy_timeout_seconds']}s limit."
        except Exception as e:
            return f"EXECUTION ERROR: {e}"

    # --- Termination & Monitoring ---

    def _critic_overdue(self) -> bool:
        """Check if more than N iterations since last critic pass."""
        last = self.metrics.last_critic_iteration
        return (self.iteration - last) >= CONFIG["critic_every_n"]

    def _check_compression(self):
        """Check file sizes against thresholds."""
        for filename, threshold in CONFIG["compress_threshold"].items():
            filepath = self.workspace / filename
            if filepath.exists():
                size = filepath.stat().st_size
                if size > threshold:
                    self.metrics.alert(
                        self.iteration,
                        f"{filename} size ({size}) exceeds threshold ({threshold}). "
                        f"Scheduling compression."
                    )
                    # Will be picked up by orchestrator on next iteration,
                    # or force it now if critically large (>2x threshold)
                    if size > threshold * 2:
                        self._run_compressor({"target_file": filename})

    def _should_terminate(self) -> bool:
        """Check termination conditions beyond max_iterations."""
        state = self._read_file("RESEARCH_STATE.md")
        # Parse status from frontmatter
        if "status: \"completed\"" in state:
            return True
        if "status: \"abandoned\"" in state:
            return True
        return False

    def _git_commit(self, message: str):
        """Commit current state to git."""
        subprocess.run(["git", "add", "-A"], cwd=str(self.workspace))
        subprocess.run(["git", "commit", "-m", message, "--allow-empty"],
                       cwd=str(self.workspace))


class MetricsTracker:
    """Track per-iteration metrics, alerts, and file sizes."""

    def __init__(self):
        self.calls = []
        self.alerts = []
        self.last_critic_iteration = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.max_tokens_reached_count = 0

    def record_call(self, iteration, agent, input_tokens, output_tokens,
                    duration, max_tokens_hit):
        self.calls.append({
            "iteration": iteration,
            "agent": agent,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration": duration,
            "max_tokens_hit": max_tokens_hit,
        })
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        if max_tokens_hit:
            self.max_tokens_reached_count += 1
        if agent == "deep_critic":
            self.last_critic_iteration = iteration

    def record_iteration(self, iteration, agent_name):
        """Write current metrics to METRICS.md."""
        ...

    def alert(self, iteration, message):
        self.alerts.append({"iteration": iteration, "message": message})

    def to_markdown(self) -> str:
        """Render metrics as Markdown for METRICS.md."""
        ...
```

### 5.2 Entry Point

```python
# main.py

from sciralph import SciRalph

problem = """
Derive the Hawking temperature T_H = ℏc³/(8πGMk_B) for a Schwarzschild
black hole using the Euclidean path integral approach. Start from the
Schwarzschild metric, perform Wick rotation, identify the conical
singularity condition, and extract the temperature from the required
periodicity of Euclidean time.
"""

agent = SciRalph(problem)
agent.run()
```

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

1. **At least one HARD or MEDIUM signal** supports it (a computation agrees).
2. **A Deep Critic pass has reviewed it** with no unresolved HIGH critiques.
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

All system activity is logged to a structured JSONL file (`workspace/audit.jsonl`) for human review and debugging. This log is **not** consumed by any agent — it exists purely for the operator.

In addition, every LLM call produces a Markdown file in `logs/` containing the full system prompt, user content, and assistant response. Files are named `iter{NNN}_{agent}_{seq}.md` where `seq` is a per-iteration sequence number (handling retries and multiple calls within one iteration). These files enable inspection and replay of any individual call.

Each log entry is a JSON object with a `type` field. Entry types:

```json
{"type": "llm_call", "timestamp": "...", "iteration": 42, "agent": "researcher",
 "system_prompt_hash": "a1b2c3", "context_chars": 38420,
 "response_chars": 4210, "input_tokens": 38420, "output_tokens": 4210,
 "stop_reason": "end_turn", "duration_s": 12.3,
 "system_prompt": "...", "context": "...", "response": "..."}

{"type": "file_write", "timestamp": "...", "iteration": 42,
 "filename": "PROPOSED_CHANGES.md", "size_chars": 2400}

{"type": "sandbox_exec", "timestamp": "...", "iteration": 42,
 "script": "computations/comp_043.py", "returncode": 0,
 "timed_out": false, "stdout_chars": 340, "stderr_chars": 0}

{"type": "tool_call", "timestamp": "...", "iteration": 42, "agent": "computationalist",
 "tool": "execute_python", "input": {"code": "..."}, "output": "...", "duration_s": 3.1}

{"type": "git_commit", "timestamp": "...", "iteration": 42,
 "message": "Iteration 42: researcher — TASK-043", "sha": "abc1234"}

{"type": "decision", "timestamp": "...", "iteration": 42,
 "decision": "force_critic", "reason": "4 iterations since last critic pass"}

{"type": "alert", "timestamp": "...", "iteration": 42,
 "message": "max_tokens_reached on researcher call"}
```

The logger is a thin wrapper around `json.dumps` + file append. No buffering — every entry is flushed immediately so the log survives crashes. The full LLM prompts and responses are included (these are the most valuable part for debugging), making the log potentially large (hundreds of MB for long sessions). This is acceptable since it lives in the gitignored workspace.

A companion `replay` utility (future) could parse the JSONL to reconstruct a session timeline, filter by agent, visualize token usage, or replay the context that a specific agent saw at a given iteration.

### 7.5 Agentic Tool Use (Tool-Use Loop)

The MVP uses a one-shot pattern: the scaffold builds context, calls the LLM once, and processes the text response. This has a critical limitation for the Computationalist — if generated code has a bug, an entire iteration is wasted.

The improved design gives select agents access to **tools** via the Anthropic tool-use API. Instead of a single `messages.create` call, the scaffold runs a **tool-use loop**:

```python
def run_agent_loop(system, context, tools, config) -> AgentResult:
    """Run an agent with tool use until it produces a final text response."""
    messages = [{"role": "user", "content": context}]

    while True:
        response = client.messages.create(
            model=config.model,
            max_tokens=config.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )

        # Collect tool uses and text blocks
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if not tool_results:
            # No tool calls — agent is done
            break

        # Feed tool results back
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return AgentResult(text=..., tool_calls=..., tokens=...)
```

#### Tool Definitions per Agent

| Agent | Tools | Rationale |
|-------|-------|-----------|
| Orchestrator | `read_file(path)` | Can drill into specific sections on demand rather than receiving everything upfront |
| Researcher | `read_file(path)`, `list_files()` | Can load external references and specific workspace files |
| Computationalist | `execute_python(code)`, `read_file(path)` | Can write code, see output, iterate on bugs — all in one turn |
| Deep Critic | `read_file(path)` | Read-only; can request specific evidence |
| Compressor | _(none — one-shot is fine)_ | Simple transformation task |

#### Tool Definitions

```python
TOOLS = {
    "read_file": {
        "name": "read_file",
        "description": "Read a file from the workspace or reference directory. "
                       "Returns the file contents as a string.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from workspace root, or absolute path "
                                   "to a reference file listed in the problem definition."
                }
            },
            "required": ["path"]
        }
    },
    "list_files": {
        "name": "list_files",
        "description": "List files in a workspace directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Relative path from workspace root. Default: root.",
                    "default": "."
                }
            }
        }
    },
    "execute_python": {
        "name": "execute_python",
        "description": "Execute a Python script and return its output. "
                       "The script has access to sympy, numpy, scipy, matplotlib. "
                       "Timeout: 60 seconds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Complete, self-contained Python script to execute."
                },
                "filename": {
                    "type": "string",
                    "description": "Filename to save the script as (in computations/ dir).",
                    "default": "scratch.py"
                }
            },
            "required": ["code"]
        }
    }
}
```

#### Security Constraints

- `read_file`: restricted to workspace root and explicitly allowed reference paths (from problem YAML `files:` list). Path traversal (`../`) outside allowed roots is rejected.
- `execute_python`: runs in subprocess with timeout. No network access (future: use a network namespace or nsjail). No filesystem writes outside `computations/`.
- `list_files`: restricted to workspace root.
- **Max tool rounds per agent invocation:** configurable (default 10) to prevent runaway loops.
- Every tool call is logged to the audit log (§7.4).

### 7.6 External Reference Files

Problem definitions can include external files (papers, notes, LaTeX documents) that agents can access during their tool-use loops.

```yaml
# problems/hawking_temperature.yaml
problem: |
  Derive the Hawking temperature...

files:
  - path: "references/birrell_davies_ch3.md"
    description: "Chapter 3 of Birrell & Davies on quantum fields in curved spacetime"
  - path: "references/gibbons_hawking_1977.tex"
    description: "Gibbons-Hawking 1977 paper on cosmological event horizons"
  - path: "notes/euclidean_methods.md"
    description: "Personal notes on Euclidean quantum gravity techniques"

```

At workspace initialization, reference files are copied into `workspace/references/`. The `read_file` tool allows agents to access these paths. The file descriptions are included in the orchestrator's context so it can direct agents to relevant references.

For large files (LaTeX papers), the system could (future) pre-process them into chunked summaries, but for MVP the full file is loaded when requested by the agent — the tool-use pattern means only agents that need the file pay the token cost.

### 7.7 Workspace Git Strategy

The workspace (`workspace/`) is an **independent git repository**, gitignored from the SciRalph source repo. This separation is deliberate:

- The SciRalph source code evolves independently of any research session.
- Multiple research sessions can coexist (each with its own workspace directory).
- The workspace git history is a complete, replayable record of the research.

**Commit strategy:** The scaffolding loop (not any agent) manages git. One commit per iteration, after all file writes are complete. The commit message includes iteration number, agent name, and task ID.

**Branching (future):** When the orchestrator proposes exploring a speculative direction, the scaffold could create a branch. If the direction is marked as a Dead End, the branch is preserved (for audit) but the main branch is not polluted. This would require the orchestrator to emit a `branch_hint` field in its task output. For MVP, linear history is sufficient — the Dead Ends section in RESEARCH_STATE.md serves the same purpose.

**Session isolation:** Each `sciralph.main` invocation creates a fresh workspace (or resumes an existing one if `--workspace-dir` points to an initialized workspace). The `--workspace-dir` flag defaults to `workspace/` but can be set to e.g. `workspace/session_2026_03_07/` for multiple concurrent sessions.

---

## 8. Known Limitations & Risks

1. **No formal verification.** All verification is "soft." A consistent
   but wrong derivation could pass all checks. Mitigation: sub-problem
   calibration, multiple independent derivation paths, human review of
   final output.
2. **Orchestrator quality is critical.** A bad orchestrator can waste
   iterations on dead ends or skip necessary verification. Mitigation:
   hard-coded rules (forced critic passes, promotion rules) limit
   orchestrator discretion.
3. **Compressor information loss.** Compression may lose nuances.
   Mitigation: git history preserves originals; only resolved critiques
   and superseded computations are compressed.
4. **LLM cost.** Each iteration involves a full-context LLM call.
   At ~40K input tokens per call and 200 iterations, expect ~8M input
   tokens per research session (~$20-80 depending on model and pricing).
5. **Symbolic computation limitations.** SymPy cannot handle all
   computations (e.g., tensor algebra, group theory, advanced differential
   geometry). This is the primary motivation for MCP extension.
6. **One-shot computation fragility.** Without tool use, a single bug in
   generated code wastes an entire iteration. Mitigation: agentic tool-use
   loop (§7.5) lets the computationalist self-correct.
7. **Tool-use cost amplification.** Agentic tool-use loops multiply token
   usage per iteration (each round-trip adds input tokens). Mitigation:
   max tool rounds cap (default 10), audit logging to monitor cost.
8. **Reference file size.** Large external files (LaTeX papers) loaded via
   tool use can fill the context window. Mitigation: agents request files
   on demand (not all upfront), future chunking/summarization.

---

## 9. Implementation Roadmap

### Phase 1: Core Loop (MVP) [DONE]
- [x] File initialization and git setup
- [x] LLM API wrapper with token tracking and retry logic
- [x] Orchestrator agent (task emission + promotion rules)
- [x] Researcher agent (proposed changes workflow)
- [x] Computationalist agent (SymPy execution sandbox)
- [x] Deep Critic agent (adversarial review)
- [x] Compressor agent
- [x] Metrics tracking and METRICS.md generation
- [x] Main loop with forced critic passes

### Phase 1.5: Audit & Agentic Tools
- [ ] JSONL audit logger (§7.4)
- [ ] Tool-use loop in llm.py (§7.5)
- [ ] Tool executor with security constraints
- [ ] Agentic computationalist (execute_python tool)
- [ ] Agentic researcher/orchestrator/critic (read_file, list_files tools)
- [ ] External reference file support in problem YAML (§7.6)
- [ ] Reference file copying at workspace init
- [ ] Workspace session resume (`--workspace-dir` pointing to existing workspace)

### Phase 2: Robustness
- [ ] Structured output parsing with fallback (YAML frontmatter extraction)
- [ ] Sandbox hardening (restricted Python execution environment)
- [ ] Context truncation strategies (smart selection of relevant sections)
- [ ] CLI interface for launching research sessions

### Phase 3: Extensions
- [ ] MCP tool integration (Cadabra, xAct)
- [ ] Parallel subagent support
- [ ] Literature search agent
- [ ] Web UI for monitoring live research sessions
- [ ] Human-in-the-loop breakpoints (pause and ask human at critical junctures)
- [ ] Audit log replay utility
