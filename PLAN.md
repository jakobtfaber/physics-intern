# SciRalph Implementation Plan

## Completed

### Phase 1 — Core loop (DONE)

---

## Phase 1.5 — Cleanup, logging, tool-use loop, agentic agents

**Prerequisite:** Phase 1.1 fixes (done). Audit logging (done — lightweight version in `llm.py`; the full `AuditLogger` class from the original plan can be deferred or built on top).

Four steps, in implementation order.

---

### Step 1: Remove warm-ups, let the orchestrator plan sub-problems

DONE

---

### Step 2: Full prompt/response logging

DONE
---

### Step 3: Tool-Use Loop

**Files:** new `src/sciralph/tools.py`, major refactor of `llm.py`, edits to `agents/base.py`

**Goal:** Replace one-shot `call_llm()` with `run_agent_loop()` that supports tool calls. The old `call_llm()` is preserved for agents that don't need tools (compressor).

#### 3a. `tools.py` — Tool definitions and executor

```python
# Tool definitions (Anthropic API format)
READ_FILE_TOOL = { "name": "read_file", "description": "...", "input_schema": {...} }
LIST_FILES_TOOL = { "name": "list_files", ... }
EXECUTE_PYTHON_TOOL = { "name": "execute_python", ... }

# Tool sets per agent role
AGENT_TOOLS = {
    "orchestrator": [READ_FILE_TOOL],
    "researcher": [READ_FILE_TOOL, LIST_FILES_TOOL],
    "computationalist": [READ_FILE_TOOL, EXECUTE_PYTHON_TOOL],
    "deep_critic": [READ_FILE_TOOL],
    "compressor": [],  # no tools
}

class ToolExecutor:
    """Executes tool calls within security constraints."""

    def __init__(self, workspace: WorkspaceManager, allowed_paths: list[Path],
                 config: Config, logger: AuditLogger | None = None):
        self.workspace = workspace
        self.allowed_paths = allowed_paths  # workspace root + reference dirs
        self.config = config
        self.logger = logger

    def execute(self, tool_name: str, tool_input: dict,
                iteration: int, agent: str) -> str:
        """Dispatch and execute a tool call. Returns result string."""
        ...

    def _read_file(self, path: str) -> str:
        """Read file with path validation."""
        resolved = self._resolve_and_validate(path)
        return resolved.read_text()

    def _list_files(self, directory: str) -> str:
        """List files with path validation."""
        ...

    def _execute_python(self, code: str, filename: str) -> str:
        """Write code to file, execute with sandbox, return output."""
        ...

    def _resolve_and_validate(self, path: str) -> Path:
        """Resolve path and check it's within allowed roots. Raise on violation."""
        ...
```

Security: `_resolve_and_validate` uses `Path.resolve()` and checks the resolved path starts with one of `allowed_paths`. Rejects `..` traversal.

#### 3b. `llm.py` — Add `run_agent_loop()`

```python
def run_agent_loop(system: str, context: str, tools: list[dict],
                   tool_executor: ToolExecutor, config: Config,
                   max_rounds: int = 10,
                   iteration: int = 0,
                   agent: str = "") -> AgentResult:
    """Run tool-use loop until the agent produces a final text response."""
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
```

#### 3c. `agents/base.py` — Support tool-use agents

Add a `tools` property to `BaseAgent` (default empty list). Modify `run()`:
- If `self.tools` is empty: use old one-shot `call_llm` path
- If `self.tools` is non-empty: use `run_agent_loop`

Each agent subclass declares its tools:
```python
class ComputationalistAgent(BaseAgent):
    tool_names = ["read_file", "execute_python"]
```

#### 3d. Tests

- `tests/test_tools.py`: test path validation (allowed, rejected, traversal attack), test `execute_python` tool writes file and returns output, test max rounds enforcement.

---

### Step 4: Agentic Agents

**Files:** edits to all agent files, edits to `engine.py`, `workspace.py`

**Goal:** Wire tool use into agents. Update agent prompts to describe available tools.

#### 4a. Computationalist — the highest-impact change

Current flow: LLM emits code in fenced block → scaffold extracts → executes → logs.

New flow: LLM calls `execute_python` tool → sees output → can iterate → scaffold logs all calls.

Changes to `agents/computationalist.py`:
- `build_context`: same as before (task + research state + recent computations)
- `process_response`: instead of extracting code from text, collect tool calls from `AgentResult.tool_calls`. Each `execute_python` call is already executed. The final text output is the log entry.
- Prompt update (`prompts/computationalist.md`): add tool-use instructions. Tell it to use `execute_python` rather than writing code in fenced blocks. Tell it to iterate on errors.

#### 4b. Orchestrator, Researcher, Critic — read_file tool

These agents currently receive all their context upfront. With `read_file`, they can:
- Orchestrator: drill into specific sections when the full state is too large
- Researcher: load external reference files relevant to the current task
- Critic: request specific computation scripts for closer inspection

Changes are lighter here — the upfront context stays (it's efficient), but the agent CAN request more if needed.

Prompt updates: add a note like "You have access to a `read_file` tool to load additional files from the workspace or reference directory if needed."

#### 4c. External reference files

Changes to `workspace.py` (`WorkspaceManager.init`):
- Accept `files` list from problem YAML
- Copy each file into `workspace/references/`
- Store the allowed reference paths for the `ToolExecutor`

Changes to `main.py`:
- Parse `files:` from problem YAML
- Pass to `SciRalph.__init__` → workspace init

Changes to orchestrator context: include a "Available reference files" section listing filenames and descriptions.

#### 4d. Workspace resume

Changes to `WorkspaceManager.init`:
- If workspace already exists and has a `.git` directory, skip initialization. Read existing state.
- Load iteration count from METRICS.md frontmatter.

Changes to `engine.py`:
- If resuming, set `self.iteration` from existing metrics.

#### 4e. Tests

- `tests/test_tools.py` (extend): test reference file access, test path outside workspace rejected.
- Manual smoke test: run 3 iterations with tool use, verify AUDIT_LOG.jsonl has tool_call entries, verify computationalist iterates on a deliberate error.

---

## Implementation Order Summary

```
Step 1: Remove warm-ups
  1a. Strip warm_ups from problem YAMLs (edit)
  1b. Remove warm-up plumbing           (edit main.py, engine.py, workspace.py)
  1c. Update orchestrator prompt        (edit)
  1d. Remove warmup task type           (edit engine.py)
  1e. Update DESIGN.md                  (edit)
  1f. Tests                             (extend)

Step 2: Full prompt/response logging
  2a. Create logs/ directory            (edit workspace.py)
  2b. Write per-call log files          (edit llm.py)
  2c. Extend for tool-use loop          (edit llm.py — after Step 3)
  2d. AUDIT_LOG.jsonl unchanged
  2e. Update DESIGN.md                  (edit)

Step 3: Tool-Use Loop
  3a. tools.py                          (new file)
  3b. llm.py — run_agent_loop           (edit)
  3c. agents/base.py — tool support     (edit)
  3d. test_tools.py                     (new file)

Step 4: Agentic Agents
  4a. Computationalist + prompt update  (edit)
  4b. Orchestrator/Researcher/Critic    (edit)
  4c. External reference files          (edit workspace.py, main.py)
  4d. Workspace resume                  (edit workspace.py, engine.py)
  4e. Tests                             (extend)
```

## Files affected

| File | Step 1 | Step 2 | Step 3 | Step 4 |
|------|--------|--------|--------|--------|
| `problems/*.yaml` | edit | | | |
| `src/sciralph/main.py` | edit | | | edit |
| `src/sciralph/engine.py` | edit | | | edit |
| `src/sciralph/workspace.py` | edit | edit | | edit |
| `src/sciralph/llm.py` | | edit | edit | |
| `src/sciralph/tools.py` | | | **NEW** | |
| `src/sciralph/agents/base.py` | | | edit | |
| `src/sciralph/agents/computationalist.py` | | | | edit |
| `src/sciralph/agents/orchestrator.py` | | | | edit |
| `src/sciralph/agents/researcher.py` | | | | edit |
| `src/sciralph/agents/critic.py` | | | | edit |
| `src/sciralph/prompts/orchestrator.md` | edit | | | edit |
| `src/sciralph/prompts/computationalist.md` | | | | edit |
| `src/sciralph/prompts/researcher.md` | | | | edit |
| `src/sciralph/prompts/deep_critic.md` | | | | edit |
| `DESIGN.md` | edit | edit | | |
| `tests/test_tools.py` | | | **NEW** | extend |
| `tests/` (existing) | extend | | | |
