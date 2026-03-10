# SciRalph — Task List

Items are ordered by priority within each tier. Tier 1 items are quick fixes that should be done first; Tier 2 items are prompt/logic improvements; Tier 3 is the big agentic computationalist feature; Tier 4 is everything else.

Ordering informed by the audit of 8 workspace runs (March 2026): 7/8 solved correctly, Chandrasekhar failed with 13.6% systematic error. Computation scripts failed execution in ~50% of cases across all runs, mostly due to preventable causes.






---

## Intermediary review checkpoint (before Tier 3)

- **Prompt review** — review all prompts for conciseness, clarity, completeness, and consistency. Do this after the Tier 1 and Tier 2 prompt changes are landed.

---


## Tier 3 — Agentic computationalist (big feature)

The tool-use loop is the single largest improvement planned. It structurally addresses most computation failure patterns by letting the LLM see tracebacks and iterate: deprecated APIs get fixed on the spot, type errors get caught, tolerance issues get diagnosed interactively. Items in this section are ordered by implementation dependency.

- **ToolExecutor + execute_python tool** — new `src/sciralph/tools.py`. Tool definitions in appropriate format (depends on provider?). `ToolExecutor` class dispatches tool calls: `_execute_python` writes code to file, runs via `sandbox.py`, returns stdout+stderr (truncated to 10K chars). Path validation via `_resolve_and_validate` (rejects `..` traversal). Only the computationalist gets tools initially.

- **run_agent_loop in llm.py** — add `run_agent_loop()` alongside existing `call_llm()`. Runs a tool-use loop until stop_reason="end_turn", max_rounds, or token_budget. Returns `AgentResult` dataclass (text, tool_calls log, token counts, rounds, truncated flag). Old `call_llm()` stays for agents that don't need tools.

- **Tool support in base agent** — edit `agents/base.py`. Add `tools` class attribute (default empty). If tools present, `run()` uses `run_agent_loop`; otherwise old one-shot `call_llm` path.

- **Tool-use loop tests** — new `tests/test_tools.py`. Path validation (allowed, rejected, traversal attack). execute_python: writes file, executes, returns output. Output truncation. Max rounds enforcement (mock LLM always requests tools). Token budget enforcement.

- **Agentic computationalist** — refactor `agents/computationalist.py`. Current flow: LLM emits fenced code block → scaffold extracts → executes → separate review call. New flow: LLM calls `execute_python` tool → sees output → iterates on errors → emits final COMPUTATION_LOG entry with VERDICT as text. Remove separate `computationalist_review` call. Update `prompts/computationalist.md` accordingly (tool-use instructions, replace hard asserts with soft-check pattern per Tier 1 item, instruct to self-review after seeing output). Remove `prompts/computationalist_review.md`.

- **Structured timeout errors in tool-use loop** — when `execute_python` hits the sandbox timeout (60s), return a structured error message (e.g. `{"error": "timeout", "limit_seconds": 60}`) rather than treating it as a fatal failure. This lets the agentic computationalist see the timeout and respond by simplifying the algorithm, reducing grid sizes, or switching to analytical approaches. Observed in 2 cases (Perihelion COMP-006, Casimir COMP-006) where timeouts produced permanent INCONCLUSIVE verdicts with no recovery path.

- **Tool-use metrics** — surface tool-use metadata in METRICS.md: per-agent-call (rounds, tool calls, truncated flag), cumulative total tool calls.

- **Agentic computationalist tests** — extend `tests/test_computationalist.py` for tool-use flow. Smoke test: run 3 iterations on a real problem, verify AUDIT_LOG.jsonl has tool_call entries, verify agent iterates on a deliberate SymPy import error.

- **Prompt cleanup for agentic flow** — once the agentic computationalist is working, revisit prompts to remove/rework rules that are now redundant:
  - `prompts/computationalist_review.md` — delete entirely (the agentic loop self-reviews inline).
  - `prompts/computationalist.md` — remove the two-step generate/review instructions (lines about "separate review step", "do not include VERDICT or NOTES", "do NOT predict output in RESULT"). Rewrite OUTPUT FORMAT: the agent now emits VERDICT directly after seeing execution output.
  - `prompts/computationalist.md` — BANNED APIs section becomes nice-to-have rather than essential (the agent sees ImportErrors and can fix them). Consider trimming to a one-line note or moving to a tool description.
  - `prompts/computationalist.md` — soft-check CODE PATTERN: still good practice but less critical since the agent can see crashes and iterate. Consider relaxing from MANDATORY to recommended.
  - `prompts/computationalist.md` — some NUMERICAL PITFALLS items (overflow, stiff ODEs) become less urgent since the agent sees warnings and wrong results interactively. Keep as upfront guidance to save tool-use rounds but deprioritize.

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
## DONE
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