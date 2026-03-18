# SciRalph — Task List

## CANDIDATE FIXES

## NEW ARCHITECTURE

### RQ-WH-ER workflow

- Goal modify the explore and verify logic along with the workflow of from RQ to WH to ER
- Agents are : researcher, computer and verifier (2 explore agents, one verifier)
- a research question is directed towards either the "researcher" (for reasoning/analytics) or the "computer" (for computational formal or numerical). The orchestrator should extensively prescribe context, background, method, assumptions, etc. Rely less on research state that bloats the context.
#### Researcher agent
- a researcher agent submit as a result a detailed reasoning/analytics that can be used as evidence for a WH
- we have to save the reasoning.
#### Computer agent
- computer agent is first asked to reason and lay out a plan, assumptions etc. regarding the computation (via a tool document_approach() call so we can record it)
- then it can write and submit code for execution using execute_python tool, we run it, save the output and return it
- the computer agent can keep writing code or submit a final result.
- a computer agent submit the name of the script that allowed it to reach its conclusion (the output has been gathered as well) along with any assumption or methodology that supports the results
#### Verifier
- from researcher or computer result, the orchestrator can formulate a (refutable) WH.
- the WH is then given to a "verifier" agent, that will look at the task description, method, assumption, evidence (reasoning/analytics or code/output), it does not have access to code but can play the role of an adversarial critique (rather than just redoing the same as it happens today). This somehow replaces the deep critique agent that we have today. The verifier submit a verdict.
- the orchestrator then update the ER and the strategy based on the verdict, and iterate.
- we can keep a higher level critique agent (for the strategy in particular)

### Sections in the research state that are maintained by the orchestrator
- give more editable sections to the orchestrator.
- conventions (already there)
- high level strategy (already there but keep it higher level, it shouldn't change too often)
- short term plan (more operational)
- various research notes like dead ends, open questions, ideas, etc. (append only, use append_note() tool)

### Deep critic
- we can keep a deep critic but it should be more focused on the strategy and the overall research direction and should raise red flag or possible pitfalls but not recheck every claim.
- It can be triggered after a certain number of iterations, or when the orchestrator thinks it's necessary (as of today)

### Naming and files
- today we have a computationalist and apparently the researcher agents depend on it, it shoudln't be
- today we have non-consistent nameing "critic/critique/deep_critic". 
- we should have a depp_critic and a verifier (that does not compute and replaces the researcher_verify and the computer_verify that we have today and are doing double work).


## OTHER IDEAS


### Improve orchestrator

- prompt the orchestrator for better problem decomposition
- create some warm up problems
- Add a more open ended "brainstorm" task and maybe a dedicated section in the research state for ideas, possible routes, alternatives, etc.
- brainstorm internal consistency checks ?
- open questions and dead ends ??

### Improve computationalist
- to improve token efficiency, should we strip the previous code from the conversation

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

### Human-in-the-loop breakpoints
- allow the operator to pause the loop, inspect state, and intervene