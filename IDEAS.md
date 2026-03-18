# SciRalph — Task List

## CANDIDATE FIXES

### From Kimi run

#### Reinforce script independence for compute agents (NameError recovery)

**Problem:** Kimi K2.5 consistently treats `execute_python` as a REPL with persistent state — it defines functions/variables in one round, then references them in the next without redefining. This causes NameErrors in nearly every compute session (iter001: round 2, iter003: rounds 2, 3, 6, iter006: multiple, iter009: round 4). Each NameError wastes a full round of output. Across 5 compute sessions, ~8 rounds were lost to this pattern.

**Fix — two parts:**

1. **Prompt reinforcement in `computationalist.md` (and by inheritance all compute agent prompts):** Move the "every script must be self-contained" instruction to the very top of the tool-use section, make it a bold warning, and add a concrete example: "Each `execute_python` call runs in a **fresh Python process** — no variables, functions, or imports carry over between calls. Every script must re-import libraries and redefine any functions it needs. If your previous script defined `propagate_error()`, your next script must define it again."

2. **NameError-specific recovery hint in `tools.py`:** When `execute_python` returns a traceback containing `NameError`, append a short reminder to the tool result: "⚠ NameError — reminder: each script runs in a fresh Python process. You must include all imports and function definitions in every script." This is cheaper than burning a full round on the agent figuring out why it failed.

**Expected impact:** Should eliminate most NameError rounds for models that don't fully internalize the statelessness constraint from the system prompt alone. Cost is negligible (a few extra tokens in error results).

**Files:** `src/sciralph/prompts/computationalist.md`, `src/sciralph/tools.py`.

#### Named Python scripts in execute_python (audit trail + self-containment)

**Problem:** Scripts are saved as `tool_exec_001.py`, `tool_exec_002.py` — opaque sequential names that mean nothing when reading logs. The model never sees these names. The tool result is raw stdout/stderr with no reference to which file produced it. This has two consequences: (1) poor audit trail — post-hoc inspection of `computations/` is painful, and (2) the model has no psychological reinforcement that each call produces an *independent file*, contributing to the NameError problem above.

Meanwhile, the existing `purpose` field is filled well by models (Kimi K2.5 writes specific, meaningful purposes like "Fix the Pauli bit encoding bug (Y and Z were swapped)") but is **completely discarded** — never stored, never logged, never shown back to the model.

**Fix — add a required `filename` parameter to `execute_python`:**

1. **Tool definition change in `tools.py`:** Add a required `filename` field:
   ```python
   "filename": {
       "type": "string",
       "description": "A short, descriptive filename for this script (e.g. 'verify_enumeration.py', 'spot_check_formula.py'). Each script runs as an independent Python file — all imports and definitions must be included."
   }
   ```
   The field description doubles as a self-containment reminder — every time the model fills `filename`, it reads "independent Python file."

2. **Script saving in `tools.py` (`_execute_python`):** Replace `tool_exec_{counter}.py` with `{counter:03d}_{sanitized_filename}`. Example: `001_verify_enumeration.py`, `002_spot_check_formula.py`. Keep the counter prefix for ordering; append the model-chosen name for readability. Sanitize the filename (strip path separators, limit length, ensure `.py` extension).

3. **Tool result message:** Change the result shown to the model from raw stdout to a structured receipt:
   ```
   === 001_verify_enumeration.py ===
   Purpose: Full enumeration of 16^5 error configs to verify WH-003
   Exit: success (3.2s)

   <stdout output here>
   ```
   This gives the model a clear receipt linking filename → purpose → output. When it sees `002_spot_check_formula.py` in the next round, the naming makes it viscerally obvious these are separate files.

4. **Store purpose alongside result:** Save the `purpose` string in the computation record (e.g. as a field on the internal execution log, or in the JSONL audit). Currently discarded.

**Prompt changes for submit_verdict / submit_result:**

5. **Tool definition for `submit_verdict` and `submit_result`:** Add guidance in the description (or a new optional `evidence_scripts` field) telling the model to reference which scripts provide the evidence. Example for `submit_verdict`:
   ```
   "description": "Submit your verification verdict. Reference the script filenames that provide the key evidence (e.g. 'Based on 001_full_enumeration.py which matched all coefficients, and 003_spot_check.py which confirmed 5 test points')."
   ```
   This improves tracing: when reviewing a VERIFIED or REFUTED verdict, you can immediately find the code that produced it.

6. **Prompt change in `compute_verify.md` / `compute_explore.md`:** Add a line: "When calling `submit_verdict` (or `submit_result`), cite the script filenames that provide the strongest evidence for your conclusion. This creates an audit trail linking your verdict to specific computations."

**Expected benefits:**
- **Self-containment reinforcement:** Naming a file is a stronger "this is independent" signal than reading a warning in the prompt. Should reduce NameErrors organically, complementing the explicit fix above.
- **Audit trail:** `computations/003_verify_wh003_enumeration.py` is infinitely more useful than `computations/tool_exec_003.py` when debugging a failed run.
- **Back-reference in verdicts:** When the model writes "VERIFIED based on `001_full_enumeration.py` and `002_spot_check.py`", post-hoc analysis can trace exactly which code supported the verdict.
- **Discrepancy detection:** If a REFUTED verdict cites `003_buggy_enumeration.py`, and we can inspect that file to find the Pauli encoding bug (as in TASK-006), the tracing becomes immediate.

**Cost:** Negligible — one extra required field per tool call, ~20 extra tokens in the tool result header.

**Files:** `src/sciralph/tools.py` (tool definition + `_execute_python` + result formatting), `src/sciralph/prompts/compute_verify.md`, `src/sciralph/prompts/compute_explore.md`, `src/sciralph/prompts/computationalist.md`.

### From Qwen run

#### Orchestrator must translate strategy into actionable task descriptions

**Problem:** The strategist produced excellent guidance for the QEC problem — explicitly recommending full 16⁵ enumeration of multi-error configurations and warning that single-error analysis is insufficient. But the compute agents never saw this because `render_compute_research_state()` doesn't include strategy notes (by design — strategy is mostly noise for compute agents). The orchestrator *does* see the strategy, but when dispatching compute_explore and compute_verify tasks, it wrote vague task descriptions like "Numerically verify the logical fidelity expression" without specifying the required enumeration scope. The compute agents then defaulted to single-error analysis (75 patterns instead of 1M+), which happens to give F(p)=1.0 — a wrong answer that looks self-consistent.

**Root cause:** The orchestrator prompt doesn't tell it to distill relevant strategy insights into the task description. The strategy is treated as background context for the orchestrator's own reasoning, not as something that should flow downstream through task specs.

**Proposed fix:** Add guidance in `orchestrator.md` prompt telling the orchestrator to include methodologically critical details from the strategy in task descriptions. Something like: "When dispatching compute or verify tasks, your task description is the ONLY guidance the agent receives — it does not see the research strategy. Include any methodological requirements that are critical to getting the computation right: required enumeration scope, edge cases to cover, known pitfalls from the strategy, specific approaches to use or avoid."

**Why prompt-only (not renderer change):** Dumping the full strategy into compute agent context would add ~2-3K tokens of mostly irrelevant text per call. The orchestrator is better positioned to extract and compress the 1-2 relevant bullet points into the task description. This also forces the orchestrator to think about what the compute agent actually needs to know, which is a useful forcing function.

**Expected impact:** High for this specific failure mode. The strategy said "enumerate all 15⁵ patterns" — if that single sentence had appeared in the task description, even Qwen would likely have implemented the full enumeration instead of defaulting to single-error analysis.

**Files:** `src/sciralph/prompts/orchestrator.md` only.

#### Strengthen the critic as methodological safety net

**Problem:** In the Qwen QEC run, the error-correction cycle worked mechanically — the verifier correctly REFUTED the initial wrong formula, the critic correctly flagged the classification error, the orchestrator updated the hypothesis. But the correction went in the wrong direction (F(p)=1.0 instead of the correct non-trivial rational function) because all agents shared the same blind spot: only analyzing single-error patterns. The critic (CRIT-001) caught the *internal inconsistency* (5/7 survivors classified as harmful when simulation showed 0/7 harmful) but never questioned the *methodological completeness* (single-error analysis when the problem requires full multi-error enumeration).

This matters because the verify path can't catch this class of error on its own: when the claim is trivially simple (F(p)=constant), any incomplete verification will trivially confirm it. The critic is the only agent positioned to catch scope errors before promotion.

**Fix — two parts:**

1. **Pass strategy notes to the critic** (`renderers.py` + `agents/critic.py`): The critic currently receives `render_research_state_md(state)`, which calls `_research_state_body(state)` with `include_research_strategy=False`. The critic never sees the strategist's guidance. Change `build_context()` in `CriticAgent` to include the strategy — either by calling `_research_state_body` with `include_research_strategy=True`, or by appending a separate `render_research_strategy(state)` section.

   In the Qwen run, the strategy explicitly said: *"Third: Combine error patterns. Multiple gates can have errors simultaneously"* and *"Approach A: requires careful bookkeeping of 15⁵ potential patterns."* If the critic had seen this, it could have noticed that the computation only covered 75 single-gate patterns out of the 1M+ the strategy recommended — a glaring scope gap.

   The strategy is typically 1-3K tokens. For the critic, this is well worth the context cost — the critic runs once or twice per run, and methodological awareness is central to its job.

2. **Add a METHODOLOGICAL CHECKS section to `deep_critic.md`**: The current prompt has four check categories: LOGICAL, MATHEMATICAL, PHYSICAL, META. Add a fifth:

   ```
   METHODOLOGICAL CHECKS:
   - Does the computation cover the full scope of the problem, or only a subset
     of cases? (e.g., single-error patterns when multi-error combinations are
     possible; leading-order expansion when the exact answer is requested;
     special parameter values when the full parameter range matters)
   - Is a trivially simple result (constant, zero, exact cancellation) consistent
     with the problem's expected complexity? Unexpectedly simple results often
     indicate incomplete analysis rather than genuine simplification.
   - Does the verification method test the claim in a way that could actually
     falsify it, or would any incomplete computation trivially confirm it?
   ```

   This gives the critic a new lens — not just "is the derivation internally consistent?" but "did the computation actually cover what it needed to cover?"

**Expected impact:** Part 1 is high-impact and low-risk — strategy context enables the critic to spot scope gaps that are invisible without it. Part 2 is a soft nudge — it can't guarantee the model will notice a specific gap, but it shifts the critic's attention toward the right questions.

**Files:** `src/sciralph/agents/critic.py` (context building), `src/sciralph/renderers.py` (minor — may need a dedicated critic renderer), `src/sciralph/prompts/deep_critic.md` (new METHODOLOGICAL CHECKS section).

### Naming
- why "computationalist" knowing research also derives from it, should make it Abstract Base Class, remove it prompt file ?
- deep critic / critique / critic

## OTHER IDEAS

### Quality of Life
- timing on the console output


### Improve orchestrator

- strategist to gather some background knowledge about the problem domain
- prompt the orchestrator for better problem decomposition
- create some warm up problems
- Add a more open ended "brainstorm" task and maybe a dedicated section in the research state for ideas, possible routes, alternatives, etc.
- brainstorm internal consistency checks ?
- open questions and dead ends ??

### Improve computationalist
- to improve token efficiency, should we strip the previous code from the conversation

### Problem YAML features

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

### MCP tool integration

- **Additional computational backends** — abstract the computationalist's tool access behind a `ToolBackend` interface to support Cadabra (tensor algebra), xAct (differential geometry), Mathematica (symbolic CAS), or simulation codes via MCP. The computationalist prompt would gain a tool-use section describing available MCP tools and their capabilities.

### Parallel subagents

- **Parallel task execution** — the orchestrator emits multiple tasks tagged with dependency relationships; a `TaskQueue` runs independent tasks in parallel; a `MergeAgent` reconciles results before the next orchestrator pass. For contradictory parallel results, spawn a "debate" task where each result is critiqued in light of the other.

### Literature integration

- **Librarian agent** — an agent with web search access that can verify results against known literature, find relevant papers when the system gets stuck, and check whether a "novel" result is actually already known.

### Workspace management

- **Workspace resume** — `--resume <workspace-dir>` to continue a previous run. Skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state (corrupted state, version mismatches).

### Human-in-the-loop breakpoints
- allow the operator to pause the loop, inspect state, and intervene