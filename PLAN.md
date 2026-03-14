# SciRalph — Task List

## CANDIDATE FIXES

### Failure Artifact Store

**Problem:** Three distinct failure patterns (max-tokens retry, computationalist zero-text bailout, blind recompute after INCONCLUSIVE) share a common root cause: structured failure data is generated at the LLM boundary but never persisted in a form accessible to subsequent orchestration decisions.

**Information loss chain:**

| Boundary | What's lost |
|---|---|
| `run_agent_loop()` → `AgentResult` | All intermediate tool I/O except last 500-char stub from `_synthesize_from_tool_history()` |
| `_call_with_retry()` → engine | Max-tokens events invisible during retries; only final stop reason reaches `_record_agent_failures()` |
| `_agent_failures.clear()` in `_build_context_prefix()` | Failure signals consumed once then gone |
| P6 enrichment → computationalist context | Truncated again at `prior_failure_excerpt_chars`; only METHOD/RESULT/NOTES, no raw code |

**Proposed design:** A workspace file (`FAILURE_ARTIFACTS.jsonl`) written at the LLM boundary with full context:

- **Keyed by** normalized claim/task ID
- **Written by** `run_agent_loop()` (on max_rounds/zero-text bailout) and `_call_with_retry()` (on max_tokens)
- **Contains:** full code of all rounds, error tracebacks, round count, stop reason, intermediate outputs, progression of attempts
- **Read by** `_enrich_compute_task_with_prior_failures()` (instead of parsing COMPUTATION_LOG.md stubs) and `_build_context_prefix()` (instead of relying on ephemeral `_agent_failures` list)

**Files:** `workspace.py` (add `append_failure_artifact()` / `read_failure_artifact()`), `llm.py` (write artifact before returning), `agents/base.py` (write on max_tokens), `engine.py` (read in enrichment + context prefix).


## OTHER IDEAS

### Improve orchestrator

- prompt the orchestrator for better problem decomposition
- create some warm up problems
- gather some background knowledge about the problem domain
- Use a more structured output format for agent responses (e.g., JSON with separate fields for "verdict", "summary", "next_steps") to reduce ambiguity and parsing errors.
- Orchestrator calls tools to mutate a formal "ResearcherState" object instead of free-form text instructions. Maybe with a scratchpad for informal notes.
- Add a more open ended "brainstorm" task and maybe a dedicated section in the research state for ideas, possible routes, alternatives, etc.
- brainstorm internal consistency checks ?

### Agent tool use

- **`read_file` tool for orchestrator/researcher/critic** — currently only the computationalist has tool access. Giving other agents a `read_file` tool would let them access reference materials and large workspace files on demand instead of stuffing everything into context.
- Today computationalist is the only agent with tool use, so some computationalist-specific instructions (about COMP etc.) are in llm.py.

### Mandatory critique for each WH promoted to ER

### Problem YAML features

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

### MCP tool integration

- **Additional computational backends** — abstract the computationalist's tool access behind a `ToolBackend` interface to support Cadabra (tensor algebra), xAct (differential geometry), Mathematica (symbolic CAS), or simulation codes via MCP. The computationalist prompt would gain a tool-use section describing available MCP tools and their capabilities.

### Parallel subagents

- **Parallel task execution** — the orchestrator emits multiple tasks tagged with dependency relationships; a `TaskQueue` runs independent tasks in parallel; a `MergeAgent` reconciles results before the next orchestrator pass. For contradictory parallel results, spawn a "debate" task where each result is critiqued in light of the other.

### Literature integration

- **Librarian agent** — an agent with web search access that can verify results against known literature, find relevant papers when the system gets stuck, and check whether a "novel" result is actually already known.

### Workspace management

- **Workspace resume** — `--resume <workspace-dir>` to continue a previous run. Skip `init()` if `.git` exists, load iteration from METRICS.md, handle partial state (corrupted state, version mismatches).

### Misc ideas
- compare with direct call ?
- add timing outputs in console
- unified the two logs .jsonl
- Add a linting step for computation scripts to avoid running obviously broken code (syntax errors, missing imports). This could be a lightweight static check before execution.
- Human-in-the-loop breakpoints — allow the operator to pause the loop, inspect state, and intervene