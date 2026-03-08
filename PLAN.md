# SciRalph Implementation Plan

## Completed

### Phase 1 — Core loop (DONE)

---

## Phase 1.5 — Cleanup, logging, tool-use loop, agentic agents

**Prerequisite:** Phase 1.1 fixes (done). Audit logging (done — lightweight version in `llm.py`; the full `AuditLogger` class from the original plan can be deferred or built on top).

Four steps, in implementation order.

---

### Step 1: Remove warm-ups, let the orchestrator plan sub-problems

**Files:** `problems/*.yaml`, `main.py`, `engine.py`, `workspace.py`, `prompts/orchestrator.md`

**Goal:** Remove the hand-crafted `warm_ups` field from problem YAMLs. The orchestrator should autonomously decide to create prerequisite sub-problems when facing a complex derivation.

#### 1a. Strip `warm_ups` from problem YAMLs

Remove the `warm_ups:` key from `hawking_temperature.yaml` and `qho_thermodynamics.yaml`. Problem files become a single `problem:` field (plus optional `files:` for reference data added later in Step 4c).

#### 1b. Remove warm-up plumbing from scaffold code

- `main.py`: stop reading `warm_ups` from YAML, stop passing to `SciRalph.__init__`
- `engine.py` (`SciRalph.__init__`): remove `warm_ups` parameter
- `workspace.py` (`WorkspaceManager.init`): remove `warm_ups` parameter, remove the "# Warm-Up Problems" section from the initial `RESEARCH_STATE.md` template

#### 1c. Update orchestrator prompt

In `prompts/orchestrator.md`:
- Remove `warmup` from the VALID TASK TYPES list
- Strengthen the existing sub-problem guidance: instead of "Create warm-up tasks for sub-problems that have known solutions", instruct the orchestrator to proactively identify prerequisite sub-problems and simpler analogues that build toward the main result, and to emit `derive` or `research` tasks for them
- Add a DECOMPOSITION STRATEGY section: "For complex problems, identify simpler sub-problems or known analogues whose solutions inform the main derivation. Tackle these first as `derive` tasks before attempting the full problem."

#### 1d. Remove `warmup` task type from dispatch

- `engine.py` (`_dispatch`): remove `warmup` from the list that routes to researcher (it becomes just `research`, `derive`, `resolve`, `synthesize`)

#### 1e. Update DESIGN.md

Remove or update all warm-up references in DESIGN.md:
- §3.1: Remove "# Warm-Up Problems" section from RESEARCH_STATE.md example
- §3.4: Remove `warmup` from task_type comment
- §4.1: Update orchestrator prompt excerpt (warm-up → decomposition strategy)
- §5.1: Remove `warm_up_required` from CONFIG, `warm_ups` from `__init__`, `warmup` from dispatch
- §5.2: Remove `warm_ups` from entry point example
- §7.6: Remove `warm_ups: [...]` from YAML example
- §8: Replace "warm-up calibration" with "sub-problem calibration" in limitations
- §9 Phase 2: Remove "Warm-up problem validation framework"

#### 1f. Tests

- Update any tests that reference `warm_ups` or the `warmup` task type
- Add a test verifying that `WorkspaceManager.init` no longer accepts or renders warm-ups

---

### Step 2: Full prompt/response logging

**Files:** `llm.py`, `workspace.py`

**Goal:** Log the complete system prompt, user content, and LLM response for every call, enabling full inspection and replay of any run.

#### 2a. Create `logs/` directory in workspace

In `WorkspaceManager.init`, create a `logs/` subdirectory alongside `computations/` and `archive/`. Expose the path as `self.logs_dir`.

#### 2b. Write full conversation logs per LLM call

Extend `_write_audit_entry` in `llm.py` (or add a sibling function) to write a Markdown file to `logs/` for each call:

Naming: `iter{NNN}_{agent}_{seq}.md` where `seq` is a per-iteration call counter (handles retries or, later, tool-loop rounds).

File contents:

```markdown
# LLM Call — iter 3, researcher

- **Timestamp:** 2026-03-07T14:25:30Z
- **Model:** claude-sonnet-4-6
- **Input tokens:** 12,340
- **Output tokens:** 3,210
- **Duration:** 8.42s
- **Stop reason:** end_turn

## System Prompt

<content of system prompt>

## User Content

<content of user message>

## Response

<full LLM response text>
```

To pass the `logs/` path to `call_llm`, either add a `logs_dir: Path | None` parameter or pass it via `Config`.

#### 2c. Extend for tool-use loop (when Step 3 lands)

When `run_agent_loop` is implemented, each round within the loop gets its own log file (e.g. `iter003_computationalist_001.md`, `..._002.md`). The log file additionally includes tool calls made (name + input) and tool results returned.

#### 2d. Keep AUDIT_LOG.jsonl as-is

The existing JSONL audit log stays for quick metrics and aggregation. The `logs/` files are the detailed complement for debugging and inspection. No new config needed — logging is always on. The `logs/` dir lives inside the workspace, committed with git.

#### 2e. Update DESIGN.md

- §7.4: Add mention of per-call Markdown log files in `logs/` alongside the JSONL audit log. Note that JSONL is for metrics/aggregation, Markdown files are for full inspection.

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
