# SciRalph — Task List

## Prompt engineering

- **Prompt review** — review all prompts for conciseness, clarity, completeness, and consistency.

## Mode models

- **More models** — add support for more models (GPT, Gemini, Open models via Hugging Face inference providers). Should we define a unified format for tool calls in prompts that works across providers? (e.g. JSON with "tool_name" and "args" fields)

## Tool support and agentic computationalist

- **ToolExecutor + execute_python tool** — new `src/sciralph/tools.py`. Tool definitions in appropriate format (depends on provider?). `ToolExecutor` class dispatches tool calls: `_execute_python` writes code to file, runs via `sandbox.py`, returns stdout+stderr (truncated to 10K chars). Path validation via `_resolve_and_validate` (rejects `..` traversal). Only the computationalist gets tools initially.

- **run_agent_loop in llm.py** — add `run_agent_loop()` alongside existing `call_llm()`. Runs a tool-use loop until stop_reason="end_turn", max_rounds, or token_budget. Returns `AgentResult` dataclass (text, tool_calls log, token counts, rounds, truncated flag). Old `call_llm()` stays for agents that don't need tools.

- **Tool support in base agent** — edit `agents/base.py`. Add `tools` class attribute (default empty). If tools present, `run()` uses `run_agent_loop`; otherwise old one-shot `call_llm` path.

- **Tool-use loop tests** — new `tests/test_tools.py`. Path validation (allowed, rejected, traversal attack). execute_python: writes file, executes, returns output. Output truncation. Max rounds enforcement (mock LLM always requests tools). Token budget enforcement.

- **Agentic computationalist** — refactor `agents/computationalist.py`. Current flow: LLM emits fenced code block → scaffold extracts → executes → separate review call. New flow: LLM calls `execute_python` tool → sees output → iterates on errors → emits final COMPUTATION_LOG entry with VERDICT as text. Remove separate `computationalist_review` call. Update `prompts/computationalist.md` accordingly (tool-use instructions, keep assertion discipline, instruct to self-review after seeing output). Remove `prompts/computationalist_review.md`.

- **Tool-use metrics** — surface tool-use metadata in METRICS.md: per-agent-call (rounds, tool calls, truncated flag), cumulative total tool calls.

- **Agentic computationalist tests** — extend `tests/test_computationalist.py` for tool-use flow. Smoke test: run 3 iterations on a real problem, verify AUDIT_LOG.jsonl has tool_call entries, verify agent iterates on a deliberate SymPy import error.

- **Tolerance calibration in computationalist prompt** — recurring failure pattern: overly strict assertions cause INCONCLUSIVE verdicts on correct physics (4 cases across QHO and Casimir workspaces). Update `prompts/computationalist.md` to include explicit tolerance rules: default `rtol=1e-6` for numerical comparisons, never use exact equality for floats, if an assertion fails close to machine precision then relax the tolerance and re-run rather than declaring failure. Also instruct to use `np.isclose`/`np.allclose` instead of raw comparison operators.

- **Structured timeout errors in tool-use loop** — when `execute_python` hits the sandbox timeout (60s), return a structured error message (e.g. `{"error": "timeout", "limit_seconds": 60}`) rather than treating it as a fatal failure. This lets the agentic computationalist see the timeout and respond by simplifying the algorithm, reducing grid sizes, or switching to analytical approaches. Observed in 2 cases (Perihelion COMP-006, Casimir COMP-006) where timeouts produced permanent INCONCLUSIVE verdicts with no recovery path.

- **Available packages documentation** — the computationalist prompt (or the `execute_python` tool description) should list available packages and known version caveats. Observed failure: QHO COMP-007 used `scipy.misc.derivative` which was removed in SciPy 2.0, causing a permanent ImportError. At minimum, instruct the computationalist to wrap imports in try/except and fall back to alternatives (e.g. `numpy.gradient` or manual finite differences instead of `scipy.misc.derivative`).

- **Priority-based compression** — compressor falls behind on large COMPUTATION_LOG files (40K+ chars observed). Compress the largest file first instead of round-robin. Old VERIFIED computations: archive everything except the verdict line. Consider lowering threshold or triggering compression mid-iteration at 1.5x threshold.

- **read_file tool for orchestrator/researcher/critic** — currently these agents get full context via `build_context()`. Adding `read_file` lets them drill into large files or inspect computation scripts. Implement when file sizes regularly approach compression thresholds.

## Problem YAML features

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

## Workspace management

- **Workspace resume** — `--resume <workspace-dir>` to continue a previous run. Skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state (corrupted state, version mismatches).
