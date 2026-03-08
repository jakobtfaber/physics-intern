# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics.

Uses iterative LLM calls (Anthropic Claude) with externally persisted state in Markdown files and a layered verification stack: adversarial critique, symbolic/numerical computation, and structured sanity checks.

## Quick Start

```bash
# Install (requires Python 3.12+ and uv)
uv sync --extra dev

# Run tests
uv run python -m pytest -v

# Run a research problem (requires ANTHROPIC_API_KEY in .env or env var)
uv run python -m sciralph.main problems/hawking_temperature.yaml --max-iterations 10
```

### CLI Options

```
python -m sciralph.main <problem.yaml> [options]

  --model MODEL           LLM model (default: claude-sonnet-4-20250514)
  --max-iterations N      Max loop iterations (default: 200)
  --workspace-dir DIR     Workspace directory (default: workspaces/YYYYMMDD_HHMMSS_<problem>)
```

## Architecture

Five agents take turns in a main loop. Each agent gets a fresh context per call (no conversation history). All state lives in Markdown files with YAML frontmatter under `workspace/`, which is a separate git repo.

```
┌─────────────────────────────────────────────────┐
│                   Main Loop                      │
│                                                  │
│  ┌──────────────┐    ┌──────────────────────┐   │
│  │ Orchestrator  │───>│  Dispatch to Agent    │   │
│  │ (plan next    │    │                      │   │
│  │  task)        │    │  research / derive   │   │
│  └──────────────┘    │  compute             │   │
│         ▲            │  critique            │   │
│         │            │  compress            │   │
│         │            │  synthesize          │   │
│  ┌──────┴───────┐    │  terminate           │   │
│  │ Workspace     │<───└──────────────────────┘   │
│  │ (Markdown     │                               │
│  │  files + git) │                               │
│  └──────────────┘                                │
└─────────────────────────────────────────────────┘
```

### Agents

| Agent | Role | Reads | Writes |
|-------|------|-------|--------|
| **Orchestrator** | Plans next task, integrates proposed changes | All state files | `CURRENT_TASK.md`, `RESEARCH_STATE.md` |
| **Researcher** | Derivations, hypotheses, conceptual reasoning | Task + research state | `PROPOSED_CHANGES.md` |
| **Computationalist** | Symbolic/numerical verification via Python | Task + research state + computation log | `COMPUTATION_LOG.md`, code files |
| **Deep Critic** | Adversarial review — finds flaws, gaps, errors | Research state + computation log + critique log | `CRITIQUE_LOG.md` |
| **Compressor** | Archives and shrinks files exceeding size thresholds | Target file | Compressed target file |

### Verification Stack

Results go through layered verification before being promoted to "Established":

1. **Computational verification** — SymPy/NumPy checks (dimensional analysis, limiting cases, numerical agreement)
2. **Adversarial critique** — Deep Critic reviews with no unresolved HIGH critiques
3. **Dependency tracking** — All prerequisite results must themselves be Established

The orchestrator enforces these promotion criteria and forces periodic critic passes every N iterations.

### Workspace Files

All research state is persisted under `workspaces/<run>/` (each run gets a timestamped subdirectory, gitignored from this repo, has its own git):

| File | Purpose |
|------|---------|
| `RESEARCH_STATE.md` | Established results, working hypotheses, dead ends |
| `CURRENT_TASK.md` | Current task with YAML frontmatter |
| `PROPOSED_CHANGES.md` | Researcher output, pending integration |
| `COMPUTATION_LOG.md` | Log of all computations and their outputs |
| `CRITIQUE_LOG.md` | All critiques with severity and resolution status |
| `METRICS.md` | Token usage, file sizes, alerts |
| `AUDIT_LOG.jsonl` | Per-LLM-call metadata (tokens, duration, char counts) |
| `computations/` | Saved Python scripts from computationalist |

## Project Structure

```
src/sciralph/
  main.py              — Entry point, CLI argument parsing
  engine.py            — Main loop: orchestrate → dispatch → compress → metrics → git
  config.py            — Config dataclass (model, thresholds, timeouts, audit log)
  llm.py               — Anthropic API wrapper (call_llm) with audit logging
  workspace.py         — File I/O + git operations on workspace/
  markdown.py          — YAML frontmatter parsing, section extraction, critique counting
  sandbox.py           — Python script execution with timeout
  metrics.py           — MetricsTracker (token counts, alerts, Markdown rendering)
  agents/
    base.py            — BaseAgent ABC with template method + retry on max_tokens
    orchestrator.py    — Plans tasks, integrates proposed changes into research state
    researcher.py      — Derivations and reasoning
    computationalist.py — Code extraction, execution, failure flagging
    critic.py          — Adversarial review, critique counting
    compressor.py      — File size management
  prompts/             — Static .md system prompt files (one per agent)
tests/                 — pytest tests (markdown, sandbox, metrics, orchestrator)
problems/              — YAML problem definitions
```

## Problem Definitions

Problems are defined in YAML files under `problems/`:

```yaml
problem: |
  Derive the Hawking temperature of a Schwarzschild black hole
  from the Euclidean path integral approach...
```

Available problems:
- `hawking_temperature.yaml` — Hawking temperature from Euclidean path integral
- `qho_thermodynamics.yaml` — QHO thermodynamics

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run python -m pytest -v

# Run with coverage
uv run python -m pytest --cov=sciralph -v
```

## Design Documents

- **`DESIGN.md`** — Full system design: architecture, file formats, agent prompts, pseudocode
- **`PLAN.md`** — Implementation plan for upcoming phases (tool-use loop, agentic agents)
