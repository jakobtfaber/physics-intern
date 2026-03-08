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

### Verification

After a run completes, you can independently verify the scientific results using a stronger model (Claude Opus by default):

```bash
# Verify a completed workspace
uv run python -m sciralph.verify workspaces/<run_dir>/ --write-report

# Also re-run computation scripts
uv run python -m sciralph.verify workspaces/<run_dir>/ --rerun-computations --write-report

# Run + verify in one command
./run_and_verify.sh problems/hawking_temperature.yaml --max-iterations 10
./run_and_verify.sh problems/qho_thermodynamics.yaml -- --rerun-computations
```

```
python -m sciralph.verify <workspace_dir> [options]

  --model MODEL              LLM model (default: claude-opus-4-20250514)
  --max-tokens N             Max output tokens (default: 16384)
  --rerun-computations       Re-run computation scripts before verification
  --timeout N                Computation timeout in seconds (default: 60)
  --write-report             Write VERIFICATION.md into workspace
```

The verifier evaluates each Established Result for mathematical/physical validity, checks chain coherence between results, and outputs a verdict: VALID, PARTIALLY_VALID, INVALID, or INCONCLUSIVE.

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
| `VERIFICATION.md` | Independent verification report (written by `--write-report`) |
| `computations/` | Saved Python scripts from computationalist |

## Project Structure

```
src/sciralph/
  main.py              — Entry point, CLI argument parsing
  engine.py            — Main loop: orchestrate → dispatch → compress → metrics → git
  verify.py            — Independent verification script (Claude Opus, streaming)
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
  prompts/             — Static .md system prompt files (one per agent, plus verifier)
tests/                 — pytest tests (markdown, sandbox, metrics, orchestrator, verify)
problems/              — YAML problem definitions
run_and_verify.sh      — Run a problem then verify results in one command
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
- `qho_thermodynamics.yaml` — Quantum harmonic oscillator thermodynamics
- `ising_1d_transfer_matrix.yaml` — 1D Ising model via transfer matrix method
- `hydrogen_fine_structure.yaml` — Fine structure corrections to hydrogen energy levels
- `casimir_effect.yaml` — Casimir force via zeta-function regularisation
- `perihelion_precession.yaml` — Anomalous perihelion precession of Mercury from GR
- `berry_phase_spin.yaml` — Berry phase for spin-1/2 in a rotating magnetic field
- `chandrasekhar_limit.yaml` — Chandrasekhar mass limit via Lane-Emden equation
- `path_integral_harmonic_oscillator.yaml` — Exact path integral for the harmonic oscillator
- `renormalisation_phi4.yaml` — One-loop renormalisation of scalar φ⁴ theory

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
