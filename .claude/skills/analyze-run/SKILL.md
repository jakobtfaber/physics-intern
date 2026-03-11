---
name: analyze-run
description: "Analyzes one or several SciRalph workspace run by reading the verification report and tracing issues back through project files. Use to understand what went well, what went wrong, and why."
---

# Analyze a SciRalph Run

Given a workspace directory (under `workspaces/` in the SciRalph project), perform a systematic post-mortem analysis of the run.
The user may provide a folder name or path; if ambiguous, list available workspaces and ask.
If the user specifies multiple runs, analyze each one with a dedicated subagent equipped with the same skill, and then synthesize a comparative report.

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

Read `VERIFICATION.md` in the workspace folder. Extract from both sections:

**From the scientific verification:**
- `verdict` and `confidence` from YAML frontmatter
- Per-result assessments (ER-NNN: VALID / INVALID / UNCERTAIN)
- Chain coherence (YES / PARTIAL / NO)
- Unresolved concerns — these are the starting points for tracing scientific issues

**From the process audit:**
- `process_verdict` from YAML frontmatter
- Process events — note each EVENT-NNN with its tag (SUCCESS / FAILURE / MIXED)
- Token efficiency assessment
- Recommendations

### Step 2: Trace scientific issues through workspace files

For any results marked INVALID or UNCERTAIN, or for unresolved concerns flagged in the scientific verification, trace them back through the workspace files:

- Read `RESEARCH_STATE.md` — check the relevant ER/WH entries, their claimed verification status, and referenced task IDs
- Read `COMPUTATION_LOG.md` — verify that claimed computations exist and check their verdicts
- Read `CRITIQUE_LOG.md` — check if related critiques were properly resolved

**Critical check**: Compare task IDs referenced in RESEARCH_STATE against those actually present in COMPUTATION_LOG. Flag any **phantom references** — task IDs claimed in RESEARCH_STATE that don't exist in the computation log.

### Step 3: Validate process audit claims

Cross-reference the process audit's events and claims against the actual workspace files:

- Read `METRICS.md` — verify token counts, alert counts, iteration numbers cited in the process audit
- Check for **alerts** (tool_loop_truncated, max_tokens_hit, budget_override) that the process audit may have missed
- If the process audit flags computation failures, spot-check scripts in the `computations/` subfolder

### Step 4: Check for leftover state

- If `PROPOSED_CHANGES.md` exists, it means the orchestrator never integrated the last researcher output. Note what was proposed but not integrated.
- Check if any critiques marked RESOLVED lack evidence of actual resolution (rubber-stamp critiques).

### Step 5: Synthesize

Combine the verification report's assessments with your own cross-referencing to form a complete picture. Note any discrepancies between the verification report's claims and what you found in the workspace files.

## Output Format

Present the analysis as a structured report:

```
## Run Analysis: [problem name]

**Workspace:** `workspaces/[folder]/`
**Scientific verdict:** [verdict] (confidence: [confidence])
**Process verdict:** [process_verdict]
**Iterations:** [N] | **Status:** [completed/budget-exceeded/...]

### Scientific Assessment
[Summary of the scientific verification: which results are valid, any issues found, chain coherence.
For INVALID/UNCERTAIN results, include the traced root cause from workspace files.]

### Process Assessment
[Summary of the process audit: what the multi-agent system did well, where it failed.
Include key process events (successes, failures, mixed) with brief descriptions.]

### Issues Found
For each issue (from either section or from your own cross-referencing):
- **[Issue title]** — [1-2 sentence description]
  - Evidence: [specific file + section reference]
  - Root cause: [what went wrong in the pipeline]

### Verification Gaps
- [Results claimed but not computationally verified]
- [Phantom task references]
- [Critiques resolved without evidence]
- [Discrepancies between verification report and workspace files]

### Metrics Summary
- Iterations: [N], LLM calls: [N], Tokens: [input/output]
- Tool calls: [N], Max token hits: [N]
- Alerts: [list]

### Recommendations
[Combine the process audit's recommendations with any additional insights from your analysis.
Focus on actionable improvements for the scaffolding system.]
```

## Key Failure Patterns to Watch For

1. **Phantom verification** — RESEARCH_STATE claims "VERIFIED by computation (TASK-XXX)" but TASK-XXX doesn't exist in COMPUTATION_LOG. Root cause: orchestrator hallucinated task references or integrated unverified claims.

2. **Silent computation failure** — COMPUTATION_LOG entry exists but verdict is INCONCLUSIVE with "Agent produced no text output". Root cause: computationalist hit max rounds or token limit before completing.

3. **Rubber-stamp critiques** — CRITIQUE_LOG entries marked RESOLVED at a specific iteration, but no actual work was done at that iteration to address the critique. Root cause: orchestrator marked critiques resolved during integration without actual fixes.

4. **Budget exhaustion without convergence** — max iterations reached with `research_status` not `COMPLETE`, PROPOSED_CHANGES.md still present, budget_override alerts in metrics. The problem was too hard or the agents went in circles.

5. **Computational desert** — Very few COMPUTATION_LOG entries relative to the number of established results. The research relied on LLM reasoning without numerical sanity checks.

6. **Critique-resolution mismatch** — Critique targets a specific mathematical error, resolution claims "addressed" but the actual formula in RESEARCH_STATE is unchanged.

7. **Premature termination** — System stops with unexecuted tasks and remaining budget. The process audit may flag this as a FAILURE event. Check if `CURRENT_TASK.md` contains an unexecuted task and whether budget was actually exhausted.
