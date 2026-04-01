# SciRalph — Task List

## LLM
- strengthen token count, reasoning storage, etc. per provider

## Code audit
- clean codebase.md
- clean tests
- clean engine
- improve structure : utils, etc.
- what to do with dead ends ?

## Agents improvements

### Surveyor
- 
- create some warm up problems
- remove re-surveyor

### Improve orchestrator

- orchestrator to better document its thoughts ?
- orchestrator context rot ?
- unique orchestrator call ?

### Improve computationalist
- to improve token efficiency, should we strip the previous code from the conversation

### Improve critic
- should we call a deep critic after refutation ?



## LONGER TERM

### Problem YAML features

- **External reference files** — allow problem YAML to specify a `files:` list. Copy into `workspace/references/`. Requires `read_file` tool for agents to access them. Useful for problems that need external papers or formula sheets.

### MCP tool integration

- **Additional computational backends** — abstract the computationalist's tool access behind a `ToolBackend` interface to support Cadabra (tensor algebra), xAct (differential geometry), Mathematica (symbolic CAS), or simulation codes via MCP. The computationalist prompt would gain a tool-use section describing available MCP tools and their capabilities.

### Parallel subagents

- **Parallel task execution** — the orchestrator emits multiple tasks tagged with dependency relationships; a `TaskQueue` runs independent tasks in parallel; a `MergeAgent` reconciles results before the next orchestrator pass. For contradictory parallel results, spawn a "debate" task where each result is critiqued in light of the other.

### Literature integration

- **Librarian agent** — an agent with web search access that can verify results against known literature, find relevant papers when the system gets stuck, and check whether a "novel" result is actually already known.

### Human-in-the-loop breakpoints

- allow the operator to pause the loop, inspect state, and intervene
- add a "user guidelines" file ? resume the process with a planner if user guidelines are added ?
