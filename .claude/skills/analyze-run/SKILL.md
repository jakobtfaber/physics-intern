---
name: analyze-run
description: "Analyzes one or several SciRalph workspace run by reading the verification report and tracing issues back through project files. Use to understand what went well, what went wrong, and why."
---

# Analyze a SciRalph Run

Given a workspace directory (under `workspaces/` in the SciRalph project), perform a systematic post-mortem analysis of the run. 
The user may provide a folder name or path; if ambiguous, list available workspaces and ask.
If the user specifies multiple runs, analyze each one with a dedicated subagent equipped with the same skill, and then synthesize a comparative report.


## Procedure

### Step 1: Read the verification report

Read `VERIFICATION.md` in the workspace folder. Extract:
- **Verdict** and **confidence** from YAML frontmatter
- **Per-result assessments** (ER-NNN: VALID / INVALID / UNCERTAIN)
- **Chain coherence** (YES / PARTIAL / NO)
- **Unresolved concerns** — these are the starting points for tracing

### Step 2: Read the research state

Read `RESEARCH_STATE.md`. Extract from frontmatter:
- `iteration` count, `research_status`
- `total_established_results`, `total_working_hypotheses`, `total_dead_ends`
- `unresolved_high_critiques`, `unresolved_medium_critiques`
- `resolved_critiques` list

Scan the body for each Established Result (ER-NNN) and Working Hypothesis (WH-NNN). Note which ones claim computational verification and which task IDs they reference.

### Step 3: Cross-reference the computation log

Read `COMPUTATION_LOG.md`. For each computation entry:
- Note the task/comp ID, the claim being verified, and the **verdict** (VERIFIED / REFUTED / INCONCLUSIVE)
- Check if "Agent produced no text output" or similar failure markers appear

**Critical check**: Compare task IDs referenced in RESEARCH_STATE against those actually present in COMPUTATION_LOG. Flag any **phantom references** — task IDs claimed in RESEARCH_STATE that don't exist in the computation log. This is the most common failure mode.

### Step 4: Review the critique log

Read `CRITIQUE_LOG.md`. For each critique (CRIT-NNN):
- Note severity, target (which ER/WH), status (RESOLVED / OPEN)
- Check if resolution claims are substantiated — does the resolution reference actual computation or rederivation?

Flag critiques marked RESOLVED without evidence of actual resolution.

### Step 5: Check metrics and alerts

Read `METRICS.md`. Look for:
- **Alerts section** — tool_loop_truncated, max_tokens_hit, budget_override events
- **Max Tokens Hit** column — iterations where the agent was cut off
- **Total iterations** vs problem complexity — did the run hit the budget limit?
- **Tool calls** count — very low tool calls for a computationalist-heavy problem suggests failed computations

### Step 6: Spot-check computation scripts (if issues found)

If Steps 3-5 revealed computation failures, look in the `computations/` subfolder. Read a couple of `tool_exec_*.py` scripts to check:
- Did the code run to completion?
- Were there import errors, timeouts, or empty outputs?
- Does the code actually test the claimed result?

### Step 7: Check for leftover PROPOSED_CHANGES.md

If `PROPOSED_CHANGES.md` exists, it means the orchestrator never integrated the last researcher output. Note what was proposed but not integrated.

## Output Format

Present the analysis as a structured report:

```
## Run Analysis: [problem name]

**Workspace:** `workspaces/[folder]/`
**Verdict:** [verdict] (confidence: [confidence])
**Iterations:** [N] | **Status:** [completed/budget-exceeded/...]

### What Went Well
- [Bullet points: correctly established results, successful computations, well-resolved critiques]

### Issues Found
For each issue:
- **[Issue title]** — [1-2 sentence description]
  - Evidence: [specific file + line/section reference]
  - Root cause: [what went wrong in the pipeline]

### Verification Gaps
- [Results claimed but not computationally verified]
- [Phantom task references]
- [Critiques resolved without evidence]

### Metrics Summary
- Iterations: [N], LLM calls: [N], Tokens: [input/output]
- Tool calls: [N], Max token hits: [N]
- Alerts: [list]

### Assessment
[2-3 sentence overall assessment: Was the science correct? Was the process efficient?
What would improve the next run on a similar problem?]
```

## Key Failure Patterns to Watch For

1. **Phantom verification** — RESEARCH_STATE claims "VERIFIED by computation (TASK-XXX)" but TASK-XXX doesn't exist in COMPUTATION_LOG. Root cause: orchestrator hallucinated task references or integrated unverified claims.

2. **Silent computation failure** — COMPUTATION_LOG entry exists but verdict is INCONCLUSIVE with "Agent produced no text output". Root cause: computationalist hit max rounds or token limit before completing.

3. **Rubber-stamp critiques** — CRITIQUE_LOG entries marked RESOLVED at a specific iteration, but no actual work was done at that iteration to address the critique. Root cause: orchestrator marked critiques resolved during integration without actual fixes.

4. **Budget exhaustion without convergence** — 25 iterations with `research_status` not `COMPLETE`, PROPOSED_CHANGES.md still present, budget_override alerts in metrics. The problem was too hard or the agents went in circles.

5. **Computational desert** — Very few COMPUTATION_LOG entries relative to the number of established results. The research relied on LLM reasoning without numerical sanity checks.

6. **Critique-resolution mismatch** — Critique targets a specific mathematical error, resolution claims "addressed" but the actual formula in RESEARCH_STATE is unchanged.
