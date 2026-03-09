# SciRalph — Task List


## Done

- **Convention locking in orchestrator**.

- **Budget-aware termination** — orchestrator context now shows "iteration X of Y (Z remaining)". When ≤3 iterations remain and ≥1 Established Result exists, a BUDGET SYNTHESIS REQUIRED banner forces synthesis regardless of WH/critique state. Engine recognizes `partially_complete` status for graceful early exit. Prompt updated with BUDGET-AWARE TERMINATION rules.



## To Do

- **Critic self-retraction gate** — the critic sometimes files a critique then immediately argues against it in Phase 2. Add a gate: if Phase 2 concludes the objection is unfounded, do not emit the critique. Alternatively, instruct the critic to draft Phase 2 mentally before committing to filing.

- **CRITIQUE_LOG cleanup on resolution** — when moving a critique to Resolved, remove the entire block (header + body) from Active, not just the CRIT-NNN header. Currently leaves orphaned Phase 1/Phase 2 body text.

- **Check CLI arguments**

- **config.yaml** — add a YAML config file with command-line override. Include model selection, thresholds, timeouts, audit logs settings, etc.

- **Prompt review** — review all prompts for clarity, completeness, and consistency.

- **More models** — add support for more models (GPT, Gemini, Open models via Hugging Face inference providers).

- **ToolExecutor + execute_python tool** — new `src/sciralph/tools.py`. Tool definitions in Anthropic API format. `ToolExecutor` class dispatches tool calls: `_execute_python` writes code to file, runs via `sandbox.py`, returns stdout+stderr (truncated to 10K chars). Path validation via `_resolve_and_validate` (rejects `..` traversal). Only the computationalist gets tools initially.

- **run_agent_loop in llm.py** — add `run_agent_loop()` alongside existing `call_llm()`. Runs a tool-use loop until stop_reason="end_turn", max_rounds, or token_budget. Returns `AgentResult` dataclass (text, tool_calls log, token counts, rounds, truncated flag). Old `call_llm()` stays for agents that don't need tools.

- **Tool support in base agent** — edit `agents/base.py`. Add `tools` class attribute (default empty). If tools present, `run()` uses `run_agent_loop`; otherwise old one-shot `call_llm` path.

- **Tool-use loop tests** — new `tests/test_tools.py`. Path validation (allowed, rejected, traversal attack). execute_python: writes file, executes, returns output. Output truncation. Max rounds enforcement (mock LLM always requests tools). Token budget enforcement.

- **Agentic computationalist** — refactor `agents/computationalist.py`. Current flow: LLM emits fenced code block → scaffold extracts → executes → separate review call. New flow: LLM calls `execute_python` tool → sees output → iterates on errors → emits final COMPUTATION_LOG entry with VERDICT as text. Remove separate `computationalist_review` call. Update `prompts/computationalist.md` accordingly (tool-use instructions, keep assertion discipline, instruct to self-review after seeing output). Remove `prompts/computationalist_review.md`.

- **Tool-use metrics** — surface tool-use metadata in METRICS.md: per-agent-call (rounds, tool calls, truncated flag), cumulative total tool calls.

- **Agentic computationalist tests** — extend `tests/test_computationalist.py` for tool-use flow. Smoke test: run 3 iterations on a real problem, verify AUDIT_LOG.jsonl has tool_call entries, verify agent iterates on a deliberate SymPy import error.

- **Priority-based compression** — compressor falls behind on large COMPUTATION_LOG files (40K+ chars observed). Compress the largest file first instead of round-robin. Old VERIFIED computations: archive everything except the verdict line. Consider lowering threshold or triggering compression mid-iteration at 1.5x threshold.

- **read_file tool for orchestrator/researcher/critic** — currently these agents get full context via `build_context()`. Adding `read_file` lets them drill into large files or inspect computation scripts. Implement when file sizes regularly approach compression thresholds.

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

- **Workspace resume** — `--resume <workspace-dir>` to continue a previous run. Skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state (corrupted state, version mismatches).
