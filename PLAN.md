# SciRalph — Task List

## Completed: Report Recommendations (March 2026)

All 9 recommendations from `reports/20260311_1248_analysis_report.md` implemented:

- **P0-A** — Zero-text watchdog: bails out of tool-only loops after N consecutive zero-text rounds (`zero_text_bailout` config, default 3)
- **P0-B** — Single-target compute: orchestrator prompt enforces one WH/ER per compute task
- **P1-A** — Checkpoint message at round N: nudges computationalist to write text midway (`checkpoint_round` config, default 5)
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