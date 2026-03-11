# SciRalph — Task List

## Completed: Report Recommendations (March 2026)

All 9 recommendations from `reports/20260311_1248_analysis_report.md` implemented:

- **P0-A** — Zero-text watchdog: bails out of tool-only loops after N consecutive zero-text rounds (`zero_text_bailout` config, default 5). Retuned March 2026: raised from 3→5 so checkpoint fires before bailout (see below).
- **P0-B** — Single-target compute: orchestrator prompt enforces one WH/ER per compute task
- **P1-A** — Checkpoint message at round N: nudges computationalist to write text midway (`checkpoint_round` config, default 2). Retuned March 2026: lowered from 5→2 so the nudge fires after the first tool call, before the bailout window opens.
- **P1-B** — Per-computation token alert: fires `computation_token_alert` when input exceeds threshold (default 150K)
- **P2-A** — Stale `[unverified]` label promotion: new `check_stale_unverified_labels()` in validation pipeline. Bugfix (March 2026): now expands WH→ER mapping so promoted hypotheses (WH-001 → ER-001) are matched in synthesis tables
- **P2-B** — WH→ER header promotion: `check_er_promotion_gate()` now promotes WH headers when body uses ER-NNN with VERIFIED backing
- **P2-C** — Fixed `total_computations` counter: only counts `## COMP-` headers, not `## TASK-` sub-entries; uses direct set instead of `max()`. Bugfix (March 2026): `check_id_consistency()` now also filters to COMP-only (was overcounting via `_parse_comp_entries` which includes TASK headers)
- **P2-D** — Critique resolution regex: captures multi-line text up to paragraph boundary, caps at 300 chars at sentence boundary
- **P3** — Skip redundant critic: `_critic_overdue()` returns False when critic already reviewed latest content

Also fixed: forced final call `rounds` now uses actual round count (not `max_rounds`) for correct reporting on early bailout.

Also fixed (March 2026): post-dispatch phantom reference check in engine.py — `check_phantom_references()` now runs after agent dispatch (step 6b), catching phantom COMP/TASK references introduced by agents within the same iteration instead of waiting for the next iteration's orchestrator pass.

## Future work

### Termination gate deadlock — forced compute override

**Problem observed in QHO test run (March 2026):** The orchestrator LLM never emits `task_type: compute`, so COMPUTATION_LOG stays empty. When `requires_numerical: true`, `can_terminate()` blocks every termination attempt. Blocker messages were made actionable (now say "emit task_type: compute, assigned_to: computationalist"), but if the orchestrator still ignores this after N consecutive blocked terminations, the loop wastes iterations.

**Proposed fix:** In `_apply_overrides()`, track consecutive blocked terminations via a counter (`_consecutive_termination_blocks`). After 2 consecutive blocks due to "0 computations", force a compute task (similar to forced critic):

```python
# In _apply_overrides, after P3 (forced critic):
if self._consecutive_termination_blocks >= 2 and self._last_termination_blocker_is_compute:
    return self._make_forced_compute_task()
```

Reset the counter when a computation succeeds. This ensures the system eventually runs the computationalist even when the orchestrator prompt doesn't naturally produce compute tasks.

### Forced final call recovery

**Problem observed across 8 runs (March 2026):** When the computationalist exhausts rounds (via `max_tool_rounds` or `zero_text_bailout`), `run_agent_loop` makes a forced text-only API call (tools removed) asking the model to write its COMP entry. This call fails ~76% of the time, producing ~8 tokens of nothing. The model has spent several rounds in pure tool-use mode (emitting only `tool_use` blocks) and appears unable to switch to text output when tools are suddenly removed. The underlying tool calls mostly executed *correctly* — the agent just ran out of rounds before writing a verdict.

**Possible approaches (not yet decided):**

1. **Retry the forced call** — if the forced call output is < 20 tokens, retry 1-2 more times. Cheap, but the ~76% failure rate suggests the model is stuck in a pattern, not randomly failing. May help in the ~24% marginal cases.

2. **Inject tool output summary before the forced call** — before the text-only call, append a user message that concatenates the stdout from all prior `execute_python` calls as plain text: "Here are the results from your computations: [output1] [output2] ...". This gives the model text context to pattern-match against, bridging the tool-use → text transition. Most promising approach: addresses the root cause (the model's conversation history is all tool_result blocks with no text to continue from).

3. **Pre-populate a structured template** — include a partially-filled COMP template in the forced call prompt ("## COMP-NNN\n**CLAIM:** [fill in]\n**VERDICT:** [fill in]"). Gives the model a text scaffold to complete rather than generating from scratch.

4. **Combine approaches 2+3** — inject the tool output summary AND a template. The model sees: "Your computations produced these results: [...]. Now fill in this template: [...]".

The retuned `checkpoint_round=2` + `zero_text_bailout=5` should reduce how often the forced call is needed in the first place. But when it does fire, fixing it would recover useful output from computations that actually succeeded but whose results were lost.

### Garbled critique resolution text

**Problem observed in 5/8 runs (March 2026):** The orchestrator writes its internal planning fragments into CRITIQUE_LOG resolution fields instead of the actual resolution text. Examples: `"2. Decide on the next action"`, concatenated critique descriptions, `"remain open (LOW priority)"` on a RESOLVED critique.

**Root cause:** The critique resolution regex (P2-D) captures multi-line text up to paragraph boundary, but the orchestrator's output intermixes planning/reasoning text with resolution content. The regex grabs from the wrong section.

**Possible fixes:**
- Post-integration validation: `check_critique_resolution_quality()` in `validation.py` that flags resolution text containing planning keywords ("Decide on the next action", "remain open", numbered step lists).
- Tighter regex anchoring: require resolution text to follow a specific label and reject text matching planning patterns.

### Compressor dual YAML frontmatter

**Problem observed in 2/8 runs (Path integral HO, Chandrasekhar — the two longest runs).** When the compressor archives a file, it preserves the original YAML frontmatter inside the compressed body. The engine then prepends a new, updated frontmatter. Result: two contradictory frontmatter blocks (inner one is stale).

**Fix:** Compressor should strip YAML frontmatter from content before archiving, or the markdown parser should only read the first frontmatter block.

### COMP verdict consistency check

**Problem observed in 1/8 runs (Chandrasekhar):** RESEARCH_STATE cited COMP-013 as VERIFIED, but COMP-013's actual verdict in COMPUTATION_LOG was INCONCLUSIVE. The science was correct (other COMPs backed the claim), but the citation was wrong.

**Fix:** Add `check_comp_verdict_consistency()` to `validation.py` — cross-reference COMP-NNN verdicts cited in RESEARCH_STATE against actual verdicts in COMPUTATION_LOG. Flag any INCONCLUSIVE comp cited as VERIFIED.

### Pre-termination WH→ER enforcement

**Problem observed in 1/8 runs (Chandrasekhar):** 4 verified working hypotheses (WH-012 through WH-015) were never promoted to ER in section headers, despite being treated as established results for termination. The existing `check_er_promotion_gate()` (P2-B) didn't catch these.

**Fix:** Add a pre-termination check: if `can_terminate()` passes but RESEARCH_STATE still has WH-NNN sections with VERIFIED computation verdicts, either auto-promote or block termination until promoted.

### Consecutive stall escalation

**Problem observed across long runs (Path integral HO: 6 stalls, Chandrasekhar: 7 stalls).** The orchestrator blindly retries failed computations without reformulating the task. After 2+ consecutive INCONCLUSIVE results on the same claim, the task should be decomposed into smaller subtasks (compute one formula at a time) rather than retried as-is.

**Proposed approach:** In `_apply_overrides()`, track consecutive INCONCLUSIVE computations. After 2 consecutive stalls, inject a context prefix telling the orchestrator to simplify the computation task (fewer checks per call, provide skeleton code, split multi-formula verification into separate tasks).

### Misc ideas
- Use a more structured output format for agent responses (e.g., JSON with separate fields for "verdict", "summary", "next_steps") to reduce ambiguity and parsing errors.
- Use AgentType enum instead of string literals for agent routing and validation.
- Add a linting step for computation scripts to avoid running obviously broken code (syntax errors, missing imports). This could be a lightweight static check before execution.
- Add a more open ended "brainstorm" task and maybe a dedicated section in the research state for ideas, possible routes, alternatives, etc.

### Compression and context management

- **Priority-based compression** — compressor falls behind on large COMPUTATION_LOG files. Compress the largest file first instead of round-robin. Old VERIFIED computations: archive everything except the verdict line.

- **read_file tool for orchestrator/researcher/critic** — currently these agents get full context via `build_context()`. Adding `read_file` lets them drill into large files or inspect computation scripts. Implement when file sizes regularly approach compression thresholds.

### Multi-model support

- **More models** — add support for more models (GPT, Gemini, Open models via Hugging Face inference providers). Should we define a unified format for tool calls in prompts that works across providers? (e.g. JSON with "tool_name" and "args" fields)

### Problem YAML features

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

### Workspace management

- **Workspace resume** — `--resume <workspace-dir>` to continue a previous run. Skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state (corrupted state, version mismatches).