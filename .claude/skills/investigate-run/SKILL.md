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
| `RESEARCH_GRAPH.json` | Authoritative structured state: hypotheses (with evidence + review), research_questions (with evidence), critiques, failed_approaches with explicit cross-links |
| `RESEARCH_STATE.md` | Rendered snapshot of the research state (from ResearchState, write-only for git/audit) |
| `EVIDENCE_LOG.md` | Rendered snapshot of all evidence and review results (from ResearchState, write-only for git/audit) |
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
- Investigate "strategy" and "situation_assessment" fields and assess the approach
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

**Review integrity:**
- Does every ER have a `review` field with `verdict: "VERIFIED"`?
- Are there hypotheses with `review.verdict: "REFUTED"` that weren't abandoned?
- Are there WHs that were never sent to the reviewer?

**Research questions:**
- Are RQs resolved (`status: resolved`) with `resolved_to` pointing to WH/ER IDs?
- Are there abandoned or stale open RQs?
- Do RQs have `evidence` attached (from researcher/computer agents)?
- Check entity numbering: RQ-NNN → WH-NNN → ER-NNN should share numbers when a question was explored then promoted.

**Critique tracking:**
- Do resolved critiques have `iteration_resolved` set (not null)?
- Are resolution texts specific (not generic "addressed by integration")?
- Are there unresolved HIGH-severity critiques that should have blocked promotion?
- Check for strategy critiques (`target_id: "STRATEGY"`) — were they justified?

**Failed approaches:**
- Are there entries in `failed_approaches`? Do they correspond to abandoned hypotheses?
- Were failures tracked for claims that were retried?

### Step 3: Entity lifecycle report

**This is the most important deliverable.** Reconstruct the full lifecycle of every entity (RQ, WH, ER) from `RESEARCH_GRAPH.json` and `EVENT_LOG.jsonl`. Present this as a structured per-entity timeline so the user can visualize how the research unfolded.

#### How to reconstruct the lifecycle

**Data sources:**
- `RESEARCH_GRAPH.json` — the final snapshot of all entities with their fields (`iteration_created`, `iteration_modified`, `iteration_resolved`, `status`, `evidence`, `review`, `resolved_to`, `depends_on`, `promotion_justification`, etc.)
- `EVENT_LOG.jsonl` — timestamped events that record when mutations happened: `add_research_question`, `add_hypothesis`, `promote_hypothesis`, `abandon_hypothesis`, `resolve_research_question`, `resolve_critique`, `file_critique`, `er_demotion_safety`

**Entity numbering convention:** RQ, WH, and ER share a single counter. When an RQ is explored and the result formulated as a hypothesis, RQ-001 → WH-001 → ER-001. The `from_rq` field on `add_hypothesis` events and the `resolved_to` field on RQs confirm these links.

#### Output format

Present each entity as a mini-timeline using this format:

```
### Entity NNN: [one-line description of the claim]

**RQ-NNN** (iter X)
  Question: "..."
  Evidence: [type] via [agent], confidence=[exact/approximate/partial]
  Scripts: [list if compute]
  → Resolved to WH-NNN (iter Y), reason: "..."

**WH-NNN** (iter Y)
  Statement: "..."
  Evidence: [auto-copied from RQ-NNN / gathered independently]
  Review: [VERIFIED/REFUTED/INCONCLUSIVE] (iter Z)
  Critiques: [CRIT-NNN if any, severity, status]
  → Promoted to ER-NNN (iter W), justification: "..."
  OR → Abandoned (iter W), reason: "..."

**ER-NNN** (iter W)
  Promotion justification: "..."
  Depends on: [list]
  Post-promotion events: [demotions, new critiques]
```

For entities without the full RQ→WH→ER chain (e.g., WH created directly without an RQ, or RQ that was abandoned), show only the relevant stages.

#### What to check for each entity

**For each Research Question (RQ-NNN):**
- When it was created (`iteration_created`) and the question posed
- Current status: `open`, `resolved`, or `abandoned`
- Whether evidence was gathered (check `evidence` field) and by which agent type (research/compute)
- If resolved: what it resolved to (`resolved_to` list of WH/ER IDs), when (`iteration_resolved`), and why (`resolution_reason`)
- If abandoned: was there evidence that was never used?

**For each Working Hypothesis (WH-NNN):**
- When it was created (`iteration_created`) and the claim statement
- Which RQ it originated from (check `resolved_to` on RQ-NNN, or `from_rq` in the `add_hypothesis` event)
- Evidence attached: type (research/compute), method, confidence, scripts (if compute)
- Was evidence auto-copied from an RQ (via `from_rq` on `add_hypothesis`)?
- Dependencies (`depends_on`) — are they satisfied (all dependencies established)?
- Review status: verdict (VERIFIED/REFUTED/INCONCLUSIVE), summary, iteration
- Critiques from deep critic targeting it: severity, status, resolution
- Final outcome: was it promoted to ER, abandoned, or left as WH? When (`iteration_modified`)?

**For each Established Result (ER-NNN):**
- When it was promoted (`iteration_modified`) and the `promotion_justification`
- Which WH it was promoted from (same number)
- The review result that justified promotion (verdict + summary)
- Any post-promotion critiques or demotions (`er_demotion_safety` events in EVENT_LOG.jsonl)
- Dependencies (`depends_on`) — verify the full chain is established

**For critiques (CRIT-NNN):**
- When filed (`iteration_filed`), severity, target entity or STRATEGY
- The argument (what the critic objected to)
- If resolved: when (`iteration_resolved`), resolution text, was it substantive?
- If still active: is it blocking promotion or termination?

**For failed approaches:**
- Map each `failed_approaches` entry to the hypothesis that triggered it
- Note the iteration and reason for failure
- Was the approach retried with a different method?

#### Anomalies to flag

After presenting the per-entity timeline, explicitly flag any of these anomalies:

- **Promotions without VERIFIED review** — ER exists but `review.verdict` is not VERIFIED
- **Unresolved HIGH-severity critiques on established results** — should have blocked promotion
- **Orphaned RQs** — RQ with no evidence and no resolution (never explored)
- **Orphaned WHs** — WH with no evidence or no review (never completed its lifecycle)
- **Broken dependency chains** — ER depends on a non-established entity
- **Entities that cycled** — promoted → demoted → re-promoted (check `er_demotion_safety` events)
- **Evidence gathered but never used** — RQ with evidence but `resolved_to` is empty
- **False refutations** — WH abandoned after REFUTED review, but a later WH/ER has the same or equivalent expression (compare polynomial coefficients or symbolic expressions)
- **Duplicate claims** — multiple WHs with semantically identical statements
- **Stale strategy** — strategy text references abandoned or refuted entities (check `strategy` field in RESEARCH_GRAPH.json against entity statuses)

### Step 4: Investigate scaffold events

Read `EVENT_LOG.jsonl`. Events fall into 4 categories: `call_reliability`, `state_invariants`, `loop_control`, `output_normalization`.

**State mutations (state_invariants category) — the research narrative:**
- `add_hypothesis` — new WH created; check if from_rq and depends_on are noted
- `promote_hypothesis` — WH→ER promotion; check timing relative to VERIFIED review
- `abandon_hypothesis` — check if dependents are noted and handled
- `resolve_critique` — critique resolution; check if resolution text is meaningful
- `file_critique` — new critique filed; check severity and target
- `add_research_question` / `resolve_research_question` — RQ lifecycle tracking
- `append_note` — research notes added by orchestrator

**Validation checks (state_invariants category):**
- `er_demotion_safety` — ER was demoted back to WH due to REFUTED review (1-2 is healthy; 5+ suggests a loop)
- `phantom_labels` — references to non-existent hypotheses
- `stale_unverified_labels` — labels promoted/demoted based on review status
- `critique_resolution_consistency` — resolved critiques that shouldn't be

**Loop control events — process health:**
- `forced_critic` — critic was forced because it hadn't run recently
- `termination_blocked` — orchestrator tried to terminate but was blocked (read the blocker text)
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

**LLM call entries** (`kind: "llm_call"`):
- Track `agent`, `model`, `input_tokens`, `output_tokens`, `duration`, `round` (for agentic calls)
- Use these to compute per-agent token budgets and identify bloated contexts

### Step 5: Trace specific issues

For any issue from Steps 1-4 that lacks sufficient explanation:

- Read the relevant LLM call logs in `logs/` (e.g., `iter003_orchestrator_1.md` for iteration 3)
- Check `EVIDENCE_LOG.md` for evidence entries and review results
- Check `CRITIQUE_LOG.md` for unresolved critiques and their severity
- Look at `METRICS.md` for token usage anomalies (context bloat, max_tokens hits)
- Key failures to look for: empty/truncated outputs, repeated document_approach calls without execute_python, repeating the same task, tool loops cut off by max_rounds or max_tokens, reviewer not receiving focused context

### Step 6: Synthesize

Combine findings into a complete picture:

- **Science quality:** Are the established results (ERs) well-supported by verified evidence? Any gaps in the evidence chain?
- **Entity lifecycle health:** Did entities follow the expected RQ→WH→ER progression? Any orphaned, stuck, or cycling entities?
- **Process efficiency:** How many iterations to completion? What fraction of tokens went to the orchestrator vs productive agents? Any wasted iterations (repeated evidence gathering, redundant reviews)?
- **Failure patterns:** Identify 0-3 key failure patterns (if any) that are not just LLM stochasticity but reflect real issues in the process design.
- For each pattern, provide a recommendation for how to address it.
- If no significant failure patterns are found, state that clearly.
