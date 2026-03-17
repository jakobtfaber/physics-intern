---
name: investigate-run
description: "Investigates SciRalph workspace run by reading the verification report, formal research graph, and event log, then tracing issues back through project files. Use to understand what went wrong and could be improved in the multi-agent research process."
---

# Analyze a SciRalph Run

Given a workspace directory (under `workspaces/` in the SciRalph project), perform a systematic post-mortem analysis of the run and its possible failure modes or inefficiencies.
The user may provide a folder name or path; if ambiguous, list available workspaces and ask.

## Workspace Structure

A workspace contains these key files:

| File | Purpose |
|---|---|
| `VERIFICATION.md` | Independent verification report (science + process audit) |
| `RESEARCH_GRAPH.json` | Authoritative structured state: hypotheses, research_questions, computations, critiques, failed_approaches with explicit cross-links |
| `RESEARCH_STATE.md` | Rendered snapshot of the research state (from ResearchState, write-only for git/audit) |
| `COMPUTATION_LOG.md` | Rendered snapshot of all computations (from ResearchState, write-only for git/audit) |
| `CRITIQUE_LOG.md` | Rendered snapshot of all critiques (from ResearchState, write-only for git/audit) |
| `EVENT_LOG.jsonl` | Structured scaffold events (4 categories) and LLM call metadata |
| `METRICS.md` | Per-iteration token counts and alerts |
| `ANSWER.md` | Final formatted answer (produced by formatter agent on successful termination) |
| `logs/` | Per-iteration LLM call logs (system prompt, user content, response) |

**Important:** `RESEARCH_GRAPH.json` is the authoritative source of truth. The `.md` files (RESEARCH_STATE, COMPUTATION_LOG, CRITIQUE_LOG) are rendered snapshots — useful for human reading but derived from the JSON.

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

- If the science is INVALID, your main goal will be to trace back to the core reason of the failure.
- If the science is correct, move to the process audit section.
- Focus on the process audit section and reported FAILURE or MIXED events.
- Read the recommendations.
- Note any problem or event that seems to come from a flaw in the multi-agent process.

### Step 2: Examine the formal research state

Read `RESEARCH_GRAPH.json` (this is the authoritative state, not the markdown files):

**Hypothesis integrity:**
- Do any hypotheses have status `abandoned`? Are they recorded in `failed_approaches`?
- Check `depends_on` fields — are dependency chains satisfied for established results?
- Do promoted ERs have `promotion_justification` filled in?

**Research questions:**
- Are RQs resolved (`status: resolved`) with `resolved_to` pointing to WH/ER IDs?
- Are there abandoned or stale open RQs?
- Check entity numbering: RQ-NNN → WH-NNN → ER-NNN should share numbers when a question was explored then promoted.

**Computation link quality:**
- Does every computation have a non-empty `target_hypothesis`?
- Are there `zero_output: true` entries? These indicate agent bailouts.
- Check `kind` field distribution: `explore` vs `verify` vs `research_verify` vs `research_explore`.
- Are VERIFIED computations targeting the right WH/ER IDs?

**Critique tracking:**
- Do resolved critiques have `iteration_resolved` set (not null)?
- Are resolution texts specific (not generic "addressed by integration")?
- Are there unresolved HIGH-severity critiques that should have blocked promotion?

**Failed approaches:**
- Are there entries in `failed_approaches`? Do they correspond to REFUTED/INCONCLUSIVE computations?
- Were failures tracked for claims that were retried?

### Step 3: Investigate scaffold events

Read `EVENT_LOG.jsonl`. Events fall into 4 categories: `call_reliability`, `state_invariants`, `loop_control`, `output_normalization`.

**State mutations (state_invariants category) — the research narrative:**
- `add_hypothesis` — new WH created; check if from_rq and depends_on are noted
- `promote_hypothesis` — WH→ER promotion; check timing relative to VERIFIED computations
- `abandon_hypothesis` — check if dependents are noted and handled
- `resolve_critique` — critique resolution; check if resolution text is meaningful
- `add_research_question` / `resolve_research_question` — RQ lifecycle tracking

**Validation checks (state_invariants category):**
- `er_demotion_safety` — ER was demoted back to WH (1-2 is healthy; 5+ suggests a compute loop)
- `phantom_labels` — references to non-existent hypotheses
- `stale_unverified_labels` — ERs without verification evidence
- `critique_resolution_consistency` — resolved critiques that shouldn't be

**Loop control events — process health:**
- `forced_critic` — critic was forced because it hadn't run recently
- `termination_blocked` — orchestrator tried to terminate but was blocked (read blockers)
- `dispatch_failure` — agent dispatch failed (transient error)
- `compute_enrichment` — prior failure context injected into compute task
- `compute_verdict_failed` — non-VERIFIED verdict with attempt counter (watch for high counts = stall)
- `explore_result_suppressed` — explore result was dropped (zero_output or missing target)
- `agent_failure_max_tokens` — agent hit token limit
- `agent_failure_max_rounds` — agent exhausted tool-use rounds
- `max_tokens_no_retry` — one-shot agent hit max_tokens
- `no_critiques_filed` — critic found nothing to critique (healthy if late in run)
- `status_field_exit` — run ended via status field check

**Call reliability events — LLM interaction health:**
- `api_retry` — API call needed retry (transient errors)
- `tool_call_failure_fallback` — tool-calling broke, fell back to text-only
- `empty_end_turn_recovery` — agent produced empty response, recovery attempted
- `empty_end_turn_fallthrough` — recovery failed, forced final call
- `progress_check` — agent was reminded to wrap up after many consecutive execute_python calls
- `forced_final_call` — agent exhausted rounds, forced text-only final response
- `forced_final_call_failed` — even the forced final call errored
- `tool_timeout` — tool execution timed out
- `tool_output_truncation` — tool output was truncated

**Output normalization:**
- `empty_response_stub` — agent produced no output, stub computation created

**LLM call entries** (`event: llm_call`):
- Track `agent`, `model`, `input_tokens`, `output_tokens`, `duration`, `round` (for agentic calls)
- Use these to compute per-agent token budgets and identify bloated contexts

### Step 4: Trace specific issues

For any issue from Steps 1-3 that lacks sufficient explanation:

- Read the relevant LLM call logs in `logs/` (e.g., `iter003_orchestrator_1.md` for iteration 3)
- Check `COMPUTATION_LOG.md` for the specific computation entries involved
- Check `CRITIQUE_LOG.md` for unresolved critiques and their severity
- Look at `METRICS.md` for token usage anomalies (context bloat, max_tokens hits)
- Key failures to look for: empty/truncated outputs, computational failures, repeating the same task, tool loops cut off by max_rounds or max_tokens

### Step 5: Synthesize

Combine findings into a complete picture:

- **Science quality:** Are the established results (ERs) well-supported by verified computations? Any gaps in the derivation chain?
- **Process efficiency:** How many iterations to completion? What fraction of tokens went to the orchestrator vs productive agents? Any wasted iterations (stalled verify loops, redundant explorations)?
- **Failure patterns:** Identify 0-3 key failure patterns (if any) that are not just LLM stochasticity but reflect real issues in the process design.
- For each pattern, provide a recommendation for how to address it.
- If no significant failure patterns are found, state that clearly.
