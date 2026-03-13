# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics.

## What is this?

SciRalph takes a problem stated in plain language (e.g. "derive the Hawking temperature from the Euclidean path integral") and works through it autonomously — breaking it into sub-problems, performing derivations, writing and running verification code, and critically reviewing its own results — until it produces a coherent, verified solution.

**How it works.** Five specialised LLM agents (orchestrator, researcher, computationalist, critic, compressor) take turns in a loop. No agent carries conversation history: each call starts from a fresh context and reads/writes shared Markdown files in a workspace directory. The orchestrator plans the next step, a worker agent executes it, and the cycle repeats. A layered verification stack — SymPy/NumPy computations, adversarial critique with severity tracking, and dependency-aware result promotion — acts as backpressure against errors. The workspace is version-controlled with git, so every step is recoverable. Supports multiple LLM providers (Anthropic, OpenAI, Google Gemini, HuggingFace) via a provider abstraction layer with a `models.yaml` registry.

**Current status.** Core functionality is complete (450 tests passing). The system produces correct science on all tested problems. A comprehensive scaffolding hardening stack (50+ mechanisms across 10 layers) compensates for predictable LLM failures: premature result promotion, hallucinated IDs, malformed YAML, ignored instructions, empty outputs, and premature termination. Every mechanism is instrumented — `SCAFFOLDING_LOG.jsonl` records each intervention with layer, event key, and detail, enabling profiling of which mechanisms actually fire per model. See `CODEBASE.md` §7 for the full catalog.

## Quick Start

```bash
# Install (requires Python 3.12+ and uv)
uv sync --extra dev

# Run tests
uv run python -m pytest -v

# Run a research problem (requires ANTHROPIC_API_KEY in .env or env var)
uv run python -m sciralph.main problems/tier1/hawking_temperature.yaml --max-iterations 10

# Run with a different provider (auto-resolved from models.yaml)
uv sync --extra openai
uv run python -m sciralph.main problems/tier1/hawking_temperature.yaml --model gpt-4o --max-iterations 10
```

### CLI Options

```
python -m sciralph.main <problem.yaml> [options]

  --model MODEL           LLM model key (default: claude-sonnet-4-6, resolved via models.yaml)
  --provider PROVIDER     Force provider (anthropic/openai/google/huggingface)
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
./run_and_verify.sh problems/tier1/hawking_temperature.yaml --max-iterations 10
./run_and_verify.sh problems/tier1/qho_thermodynamics.yaml -- --rerun-computations
```

```
python -m sciralph.verify <workspace_dir> [options]

  --model MODEL              LLM model (default: claude-opus-4-6)
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
│         │            │  terminate           │   │
│  ┌──────┴───────┐    │                      │   │
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
| **Computationalist** | Symbolic/numerical verification via Python | Task + research state | `COMPUTATION_LOG.md`, code files |
| **Deep Critic** | Adversarial review — finds flaws, gaps, errors | Research state + computation log + critique log | `CRITIQUE_LOG.md` |
| **Compressor** | Archives and shrinks files exceeding size thresholds | Target file | Compressed target file |

### Verification Stack

Results go through layered verification before being promoted to "Established":

1. **Computational verification** — SymPy/NumPy checks (dimensional analysis, limiting cases, numerical agreement)
2. **Adversarial critique** — Deep Critic reviews with no unresolved HIGH critiques
3. **Dependency tracking** — All prerequisite results must themselves be Established

The orchestrator enforces these promotion criteria and forces periodic critic passes every N iterations. A post-integration validation pipeline (`validation.py`) runs 8 checks after every orchestrator pass — demoting unverified ERs, promoting verified WHs, stripping phantom labels, fixing agent routing, ensuring critique resolutions were actually applied. Termination goes through gates requiring critic review, no unresolved HIGH critiques, and numerical verification when required by the problem. See `CODEBASE.md` §7 for the full LLM failure compensation catalog.

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
| `SCAFFOLDING_LOG.jsonl` | Per-event log of scaffolding interventions (override firings, validation fixes, bailouts) |
| `VERIFICATION.md` | Independent verification report (written by `--write-report`) |
| `computations/` | Saved Python scripts from computationalist |

## Project Structure

```
src/sciralph/
  main.py              — Entry point, CLI argument parsing
  engine.py            — Main loop: orchestrate → validate → override → dispatch → compress → git
  validation.py        — Post-integration checks (ER gate, phantom labels, routing) + termination gates
  verify.py            — Independent verification script (Claude Opus, streaming)
  config.py            — Config dataclass (model, thresholds, timeouts, audit log)
  llm.py               — Provider-agnostic LLM wrapper (call_llm, run_agent_loop) with retry + audit logging
  task.py              — Task dataclass + TaskType enum for typed task handling
  workspace.py         — File I/O + git operations on workspace/
  markdown.py          — YAML frontmatter parsing, section extraction, critique counting
  sandbox.py           — Python script execution with timeout
  metrics.py           — MetricsTracker (token counts, alerts, Markdown rendering)
  agents/
    base.py            — BaseAgent ABC with template method + retry on max_tokens
    orchestrator.py    — Plans tasks, integrates proposed changes into research state
    researcher.py      — Derivations and reasoning
    computationalist.py — Agentic code execution via execute_python tool, verdict writing
    critic.py          — Adversarial review, critique counting
    compressor.py      — File size management
  prompts/             — Static .md system prompt files (one per agent, plus verifier)
  providers/
    base.py            — LLMProvider ABC + ProviderResponse dataclass
    anthropic.py       — Anthropic Claude adapter
    openai.py          — OpenAI adapter
    google.py          — Google Gemini adapter
    huggingface.py     — HuggingFace Inference Providers adapter
  models.yaml          — Model registry (friendly keys → provider + model_id + env_key + cost)
tests/                 — pytest tests (engine, validation, markdown, tools, orchestrator, computationalist, verify, workspace, ...)
problems/
  tier1/               — 10 core problems
  tier2/               — 12 advanced problems
run_and_verify.sh      — Run a problem then verify results in one command
```

## Problem Definitions

Problems are defined in YAML files under `problems/tier1/` and `problems/tier2/`:

```yaml
problem: |
  Derive the Hawking temperature of a Schwarzschild black hole
  from the Euclidean path integral approach...

requires_numerical: true  # enforced by termination gate
```

**Tier 1** — core problems (`problems/tier1/`):
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

**Tier 2** — advanced problems (`problems/tier2/`):
- `aharonov_bohm_scattering.yaml` — Aharonov-Bohm differential scattering cross section
- `bremsstrahlung.yaml` — Classical bremsstrahlung spectral energy distribution
- `dirac_coulomb.yaml` — Exact Dirac equation in Coulomb potential
- `h2_plus_molecule.yaml` — H₂⁺ ground-state potential energy curve
- `ising_2d_onsager.yaml` — Exact critical temperature of 2D Ising model (Onsager)
- `lamb_shift.yaml` — Leading-order Lamb shift for hydrogen
- `schwinger_pair_production.yaml` — Schwinger pair production rate
- `stark_effect_parabolic.yaml` — Hydrogen Stark effect in parabolic coordinates
- `thomas_fermi.yaml` — Thomas-Fermi model of the atom
- `tov_buchdahl.yaml` — TOV equation and Buchdahl bound
- `unruh_effect.yaml` — Unruh effect for uniformly accelerated observer
- `wkb_quartic_oscillator.yaml` — WKB approximation for pure quartic oscillator

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

- **`PLAN.md`** — Future work ideas and roadmap
- **`CODEBASE.md`** — Developer-oriented codebase reference (architecture, data flow, known issues, planned changes)
