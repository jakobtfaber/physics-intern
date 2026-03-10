# SciRalph — Task List

## Phase 1: Structural Cleanup (Completed March 10 2026)

Cleaned up accumulated technical debt before implementing P1-P8 fixes:
- Shared critique regex constants (`CRIT_ID_RE`, `CRIT_HEADER_RE`, `CRIT_UNRESOLVED_RE`) + helper functions (`extract_resolved_critique_ids`, `recount_critique_metadata`) in `markdown.py`
- Connected `tool_output_limit` config to `ToolExecutor` (was hardcoded)
- Added `min_er_for_completion` config field (was hardcoded `3`)
- Deleted legacy computationalist two-pass path (~100 lines dead code, `computationalist_review.md` prompt)
- Created `Task` dataclass + `TaskType` enum in `task.py` — replaces untyped dicts throughout engine, agents, and tests
- Rewrote `orchestrator.md` (195→80 lines): removed sections the scaffold already enforces (REFUTED handling, stall handling, budget rules, critic scheduling)
- Cleaned up `computationalist.md` (removed duplicate BANNED APIs list, merged verdict rules) and `deep_critic.md` (merged severity sections, fixed "you do not suggest fixes" contradiction)
- Moved all deferred imports to top-level

## Phase 2: P1-P8 Fixes

Findings from the 8 Tier-0 test runs (March 10 2026). Post-Phase-1 status notes are from 8 validation runs (QHO, Ising, Hawking, Berry Phase, Chandrasekhar, Path Integral HO, Perihelion, Renormalisation) after the structural cleanup.

**Implementation priority** (based on post-Phase-1 evidence):
1. **P4 then P2** — highest ROI. P4 (forced partial output) is simpler and always useful; P2 (stall detection) builds on it to prevent retrying failed computations. Together they address 40-50% token waste on hard problems.
2. **P1** — scaffold-level ER gate. Prevents phantom results (Chandrasekhar) and unverified promotions (Berry Phase).
3. **P5** — terminal critic review + problem statement coverage check.
4. **P7, P8** — cosmetic/bookkeeping, implement opportunistically.
5. **P3, P6** — 0 occurrences across 8 post-Phase-1 runs; prompt rewrites may have resolved them. Defer unless they resurface.

### P1 — Enforce ER promotion gate in scaffold

The orchestrator promotes results to Established Results before the verification gate is satisfied (no VERIFIED COMP, no critic pass). Observed in 3/8 runs (Hawking, Ising, Berry Phase). Hawking promoted wrong κ = c⁴/(4GM) to ER before any computation; Ising promoted transfer matrix results before critic ran.

**Fix:** In `engine.py`, after orchestrator integration, scan RESEARCH_STATE for any new ER-NNN entries. If a newly promoted ER does not reference a VERIFIED COMP-NNN that exists in COMPUTATION_LOG, demote it back to Working Hypothesis and inject a warning into the orchestrator's next context. This is a scaffold-level gate, not a prompt change — the prompt already says this but the LLM ignores it for HIGH-confidence claims.

**Post-Phase-1:** Prompt rewrite helped (Ising fixed, Hawking wrong formula no longer promoted) but promotion still bypasses critic gate (Hawking ER-001 promoted before critic reviewed it) and results without any VERIFIED COMP still get promoted (Berry Phase ER-002–005). Chandrasekhar is the worst case: orchestrator integrated researcher-written numerical results (mass-radius relation, M_Ch confirmation) as established claims when zero computations actually succeeded — every integration attempt was truncated. Scaffold gate still needed.

### P2 — Computation stall detection for repeated failures

The orchestrator re-dispatches the same computation task after 2-3 failures without reducing scope or trying an alternative approach. Observed in 3/8 runs (Chandrasekhar ×4 truncations, Perihelion ×3 truncations, Path Integral ×3 truncations). Perihelion wasted 38% of its token budget on 3 identical failed numerical integration attempts.

**Fix:** In `engine.py`, track consecutive INCONCLUSIVE/truncated COMPs targeting the same ER/WH claim (match by ER-NNN/WH-NNN reference in CURRENT_TASK). After 2 failures on the same claim, inject a stall alert into the orchestrator context: "COMPUTATION STALL on [claim]: 2 consecutive failures. You MUST either (a) reduce scope, (b) assign to researcher for alternative approach, or (c) skip and move on." Optionally also add to orchestrator prompt.

**Post-Phase-1:** Worst case is Chandrasekhar: 8 truncations (6 targeting the same numerical ODE integration), 50.4% of total tokens wasted, orchestrator never decomposed the task. Perihelion confirms the same pattern unchanged: 3 attempts at the same intractable numerical integration (float64 precision wall), 42.1% of tokens wasted — virtually identical to the 38% pre-Phase-1. Not observed in simpler problems (QHO, Ising, Hawking, Berry Phase).

### P3 — Scaffold-level verdict validator

The LLM declares VERIFIED on computations that partially or fully failed, by rationalizing discrepancies or widening tolerances. Observed in 3/8 runs: Chandrasekhar COMP-026 changed the theoretical expectation to make the wrong answer look right; Berry Phase COMP-013 declared VERIFIED despite 25-35% systematic error; Renormalisation COMP-021 declared VERIFIED despite printing dimensional inconsistency warnings in script output.

**Fix:** After the computationalist writes a COMP entry, have the scaffold parse the verdict and cross-check against the script output. Heuristics: (1) if the script stdout contains "FAIL", "ERROR", "discrepancy", "inconsistency" or similar warning keywords and the verdict is VERIFIED, flag for review and inject a warning; (2) if the reported relative error exceeds a threshold (e.g. 5%), reject a VERIFIED verdict and force INCONCLUSIVE. This is a safety net — not a replacement for prompt improvements.

**Post-Phase-1:** Not observed in 8 validation runs. Chandrasekhar's pre-Phase-1 COMP-026 false VERIFIED did not recur. Renormalisation's COMP-009 says VERIFIED but is verifying the *existence* of a dimensional inconsistency (technically correct) — a milder semantic variant, not a false verdict on formula correctness. Prompt rewrites may have resolved this; consider deferring unless it resurfaces.

### P4 — Forced partial output on tool-loop truncation

When the computationalist hits the 10-round limit, the COMPUTATION_LOG gets a bare stub ("Agent produced no text output") and the entire iteration is wasted. 11 truncation events across 8 runs (Chandrasekhar ×4, Perihelion ×3, Path Integral ×3, Renormalisation ×1).

**Fix:** In `llm.py` `run_agent_loop`, when hitting max rounds, append a final system message: "You have reached the maximum number of tool-use rounds. You MUST now write your COMP-NNN entry with whatever results you have. Use INCONCLUSIVE if incomplete." Then make one final LLM call (no tools) to extract a partial result. Also consider summarizing prior tool outputs to reduce the quadratic context growth that contributes to truncation.

**Post-Phase-1:** Still occurs. Berry Phase TASK-006 hit max rounds and produced a raw conversational fragment instead of a COMP entry. Chandrasekhar is far worse: 8 truncations total — 4 produced "Agent produced no text output" stubs, 4 produced partial debug notes with no verdict. Combined with P2, this consumed 50.4% of the token budget with zero usable output. Path Integral HO improved (2 down from 3, recovered). Perihelion: 2 truncations with partial stubs, 1 INCONCLUSIVE. Renormalisation: 1 truncation, recovered. **Implement P4 before P2** — forced partial output is simpler and gives P2's stall detector actual verdict data to work with.

### P5 — Terminal critic review before termination

The critic's non-repetition rules mean it rubber-stamps the terminal state when all claims have been promoted to ER. Observed in 3/8 runs: Hawking had zero critic passes total; QHO's heat capacity was never reviewed; Berry Phase's solid angle convention issue escaped because the critic saw zero Working Hypotheses.

**Fix:** In `engine.py`, when the orchestrator emits `terminate` or `synthesize`, check if any ER was promoted since the last critic pass. If so, insert one final critic pass before termination. Adjust the critic prompt to allow reviewing ERs that were promoted since its last pass (currently the non-repetition rule blocks this).

**Post-Phase-1:** Prompt rewrite helped (Hawking now gets 1 critic pass vs zero before). But Berry Phase promoted 4 new ERs after its single critic pass with no terminal review. QHO also terminated without reviewing the final promotion batch. Path Integral HO declared "completed" with 2 of 3 problem sub-objectives unaddressed (discretised path integral, operator formalism) — the terminal check should also verify problem statement coverage, not just critic review.

### P6 — Task-agent type validation

The orchestrator dispatched the researcher (no code execution capability) to "generate plots." The researcher confabulated a completion claim with "machine precision ≤10⁻¹²" despite having no tools. Observed in Ising run.

**Fix:** In `engine.py`, after parsing CURRENT_TASK, validate that `compute` tasks are assigned to the computationalist, not the researcher. If the orchestrator emits a `research` task whose description contains computation keywords (plot, numerical, compute, verify numerically), override to `compute` with a log warning.

**Post-Phase-1:** Not observed in 8 validation runs. Ising plots correctly sent to computationalist (COMP-011). Prompt rewrites may have resolved this; consider deferring unless it resurfaces.

### P7 — Critique log preamble stripping

The critic's first-person reasoning preamble gets appended verbatim to CRITIQUE_LOG.md ("I will examine both claims systematically..."). Observed in 4/8 runs (QHO, Ising, Berry Phase, Renormalisation).

**Fix:** In `workspace.py` (or the critic output handler), strip all text before the first `## CRIT-` heading when appending to the critique log.

**Post-Phase-1:** Still present across all 8 validation runs (single line each in QHO, Ising, Hawking, Berry Phase; 4 instances in Renormalisation). Cosmetic — does not corrupt critique structure.

### P8 — COMP and CRIT ID management in scaffold

COMP IDs have gaps (counter increments per tool-call round, not per finished computation) and CRIT IDs can collide (critic reuses IDs from prior passes). Observed in QHO (COMP gaps), Berry Phase (duplicate CRIT-002).

**Fix:** (a) Assign COMP IDs in the scaffold when writing to COMPUTATION_LOG, not in the LLM's output. Pass the next available ID into the computationalist's context. (b) Same for CRIT IDs: pass `next_crit_id: CRIT-NNN` into the critic's context so it doesn't pick already-used numbers.

**Post-Phase-1:** COMP-ID gaps still present (QHO has only COMP-002, no COMP-001). No duplicate CRITs observed.

---

## Future work

### Compression and context management

- **Priority-based compression** — compressor falls behind on large COMPUTATION_LOG files. Compress the largest file first instead of round-robin. Old VERIFIED computations: archive everything except the verdict line.

- **read_file tool for orchestrator/researcher/critic** — currently these agents get full context via `build_context()`. Adding `read_file` lets them drill into large files or inspect computation scripts. Implement when file sizes regularly approach compression thresholds.

### Multi-model support

- **More models** — add support for more models (GPT, Gemini, Open models via Hugging Face inference providers). Should we define a unified format for tool calls in prompts that works across providers? (e.g. JSON with "tool_name" and "args" fields)

### Problem YAML features

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

### Workspace management

- **Workspace resume** — `--resume <workspace-dir>` to continue a previous run. Skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state (corrupted state, version mismatches).