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


### Formal COMP→WH Registry

**Problem:** There is no authoritative mapping between computations (COMP entries) and the claims they verify (WH/ER). The link is currently reconstructed at query time via substring matching (`"WH-001" in e["claim"]`), which fails in three observed ways:

1. **No ID in CLAIM line** — when compute is dispatched before any WH exists (observed: gemini-3.1-pro), the task body has no WH/ER ID, so the CLAIM line is purely descriptive. The promotion gate then can't link the COMP to the later-created WH and silently refuses promotion.
2. **Circular verification invisible** — two COMPs can both say VERIFIED for the same claim without the scaffolding knowing they're related. In the qwen run, COMP-009 "verified" the formula by plugging in COMP-002's own numbers. Nothing detected the circularity because each COMP is validated in isolation.
3. **Contradictory VERIFIEDs undetected** — COMP-002 (30 detected/45 undetected) and COMP-008 (44 detected/31 undetected) both carried VERIFIED on overlapping claims. The scaffolding never compared their outputs because there's no structure connecting COMPs that target the same claim.

**Proposed design:** A `comp_registry` in `LoopState` that formally tracks every computation's target claim and verdict.

```python
@dataclass
class CompRecord:
    comp_id: str            # COMP-003
    target_claim: str       # WH-001
    verdict: str            # VERIFIED / REFUTED / INCONCLUSIVE
    iteration: int
    # Future: key_numbers for contradiction detection

comp_registry: dict[str, CompRecord] = field(default_factory=dict)  # keyed by comp_id
```

The orchestrator declares which claim a compute task targets via a `target_claim` frontmatter field in CURRENT_TASK.md. The engine reads it, passes it through the Task object, and registers the result after dispatch.

**Implementation guidelines:**

1. **`task.py`** — Add `target_claim: str = ""` field to `Task`. Parse from CURRENT_TASK.md frontmatter (`target_claim: WH-003`). Fallback: extract first `WH-NNN`/`ER-NNN` from `task.body` using existing `_ER_WH_ID_RE`.

2. **`engine.py`** — Add `CompRecord` dataclass and `comp_registry: dict[str, CompRecord]` to `LoopState`. In `_track_compute_verdict()`, after parsing the last COMP entry, register a `CompRecord` with the target claim from `task.target_claim` (authoritative) or the parsed CLAIM line (fallback). This replaces the fire-and-forget behavior where VERIFIED clears tracking.

3. **`computationalist.py`** — Use `task.target_claim` (when present) instead of regex-extracting from `task.body` for the CLAIM line injection. This makes the injection deterministic rather than heuristic.

4. **`validation.py`** — `check_er_promotion_gate()` should consult the registry as the primary lookup (`any(r.target_claim == wh_id and r.verdict == "VERIFIED" for r in registry.values())`), falling back to the current substring matching for COMPs registered before this change or with missing target_claim.

5. **Orchestrator prompt** — Add `target_claim` to the CURRENT_TASK.md frontmatter spec for compute tasks: "For `task_type: compute`, you MUST include `target_claim: WH-NNN` identifying the single hypothesis being verified."

6. **Deferred binding** — When compute is dispatched before any WH exists (iteration 1 on a fresh problem), register the CompRecord with `target_claim=""`. When the researcher later formalizes a WH whose content matches the COMP's claim, bind retroactively. Heuristic: match on `_normalize_claim_key`. This is the only non-trivial piece — keep it simple and log a scaffold event when deferred binding fires.

7. **Scope boundary** — The registry tracks COMP→WH links and verdicts only. Cross-COMP contradiction detection (comparing numerical outputs) and verification independence checks (detecting circular verification) are separate features that build on top of the registry but should be implemented independently.

**Files:** `task.py` (+1 field, +2 lines in `from_frontmatter`), `engine.py` (+CompRecord dataclass, +comp_registry field, ~15 lines in `_track_compute_verdict`, ~10 lines deferred binding helper), `computationalist.py` (~5 lines to prefer `task.target_claim`), `validation.py` (~10 lines to use registry in promotion gate), `prompts/orchestrator.md` (add `target_claim` to frontmatter spec).


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