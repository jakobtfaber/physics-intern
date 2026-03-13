---
name: investigate-run
description: "Investigates SciRalph workspace run by reading the verification report and tracing issues back through project files. Use to what went wrong and coule be improved in the multi-agent research process."
---

# Analyze a SciRalph Run

Given a workspace directory (under `workspaces/` in the SciRalph project), perform a systematic post-mortem analysis of the run and its possible failure modes or inefficiencies.
The user may provide a folder name or path; if ambiguous, list available workspaces and ask.

**Tools:** This is a read-only analysis. Use only `Read`, `Glob`, and `Grep`. Do NOT use `Bash` — all data is in workspace files that can be read directly.

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

The scaffolding log SCAFFOLDING_LOG.jsonl logs all the events and decisions where the scaffolding had to intervene. 
It should help you diagnose the root cause of any process issues flagged in the verification report, especially those tagged as FAILURE or MIXED.

## Procedure

### Step 1: Read the verification report `VERIFICATION.md` in the workspace folder

   - Especially focus on the process audit section and the reported FAILURE or MIXED events.
   - Check for **alerts** (tool_loop_truncated, max_tokens_hit, budget_override)
   - Read the recommendations.
   - Note any relevant problem / event that seems to be coming from a problem in the multi-agent process.

### Step 2: Investigate

   - If an issue raised lacks sufficient explanations to understand what went wrong and what could be fixed, trace it back
   - Look at the scaffolding events in the scaffolding log, and through the workspace files to find the root cause and make it explicit.
   - Key failure to look for : empty/truncated outputs, computational failures, repeating the same task multiple times, tool loops that are cut off by max rounds or max tokens, and any event tagged as FAILURE or MIXED in the process audit.

### Step 3: Synthesize

   - Combine the verification report's assessments with your own cross-referencing to form a complete picture.
   - Identify 0-3 key failure patterns (if any) that emerged in this run, and are not just LLM stochasticity but reflect real issues in the multi-agent process design or execution. 
   - For each provide a recommendation for how to address it in future runs.
   - If no significant failure patterns are found, just state it.