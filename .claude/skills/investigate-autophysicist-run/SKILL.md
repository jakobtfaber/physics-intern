---
name: investigate-autophysicist-run
description: "Investigates an Autophysicist workspace run. Use to understand what went wrong and could be improved in the single-agent iterative research process."
allowed-tools: Read, Grep
model: opus
---

# Analyze an Autophysicist Run

Read README.md to understand how the Autophysicist research mode works (single-agent iterative loop with ephemeral sub-agents).

Given a workspace directory (under `workspaces/` in the PhysicsIntern project — autophysicist runs end in `_autophysicist`; legacy runs may live under `workspaces/autophysicist/`), perform a systematic post-mortem analysis of the run. The user may provide a folder name or path; if ambiguous, list available workspaces and ask.

Check `references/` in the project root for a reference document matching the problem. These files describe what a correct answer looks like and what a typical successful run looks like for known problems. The reference is written for the vanilla multi-agent pipeline, but the correct answer and key pitfalls still apply.

**Key deliverables:**
- A list of specific failures and inefficiencies: what went wrong, in which iteration, and why.
- Assessment of the Manager's strategic quality: decomposition, sub-agent design, verification discipline, memory management.
- A list of insights for improvements in the Manager prompt, sub-agent usage patterns, and scaffold configuration.

## Tools

For any Python code you need to write to analyze the files, use the /tmp folder to write and run temporary files.
Do not run directly in the command line. Instead, write a Python script that reads the relevant files, performs the analysis, and prints the results.
Then run that script and read its output.


## Workspace Structure

An Autophysicist workspace contains these key files:

| File | Purpose |
|------|---------|
| `problem.yaml` | Problem definition (problem text, answer_template, and possibly the true answer — not visible to the Manager) |
| `PROBLEM.md` | Problem statement in readable form |
| `ANSWER.md` | Final answer (present only if `submit_final_answer` was called) |
| `PERMANENT_MEMORY.md` | Append-only verified results — the Manager's canonical output |
| `SCRATCHPAD.md` | Rolling working notes (full history on disk; only last N entries were visible to Manager each iteration) |
| `METRICS.md` | Per-iteration token usage with YAML frontmatter summary |
| `VERIFICATION.md` | Formal answer evaluation only (correct/incorrect/inconclusive/skipped) — no LLM diagnosis |
| `EVENT_LOG.jsonl` | LLM call metadata + scaffold events |
| `config.json` | Run configuration (model, budgets, caps) |
| `.iteration` | Final iteration counter |
| `logs/` | Per-call logs: `iter{N:03d}_{M:02d}_{agent_name}.md` |
| `computations/` | Code execution scripts: `subagent_iter{N}_{idx}_attempt{M}.py` |
| `console.log` | Raw console output |

**Important:** Unlike the vanilla pipeline, there is no `RESEARCH_GRAPH.json`, no `RESEARCH_STATE.md`, no `EVIDENCE_LOG.md`, no `CRITIQUE_LOG.md`, and no automated diagnosis in `VERIFICATION.md`. The entire research narrative must be reconstructed from `PERMANENT_MEMORY.md`, `SCRATCHPAD.md`, `EVENT_LOG.jsonl`, and the log files.


## Log File Format

**Manager logs** (`iter{N:03d}_01_manager.md`): Contains the full agentic conversation for one iteration:
- `<SYSTEM_PROMPT>` — the Research Manager system prompt (same every iteration)
- `<TOOLS>` — available tool definitions
- `<USER_MESSAGE>` — contains iteration number, problem statement, permanent memory contents, and visible scratchpad entries
- Then one or more `<ROUND n="N">` blocks, each containing:
  - `<LLM_RESPONSE>` — the Manager's reasoning and decisions (with token counts, duration, stop reason)
  - `<TOOL_CALL name="...">` — the tool invocation with JSON arguments
  - `<TOOL_RESULT name="..." duration="..." status="...">` — the tool's response

The `dispatch_subagent` tool calls are especially important: the JSON arguments contain `system_prompt` and `user_message` (revealing how the Manager designed the sub-agent) and the `TOOL_RESULT` contains the sub-agent's response wrapped in `<subagent_reasoning>`, `<code>`, and `<execution_output>` tags.

**Sub-agent logs** (`iter{N:03d}_{M:02d}_subagent_iter{N}_{idx}.md`): Contains:
- `<SYSTEM_PROMPT>` — whatever the Manager wrote
- `<USER_MESSAGE>` — the task the Manager assigned
- `<LLM_RESPONSE>` — the sub-agent's full response

For code-execution sub-agents with retries, there may be additional log files with `_retry{K}` suffixed agent names.


## Procedure

After reading the problem statement in `problem.yaml`:

### 1. Read the formal evaluation and reference

Read `VERIFICATION.md` to get the formal answer evaluation result. Unlike the vanilla pipeline, there is no automated diagnosis section — just the verdict (correct/incorrect/inconclusive/skipped) and, if applicable, the candidate-vs-truth comparison.

If a reference document exists in `references/` for this problem, read it. The correct answer and key pitfalls apply regardless of which mode produced the run.

Read `ANSWER.md` if it exists. If the formal evaluation was incorrect, compare the submitted answer against the reference to identify specifically what is wrong (wrong formula, wrong coefficients, wrong functional form, missing terms, etc.).

If no `ANSWER.md` exists (the Manager never called `submit_final_answer`), note this as a primary failure — the run did not produce a final answer.

### 2. Read PERMANENT_MEMORY.md — the canonical record

This is the most important file. Read it in full. It is the Manager's accumulated knowledge — the entire output of the research process. Analyze:

**Result progression:**
- What results were established, in what order?
- Were there corrections? (Look for entries containing "CORRECTION", "INCORRECT", "wrong", "fix" — these indicate the Manager discovered and corrected errors.)
- Did the final result match the correct answer (from the reference or formal evaluation)?

**Verification discipline:**
- For each result written to permanent memory, does the entry describe HOW it was verified?
- Was verification done by an independent method (different sub-agent, different approach, computational cross-check)?
- Or was the result written based on a single sub-agent's output without independent verification?
- Were there premature promotions (results written as "verified" that were later corrected)?

**Self-correction chains:**
- Map each correction to the original entry it corrects (by iteration number).
- How many iterations elapsed between the error and its correction?
- What triggered the correction? (A verification sub-agent, a contradiction with another result, or the Manager noticing an inconsistency?)
- Were corrections themselves verified, or did they introduce new errors?

**Memory clarity:**
- Are entries self-contained (suitable for the "amnesiac successor")?
- Do they include context, definitions, notation, the result, and how it was verified?
- Are there vague references ("the result from earlier") instead of specific citations?

### 3. Read SCRATCHPAD.md — the working narrative

Read the full scratchpad (all entries, not just the windowed view the Manager saw). This reveals:

**Strategic evolution:**
- How did the Manager plan its approach in the first iteration?
- Did the strategy evolve across iterations? Look for plan changes, pivots, or escalation.
- Were there explicit "next steps" that were actually followed in subsequent iterations?

**Stagnation detection:**
- Were there repeated similar entries across multiple iterations with no new results?
- Did the Manager note being stuck? Did it change approach when stuck?

**Context loss:**
- Compare the full scratchpad to what was visible (last N entries, as configured in `config.json`). Did important context scroll off?
- Were there scratchpad entries that noted crucial information that was never promoted to permanent memory and subsequently lost?

**System notes:**
- Look for `SYSTEM NOTE: Iteration N failed with error:` entries — these are injected by the scaffold when an iteration crashes. They indicate API failures, premature response endings, or other infrastructure problems.

### 4. Reconstruct the iteration timeline

Using `EVENT_LOG.jsonl` and the log files, build a per-iteration summary:

**For each iteration, determine:**
1. How many rounds (LLM calls) the Manager used
2. How many sub-agents were dispatched, and what each was tasked with (brief summary)
3. Whether code execution was used (`execute_code: true`)
4. Whether sub-agent code failed and required retries (look for `_retry` entries in EVENT_LOG)
5. What was written to memory vs scratchpad (check TOOL_CALL entries for `write_to_permanent_memory` and `write_to_scratchpad`)
6. Whether `end_turn()` or `submit_final_answer()` was called
7. Total tokens consumed (input + output + reasoning)
8. Whether wind-down or hard budget limit was triggered

**Present this as a timeline table**, then flag anomalies:
- Iterations with zero sub-agent dispatches (Manager reasoning alone — risky for complex problems)
- Iterations with zero memory/scratchpad writes (nothing preserved = wasted iteration)
- Iterations that ended via scaffold error rather than `end_turn()`
- Sub-agents that failed all retry attempts
- Unusually high or low token consumption per iteration

### 5. Analyze sub-agent design and usage

This is the core of the Autophysicist analysis. For each sub-agent dispatch (visible in Manager log `<TOOL_CALL name="dispatch_subagent">`):

**Task design quality:**
- Was the `system_prompt` specific enough? (e.g., "You are a quantum error correction expert" with a precise task vs. a vague "investigate this")
- Was the `user_message` well-scoped? (A single, concrete question/task vs. multiple interleaved questions)
- Was all necessary context provided? (The sub-agent cannot see the problem statement, memory, or prior results — did the Manager copy in everything needed?)
- Was `execute_code` set appropriately? (Computational tasks should use it; pure reasoning tasks should not)

**Result utilization:**
- Did the Manager critically evaluate the sub-agent's response, or accept it uncritically?
- When two sub-agents disagreed, how did the Manager resolve the conflict?
- Were sub-agent results that were later found incorrect initially accepted as verified?

**Verification strategy:**
- For important results, did the Manager dispatch a second sub-agent to verify?
- Classify each verification as:
  - **Redundant derivation** — same problem, different method or different sub-agent
  - **Adversarial review** — sub-agent asked explicitly to find errors in a derivation
  - **Computational cross-check** — analytical result checked numerically (or vice versa)
  - **Limiting case analysis** — checking known limits
  - **No verification** — result accepted from a single sub-agent
- Were there circular verifications? (Asking a sub-agent to "check this" and it says "looks correct" without independent computation)

### 6. Investigate scaffold events and infrastructure

Read `EVENT_LOG.jsonl`. Events have `kind: "llm_call"` or `kind: "scaffold"`.

**LLM call entries** (`kind: "llm_call"`):
- `agent: "manager"` — Manager rounds within an iteration. Multiple per iteration. Track `round` number, `input_tokens`, `output_tokens`, `reasoning_tokens`, `duration_s`, `stop_reason`.
- `agent: "subagent_iter{N}_{M}"` — Sub-agent calls. `round: 0` (one-shot). Note `system_prompt_chars` and `user_content_chars` to gauge context size.
- `agent: "subagent_iter{N}_{M}_retry{K}"` — Code execution retry. Indicates the sub-agent's code failed at least once.

**Scaffold events** (`kind: "scaffold"`):
- `event: "iteration_failed"` — Iteration crashed. Read `detail` for the error message.
- `event: "api_retry"` — API call needed retry (transient provider error). Frequent retries suggest unreliable infrastructure.
- `event: "tool_output_truncation"` — A sub-agent's response was truncated before being returned to the Manager. The Manager may have received incomplete information.
- `event: "tool_call_failure_fallback"` — Tool calling broke; LLM fell back to text-only.
- `event: "empty_end_turn_recovery"` — Manager produced empty response; recovery attempted.
- `event: "text_end_turn_recovery"` — Manager ended turn via text instead of tool call; recovery attempted.
- `event: "ready_conclude_recovery"` — Manager signaled readiness to conclude without calling exit tool.
- `event: "context_too_long_fallback"` — Context exceeded provider limit.
- `event: "progress_check"` — Manager was reminded to wrap up after many consecutive tool calls.
- `event: "forced_final_call"` — Manager exhausted max rounds; forced text-only final response.
- `event: "forced_exit_tool_retry"` — Forced final call didn't produce an exit tool; retrying.
- `event: "tool_timeout"` — A tool call (likely code execution) timed out.

**Token patterns from METRICS.md:**
- Read the YAML frontmatter for totals.
- Check the per-iteration table for anomalies: iterations with disproportionately high input tokens (context bloat from large sub-agent responses), zero tool calls (Manager reasoning without action), or max_tokens hit.
- Compare `reasoning_tokens` to `answer_tokens` — a very high ratio may indicate the model spending excessive time in hidden reasoning.

### 7. Examine code execution quality

If the run involved `execute_code=True` sub-agents:

- Read computation scripts in `computations/` to assess quality.
- Check for scripts that have multiple attempts (`_attempt1.py`, `_attempt2.py`, `_attempt3.py`) — what went wrong in earlier attempts?
- Were timeout issues encountered? (Check for `tool_timeout` scaffold events)
- Did successful scripts produce results consistent across different sub-agents?
- Were there numerical precision issues (floating point vs exact rational arithmetic)?

### 8. Assess the final answer pathway

**If the Manager called `submit_final_answer`:**
- In which iteration was it called?
- Was the submitted answer based on the latest permanent memory entries?
- Was there sufficient verification before submission?
- Did the Manager verify the answer satisfied limiting cases and sanity checks?
- Did the answer match the answer_template format from the problem YAML?

**If the Manager never called `submit_final_answer`:**
- Why not? Did the run hit the maximum iteration limit? Did infrastructure failures prevent progress?
- Was there a result in permanent memory that could have been submitted?
- Did the Manager show awareness that it should submit an answer, or did it lose track of the goal?


## Failure Attribution

For every failure or significant inefficiency, trace it to its **root cause** using this framework specific to the Manager + sub-agent architecture:

### Manager failures (strategic)

- **Poor problem decomposition** — Manager tried to solve the whole problem in one sub-agent call instead of breaking it into verifiable pieces.
- **Inadequate sub-agent prompting** — System prompt was too vague, or user message was missing critical context. Read the actual `system_prompt` and `user_message` from the `<TOOL_CALL>` to assess this.
- **Skipped verification** — Result written to permanent memory without independent verification. Especially damaging if the result was wrong.
- **Premature answer submission** — Called `submit_final_answer` before adequate verification.
- **Failed self-correction** — Manager noticed an inconsistency but corrected it incorrectly, or corrected the wrong thing.
- **Strategic drift** — Manager explored tangents not needed for the problem, wasting iterations.
- **Stagnation** — Manager repeated similar approaches across multiple iterations without changing strategy.
- **Poor memory management** — Important results left in scratchpad (and scrolled off), or memory entries written without sufficient context for future iterations.
- **Ignored sub-agent disagreement** — Two sub-agents produced different results, but the Manager picked one without resolving the discrepancy.
- **Excessive self-reasoning** — Manager attempted complex derivations in its own response instead of delegating to a sub-agent (violating "you are the least reliable component").

### Sub-agent failures (execution)

- **Computational error** — Sub-agent's code had bugs (wrong error propagation, incorrect formula, off-by-one, etc.). Check the computation scripts.
- **Analytical error** — Sub-agent's derivation had a mathematical mistake (sign error, dropped factor, unjustified step).
- **Incomplete response** — Sub-agent's output was truncated (check for `tool_output_truncation` events) or missing key parts.
- **Code execution failure** — All retry attempts failed. Check what errors occurred and whether the task was feasible within the sandbox constraints (timeout, available packages).
- **Circular verification** — "Verification" sub-agent simply reviewed the derivation and said "looks correct" without performing an independent check.

### Scaffold / infrastructure failures

- **API failures** — `api_retry` events, `iteration_failed` errors. Check if these caused lost progress.
- **Token budget issues** — Wind-down triggered too early, cutting off productive work. Or hard budget hit, losing unsaved results.
- **Tool call cap** — Manager ran out of tool calls before completing its plan for the iteration.
- **Truncated sub-agent output** — `tool_output_truncation` events causing the Manager to receive incomplete information.
- **Model limitations** — Evidence that the underlying model cannot solve the specific sub-problem (consistent wrong answers across different prompting approaches).


## Insights for Improvement

Based on the above analysis, provide specific, actionable insights in these categories:

- **Manager prompt improvements** — Changes to the Research Manager system prompt (at `src/physics_intern/autophysicist/prompt.md`) that would have prevented the observed failures. E.g., stronger guidance on verification protocol, better instructions for memory management, explicit anti-patterns to avoid.
- **Sub-agent design patterns** — Reusable patterns for effective sub-agent prompts observed in this run (or patterns that should have been used). E.g., always include the problem statement in computational sub-agent prompts, always ask verification sub-agents to produce an independent computation rather than just reviewing.
- **Scaffold adjustments** — Changes to budget parameters, tool call caps, scratchpad window size, or other configuration that would have helped. E.g., if the scratchpad window was too small and critical context was lost, recommend increasing it.
- **Verification protocol** — Specific verification strategies that would have caught the errors observed in this run. E.g., "For this problem, the Manager should have dispatched a computational cross-check sub-agent before writing the analytical result to permanent memory."