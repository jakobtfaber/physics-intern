---
name: investigate-run
description: "Investigates SciRalph workspace run. Use to understand what went wrong and could be improved in the multi-agent research process."
allowed-tools: Read, Grep
model: opus
---

# Analyze a SciRalph Run

Read README.md to understand how the multi-agent research process works.

Given a workspace directory (under `workspaces/` in the SciRalph project), perform a systematic post-mortem analysis of the run and its failure modes and inefficiencies. The user may provide a folder name or path; if ambiguous, list available workspaces and ask.

Check `references/` in the project root for a reference document matching the problem. These files describe what a correct answer looks like and what a typical successful run looks like for known problems.

**Key deliverables:**
- A list of specific failures, which agent or part of the framework didn't work, when, and why.
- A list of insights for improvements in the process design, improved agents (prompt, tools), and scaffold adjustments.

## Workspace Structure

A workspace contains these key files:

| File                  | Purpose                                                                                                                                                         |
|-----------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `problem.yaml`        | The scientific research problem to be solved, the answer template and possibly the true answer (not visible to the agents)                                      |
| `ANSWER.md`           | Final formatted answer (produced by formatter agent on successful termination)                                                                                  |
| `VERIFICATION.md`     | Independent verification report (science + process audit)                                                                                                       |
| `RESEARCH_GRAPH.json` | Authoritative structured state: hypotheses (with evidence + review), research_questions (with evidence), critiques, failed_approaches with explicit cross-links |
| `EVENT_LOG.jsonl`     | Structured scaffold events (4 categories) and LLM call metadata                                                                                                 |
| `RESEARCH_STATE.md`   | Rendered snapshot of the research state (from ResearchState, write-only for git/audit)                                                                          |
| `EVIDENCE_LOG.md`     | Rendered snapshot of all evidence and review results (from ResearchState, write-only for git/audit)                                                             |
| `CRITIQUE_LOG.md`     | Rendered snapshot of all critiques (from ResearchState, write-only for git/audit)                                                                               |
| `logs/`               | Per-iteration LLM call logs (XML-tagged: SYSTEM_PROMPT, USER_MESSAGE, ROUND, LLM_RESPONSE, TOOL_CALL, TOOL_RESULT)                                              |
| `METRICS.md`          | Per-iteration token counts and alerts                                                                                                                           |

**Important:** `RESEARCH_GRAPH.json` is the authoritative source of truth. The `.md` files (RESEARCH_STATE, EVIDENCE_LOG, CRITIQUE_LOG) are rendered snapshots — useful for human reading but derived from the JSON.

### Verification Report Structure

The verification report (`VERIFICATION.md`) is produced by two independent LLM calls and contains two sections:

1. **Scientific Verification** — assesses correctness of mathematical/physical results

2. **Process Audit** — assesses effectiveness of the multi-agent process, but you should go beyond this first preliminary audit.


## Procedure

After reading the problem statement problem.yaml

### Examine the formal research state

Read `RESEARCH_GRAPH.json` (this is the authoritative state, not the markdown files):

**Strategy**:
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

### Entity lifecycle report

Reconstruct the full lifecycle of every entity (RQ, WH, ER) from `RESEARCH_GRAPH.json` and `EVENT_LOG.jsonl`. Present this as a structured per-entity timeline so the user can visualize how the research unfolded.

**Data sources:**
- `RESEARCH_GRAPH.json` — the final snapshot of all entities with their fields (`iteration_created`, `iteration_modified`, `iteration_resolved`, `status`, `evidence`, `review`, `resolved_to`, `depends_on`, `promotion_justification`, etc.)
- `EVENT_LOG.jsonl` — timestamped events that record when mutations happened: `add_research_question`, `add_hypothesis`, `promote_hypothesis`, `abandon_hypothesis`, `abandon_research_question`, `resolve_critique`, `file_critique`, `er_demotion_safety`

**Entity numbering convention:** RQ, WH, and ER share a single counter. When an RQ is explored and the result formulated as a hypothesis, RQ-001 → WH-001 → ER-001. The `from_rq` field on `add_hypothesis` events and the `resolved_to` field on RQs confirm these links.

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

### Investigate scaffold events

Read `EVENT_LOG.jsonl`. Events fall into 4 categories: `call_reliability`, `state_invariants`, `loop_control`, `output_normalization`.

**State mutations (state_invariants category) — the research narrative:**
- `add_hypothesis` — new WH created; check if from_rq and depends_on are noted
- `promote_hypothesis` — WH→ER promotion; check timing relative to VERIFIED review
- `abandon_hypothesis` — check if dependents are noted and handled
- `resolve_critique` — critique resolution; check if resolution text is meaningful
- `file_critique` — new critique filed; check severity and target
- `add_research_question` / `abandon_research_question` — RQ lifecycle tracking
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

### Trace specific issues

For any issue from Steps 1–4 that lacks sufficient explanation:

- Read the relevant LLM call logs in `logs/` (e.g., `iter003_01_orchestrator.md` for iteration 3) — logs use ALL_CAPS XML tags (`<SYSTEM_PROMPT>`, `<ROUND>`, `<LLM_RESPONSE>`, `<TOOL_CALL>`, `<TOOL_RESULT>`, `<USER_MESSAGE>`) to separate log structure from prompt content
- Check `EVIDENCE_LOG.md` for evidence entries and review results
- Check `CRITIQUE_LOG.md` for unresolved critiques and their severity
- Look at `METRICS.md` for token usage anomalies (context bloat, max_tokens hits)
- Key failures to look for: empty/truncated outputs, repeated document_approach calls without execute_python, repeating the same task, tool loops cut off by max_rounds or max_tokens, reviewer not receiving focused context

## Failure attribution

This is the core diagnostic deliverable. For every failure or significant inefficiency identified , trace it to its **root cause** by answering: **which agent made the mistake, and why?**

Work through each of these questions systematically. Not all will apply to every run — skip those that are clearly irrelevant, but be thorough on the ones that matter.

#### Did the surveyor set the right context?

- Did the background notes contain accurate and relevant information?
- Did the surveyor accidentally anchor the system by including candidate answers, code, or numerical predictions?
- Were important conventions, definitions, or pitfalls flagged?
- Were important sanity checks reported that the reviewer should have used? Were some missed that could have caught a critical error?

#### Was the strategy sound?

- Did the planner/orchestrator formulate a reasonable initial strategy?
- Was the strategy updated after refutations, new evidence, or critiques?
- Did a stale or wrong strategy cause the system to pursue a dead end?
- Was the strategy too vague or too prescriptive?

#### Did the researcher/computer produce correct work?

- If the researcher made an error: what was the specific mathematical/physical mistake? Was it a conceptual error (wrong model), algebraic error (dropped term), or convention confusion (e.g., Δ̄ treatment)?
- If the computer made an error: was the code logically wrong, did it time out, did it enumerate incompletely, or did it produce correct intermediate results that were incorrectly assembled?
- Was the agent overwhelmed by the complexity of the task? (Signs: max_tokens hit, reasoning loops, incomplete output, multiple failed attempts at the same derivation)
- Was the task well scoped, or too big for one agent call? (Signs of too-big: multiple independent sub-goals in one step, long reasoning chains, multiple tool calls, or a mix of analytical and computational work that should have been split)
- Was the task appropriately routed? (e.g., was a pure analytical problem sent to the computer, or a computational problem sent to the researcher?)
- Occasional errors might occur, if they are caught by the reviewer.

#### Did the reviewer catch what it should have?

- If the science is wrong: did the reviewer verify a wrong result (false verification)? Read the reviewer's reasoning — did it actually check the critical steps, or did it rubber-stamp?
- Did the reviewer reject a correct result (false refutation)? What was the reviewer's stated reason? Was the reasoning plausible but wrong, or clearly flawed?
- Did the reviewer receive adequate context to make its determination? (Check the focused context: was the evidence complete, were scripts and outputs included?)
- It might happen that a computer/researcher makes a mistake, but the reviewer correctly REFUTES it.

#### Was the orchestrator effective?

- Did the orchestrator correctly interpret evidence results and review verdicts?
- Did it handle the management of the research process well (creating RQ/WH, asking reviews, promoting to ER, critique resolution, abandonment decisions)?
- Did it get "lost" — dispatching redundant tasks, creating unnecessary entities, failing to promote or abandon when it should have?
- Did it respond appropriately to refutations (update strategy, re-dispatch with different approach) or did it repeat the same failing approach?
- Did it waste iterations on housekeeping (excessive note-taking, redundant strategy updates) instead of productive work?

#### Did the critic add value or cause harm?

- Were filed critiques legitimate and actionable? Was the number of critiques reasonable (not zero, but not excessive)?
- Did a false-alarm critique send the system on an unnecessary detour?
- Did the critic miss something important that it should have caught?
- Did the critique trigger a revision of the strategy, or did the orchestrator ignore it?

#### Did the scaffold help or hinder?

- Were there scaffold-level events (forced_critic, termination_blocked, er_demotion_safety) that were appropriate and protective, or did they create unnecessary overhead?
- Did agent failures (max_tokens, max_rounds, empty responses) reflect a scaffold configuration issue or an inherently difficult task?

## Insights for improvement

Based on the above analysis, list specific insights for improving the multi-agent research process. These can be categorized into:
- **Process design** — changes to the overall workflow, entity lifecycle, or agent interactions
- **Agent improvements** — changes to prompts, tools, or reasoning approaches for specific agents
- **Scaffold adjustments** — changes to the orchestration logic, event handling, or budget management