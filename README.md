# SciRalph

Multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics.

## What is this?

SciRalph takes a problem stated in plain language (e.g. "derive the Hawking temperature from the Euclidean path integral") and works through it autonomously — breaking it into sub-problems, performing derivations, writing and running verification code, and critically reviewing its own results — until it produces a coherent, verified solution.

**How it works.** Eight specialised LLM agent roles (surveyor, orchestrator, researcher, computer, reviewer, deep critic, compressor, formatter) take turns in a loop. A **surveyor** maps the research landscape before the main loop begins. The **orchestrator** dispatches research questions to a **researcher** (analytical reasoning) or **computer** (code execution), formulates working hypotheses from the evidence, then sends them to a **reviewer** for adversarial review. No agent carries conversation history: each call starts from a fresh context. All research state lives in a structured `ResearchState` object — agents mutate it via tools, and Markdown files are rendered from it for git snapshots. The workspace is version-controlled with git, so every step is recoverable. Supports multiple LLM providers (Anthropic, OpenAI, Google Gemini, HuggingFace) via a provider abstraction layer with a `models.yaml` registry.

**Current status.** Core functionality is complete (~910 tests passing). The system produces correct science on all tested problems. A comprehensive scaffolding hardening stack (50+ mechanisms across 4 categories) compensates for predictable LLM failures. Every mechanism is instrumented — `EVENT_LOG.jsonl` records each intervention. See `CODEBASE.md` §7 for the full catalog.

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

  --model MODEL           LLM model key (default: claude-sonnet-4.6, resolved via models.yaml)
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

  --model MODEL              LLM model (default: claude-opus-4.6)
  --max-tokens N             Max output tokens (default: 16384)
  --rerun-computations       Re-run computation scripts before verification
  --timeout N                Computation timeout in seconds (default: 60)
  --write-report             Write VERIFICATION.md into workspace
```

The verifier evaluates each Established Result for mathematical/physical validity, checks chain coherence between results, and outputs a verdict: VALID, PARTIALLY_VALID, INVALID, or INCONCLUSIVE.

## Architecture

Eight agent roles take turns in a main loop. Each agent gets a fresh context per call (no conversation history). All research state lives in a structured `ResearchState` object (persisted as `RESEARCH_GRAPH.json`), with Markdown files rendered from it. The workspace is a separate git repo.

```
                 ┌────────────┐
                 │  Surveyor   │  (runs once before loop,
                 │ (one-shot)  │   can be re-invoked mid-loop)
                 └──────┬─────┘
                        ▼
┌─────────────────────────────────────────────────┐
│                   Main Loop                      │
│                                                  │
│  ┌──────────────┐    ┌──────────────────────┐   │
│  │ Orchestrator  │───>│  Dispatch to Agent    │   │
│  │ (plan next    │    │                      │   │
│  │  task)        │    │  researcher (1-shot) │   │
│  └──────────────┘    │  computer  (agentic) │   │
│         ▲            │  reviewer  (1-shot)  │   │
│         │            │  critic    (1-shot)  │   │
│         │            │  compress / format   │   │
│  ┌──────┴───────┐    │                      │   │
│  │ ResearchState │<───└──────────────────────┘   │
│  │ (JSON + MD    │                               │
│  │  snapshots)   │                               │
│  └──────────────┘                                │
└─────────────────────────────────────────────────┘
```

### Agents

| Agent | Role | Mode | Context source | Mutates |
|-------|------|------|----------------|---------|
| **Surveyor** | Maps the research landscape before the main loop | One-shot | Problem statement + ResearchState | `BackgroundSurvey` on ResearchState |
| **Orchestrator** | Plans next task, mutates state via tools | Agentic (12 tools) | ResearchState via renderers | ResearchState, `CURRENT_TASK.md` |
| **Researcher** | Analytical reasoning, derivation | One-shot (structured JSON) | Task + target entity + method hints + light state | Evidence on RQ/WH |
| **Computer** | Computational work via code | Agentic (4 tools) | Task + target entity + method hints + light state | Evidence on RQ/WH |
| **Reviewer** | Adversarial review without code | One-shot (structured JSON) | Focused package: WH + evidence + light state | ReviewResult on WH |
| **Deep Critic** | Strategic review — research direction, coherence | One-shot (structured JSON) | ResearchState via `render_critic_context()` | Critique objects |
| **Compressor** | Archives and shrinks files exceeding size thresholds | One-shot | Target file | Compressed target file |
| **Formatter** | Produces clean `ANSWER.md` from final research state | One-shot | ResearchState via renderers | `ANSWER.md` |

### Research Lifecycle

The typical lifecycle of a claim: **RQ → evidence → WH → review → ER**

1. Orchestrator creates a **Research Question** (RQ) and dispatches to researcher or computer
2. Researcher/Computer produces **Evidence** (reasoning or code+output), stored on the RQ
3. Orchestrator formulates a **Working Hypothesis** (WH) from the evidence (auto-copies evidence from RQ)
4. Orchestrator dispatches to **Reviewer** with the WH + evidence
5. Reviewer submits review (verdict: VERIFIED/REFUTED/INCONCLUSIVE, summary, details)
6. If VERIFIED: Orchestrator promotes WH to **Established Result** (ER)

Entity numbers are unified — RQ-003 → WH-003 → ER-003.

### Review Stack

Results go through layered review before being promoted to "Established":

1. **Reviewer** — adversarial examination of evidence, methodology, and logical consistency
2. **Strategic critique** — Deep Critic reviews research direction and inter-result coherence
3. **Dependency tracking** — All prerequisite results must themselves be Established

The orchestrator enforces promotion criteria via its `promote_hypothesis` tool and forces periodic critic passes every N iterations. A post-integration validation pipeline (`validation.py`) runs 4 checks on ResearchState after every orchestrator pass — demoting unreviewed ERs, stripping phantom labels, and ensuring critique resolution consistency.

### Workspace Files

All research state is persisted under `workspaces/<run>/` (each run gets a timestamped subdirectory, gitignored from this repo, has its own git):

| File | Purpose |
|------|---------|
| `RESEARCH_STATE.md` | Established results, working hypotheses, evidence, dead ends |
| `CURRENT_TASK.md` | Current task with YAML frontmatter + structured dispatch context |
| `RESEARCH_GRAPH.json` | Authoritative structured state (ResearchState serialized as JSON) |
| `EVIDENCE_LOG.md` | Log of all evidence and review results |
| `CRITIQUE_LOG.md` | All critiques with severity and resolution status |
| `METRICS.md` | Token usage, file sizes, alerts |
| `EVENT_LOG.jsonl` | Unified event log — LLM call metadata + scaffolding intervention events |
| `VERIFICATION.md` | Independent verification report (written by `--write-report`) |
| `computations/` | Saved Python scripts from computer agent |

## Project Structure

```
src/sciralph/
  main.py              — Entry point, CLI argument parsing
  engine.py            — Main loop (LoopState): orchestrate → validate → enrich → dispatch → compress → git
  research_state.py    — ResearchState dataclass: authoritative structured state (hypotheses, evidence, critiques)
  renderers.py         — Snapshot renderers (state → Markdown) + per-agent context renderers
  orchestrator_tools.py — OrchestratorToolExecutor: 12 state-mutation tools for orchestrator
  tools.py             — ToolExecutor + ToolCall for computer (document_approach, execute_python, submit_result, report_progress)
  categories.py        — CompensationCategory enum (call_reliability, state_invariants, loop_control, output_normalization)
  validation.py        — Post-integration checks (4 checks on ResearchState) + termination gates
  verify.py            — Independent verification script (Claude Opus, streaming)
  config.py            — Config dataclass (model, provider, thresholds, timeouts)
  llm.py               — Provider-agnostic LLM wrapper (call_llm, run_agent_loop) with retry + audit logging
  task.py              — Task dataclass + TaskType enum + TASK_TYPE_AGENT_MAP for typed task handling
  workspace.py         — File I/O + git operations on workspace/ + log_scaffold_event() + log_llm_call()
  markdown.py          — YAML frontmatter parsing, section extraction, critique helpers
  sandbox.py           — Python script execution with timeout
  metrics.py           — MetricsTracker (token counts, alerts, Markdown rendering)
  agents/
    base.py            — BaseAgent ABC with template method + retry + tool-use dispatch
    evidence_base.py   — EvidenceAgent base class shared by researcher and computer (context building, evidence storage)
    orchestrator.py    — Plans tasks, mutates ResearchState via tools
    researcher.py      — Analytical reasoning (one-shot structured JSON); writes Evidence to RQ/WH
    computer.py        — Computational work via code (agentic); writes Evidence to RQ/WH
    reviewer.py        — Adversarial review (one-shot structured JSON); writes ReviewResult to WH
    critic.py          — Strategic review (one-shot structured JSON); writes Critique objects
    compressor.py      — File size management
    formatter.py       — Produces ANSWER.md from final research state (one-shot)
    surveyor.py        — Background surveyor: maps the research landscape (one-shot)
  prompts/             — Static .md system prompt files (one per agent): orchestrator.md, researcher.md, computer.md, reviewer.md, deep_critic.md, compressor.md, formatter.md, surveyor.md, verifier.md (independent verify script)
  providers/
    base.py            — LLMProvider ABC + ProviderResponse dataclass
    anthropic.py       — Anthropic Claude adapter
    openai.py          — OpenAI adapter
    google.py          — Google Gemini adapter
    huggingface.py     — HuggingFace Inference Providers adapter
  models.yaml          — Model registry (friendly keys → provider + model_id + env_key + cost)
tests/                 — ~910 pytest tests across 29 files
problems/
  tier1/               — 10 core problems
  tier2/               — 12 advanced problems
  critpt/              — Critical-path problems (quantum error correction decomposition)
run_and_verify.sh      — Run a problem then verify results in one command
```

## Problem Definitions

Problems are defined in YAML files under `problems/tier1/` and `problems/tier2/`:

```yaml
problem: |
  Derive the Hawking temperature of a Schwarzschild black hole
  from the Euclidean path integral approach...

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
