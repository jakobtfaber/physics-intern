# PhysicsIntern

A multi-agent scaffolding system for autonomous scientific research in mathematics and theoretical physics.

## What is this?

PhysicsIntern takes a problem stated in plain language (e.g. "derive the Hawking temperature from the Euclidean path integral") and works through it autonomously — breaking it into sub-problems, performing derivations, writing and running verification code, and critically reviewing its own results — until it produces a coherent, verified solution.

The project ships two research modes: a **multi-agent pipeline** (the default) that orchestrates nine specialised roles, and **Autophysicist**, a lighter single-agent loop where one Research Manager dispatches ephemeral sub-agents on the fly.

**Multi-agent pipeline.** Nine specialised LLM agent roles take turns in a loop. A **surveyor** maps the research landscape before the main loop begins. A **planner** produces the initial research strategy (and can revise it when critiques demand). The **orchestrator** dispatches research questions to a **researcher** (analytical reasoning) or **computer** (code execution), and formulates working hypotheses from the evidence. Then the **reviewer** provides adversarial review (auto-triggered).  A **deep critic** periodically audits strategy and inter-result coherence, filing typed critiques that are routed back to the **planner** (for strategy revision) or to an **adjudicator** (for challenges on established results). Finally a **formatter** produces a clean `ANSWER.md` from the final research state.

No agent carries conversation history: each call starts from a fresh context. All research state lives in a structured `ResearchState` object — agents mutate it via tools, and Markdown files are rendered from it for git snapshots. The workspace is version-controlled with git, so every step is recoverable.

Supports multiple LLM providers (Anthropic, OpenAI, Google Gemini, HuggingFace) via a provider abstraction layer with a `models.yaml` registry.

## Quick Start

```bash
# Install (requires Python 3.12+ and uv)
uv sync --extra testing

# For non-Anthropic providers, install the relevant extra:
uv sync --extra openai          # OpenAI
uv sync --extra google          # Google Gemini
uv sync --extra huggingface     # HuggingFace Inference Providers
uv sync --extra all-providers   # all of the above

# To serve local models on a Linux GPU cluster (no-op on macOS):
uv sync --extra local

# Serve DeepSeek V4 locally through vLLM on the H100 Slurm cluster.
# This launches 4 external replicas behind the documented load-balancer flow.
# See DOCUMENTATION.md for the required cu129 vLLM install and DeepGEMM wheel.
# Idle serve replicas auto-cancel after 2 hours without completed, running, or waiting requests.
# Queued replacements wait without timing out; failed replacements are cancelled before resubmit.
./serve/serve.slurm --model deepseek-ai/DeepSeek-V4-Pro

# Run full CritPt evaluation against existing serve jobs. The eval wrapper
# defaults to hopper-prod to avoid spot/preemption cancellations on hopper-cpu.
# Resume automatically reuses scoreable `ANSWER.md` files and removes empty or
# formatter-rejection artifacts before granting an unfinished workspace more budget.
# If no --serve-job is provided, the load balancer discovers matching running
# vLLM serve jobs from Slurm every 10 minutes and adds healthy backends.
# Full evaluations default to a 3-day Slurm time limit.
# The load balancer caps active requests per backend (default: 8) and queues
# overflow requests so newly discovered backends increase capacity safely.
./serve/full_eval.slurm --model deepseek-ai/DeepSeek-V4-Pro --serve-job <JOB_ID>

# Run a research problem (requires model API key in .env or env var)
uv run physics_intern problems/critpt/quantum_error_correction_main.yaml --model gemini-3-flash-preview
```

### Environment Variables

Set API keys for the providers you want to use (in `.env` or as env vars):


| Variable            | Provider                        |
| ------------------- | ------------------------------- |
| `ANTHROPIC_API_KEY` | Anthropic                       |
| `OPENAI_API_KEY`    | OpenAI                          |
| `GOOGLE_API_KEY`    | Google Gemini (default)         |
| `HF_TOKEN`          | HuggingFace Inference Providers |


### CLI Options

```
physics_intern [problem.yaml] [options]

  problem.yaml                Problem YAML file (default: problems/critpt/quantum_error_correction_main.yaml)
  --model MODEL               LLM model key (default: from config.default.yaml, resolved via models.yaml)
  --replay DIR                Replay console log from a workspace (no run)
  --max-iterations N          Max loop iterations (default: see config.default.yaml)
  --workspace-dir DIR         Workspace directory (default: workspaces/YYYYMMDD_HHMMSS_<problem>)
  --resume DIR                Resume from existing workspace if DIR exists
  --config FILE               Path to config YAML file (overrides defaults)
```

All defaults live in `config.default.yaml` (single source of truth). Override with a config YAML file (`--config`) or individual CLI flags. The precedence is: CLI flags > config file > `config.default.yaml`.

### Tests and CI

```bash
uv sync --extra quality --extra testing
uv run ruff check tests src scripts serve
uv run ruff format --check tests src scripts serve
uv run python -m pytest ./tests/ --cov=physics_intern --cov-report=term-missing
```

GitHub Actions runs the same checks on pull requests and pushes to `main`/`master` (Python 3.12 and 3.13). The workflow uses locked `uv` installs with dependency caching, uploads coverage reports, and only runs for code, test, workflow, dependency, script, and serve-entrypoint changes.
Coverage is enforced at `75%` for the core package code; CLI entrypoints and external-provider integration adapters are omitted from the threshold because they are better covered by focused smoke/integration tests.

Install the optional pre-commit hook to run Ruff before committing:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

## Documentation

For everything else — alternative modes (Autophysicist, one-shot baseline, two-step baseline, RSA), serving local models with vLLM, batch scripts, supported models, architecture deep-dive, project structure — see [DOCUMENTATION.md](DOCUMENTATION.md).
