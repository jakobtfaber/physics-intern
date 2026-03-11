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

Also implemented (March 2026, "token waste" batch):
- Early-exit for computationalist stalls: `zero_text_bailout` + cumulative-text bailout in `run_agent_loop`
- Idempotent phantom stripping + TASK→COMP mapping in validation
- Displaced task logging in engine overrides + orchestrator context
- Automated WH→ER label propagation in `check_er_promotion_gate()` and `check_verified_frontmatter_backfill()`
- Critique WITHDRAWN status for no-flaw/self-retracted critiques
- Task routing safety + `_validate_resolution_note()` for garbled resolution text
- Stall threshold (`stall_threshold` config, default 2): blocks compute dispatch after 2 consecutive non-VERIFIED verdicts on the same claim; zero-output stubs now include CLAIM from task body for correct matching

## Future work

### Forced final call recovery (HIGH PRIORITY)

**Problem observed across 8+ runs (March 2026):** When the computationalist exhausts rounds (via `max_tool_rounds` or `zero_text_bailout`), `run_agent_loop` makes a forced text-only API call (tools removed) asking the model to write its COMP entry. This call fails ~76% of the time, producing ~8 tokens of nothing. The model has spent several rounds in pure tool-use mode (emitting only `tool_use` blocks) and appears unable to switch to text output when tools are suddenly removed. The underlying tool calls mostly executed *correctly* — the agent just ran out of rounds before writing a verdict.

This is now the single biggest source of token waste. The checkpoint + bailout tuning reduces how often it fires, but when it does, the forced call almost always fails — wasting the entire computation session.

**Possible approaches (not yet decided):**

1. **Inject tool output summary before the forced call** — before the text-only call, append a user message that concatenates the stdout from all prior `execute_python` calls as plain text: "Here are the results from your computations: [output1] [output2] ...". This gives the model text context to pattern-match against, bridging the tool-use → text transition. Most promising approach: addresses the root cause (the model's conversation history is all tool_result blocks with no text to continue from).

2. **Pre-populate a structured template** — include a partially-filled COMP template in the forced call prompt ("## COMP-NNN\n**CLAIM:** [fill in]\n**VERDICT:** [fill in]"). Gives the model a text scaffold to complete rather than generating from scratch.

3. **Combine approaches 1+2** — inject the tool output summary AND a template. The model sees: "Your computations produced these results: [...]. Now fill in this template: [...]".

### Termination gate deadlock — forced compute override

**Problem observed in QHO test run (March 2026):** The orchestrator LLM never emits `task_type: compute`, so COMPUTATION_LOG stays empty. When `requires_numerical: true`, `can_terminate()` blocks every termination attempt. Blocker messages were made actionable (now say "emit task_type: compute, assigned_to: computationalist"), but if the orchestrator still ignores this after N consecutive blocked terminations, the loop wastes iterations.

**Proposed fix:** In `_apply_overrides()`, track consecutive blocked terminations via a counter (`_consecutive_termination_blocks`). After 2 consecutive blocks due to "0 computations", force a compute task (similar to forced critic):

```python
# In _apply_overrides, after P3 (forced critic):
if self._consecutive_termination_blocks >= 2 and self._last_termination_blocker_is_compute:
    return self._make_forced_compute_task()
```

Reset the counter when a computation succeeds. This ensures the system eventually runs the computationalist even when the orchestrator prompt doesn't naturally produce compute tasks.

### Compressor dual YAML frontmatter

**Problem observed in 2/8 runs (Path integral HO, Chandrasekhar — the two longest runs).** When the compressor archives a file, it preserves the original YAML frontmatter inside the compressed body. The engine then prepends a new, updated frontmatter. Result: two contradictory frontmatter blocks (inner one is stale).

**Fix:** Compressor should strip YAML frontmatter from content before archiving, or the markdown parser should only read the first frontmatter block.

### COMP verdict consistency check

**Problem observed in 1/8 runs (Chandrasekhar):** RESEARCH_STATE cited COMP-013 as VERIFIED, but COMP-013's actual verdict in COMPUTATION_LOG was INCONCLUSIVE. The science was correct (other COMPs backed the claim), but the citation was wrong.

**Fix:** Add `check_comp_verdict_consistency()` to `validation.py` — cross-reference COMP-NNN verdicts cited in RESEARCH_STATE against actual verdicts in COMPUTATION_LOG. Flag any INCONCLUSIVE comp cited as VERIFIED.

### Pre-termination WH→ER enforcement

**Problem observed in 1/8 runs (Chandrasekhar):** 4 verified working hypotheses (WH-012 through WH-015) were never promoted to ER in section headers, despite being treated as established results for termination.

**Partially addressed:** `check_er_promotion_gate()` now auto-promotes WH headers with VERIFIED backing (P2-B), and `check_verified_frontmatter_backfill()` propagates WH→ER IDs. Still observed in post-fix Path Integral run (WH-002 in ER section header). May need a pre-termination gate in `can_terminate()` to catch stragglers.

### Orchestrator inline synthesis — eliminate termination tail

**Problem observed in 6/8 runs (March 2026 batch):** The end-of-run flow wastes 1–2 iterations on synthesis and termination bookkeeping. Typical pattern: orchestrator sees completion → emits `synthesize` → researcher writes a mostly-mechanical reformatting of existing ERs into PROPOSED_CHANGES.md (~5–15K tokens) → orchestrator integrates + emits `terminate`. The researcher adds little value here since all results are already established and verified.

**Proposed fix:** When the normal completion banner fires (`_completion_analysis`), instruct the orchestrator to write a brief `## Synthesis` section directly into RESEARCH_STATE.md during its integration pass, then emit `task_type: terminate` immediately. Remove `synthesize` from the suggested task types in the normal completion path. Keep `task_type: synthesize` only for the budget-aware path (≤3 iterations remaining with partial results), where the researcher needs to do real narrative framing under constraints.

This collapses the 2-iteration termination tail into 1 iteration. The orchestrator already has full context (all ERs, computations, resolved critiques) and routinely writes RESEARCH_STATE.md content during integration — asking it to add a short synthesis paragraph is well within scope.

### Stall escalation — task decomposition (enhancement)

**Core problem addressed:** `stall_threshold=2` blocks compute dispatch after 2 consecutive failures on the same claim (P5 override). Zero-output stubs include CLAIM from task body for correct matching. The stall_block redirects to a generic RESEARCH task.

**Possible enhancement:** Instead of a generic "Alternative Approach Needed" research task, inject specific guidance: decompose multi-formula verification into single-formula tasks, provide skeleton code, or suggest analytical alternatives. Low priority — the current blocking approach prevents runaway retries, which was the main token waste issue.

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