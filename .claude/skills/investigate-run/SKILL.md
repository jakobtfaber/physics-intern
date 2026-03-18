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
| `RESEARCH_GRAPH.json` | Authoritative structured state: hypotheses (with evidence + verification), research_questions (with evidence), critiques, failed_approaches with explicit cross-links |
| `RESEARCH_STATE.md` | Rendered snapshot of the research state (from ResearchState, write-only for git/audit) |
| `EVIDENCE_LOG.md` | Rendered snapshot of all evidence and verification results (from ResearchState, write-only for git/audit) |
| `CRITIQUE_LOG.md` | Rendered snapshot of all critiques (from ResearchState, write-only for git/audit) |
| `EVENT_LOG.jsonl` | Structured scaffold events (4 categories) and LLM call metadata |
| `METRICS.md` | Per-iteration token counts and alerts |
| `ANSWER.md` | Final formatted answer (produced by formatter agent on successful termination) |
| `logs/` | Per-iteration LLM call logs (system prompt, user content, response) |

**Important:** `RESEARCH_GRAPH.json` is the authoritative source of truth. The `.md` files (RESEARCH_STATE, EVIDENCE_LOG, CRITIQUE_LOG) are rendered snapshots — useful for human reading but derived from the JSON.

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

**Initial strategy**:
- Investigate "strategy" and "short_term_plan" fields and assess the approach
- Check "research_notes" for intermediate insights and decisions

**Hypothesis integrity:**
- Do any hypotheses have status `abandoned`? Are they recorded in `failed_approaches`?
- Check `depends_on` fields — are dependency chains satisfied for established results?
- Do promoted ERs have `promotion_justification` filled in?

**Evidence quality:**
- Does every hypothesis with WORKING or ESTABLISHED status have an `evidence` field?
- Check evidence `type` (research vs compute) — is the right agent type used for each claim?
- For compute evidence: does `approach` document the methodology? Are `scripts` listed?
- For research evidence: is `reasoning` substantive?
- Check `confidence` values (exact/approximate/partial) — are they realistic?

**Verification integrity:**
- Does every ER have a `verification` field with `verdict: "VERIFIED"`?
- Are there hypotheses with `verification.verdict: "REFUTED"` that weren't abandoned?
- Check `verification.critiques` — are HIGH-severity critiques addressed?
- Are there WHs that were never sent to the verifier?

**Research questions:**
- Are RQs resolved (`status: resolved`) with `resolved_to` pointing to WH/ER IDs?
- Are there abandoned or stale open RQs?
- Do RQs have `evidence` attached (from researcher/computer agents)?
- Check entity numbering: RQ-NNN → WH-NNN → ER-NNN should share numbers when a question was explored then promoted.

**Critique tracking:**
- Do resolved critiques have `iteration_resolved` set (not null)?
- Are resolution texts specific (not generic "addressed by integration")?
- Are there unresolved HIGH-severity critiques that should have blocked promotion?

**Failed approaches:**
- Are there entries in `failed_approaches`? Do they correspond to abandoned hypotheses?
- Were failures tracked for claims that were retried?

### Step 3: Entity lifecycle report

Reconstruct the full lifecycle of every entity (RQ, WH, ER) from `RESEARCH_GRAPH.json` and `EVENT_LOG.jsonl`. Present this as a structured timeline so the user can visualize how the research unfolded.

**For each Research Question (RQ-NNN):**
- When it was created (`iteration_created`) and the question posed
- Current status: `open`, `resolved`, or `abandoned`
- Whether evidence was gathered (check `evidence` field) and by which agent type (research/compute)
- If resolved: what it resolved to (`resolved_to` list of WH/ER IDs), when (`iteration_resolved`), and why (`resolution_reason`)

**For each Working Hypothesis (WH-NNN):**
- When it was created (`iteration_created`) and the claim statement
- Which RQ it originated from (check if RQ-NNN exists with the same number)
- Evidence attached: type (research/compute), method, confidence, scripts (if compute)
- Dependencies (`depends_on`) — are they satisfied (all dependencies established)?
- Verification status: verdict (VERIFIED/REFUTED/INCONCLUSIVE), critiques filed, iteration
- Critiques from deep critic targeting it: severity, status, resolution
- Final outcome: was it promoted to ER, abandoned, or left as WH? When (`iteration_modified`)?

**For each Established Result (ER-NNN):**
- When it was promoted (`iteration_modified`) and the `promotion_justification`
- Which WH it was promoted from (same number)
- The verification result that justified promotion (verdict + reasoning)
- Any post-promotion critiques or demotions (`er_demotion_safety` events)
- Dependencies (`depends_on`) — verify the full chain is established

**For failed approaches:**
- Map each `failed_approaches` entry to the hypothesis that triggered it
- Note the iteration and reason for failure

**Present the report as a per-entity timeline**, ordered by entity number. For each entity, show the chain of events chronologically (created → evidence gathered → verified → critiqued → promoted/abandoned). Flag any anomalies:
- Promotions without VERIFIED verification
- Unresolved HIGH-severity critiques on established results
- Orphaned entities (RQs that never led to a WH, WHs with no evidence)
- Broken dependency chains (ER depends on a non-established entity)
- Entities that cycled (promoted → demoted → re-promoted)
- Evidence gathered but never used (RQ with evidence but never resolved to WH)

### Step 4: Investigate scaffold events

Read `EVENT_LOG.jsonl`. Events fall into 4 categories: `call_reliability`, `state_invariants`, `loop_control`, `output_normalization`.

**State mutations (state_invariants category) — the research narrative:**
- `add_hypothesis` — new WH created; check if from_rq and depends_on are noted
- `promote_hypothesis` — WH→ER promotion; check timing relative to VERIFIED verification
- `abandon_hypothesis` — check if dependents are noted and handled
- `resolve_critique` — critique resolution; check if resolution text is meaningful
- `add_research_question` / `resolve_research_question` — RQ lifecycle tracking
- `append_note` — research notes added by orchestrator

**Validation checks (state_invariants category):**
- `er_demotion_safety` — ER was demoted back to WH due to REFUTED verification (1-2 is healthy; 5+ suggests a loop)
- `phantom_labels` — references to non-existent hypotheses
- `stale_unverified_labels` — labels promoted/demoted based on verification status
- `critique_resolution_consistency` — resolved critiques that shouldn't be

**Loop control events — process health:**
- `forced_critic` — critic was forced because it hadn't run recently
- `termination_blocked` — orchestrator tried to terminate but was blocked (read blockers)
- `dispatch_failure` — agent dispatch failed (transient error)
- `compute_enrichment` — prior failure context injected into compute task
- `explore_result_suppressed` — evidence result was dropped (no evidence or missing target)
- `agent_failure_max_tokens` — agent hit token limit
- `agent_failure_max_rounds` — agent exhausted tool-use rounds
- `max_tokens_no_retry` — one-shot agent hit max_tokens
- `no_critiques_filed` — critic found nothing to critique (healthy if late in run)
- `status_field_exit` — run ended via status field check

**Call reliability events — LLM interaction health:**
- `api_retry` — API call needed retry (transient errors)
- `tool_call_failure_fallback` — tool-calling broke, fell back to text-only
- `empty_end_turn_recovery` — agent produced empty response, recovery attempted
- `progress_check` — agent was reminded to wrap up after many consecutive execute_python calls
- `forced_final_call` — agent exhausted rounds, forced text-only final response

**LLM call entries** (`event: llm_call`):
- Track `agent`, `model`, `input_tokens`, `output_tokens`, `duration`, `round` (for agentic calls)
- Use these to compute per-agent token budgets and identify bloated contexts

### Step 5: Trace specific issues

For any issue from Steps 1-3 that lacks sufficient explanation:

- Read the relevant LLM call logs in `logs/` (e.g., `iter003_orchestrator_1.md` for iteration 3)
- Check `EVIDENCE_LOG.md` for evidence entries and verification results
- Check `CRITIQUE_LOG.md` for unresolved critiques and their severity
- Look at `METRICS.md` for token usage anomalies (context bloat, max_tokens hits)
- Key failures to look for: empty/truncated outputs, repeated document_approach calls without execute_python, repeating the same task, tool loops cut off by max_rounds or max_tokens, verifier not receiving focused context

### Step 6: Synthesize

Combine findings into a complete picture:

- **Science quality:** Are the established results (ERs) well-supported by verified evidence? Any gaps in the evidence chain?
- **Process efficiency:** How many iterations to completion? What fraction of tokens went to the orchestrator vs productive agents? Any wasted iterations (repeated evidence gathering, redundant verifications)?
- **Failure patterns:** Identify 0-3 key failure patterns (if any) that are not just LLM stochasticity but reflect real issues in the process design.
- For each pattern, provide a recommendation for how to address it.
- If no significant failure patterns are found, state that clearly.
