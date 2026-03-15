---
name: investigate-run
description: "Investigates SciRalph workspace run by reading the verification report, formal research graph, and event log, then tracing issues back through project files. Use to understand what went wrong and could be improved in the multi-agent research process."
---

# Analyze a SciRalph Run

Given a workspace directory (under `workspaces/` in the SciRalph project), perform a systematic post-mortem analysis of the run and its possible failure modes or inefficiencies.
The user may provide a folder name or path; if ambiguous, list available workspaces and ask.

**Tools:** This is a read-only analysis. Use only `Read`, `Glob`, and `Grep`. Do NOT use `Bash` — all data is in workspace files that can be read directly.

## Workspace Structure

A workspace contains these key files:

| File | Purpose |
|---|---|
| `VERIFICATION.md` | Independent verification report (science + process audit) |
| `RESEARCH_GRAPH.json` | Formal research state: hypotheses, computations, critiques, failed_approaches with explicit cross-links |
| `RESEARCH_STATE.md` | Rendered Markdown view of hypotheses and derivations |
| `COMPUTATION_LOG.md` | All computation entries with CLAIM/VERDICT/METHOD/RESULT |
| `CRITIQUE_LOG.md` | Active and resolved critiques |
| `EVENT_LOG.jsonl` | Structured scaffold events (compensations, overrides, tool mutations) |
| `METRICS.md` | Per-iteration token counts and alerts |
| `ANSWER.md` | Final formatted answer |
| `logs/` | Per-iteration LLM call logs (system prompt, user content, response) |

## Verification Report Structure

The verification report (`VERIFICATION.md`) is produced by two independent LLM calls and contains two sections:

1. **Scientific Verification** (first section) — assesses correctness of mathematical/physical results
   - Frontmatter fields: `verdict` (VALID/INVALID/MIXED), `confidence` (HIGH/MEDIUM/LOW)
   - Per-result assessments (ER-NNN: VALID / INVALID / UNCERTAIN)
   - Chain coherence (YES / PARTIAL / NO)
   - Unresolved concerns

2. **Process Audit** (second section, after `---` separator) — assesses effectiveness of the multi-agent process
   - Frontmatter field: `process_verdict` (EFFECTIVE / PARTIALLY_EFFECTIVE / INEFFECTIVE)
   - Process summary, token efficiency analysis
   - Process events (EVENT-NNN with SUCCESS / FAILURE / MIXED tags)
   - Recommendations for future runs

## Procedure

### Step 1: Read the verification report

Read `VERIFICATION.md` in the workspace folder.

- Focus on the process audit section and reported FAILURE or MIXED events.
- Check for **alerts** (tool_loop_truncated, max_tokens_hit, budget_override)
- Read the recommendations.
- Note any problem or event that seems to come from a flaw in the multi-agent process.

### Step 2: Examine the formal research state

Read `RESEARCH_GRAPH.json` and cross-reference with `RESEARCH_STATE.md`:

**Hypothesis integrity:**
- Are all WH/ER sections in RESEARCH_STATE.md reflected in the graph?
- Do any hypotheses have status `abandoned`? Are they also in Dead Ends in the Markdown?
- Is the `supporting_comps` list on each hypothesis complete (every VERIFIED comp targeting it)?

**Computation link quality:**
- Does every computation in the graph have a non-empty `target_hypothesis`?
- Are any targets stale (pointing to a WH-NNN that was promoted to ER-NNN)?
- Are there phantom TASK-* stubs (entries with empty claim/target from orchestrator bailouts)?

**Critique tracking:**
- Do resolved critiques have `iteration_resolved` set (not null)?
- Are resolution texts specific (not generic "addressed by integration")?

**Failed approaches:**
- Are there entries in `failed_approaches`? Do they correspond to REFUTED/INCONCLUSIVE computations?
- Were failures tracked for claims that were retried?

### Step 3: Investigate scaffold events

Read `EVENT_LOG.jsonl` and look for:

**Orchestrator tool usage:**
- Count `orchestrator_tool_mutations` events — how many show `mutations=True` (tool path) vs any legacy fallback?
- If the orchestrator used tools: did it over-call `set_next_task` (multiple times per iteration)?

**Promotion gate behavior:**
- Count `er_promotion_gate` events — how many times did the gate fire? (1-2 is healthy; 5+ suggests the old fight-loop problem)
- Were there any silent demotions that the orchestrator then re-promoted?

**Failure enrichment:**
- Did `p6_enrichment` fire? If there were REFUTED/INCONCLUSIVE computations, was the retry enriched with prior failure context?

**Bailout events:**
- Count `progress_check`, `forced_final_call` events
- Did any bailout produce phantom computation entries?

**Other events:**
- `problem_statement_enforced` — how frequently? (should decrease with tool-based orchestrator)
- `termination_blocked` — was termination correctly gated?
- `task_agent_routing` violations

### Step 4: Trace specific issues

For any issue from Steps 1-3 that lacks sufficient explanation:

- Read the relevant LLM call logs in `logs/` (e.g., `iter003_orchestrator_1.md` for iteration 3)
- Check `COMPUTATION_LOG.md` for the specific COMP entries involved
- Look at `METRICS.md` for token usage anomalies (context bloat, max_tokens hits)
- Key failures to look for: empty/truncated outputs, computational failures, repeating the same task, tool loops cut off by max_rounds or max_tokens

### Step 5: Synthesize

Combine findings into a complete picture:

- **Architecture health:** Did the structured state (RESEARCH_GRAPH.json) stay consistent with the Markdown files? Were tool mutations working correctly?
- **Process efficiency:** How many iterations to completion? What fraction of tokens went to the orchestrator vs productive agents? Any wasted iterations?
- **Failure patterns:** Identify 0-3 key failure patterns (if any) that are not just LLM stochasticity but reflect real issues in the process design.
- For each pattern, provide a recommendation for how to address it.
- If no significant failure patterns are found, state that clearly.
