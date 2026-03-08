# SciRalph Implementation Plan

## Completed

### Phase 1 — Core loop (DONE)

### Phase 1.1 — Critical fixes (DONE)

Post-e2e bug fixes: execution failure banners, orchestrator task types + termination, critique regex broadening, audit logging.

### Phase 1.5 Steps 1-2 (DONE)

- Step 1: Remove warm-ups, let the orchestrator plan sub-problems
- Step 2: Full prompt/response logging

### Post-run fixes (DONE)

Based on analysis of `workspaces/20260308_070621_hawking_temperature`:

- **Critique lifecycle bug** — critiques now land in "# Active Critiques" (not EOF); orchestrator moves them to "# Resolved Critiques" when addressed; frontmatter counters reflect reality.
- **Metrics flush on termination** — `_final_report()` flushes metrics before printing, so the final orchestrator call is always persisted.
- **Computationalist assertion discipline** — prompt requires `assert` statements with nonzero exit on failure; review prompt caps verdict at PARTIALLY AGREES for code without assertions.
- **Critic two-phase format** — prompt enforces Phase 1 (reproduce the argument) then Phase 2 (state the objection), preventing self-contradictory critiques.

---

## Phase 1.5 Step 3 — Tool-Use Loop

**Files:** new `src/sciralph/tools.py`, refactor of `llm.py`, edits to `agents/base.py`

**Goal:** Replace one-shot `call_llm()` with `run_agent_loop()` that supports tool calls. The old `call_llm()` is preserved for agents that don't need tools (compressor).

### 3a. `tools.py` — Tool definitions and executor

```python
# Tool definitions (Anthropic API format)
EXECUTE_PYTHON_TOOL = { "name": "execute_python", ... }

# Only the computationalist gets tools in this phase
AGENT_TOOLS = {
    "computationalist": [EXECUTE_PYTHON_TOOL],
    # All other agents: [] (no tools, use one-shot path)
}

class ToolExecutor:
    """Executes tool calls within security constraints."""

    def __init__(self, workspace: WorkspaceManager, config: Config):
        self.workspace = workspace
        self.config = config

    def execute(self, tool_name: str, tool_input: dict,
                iteration: int, agent: str) -> str:
        """Dispatch and execute a tool call. Returns result string."""
        ...

    def _execute_python(self, code: str, filename: str) -> str:
        """Write code to file, execute with sandbox.py, return output.

        Reuses existing sandbox.py (subprocess isolation + timeout).
        Captures both stdout and stderr.
        Truncates output to max_output_chars (default 10,000) to avoid
        blowing up the context window on runaway prints.
        """
        ...

    def _resolve_and_validate(self, path: str) -> Path:
        """Resolve path and check it's within workspace root. Raise on violation."""
        ...
```

Security: `_resolve_and_validate` uses `Path.resolve()` and checks the resolved path starts with `workspace.root`. Rejects `..` traversal.

### 3b. `llm.py` — Add `run_agent_loop()`

```python
def run_agent_loop(system: str, context: str, tools: list[dict],
                   tool_executor: ToolExecutor, config: Config,
                   max_rounds: int = 10,
                   token_budget: int = 50_000,
                   iteration: int = 0,
                   agent: str = "") -> AgentResult:
    """Run tool-use loop until the agent produces a final text response.

    Termination conditions (whichever comes first):
    1. Agent produces a response with stop_reason="end_turn" (normal exit)
    2. max_rounds reached → take the last text block as final output,
       prepend a warning banner "⚠ MAX ROUNDS REACHED"
    3. Cumulative output tokens exceed token_budget → same as (2)
    """
    ...
```

Returns an `AgentResult` dataclass:
```python
@dataclass
class AgentResult:
    text: str                    # final text output
    tool_calls: list[dict]       # log of all tool calls made
    total_input_tokens: int
    total_output_tokens: int
    total_duration: float
    rounds: int                  # number of LLM calls (1 = no tool use)
    truncated: bool              # True if terminated by max_rounds or budget
```

### 3c. `agents/base.py` — Support tool-use agents

Add a `tools` class attribute to `BaseAgent` (default empty list). Modify `run()`:
- If `self.tools` is empty: use old one-shot `call_llm` path
- If `self.tools` is non-empty: use `run_agent_loop`

Each agent subclass declares its tools:
```python
class ComputationalistAgent(BaseAgent):
    tool_names = ["execute_python"]
```

### 3d. Tests

`tests/test_tools.py`:
- Path validation: allowed path, rejected path, traversal attack (`../../etc/passwd`)
- `execute_python`: writes file, executes, returns stdout+stderr
- `execute_python`: output truncation at limit
- Max rounds enforcement: mock LLM that always requests tools → hits max_rounds → returns with `truncated=True`
- Token budget enforcement: similar mock

---

## Phase 1.5 Step 4 — Agentic Computationalist

**Files:** edits to `agents/computationalist.py`, `prompts/computationalist.md`, `engine.py`

**Goal:** Wire `execute_python` tool into the computationalist. This is the single highest-impact change: the agent can now run code, see output, fix errors, and iterate — instead of the current fragile extract-from-fenced-block flow.

### 4a. Computationalist agent refactor

Current flow: LLM emits code in fenced block → scaffold extracts last ```python``` block → executes → separate review LLM call.

New flow: LLM calls `execute_python` tool → sees output in same conversation → can iterate on errors → emits final COMPUTATION_LOG entry as text → no separate review call needed (the agent self-reviews within the loop).

Changes to `agents/computationalist.py`:
- `build_context`: same as before (task + research state + recent computations)
- `process_response`: instead of extracting code from text, collect tool calls from `AgentResult.tool_calls`. Each `execute_python` call is already executed and logged. The final text output is the COMPUTATION_LOG entry including VERDICT.
- Remove the separate `computationalist_review` LLM call — the agent now sees execution output and writes its own verdict within the tool-use loop.

### 4b. Prompt update (`prompts/computationalist.md`)

- Replace fenced-block instructions with tool-use instructions
- Tell it to use `execute_python` to run code, read the output, and iterate if there are errors
- Keep the assertion discipline rules (added in post-run fixes)
- Tell it to write VERDICT and NOTES itself after seeing execution output (no separate review step)
- Instruct: "If your first script fails, read the error, fix the code, and call execute_python again. You have up to N rounds."

### 4c. Metrics integration

Surface tool-use metadata in METRICS.md:
- Per-agent-call: number of rounds, number of tool calls, whether truncated
- Cumulative: total tool calls across the run

### 4d. Tests

- `tests/test_computationalist.py` (extend): test that computationalist with mocked tool-use loop produces a COMPUTATION_LOG entry with VERDICT
- Smoke test: run 3 iterations on a real problem, verify AUDIT_LOG.jsonl has tool_call entries, verify computationalist iterates on a deliberate SymPy import error

---

## Phase 2 — Expanded Tool Use and Operational Features

Deferred items. Implement when there's a driving use case.

### read_file tool for orchestrator/researcher/critic

These agents currently receive full context via `build_context()`. The upfront injection pattern works well (confirmed by test runs). Adding `read_file` provides marginal benefit (drill into large files, inspect computation scripts) but risks wasted rounds. Implement when file sizes regularly approach compression thresholds.

### External reference files

Allow problem YAML to specify `files:` list. Copy into `workspace/references/`. Requires `read_file` tool (above) for agents to access them. Implement when tackling problems that need external papers or formula sheets.

### Workspace resume

Allow `--resume <workspace-dir>` to continue a previous run. Requires: skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state. Separate concern from tool use — has its own edge cases (corrupted state, version mismatches).

### read_file + list_files tools for other agents

Once `read_file` is proven with the computationalist, consider adding it to:
- **Critic**: inspect computation scripts for closer review
- **Researcher**: load reference files
- **Orchestrator**: drill into specific sections when state files grow large

---

## Implementation Order Summary

```
Step 3: Tool-Use Loop
  3a. tools.py — ToolExecutor + execute_python   (new file)
  3b. llm.py — run_agent_loop + AgentResult      (edit)
  3c. agents/base.py — tool support branching     (edit)
  3d. test_tools.py                               (new file)

Step 4: Agentic Computationalist
  4a. computationalist.py — tool-use refactor     (edit)
  4b. computationalist.md — prompt rewrite        (edit)
  4c. metrics.py — tool-use tracking              (edit)
  4d. Tests + smoke test                          (extend)

Phase 2 (deferred):
  - read_file tool for other agents
  - External reference files
  - Workspace resume
```

## Files affected

| File | Step 3 | Step 4 | Phase 2 |
|------|--------|--------|---------|
| `src/sciralph/tools.py` | **NEW** | | |
| `src/sciralph/llm.py` | edit | | |
| `src/sciralph/agents/base.py` | edit | | |
| `src/sciralph/agents/computationalist.py` | | edit | |
| `src/sciralph/prompts/computationalist.md` | | edit | |
| `src/sciralph/prompts/computationalist_review.md` | | remove | |
| `src/sciralph/metrics.py` | | edit | |
| `src/sciralph/engine.py` | | | edit |
| `src/sciralph/workspace.py` | | | edit |
| `src/sciralph/main.py` | | | edit |
| `src/sciralph/prompts/orchestrator.md` | | | edit |
| `src/sciralph/prompts/researcher.md` | | | edit |
| `src/sciralph/prompts/deep_critic.md` | | | edit |
| `tests/test_tools.py` | **NEW** | extend | extend |
| `tests/test_computationalist.py` | | extend | |
