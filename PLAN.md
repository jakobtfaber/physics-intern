# SciRalph — Task List

Items are ordered by priority within each tier. Tier 1 items are quick fixes that should be done first; Tier 2 items are prompt/logic improvements; Tier 3 is the big agentic computationalist feature; Tier 4 is everything else.

Ordering informed by the audit of 8 workspace runs (March 2026): 7/8 solved correctly, Chandrasekhar failed with 13.6% systematic error. Computation scripts failed execution in ~50% of cases across all runs, mostly due to preventable causes.

---

## Tier 4 — Future work

### Compression and context management

- **Priority-based compression** — compressor falls behind on large COMPUTATION_LOG files. Compress the largest file first instead of round-robin. Old VERIFIED computations: archive everything except the verdict line.

- **read_file tool for orchestrator/researcher/critic** — currently these agents get full context via `build_context()`. Adding `read_file` lets them drill into large files or inspect computation scripts. Implement when file sizes regularly approach compression thresholds.

### Multi-model support

- **More models** — add support for more models (GPT, Gemini, Open models via Hugging Face inference providers). Should we define a unified format for tool calls in prompts that works across providers? (e.g. JSON with "tool_name" and "args" fields)

### Problem YAML features

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

### Workspace management

- **Workspace resume** — `--resume <workspace-dir>` to continue a previous run. Skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state (corrupted state, version mismatches).

---
# We recently implemented some changes that are the Tier 3 part in the PLAN.md new run. So if you look in the workspaces folder, you can see the results of the eight runs that we made after implementing those Tier 3 changes. It looks rather good. Two problems were only considered as partially valid by the verifier.

### Tier 1 — Quick fixes (do first, all independent of tool-use work)

- **Fix soft-check pattern in computationalist prompt** — the current ASSERTION RULES pattern wraps individual checks in try/except but ends with `assert all_passed`, which still crashes the script and causes the scaffold to record EXECUTION FAILED. The LLM consistently follows this pattern — the pattern itself is the bug, not LLM non-compliance. Fix: replace the final `assert all_passed` with always-exit-0 and a structured summary (e.g. `CHECKS: 8/10 PASSED`). Teach the review phase (or agentic computationalist) to read the summary rather than relying on exit code. *Audit evidence: hard asserts killed scripts in 5/8 runs (QHO COMP-018, Chandrasekhar COMP-015, Path Integral COMP-014, Renormalization COMP-014, Perihelion COMP-001/006).*

- **Available packages documentation and blocklist** — the computationalist prompt (or the `execute_python` tool description) should list available packages and known version caveats. Confirmed broken APIs: `scipy.misc.derivative` (removed in SciPy 2.0 — use `scipy.integrate` or manual finite differences), `numpy.trapz` (renamed to `numpy.trapezoid` in NumPy 2.0), `numpy.math` (removed in NumPy 2.0 — use `math` stdlib). List these as banned with their replacements. *Audit evidence: deprecated APIs crashed scripts in 3/8 runs (QHO, Ising, Path Integral), wasting 4+ iterations total.*

- **`plt.show()` suppression in sandbox** — set `MPLBACKEND=Agg` in `sandbox.py`'s subprocess environment. This prevents any display attempt in the headless sandbox. Also add "never call `plt.show()`, use `plt.savefig()` then `plt.close()`" to the computationalist prompt as belt-and-suspenders. *Audit evidence: Ising COMP-011 timed out (60s) on `plt.show()` — the PNG was already saved, the script just couldn't exit.*

- **Compression threshold gap** — currently the engine only alerts at 1x threshold and force-compresses at 2x. Add a 1.5x trigger in `_check_compression()` that dispatches a normal (non-forced) compressor run. *Audit evidence: Path Integral COMPUTATION_LOG hit 41,164 chars (103% of 40k threshold), alerts fired at iterations 16-18, but no compression occurred because 2x = 80k was never reached.*

- **METRICS table completeness** — several runs had incomplete per-iteration tables (missing early rows). Investigate whether `to_markdown()` is overwriting rather than accumulating, or if there's a rolling-window bug. Fix so the full iteration history is always present. *Audit evidence: Berry Phase missing iterations 1-4, Renormalization missing iterations 1-9.*

- **RESEARCH_STATE status bookkeeping on terminate** — when the orchestrator emits a `terminate` task, the engine (or orchestrator) should update the RESEARCH_STATE frontmatter `status` field to `completed`. Currently left as `in_progress`. *Audit evidence: Ising showed `status: in_progress` after a clean terminate.*
---

## Tier 2 — Prompt and logic improvements

- **Tolerance calibration, quantity validation, and tolerance-widening detection** — three related failure patterns in computations. (1) Overly strict assertions cause INCONCLUSIVE verdicts on correct physics. Add explicit tolerance rules to `prompts/computationalist.md`: default `rtol=1e-6` for numerical comparisons, never use exact equality for floats, use `np.isclose`/`np.allclose`. (2) Assertions targeting the wrong quantity — e.g. checking a leading-order approximation against a full expression. Instruct: verify both sides represent the same quantity at the same order before comparing. (3) **Tolerance widening**: scripts must never silently relax their own acceptance thresholds. If the default tolerance fails, the verdict must be INCONCLUSIVE with the actual discrepancy reported — not VERIFIED with a widened gate. Add a prompt rule: "If your checks fail at 5% tolerance, do NOT widen to 15% and declare success. Report the actual error and let the orchestrator decide." *Audit evidence: Chandrasekhar COMP-018 silently widened tolerance from 5% to 15% (`assert abs(systematic_error) <= 15.0`), letting a 13.6% error pass as acceptable. This was the specific mechanism that locked in the wrong answer.*

- **Critique resolution quality gate** — HIGH critiques citing quantitative discrepancies should only be resolvable when a VERIFIED computation confirms the corrected value within tolerance. Add to the orchestrator prompt: "A HIGH critique citing a numerical error requires a VERIFIED computation with <5% agreement before marking RESOLVED. Improving from 78% wrong to 14% wrong is not resolution." *Audit evidence: Chandrasekhar CRIT-005 correctly flagged a 78% mass error. When the answer improved to 14% off (still wrong), the orchestrator closed it. The critic never re-examined whether the "fix" was physically legitimate.*

- **Numerical pitfalls checklist in computationalist prompt** — the existing 3-tier verification strategy covers methodology but not algorithm selection. Add a brief "common pitfalls" section to `prompts/computationalist.md`: (1) use log-space arithmetic for products of many exponentials to avoid float64 underflow; (2) prefer `scipy.integrate.solve_ivp` over hand-rolled integrators for long-time or stiff problems; (3) when testing a tensor/vector identity numerically, preserve the full structure — don't reduce to scalars; (4) match the fitting model to the expected functional form before fitting; (5) for oscillatory integrands, consider analytical evaluation or contour rotation rather than brute-force numerical quadrature.

- **Computation failure stall detection** — the current stall detection (engine backstop for "research appears complete" + orchestrator prompt rules for resolve loops) does not catch repeated failed computations targeting the same claim. Observed pattern: the system can spend 3–4 consecutive iterations retrying the same computation, fixing surface symptoms each time without resolving the underlying error. New: track in `engine.py` (or orchestrator context) consecutive INCONCLUSIVE/REFUTED COMPs targeting the same ER/WH. After 2–3 failures on the same claim, inject a stall alert into the orchestrator context so it can escalate (send to researcher for alternative derivation, skip and move on, or request critic review of the underlying claim).

- **Root-cause context for computation retries** — when the computationalist retries a failed computation, it tends to fix surface symptoms (e.g. an ImportError) without diagnosing the underlying bug (e.g. a sign error in the physics). Fix: when the orchestrator emits a `compute` task targeting a claim with prior failed COMPs, include the previous script's key output (actual vs expected values, error messages) in CURRENT_TASK.md with an explicit instruction: "Diagnose the root cause from the output below before writing new code." Partially addressed by the agentic computationalist (which sees output inline), but the diagnosis instruction should be explicit.

- **Scaffold-level budget enforcement** — budget-aware termination exists in the orchestrator prompt (≤3 remaining → synthesize) and `_completion_analysis` injects a BUDGET SYNTHESIS REQUIRED banner. But the LLM sometimes ignores it when deep in a verification cycle. Consider scaffold-level enforcement: if `budget_remaining <= 1` and the orchestrator emits anything other than `synthesize`/`terminate`, override the task to `synthesize` with a warning. This is a safety net, not a replacement for the prompt instruction.