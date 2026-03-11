# SciRalph — Task List

## Phase 1



## Phase 2: Engine Hardening — DONE (March 2026)

Restructured engine.py's scattered ad-hoc fixes into three testable layers (258 tests, up from 172):

- **Layer A** — Iteration contract rewrite (`engine.py`): every orchestrator-emitted task gets dispatched; `_apply_overrides()` consolidates all overrides (budget, stale loop, forced critic, REFUTED recompute, stall blocking) into a single priority chain; termination goes through `can_terminate()` gate
- **Layer B** — Post-integration validation (`validation.py`): 5 check functions (phantom references, ER promotion gate, phantom labels, task-agent routing, ID consistency) run after every orchestrator pass; violations injected into next orchestrator context
- **Layer C** — Agent loop resilience: forced text-only final call on `max_rounds` exhaustion (no more empty stubs); stall detection with threshold=2 blocks re-dispatch of stuck claims
- Cosmetic: critique preamble strip in `critic.py`; `requires_numerical` added to all 10 problem YAMLs

---

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