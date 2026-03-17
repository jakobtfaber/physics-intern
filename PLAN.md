# SciRalph — Task List

## CANDIDATE FIXES

### Naming con
why "computationalist" knowing research also derives from it, should make it Abstract Base Class, remove it prompt file ?
deep critic / critique / critic

## OTHER IDEAS

### Improve orchestrator

- prompt the orchestrator for better problem decomposition
- create some warm up problems
- gather some background knowledge about the problem domain
- Add a more open ended "brainstorm" task and maybe a dedicated section in the research state for ideas, possible routes, alternatives, etc.
- brainstorm internal consistency checks ?
- open questions and dead ends ??

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
- Human-in-the-loop breakpoints — allow the operator to pause the loop, inspect state, and intervene