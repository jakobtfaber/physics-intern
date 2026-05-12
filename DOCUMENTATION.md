# PhysicsIntern Documentation

Detailed reference for PhysicsIntern. See [README.md](README.md) for the project overview and quick start.

## Table of Contents

- [Configuration](#configuration)
  - [Soft-exit triggers](#soft-exit-triggers)
  - [Periodic best-guess snapshots](#periodic-best-guess-snapshots)
- [Verification](#verification)
- [Autophysicist](#autophysicist)
- [One-shot Baseline](#one-shot-baseline)
- [Two-step Baseline](#two-step-baseline)
- [RSA (Recursive Self-Aggregation)](#rsa-recursive-self-aggregation)
- [Serving Local Models with vLLM](#serving-local-models-with-vllm)
- [Scripts](#scripts)
- [Supported Models](#supported-models)
- [Architecture](#architecture)
  - [Agents](#agents)
  - [Research Lifecycle](#research-lifecycle)
  - [Review Stack](#review-stack)
  - [LLM Failure Compensation](#llm-failure-compensation)
  - [Workspace Files](#workspace-files)
- [Problem Definitions](#problem-definitions)
- [Project Structure](#project-structure)

## Configuration

The `max_output_tokens` budget per LLM call is **not** a general default — it is defined per-model in `models.yaml` and cannot be overridden via CLI or config YAML.

### Soft-exit triggers

In addition to `max_iterations`, the loop accepts three optional cumulative budgets — `max_wall_seconds`, `max_total_output_tokens`, and `max_cost_usd` (USD, computed from `models.yaml` pricing). All default to `0` (disabled). When any gate fires, the loop finishes its current iteration and a forced formatter writes a best-effort `ANSWER.md` (status: `partially_complete`); the triggering gate name appears in the final commit message. `max_wall_seconds` is per-`run()` invocation — it restarts on resume; the token and cost budgets are cumulative across resumes.

To grant more budget after a soft-exit and continue the run, delete `ANSWER.md` and re-run with `--resume`. `ANSWER.md` is the canonical "this run is done" signal: present ⇒ resume refuses; absent ⇒ resume resets `partially_complete` to `in_progress` and continues from the last committed iteration.

### Periodic best-guess snapshots

Setting `best_guess_every_n: N` (default `0` = disabled) makes the forced formatter run every N iterations and write `BEST_GUESS.md` as a side artifact, with its own commit. The snapshot never touches `ANSWER.md`, `RESEARCH_STATE.md`, or any other state agents see — purely an observability aid.

## Verification

After a run completes, you can independently verify the scientific results using a stronger model (Claude Opus by default):

```bash
# Verify a completed workspace
uv run python -m physics_intern.verification workspaces/<run_dir>/

# Run + verify in one command
./scripts/run_and_verify.sh --max-iterations 10
```

```
python -m physics_intern.verification <workspace_dir> [options]

  --model MODEL              LLM model (default: claude-4.6-opus)
```

The verifier writes `VERIFICATION.md` into the workspace. It evaluates each Established Result for mathematical/physical validity, runs a process audit, checks chain coherence between results, and outputs a verdict: VALID, PARTIALLY_VALID, INVALID, or INCONCLUSIVE. The problem definition is auto-loaded from `problem.yaml` in the workspace (copied there by `main.py` at run start).

## Autophysicist

Autophysicist is a single-agent iterative research mode. A Research Manager receives the problem, dispatches ephemeral sub-agents (with optional sandboxed code execution), and records results in two memory systems — a permanent memory (append-only, always visible) and a scratchpad (rolling window of the last N entries). Each iteration starts from a fresh context; the Manager can only "remember" what it wrote to memory. A per-iteration token budget triggers a wind-down phase that removes the sub-agent dispatch tool, forcing the Manager to consolidate and end the turn.

When the Manager is confident in a solution it calls `submit_final_answer`, which terminates the run and triggers formal evaluation (symbolic SymPy comparison + numerical fallback) against the ground truth in the problem YAML.

```bash
# Run a single problem
uv run physics_intern_autophysicist problems/tier1/hydrogen_fine_structure.yaml --model claude-4.6-opus

# With custom budget and iteration limits
uv run physics_intern_autophysicist problems/tier1/hydrogen_fine_structure.yaml \
  --max-iterations 30 --token-budget 100000 --tool-call-cap 20

# Resume an interrupted run
uv run physics_intern_autophysicist problems/tier1/hydrogen_fine_structure.yaml \
  --resume workspaces/<run_dir>
```

```
physics_intern_autophysicist <problem.yaml> [options]

  problem.yaml                Problem YAML file (required)
  --model MODEL               LLM model key (default: from config.default.yaml)
  --config FILE               Path to config YAML file
  --max-iterations N          Max Manager iterations (default: 50)
  --token-budget N            Per-iteration token budget for wind-down trigger (default: 64000)
  --tool-call-cap N           Max tool calls per iteration (default: 15)
  --max-rounds N              Max LLM rounds per iteration (default: 30)
  --scratchpad-window N       Visible scratchpad entries (default: 5)
  --sandbox-timeout N         Code execution timeout in seconds (default: 60)
  --workspace-dir DIR         Workspace directory (default: auto-generated)
  --resume DIR                Resume from an existing workspace
```

### Multiple concurrent runs (pass@k)

Run N independent instances on the same problem to collect pass@k statistics:

```bash
uv run python scripts/run_multiple_autophysicist.py problems/tier1/hydrogen_fine_structure.yaml \
  --runs 20 --concurrency 5 --model claude-4.6-opus --max-iterations 30
```

Results are written to a JSON file with per-run metrics and an aggregate summary (correct / incorrect / inconclusive / no_answer counts).

## One-shot Baseline

Run a single LLM call on a problem with no scaffolding — useful for benchmarking raw model capability against the multi-agent pipeline:

```bash
uv run python -m physics_intern.one_shot problems/critpt/quantum_error_correction_main.yaml
uv run python -m physics_intern.one_shot problems/critpt/quantum_error_correction_main.yaml --model gpt-5.4-high
uv run python -m physics_intern.one_shot problems/critpt/quantum_error_correction_main.yaml --runs 10  # multiple runs for statistics
uv run python -m physics_intern.one_shot problems/critpt/quantum_error_correction_main.yaml --config my_config.yaml
```

```
python -m physics_intern.one_shot <problem.yaml> [options]

  --model MODEL              LLM model key (default: from config.default.yaml)
  --config FILE              Path to config YAML file (overrides defaults)
  --runs N                   Number of runs for batch benchmarking
  --output-dir DIR           Directory for batch result JSON files (default: results/one_shot/)
  -o FILE                    Save response with metadata to a Markdown file
```

Answers are auto-evaluated against the known answer in the problem YAML (symbolic SymPy comparison + numerical fallback).

## Two-step Baseline

A second baseline that mirrors the [CritPt benchmark](https://github.com/CriticalPathAI/benchmarks)'s default `parsing=False` (two-step) procedure rather than its one-step variant. The model is called twice with **the same** system prompt, both copies of which match critpt's rendered output byte-for-byte:

1. **Derive** — user message contains the problem only (no code template). The model produces a free-form derivation ending with `Final Answer:`.
2. **Populate** — the conversation continues with the call-1 response and a new user message carrying the parse instruction + code template. The model populates the template without further reasoning.

This separation lets reasoning models concentrate on the math first and the Python syntax second, which on hard problems can outperform asking for both at once.

```bash
uv run python -m physics_intern.two_steps problems/critpt/quantum_error_correction_main.yaml
uv run python -m physics_intern.two_steps problems/critpt/quantum_error_correction_main.yaml --model gpt-5.4-high
uv run python -m physics_intern.two_steps problems/critpt/quantum_error_correction_main.yaml --config my_config.yaml
```

```
python -m physics_intern.two_steps <problem.yaml> [options]

  --model MODEL              LLM model key (default: from config.default.yaml)
  --config FILE              Path to config YAML file (overrides defaults)
  --workspace-dir DIR        Workspace directory (default: auto-generated)
  -o FILE                    Save call-2 response (populated code) to a Markdown file
```

The mode requires the problem YAML to define an `answer_template`; without one, the second call has nothing to populate and the runner exits with an error (use `physics_intern.one_shot` instead). Each run creates a workspace under `workspaces/<timestamp>_<problem>_<model>_two_steps/` containing `PROBLEM.md`, `DERIVATION.md` (call-1 output), `ANSWER.md` (call-2 populated template), `VERIFICATION.md`, and `config.json`.

## RSA (Recursive Self-Aggregation)

RSA maintains a population of N candidate solutions and iteratively refines them by aggregating random subsets of K candidates over T rounds (total LLM calls = N * T). The final answer is chosen by majority vote.

```bash
uv run python -m physics_intern.rsa problems/critpt/quantum_error_correction_main.yaml
uv run python -m physics_intern.rsa problems/critpt/quantum_error_correction_main.yaml -N 6 -K 2 -T 4
uv run python -m physics_intern.rsa problems/critpt/quantum_error_correction_main.yaml --model gpt-5.4-high --concurrency 4
uv run python -m physics_intern.rsa problems/critpt/quantum_error_correction_main.yaml --config my_config.yaml
```

```
python -m physics_intern.rsa <problem.yaml> [options]

  --model MODEL              LLM model key (default: from config.default.yaml)
  -N INT                     Population size (default: 6)
  -K INT                     Aggregation subset size (default: 2)
  -T INT                     Number of rounds (default: 4)
  --config FILE              Path to config YAML file (overrides defaults)
  --concurrency N            Max parallel LLM calls within a round (default: N)
  --workspace-dir DIR        Workspace directory (default: auto-generated under workspaces/)
  -o FILE                    Save response with metadata to a Markdown file
```

Each run creates a workspace under `workspaces/<timestamp>_<problem>_<model>_rsa/` containing `PROBLEM.md`, `ANSWER.md`, `VERIFICATION.md`, `config.json`, and `rsa_result.json` (full per-round metrics).

### Multiple concurrent RSA runs (pass@k)

Run N independent RSA instances on the same problem to collect pass@k statistics:

```bash
uv run python scripts/run_multiple_rsa.py problems/critpt/yaml/Challenge_1_main.yaml \
  --runs 10 --concurrency 3 -N 6 -K 2 -T 4 --model claude-4.6-opus
```

Each run produces its own workspace at `workspaces/<timestamp>_<problem>_<model>_rsa_runNNN/` (same naming pattern as the other modes), and an aggregate summary JSON is written to `results/multiple_rsa/`. Note that each RSA run itself fans out up to N concurrent LLM calls, so effective in-flight calls ≈ `--concurrency × N`.

## Serving Local Models with vLLM

Running a local model is a two-step process: first **serve** the model with vLLM, then **run** the problem against it.

### End-to-end example (Nemotron Super 120B)

```bash
# Step 1 — Serve the model (submits a Slurm job; returns immediately)
./serve/serve.slurm \
  --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16 \
  --nodes 1 \
  --gpus-per-node 8

# Note the Slurm job ID printed to stdout, e.g. 12345.
# Connection details are written to serve/logs/vllm/<job_id>/endpoint.env
# as soon as the job starts (before the model finishes loading).

# Step 2 — Run a problem against the served model
# eval.slurm reads endpoint.env, waits for vLLM to be healthy (up to 4 h),
# then launches the evaluation.
./serve/eval.slurm \
  --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16 \
  --problem problems/critpt/quantum_error_correction_main.yaml \
  --serve-job 12345
```

### Huge-model example (GLM-5.1)

`zai-org/GLM-5.1` runs on 3 H100 nodes with the default `models.yaml` serve
config. The script pins DeepGEMM, Triton, and TorchInductor JIT caches to
job-local `/tmp` paths and uses the loaded CUDA 12.9 toolkit so GLM can run
without `--enforce-eager`.

```bash
./serve/serve.slurm \
  --model zai-org/GLM-5.1 \
  --nodes 3 \
  --gpus-per-node 8

./serve/eval.slurm \
  --model zai-org/GLM-5.1 \
  --problem problems/critpt/quantum_error_correction_main.yaml \
  --serve-job <JOB_ID>
```

`zai-org/GLM-5.1` is very large; in our Slurm tests it required 3 nodes (`--nodes 3`) with 8 GPUs per node.

### Huge-model example (DeepSeek V4 Pro)

`deepseek-ai/DeepSeek-V4-Pro` needs vLLM 0.20+ with
`DeepseekV4ForCausalLM`, CUDA 12.9-compatible wheels, and DeepGEMM 2.5. The
tested cluster install is:

- `vllm==0.20.2rc1.dev73+g5d0fd8703.cu129`
- `deep-gemm==2.5.0+891d57b`
- `transformers==5.8.0`
- `nvidia-cuda-nvcc-cu12==12.9.86`
- `nvidia-cuda-cccl-cu12==12.9.27`
- `nvidia-cusparse-cu12==12.5.10.65`

#### Install vLLM for CUDA 12.9

Start from the project venv, then install the cu129 vLLM nightly wheel on a
GPU node. The repo script is the source of truth:

```bash
sbatch serve/upgrade_vllm.sh
```

The script performs the exact cluster install:

```bash
source "$HOME/.bashrc"
module use /admin/opt/modulefiles
module load glibc/2.38 cuda/12.9

cd /fsx/joel_niklaus/projects/open-dirac
source .venv/bin/activate
uv pip uninstall vllm || true
uv pip install -U vllm \
  --prerelease=allow \
  --python-platform x86_64-manylinux_2_34 \
  --torch-backend=cu129 \
  --extra-index-url https://wheels.vllm.ai/nightly/cu129
glibc-fix /fsx/joel_niklaus/projects/open-dirac/.venv/bin/python3.13
```

`glibc-fix` is required on the Ubuntu 20.04 H100 image because the vLLM wheel
uses a newer manylinux/glibc baseline. The serve script also loads
`glibc/2.38` and `cuda/12.9` at runtime.

If the venv is not activated, pass the interpreter explicitly:

```bash
uv pip install -U vllm \
  --prerelease=allow \
  --python-platform x86_64-manylinux_2_34 \
  --torch-backend=cu129 \
  --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
  --python /fsx/joel_niklaus/projects/open-dirac/.venv/bin/python3.13
```

Verify the installed packages before launching:

```bash
uv pip show vllm deep-gemm transformers
uv run --no-sync python - <<'PY'
import deep_gemm
import vllm

print(vllm.__version__)
print(deep_gemm.__version__)
assert hasattr(deep_gemm, "fp8_fp4_mqa_logits")
PY
```

#### DeepGEMM setup

DeepSeek V4 sparse attention calls DeepGEMM's FP8/FP4 MQA logits kernels at
runtime. Older DeepGEMM wheels load successfully but fail on the first real
request because they do not expose `fp8_fp4_mqa_logits`.

The compatible wheel is checked into the repo and wired through
`[tool.uv.sources]`:

```toml
deep-gemm = { path = "deep_gemm-2.5.0+891d57b-cp313-cp313-linux_x86_64.whl" }
```

For normal installs, run:

```bash
uv sync --extra local
```

No manual DeepGEMM install is needed after that. Only rebuild the wheel when
changing the vLLM checkout or DeepGEMM commit; the rebuild needs CUDA headers
from the NVIDIA Python wheels (`nvidia-cuda-nvcc-cu12`,
`nvidia-cuda-cccl-cu12`, and `nvidia-cusparse-cu12`) plus unversioned
`libcudart.so`/`libnvrtc.so` links for the linker.

#### Serve DeepSeek V4 Pro

Authenticate Hugging Face first:

```bash
uv run hf auth whoami
```

Then launch the model:

```bash
./serve/serve.slurm --model deepseek-ai/DeepSeek-V4-Pro
```

The DeepSeek entry in `models.yaml` supplies the production defaults:

- 4 external replicas, each using 4 H100 nodes with 8 GPUs per node
- native vLLM data parallelism inside each replica with `dp: 4`
- tensor parallelism inside each DP rank with `tp: 8`
- no pipeline parallelism, because `DeepseekV4ForCausalLM` does not support PP
- `--enable-expert-parallel`
- `--kv-cache-dtype fp8`
- `--tokenizer-mode deepseek_v4`
- DeepSeek V4 tool and reasoning parsers
- PIECEWISE CUDA graphs capped at 128
- `benchmark_combo_kernel=false` to avoid the cluster TorchInductor autotuning
  failure
- `--performance-mode throughput` with `--max-num-batched-tokens 16384`

With the default `replicas: 4`, `serve.slurm` treats those as the normal-QOS
baseline and dispatches through `serve/multi_serve.sh`. The launcher also submits
opportunistic low-QOS replicas with `--requeue`: by default it looks at idle
`hopper-prod` nodes, subtracts the nodes needed by the normal replicas, divides
the remaining nodes by `nodes_per_replica`, and submits at least two low-QOS
replicas. Override the baseline with `--normal-replicas N`, override or disable
opportunistic capacity with `--low-replicas N` or `--low-replicas 0`, and tune
the auto minimum with `--low-min-replicas N`.

Each replica writes its endpoint to `serve/logs/vllm/<job_id>/endpoint.env`; use
`serve/load_balancer.py` or `serve/full_eval.slurm` with all job IDs for a single
`/v1` endpoint.

`serve.slurm` writes each replica endpoint to
`serve/logs/vllm/<job_id>/endpoint.env` and Slurm logs to
`serve/logs/slurm/`. It also keeps `TRITON_CACHE_DIR`,
`TORCHINDUCTOR_CACHE_DIR`, and `DG_JIT_CACHE_DIR` on job-local `/tmp` paths to
avoid stale shared-filesystem JIT artifacts, and prepends the venv NVIDIA wheel
libraries to `LD_LIBRARY_PATH` so `libnvJitLink.so.12` resolves from the cu129
wheel stack instead of inherited CUDA 12.1 paths.

The serve watchdog defaults to `--idle-shutdown 7200`: after vLLM is healthy, a
replica auto-cancels if no requests have completed and no requests are running
or waiting for 2 hours. This prevents unused multi-replica jobs from holding H100
nodes after a benchmark or load balancer stops using them.

When `serve/load_balancer.py` resubmits a failed replica, Slurm queue time does
not count against the backend health timeout. The load balancer waits while the
replacement is pending and only starts the endpoint timer once Slurm starts the
job, so queued replacements can still join the pool after the cluster frees
capacity. If a started replacement still fails to produce a healthy endpoint
within the timeout, the load balancer cancels that job before submitting another
replacement so timed-out jobs do not later become orphaned replicas.

The load balancer also supports elastic serving. It scans Slurm every 10 minutes
by default for running `vllm-serve-<model>` jobs, reads
`serve/logs/vllm/<job_id>/endpoint.env`, health-checks the endpoint, and adds
healthy backends without restarting the eval job. This lets you launch extra
replicas when the cluster is empty and have the running evaluation pick them up
automatically. If no `--serve-job` is passed to `serve/full_eval.slurm`, the eval
starts in discovery-only mode and waits for the first matching healthy backend.

The load balancer caps active requests per backend to avoid overloading DeepSeek
replicas as the backend count changes. `serve/full_eval.slurm` defaults to
`--max-active-per-backend 8` and `--queue-timeout 1800`: requests above the
current healthy-backend capacity wait in the load balancer instead of being sent
to an already saturated backend. When auto-discovery adds new replicas, capacity
increases automatically; when a backend is drained or dies, new requests wait for
the remaining capacity. The cluster eval config sets `api_timeout: 3600` so
queued requests have enough client-side timeout headroom.

To free capacity, do not call `scancel` directly unless you are willing to kill
in-flight requests. Use the drain helper so the backend receives no new requests
and is cancelled only after its active request count reaches zero:

```bash
# Auto-detect the single running critpt-physicsintern eval job.
./serve/drain_backends.py <BACKEND_JOB_ID> [<BACKEND_JOB_ID> ...] --status

# Or target the eval job explicitly if several eval jobs are running.
./serve/drain_backends.py \
  --eval-job <EVAL_JOB_ID> \
  <BACKEND_JOB_ID_1> <BACKEND_JOB_ID_2> \
  --status
```

The helper calls the eval job's local load balancer endpoint
`/cancel_when_drained/<job_id>` through `srun --overlap`. The backend is marked
`draining`, disappears from new request scheduling, and is removed from the pool
after the load balancer cancels the Slurm job. You can inspect the current pool
with:

```bash
srun --jobid=<EVAL_JOB_ID> --overlap --ntasks=1 --nodes=1 \
  curl http://localhost:9000/status
```

The default DeepSeek config is tuned for high-concurrency CritPt-style traffic.
To launch a single replica for debugging, call `serve/multi_serve.sh` directly
with `--normal-replicas 1 --low-replicas 0`.

```bash
./serve/multi_serve.sh \
  --model deepseek-ai/DeepSeek-V4-Pro \
  --normal-replicas 1 \
  --low-replicas 0 \
  --nodes-per-replica 4
```

The warmed 256-output-token benchmark results on 4x8 H100s were:

| Concurrency | Requests | Aggregate tok/s | Median latency |
| ----------- | -------- | --------------- | -------------- |
| 64          | 64       | 221.1           | 74.1s          |
| 128         | 128      | 272.9           | 120.0s         |
| 256         | 256      | 300.6           | 217.5s         |

`128` concurrent short requests is the practical knee for latency-sensitive
benchmarks, but full max-think PhysicsIntern traffic should default to `64`
concurrent problems to avoid overloading the serve pool. `256` maximizes raw
aggregate throughput but mostly buys queueing. The throughput-mode setting cuts
the 262k-context KV-cache headroom by roughly 60%, so use the 4-replica
load-balanced setup for full PhysicsIntern runs. MTP
speculative decoding was tested with
`--speculative-config '{"method":"mtp","num_speculative_tokens":1}'`, but it did
not become healthy: the engine completed graph capture and then `ApiServer_0`
died before `/health` passed. MXFP4 indexer cache was tested with
`--attention-config '{"use_fp4_indexer_cache":true}'`, but Triton emitted
e2m1x2 conversion instructions that `ptxas` rejected for H100 `sm_90a`. Keep the
stable FP8 indexer-cache path. `--enable-dbo` is intentionally not enabled
because vLLM requires DeepEP all-to-all kernels for DBO; the default
`allgather_reducescatter` backend rejects it at startup.

Run the full PhysicsIntern CritPt sweep against four replicas with:

```bash
./serve/full_eval.slurm \
  --model deepseek-ai/DeepSeek-V4-Pro \
  --serve-job <JOB_1> \
  --serve-job <JOB_2> \
  --serve-job <JOB_3> \
  --serve-job <JOB_4> \
  --runner physicsintern \
  --config config.cluster.yaml \
  --fresh \
  --workspace-base workspaces_deepseek_v4_pro \
  --nodes-per-replica 4 \
  --concurrency 64 \
  --time 24:00:00
```

`serve/full_eval.slurm` defaults the eval runner to `hopper-prod` even though it
only hosts the load balancer and PhysicsIntern subprocesses. The `hopper-cpu`
partition uses spot nodes and can preempt long evaluations mid-run, while
`hopper-prod` keeps the eval wrapper on the same stable partition as the vLLM
serve replicas. Use `--partition <name>` only when you intentionally want a
different Slurm partition.

### Prerequisites

- `uv sync --extra local`
- `uv run hf auth whoami`
- The `local` extra installs `vllm`, the required `transformers` floor, and the
  vendored `deep-gemm` wheel from `[tool.uv.sources]`; no manual DeepGEMM install
  is needed for `zai-org/GLM-5.1` or `deepseek-ai/DeepSeek-V4-Pro`.
- vendored Nemotron parser plugins live in `serve/reasoning_parsers/`

### Step 1: Serve the model

Use `serve/serve.slurm`. The script self-submits with `sbatch`, launches one `vllm serve` rank per allocated node, stores Slurm logs under `serve/logs/`, and writes connection details to `serve/logs/vllm/<job_id>/endpoint.env`.

For huge local models, the default serve wall time is 24 hours and `serve/eval.slurm` will wait up to 4 hours for the endpoint to become healthy before giving up.

```bash
# Qwen on 1 node, 1 GPU
./serve/serve.slurm \
  --model Qwen/Qwen3.5-4B \
  --nodes 1 \
  --gpus-per-node 1 \
  --reasoning-parser qwen3

# Nemotron Nano on 1 node, 1 GPU
./serve/serve.slurm \
  --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
  --nodes 1 \
  --gpus-per-node 1

# Nemotron Super on 2 nodes, 8 GPUs per node
./serve/serve.slurm \
  --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16 \
  --nodes 2 \
  --gpus-per-node 8

# GLM-5.1 on 3 nodes, 8 GPUs per node
./serve/serve.slurm \
  --model zai-org/GLM-5.1 \
  --nodes 3 \
  --gpus-per-node 8

# Kimi-K2.6: auto-launches 4 replicas (PP=4 each, 16 nodes total)
./serve/serve.slurm --model moonshotai/Kimi-K2.6

# Nemotron Cascade on 1 node, 1 GPU
./serve/serve.slurm \
  --model nvidia/Nemotron-Cascade-2-30B-A3B \
  --nodes 1 \
  --gpus-per-node 1
```

The `--reasoning-parser` flag is exposed directly. Use it to enable built-in parsers such as `qwen3`. For `nano_v3` and `super_v3`, the script automatically attaches the matching plugin file from `serve/reasoning_parsers`.

### Performance tuning notes for huge models

Per-model `vllm_args` in `models.yaml` already encode the fastest configuration we found on our cluster (H100 80 GB nodes, WekaFS-backed weights). The full experiment log is in the comments above each entry; the headline results:

Load times below are wall time of `default_loader.py` "Loading weights took N seconds" on the slowest worker. They depend heavily on whether the OS page cache is warm.


| Model                  | Tput (single req) | Tput (8-way batch) | Tput (16-way batch) | Load (cold cache)                                         | Load (warm cache)                                                      | Notes                                                                                                                                                                                                                                                                                                                   |
| ---------------------- | ----------------- | ------------------ | ------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `zai-org/GLM-5.1`      | ~46 tok/s         | ~202 tok/s         | ~333 tok/s          | ~2.5h projected without prefetch                          | ~18-22 min with prefetch in the final run; earlier warm run was ~2 min | DeepGEMM JIT cache/toolkit setup lets this run without `--enforce-eager`; BF16 beats FP8 on our stack.                                                                                                                                                                                                                  |
| `moonshotai/Kimi-K2.6` | ~99 tok/s         | ~558 tok/s         | ~920 tok/s          | ~78 min without prefetch; prefetch on cold cache untested | ~6-11 min with warm cache                                              | CUDA graphs (no `--enforce-eager`) give the dominant 4× throughput win; `--enable-expert-parallel` remains the chosen default. PP=4 (4 nodes/replica) is the minimum for stable 262k-context concurrent serving; PP=2 OOMs under load. With external DP (N replicas × PP=4), aggregate throughput scales near-linearly. |


For an apples-to-apples 4-node comparison, GLM-5.1 with TP=8/PP=4 measured
~46 tok/s single-request, ~257 tok/s at 8-way concurrency, and ~383 tok/s at
16-way concurrency. That improves aggregate throughput over the 3-node default,
but not single-request latency, and the gain is smaller than the extra 33% GPU
cost, so the default stays at 3 nodes.

Kimi-K2.6 also fits on fewer nodes. With the same canonical flags:


| Kimi nodes | Tput (single req) | Tput (8-way batch) | Tput (16-way batch) | Full-context KV headroom | Notes                                                                       |
| ---------- | ----------------- | ------------------ | ------------------- | ------------------------ | --------------------------------------------------------------------------- |
| 2          | ~94 tok/s         | ~569 tok/s         | ~938 tok/s          | 3.83× at 262k context    | Best short-prompt cost/perf, but risky for full 8-way long-context sweeps.  |
| 3          | ~92 tok/s         | ~547 tok/s         | ~920 tok/s          | 7.48× at 262k context    | Almost enough for 8-way full-context use, still less headroom than 4 nodes. |
| 4          | ~92 tok/s         | ~558 tok/s         | ~920 tok/s          | 11.12× at 262k context   | Chosen default for robust 8-way CritPt runs.                                |


At PP=4 (4 nodes), aggregate throughput scales linearly up to 256 concurrent requests per replica, reaching ~4,620 tok/s with zero failures. Per-request latency increases linearly as expected.

Kimi's `max_output_tokens` is `65536` for the PhysicsIntern multi-agent runner,
which is plenty for individual agent turns. For one-shot mode, 200k was
needed (the old 131k cap left five hard problems without parseable answer code).

**Important: PP=2 is NOT safe for production with 262k context.** Under
concurrent load, the logits `all_gather` on the last PP rank triggers CUDA OOM.
PP=4 (4 nodes/replica) is the minimum for stable full-context serving. The
default in `models.yaml` is 4 replicas × 4 nodes = 16 nodes. Scale up
replicas when more nodes are available (e.g. 8 replicas = 32 nodes).

Kimi also needs vLLM's `kimi_k2` tool and reasoning parsers for the full
PhysicsIntern agent harness. The one-shot harness works without tools, but the
agent loop sends OpenAI-style tool calls; vLLM rejects `tool_choice="auto"`
unless the server is launched with `--enable-auto-tool-choice` and
`--tool-call-parser kimi_k2`.

Two flags worth knowing whenever you add a new huge model on a networked filesystem:

- `--safetensors-load-strategy prefetch` — pulls all shards into the OS page cache via background threads. Mandatory on WekaFS / Lustre / NFS where vLLM's auto-detection often misses and falls back to slow random mmap reads. It is still the best loader we found for GLM/Kimi; exact wall time depends heavily on cluster cache state.
- `--enable-expert-parallel` — for MoE models with many experts (Kimi has 384), shard them across the TP group rather than replicate. Free win for Kimi-class models.
- `--enable-auto-tool-choice` with `--tool-call-parser kimi_k2` — required for Kimi when running the multi-agent PhysicsIntern harness, because its agents use API tool calls.

What does **not** help on H100:

- `--load-format runai_streamer` — slower than `prefetch` on Weka (~6 min vs ~2 min for GLM on a warm cache).
- FlashAttention 4 — only supported on Blackwell (SM100+); Hopper falls back to FA3 (which is already what we use).
- ngram / EAGLE / Medusa / MTP / suffix speculative decoding for Kimi-K2.6 — all blocked: Kimi-K2.6 dropped MTP layers, no draft weights ship with it, suffix decoding's `arctic-inference` build needs C++20 `<span>` (GCC 9.4 lacks it), and ngram is incompatible with PP > 1 in vLLM 0.19.1. Pure-TP multi-node ngram works but is slower than the no-spec config because inter-node TP all-reduce dominates.
- `--async-scheduling`, `--mm-encoder-tp-mode data`, and the Kimi-K2.5 Eagle3 draft for Kimi-K2.6 — no throughput win or not runnable. K2.5 Eagle3 cannot be used with PP > 1, and TP-only Kimi trips the multimodal vision-tower path in vLLM 0.19.1.
- `zai-org/GLM-5.1-FP8` on this pip/wheel stack — no-eager repeatedly fails in DeepGEMM FP8 warmup with `CUDA_ERROR_FILE_NOT_FOUND`, even with isolated DeepGEMM and TorchInductor caches. Eager FP8 serves, but the best measured MTP=3 run was only ~11 tok/s single-request and ~157 tok/s at 16-way concurrency.
- `--kv-cache-dtype fp8` for Kimi-K2.6 — ~18% slower single-request (per-step dequant) and the only benefit is ~50% KV-cache memory, which is unused: the champion runs at ~12-15% KV utilization at 8-way concurrency.
- Reducing pipeline parallelism (PP=2/TP=8 on 2 nodes vs PP=4/TP=8 on 4 nodes) — gives linear per-GPU scaling (~43 vs ~90 tok/s single-req), no per-request latency win from fewer stages. Benchmark at high concurrency confirms PP depth does not help aggregate throughput: PP=2 peaks at ~2,468 tok/s vs PP=4 at ~2,384 tok/s. However, **PP=2 OOMs under concurrent load at 262k context** (CUDA OOM in logits `all_gather`), so PP=4 is the minimum safe config for production.

To go faster than the per-replica ceiling, use **external data parallelism**: run multiple replicas of the 4-node Kimi serve behind a load balancer.

#### External DP: scaling throughput with multiple replicas

Kimi-K2.6 does NOT fit on a single node (72 GiB weights per GPU; OOM even at 131k context). Native vLLM `--data-parallel-size` requires PP=1, so it cannot be used. Instead, launch N independent serve jobs and either use the load balancer or client-side round-robin:

```bash
# Launch 4 normal-QOS replicas plus auto-sized low-QOS replicas.
./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --normal-replicas 4 --nodes-per-replica 4

# Benchmark with client-side round-robin across all replicas
uv run python scripts/benchmark_throughput.py --serve-job <job1> <job2> <job3> <job4>

# Or start a load balancer for production use (single /v1 endpoint)
uv run python serve/load_balancer.py <job1> <job2> <job3> <job4>
```

**Measured scaling** (H100 80 GB, max-num-seqs=64, 512 output tokens):


| Config                      | Nodes | Agg tok/s (peak) | Scaling |
| --------------------------- | ----- | ---------------- | ------- |
| 1x replica (PP=2, baseline) | 2     | 2,468            | 1.0x    |
| 1x replica (PP=4)           | 4     | 2,384            | 1.0x    |
| 3x replicas (PP=2 each)     | 6     | 5,205            | 2.1x    |
| 5x replicas (PP=2 each)     | 10    | 11,250           | 4.6x    |
| 8x replicas (PP=2, proj.)   | 16    | ~19,700          | ~8.0x   |


Key findings:

- **PP depth does not increase throughput** (PP=2 ≈ PP=4). More PP stages only add KV headroom.
- **PP=4 (4 nodes/replica) is the production default** — PP=2 OOMs under concurrent load at 262k context.
- **External DP scales near-linearly** — each replica adds ~2,400 tok/s capacity.
- Saturation requires enough concurrent requests to fill all replicas (≥ N × 64).

Local model keys match Hub repo IDs:

- `Qwen/Qwen3.5-4B`
- `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16`
- `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16`
- `nvidia/Nemotron-Cascade-2-30B-A3B`
- `moonshotai/Kimi-K2.6` (4 replicas × 2 nodes = 8 nodes; 1T-parameter MoE, native int4 quantization)
- `zai-org/GLM-5.1` (3 nodes; sparse-attention MoE)

### Step 2: Run the problem

**Recommended: use `serve/eval.slurm`**, which reads the serve job's `endpoint.env` and exports `VLLM_BASE_URL` automatically so the Python client connects to the correct node:

```bash
./serve/eval.slurm \
  --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16 \
  --problem problems/critpt/quantum_error_correction_main.yaml \
  --serve-job <JOB_ID>
```

**Alternative: run manually from any node.** Source the endpoint env and run:

```bash
source serve/logs/vllm/<job_id>/endpoint.env
export VLLM_BASE_URL="${BASE_URL}"
uv run python -m physics_intern.one_shot \
  problems/critpt/quantum_error_correction_main.yaml \
  --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16
```

The Python vLLM provider defaults to `http://localhost:8000/v1` but respects the `VLLM_BASE_URL` environment variable, which overrides the default with the serve job's head node IP.

### Step 3 (optional): Full benchmark sweep

To run the **entire CritPt set (70 problems) in parallel** against a vLLM serve job, use `serve/full_eval.slurm`. It self-submits the eval runner to `hopper-prod` by default to avoid spot/preemption cancellations on `hopper-cpu`, starts a load balancer when multiple serve jobs are provided, and auto-fans out via `scripts/run_critpt_oneshot.py`, with `--concurrency` parallel in-flight requests sharing the same endpoint:

```bash
./serve/full_eval.slurm \
  --model moonshotai/Kimi-K2.6 \
  --serve-job <SERVE_JOB_ID> \
  --concurrency 64 \
  --time 8:00:00
```

Submission JSONs land in `results/critpt_oneshot/<model_slug>/<timestamp>/`. To resume an interrupted run, pass `--resume <DIR>` pointing at that timestamped directory; completed problems are skipped automatically and the original generation/run config is reloaded from `batch_metadata.json`.

## Scripts

### General


| Script                                  | Purpose                                                                                  |
| --------------------------------------- | ---------------------------------------------------------------------------------------- |
| `scripts/run_and_verify.sh`             | Run a research session then verify results in one command                                |
| `scripts/one_shot_batch.sh`             | Batch-run the one-shot baseline across all problems in a folder                          |
| `scripts/run_multiple.py`               | Run N concurrent multi-agent (physics_intern) instances for pass@k evaluation            |
| `scripts/run_multiple_oneshot.py`       | Run N concurrent one-shot instances for pass@k evaluation                                |
| `scripts/run_multiple_rsa.py`           | Run N concurrent RSA instances for pass@k evaluation                                     |
| `scripts/run_multiple_autophysicist.py` | Run N concurrent autophysicist instances for pass@k evaluation                           |
| `scripts/test_model.py`                 | Smoke-test a model's reasoning and tool-call support (`--list` to show available models) |


### CritPt Benchmark

These scripts run PhysicsIntern against the [CritPt](https://github.com/CriticalPathAI/benchmarks) benchmark suite (70 problems in `problems/critpt/yaml/`). They produce CritPt-format submission JSONs, support resume from interrupted runs, and handle rolling parallelism.


| Script                                 | Purpose                                                                                |
| -------------------------------------- | -------------------------------------------------------------------------------------- |
| `scripts/run_critpt_physics_intern.py` | Batch-run all CritPt problems through the full multi-agent pipeline                    |
| `scripts/run_critpt_oneshot.py`        | Batch-run all CritPt problems through the one-shot baseline                            |
| `scripts/run_critpt_two_steps.py`      | Batch-run all CritPt problems through the two-step baseline (critpt's `parsing=False`) |
| `scripts/run_critpt_rsa.py`            | Batch-run all CritPt problems through RSA                                              |
| `scripts/analyze_batch.py`             | Analyze token usage and per-agent metrics across a batch run                           |
| `scripts/fill_missing_critpt.py`       | Fill missing submission JSONs with template answers for a complete 70-problem set      |


All batch scripts support `--resume <output-dir>` to continue an interrupted run. On resume, all parameters (model, max_tokens, problem subset, RSA N/K/T, etc.) are recovered from the saved `batch_metadata.json` — no need to re-specify them. Completed submissions are automatically skipped.

```bash
# Fresh run
uv run python scripts/run_critpt_oneshot.py --model gemini-3-flash-preview --output-dir results/run1/

# Resume an interrupted run (all params recovered from batch_metadata.json)
uv run python scripts/run_critpt_oneshot.py --resume results/run1/

# Resume with different runtime settings (concurrency, timeout are overridable)
uv run python scripts/run_critpt_rsa.py --resume results/rsa_run/ --concurrency 5
```

## Supported Models

Models are registered in `models.yaml`. Use the friendly key with `--model`:


| Key                      | Provider    | Model                   |
| ------------------------ | ----------- | ----------------------- |
| `claude-4.6-opus`        | Anthropic   | claude-opus-4-6         |
| `claude-4.6-sonnet`      | Anthropic   | claude-sonnet-4-6       |
| `gpt-5.4-high`           | OpenAI      | gpt-5.4 (high effort)   |
| `gpt-5.4-medium`         | OpenAI      | gpt-5.4 (medium effort) |
| `gpt-5.4-pro`            | OpenAI      | gpt-5.4-pro             |
| `gemini-3.1-pro-preview` | Google      | gemini-3.1-pro-preview  |
| `gemini-3-flash-preview` | Google      | gemini-3-flash-preview  |
| `deepseek-v3.2`          | HuggingFace | DeepSeek-V3.2           |
| `kimi-k2.5`              | HuggingFace | Kimi-K2.5               |
| `glm-5`                  | HuggingFace | GLM-5                   |
| `gpt-oss-120b`           | HuggingFace | gpt-oss-120b            |
| `minimax-m2.5`           | HuggingFace | MiniMax-M2.5            |
| `qwen-3.5-397B-A17B`     | HuggingFace | Qwen3.5-397B-A17B       |


### Known limitations

- **OpenAI `gpt-5.4` + tools + `reasoning_effort`**: not supported on `/v1/chat/completions`. The API returns a 400 error when both function tools and `reasoning_effort` are passed together for `gpt-5.4`, and suggests migrating to `/v1/responses`. As a result, agentic loops (which always attach tools) currently run these models at the API default reasoning effort; the `reasoning_effort` configured in `models.yaml` only takes effect for tool-free calls (e.g. one-shot baseline).

## Architecture

Architecture diagram

Nine agent roles collaborate in a loop. Each agent gets a fresh context per call (no conversation history). All research state lives in a structured `ResearchState` object (persisted as `RESEARCH_GRAPH.json`), with Markdown files rendered from it. The workspace is a separate git repo.

The orchestrator is the only agent that *decides* what to do — it reads ResearchState and calls tools (create RQs, formulate WHs, dispatch researcher/computer, request termination). Everything else is auto-triggered by the engine in cascading fashion.

```
                    ┌───────────┐       ┌──────────┐
                    │  Surveyor  │──────>│  Planner  │     (pre-loop, one-shot)
                    └───────────┘       └─────┬────┘
                                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                         Main Loop                                 │
│                                                                   │
│  Orchestrator (agentic, 9 tools)                                  │
│  Reads ResearchState, decides next action via tool calls          │
│      │                    │                       │               │
│  dispatch task       add_hypothesis         request_termination   │
│      │               (creates WH)                 │               │
│      ▼                    │                       ▼               │
│  Researcher               │              Termination Gate         │
│  or Computer              │              → Formatter → ANSWER.md  │
│      │                    │                                       │
│      │ evidence           │                                       │
│      ▼                    ▼                                       │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Engine auto-triggers:                                       │  │
│  │                                                             │  │
│  │  WH created or ──> Reviewer ──VERIFIED──> auto-promote     │  │
│  │  stale review       (1-shot)               WH → ER         │  │
│  │                        │                                    │  │
│  │  (+ periodic       ┌───┘                                    │  │
│  │    every N) ──>  Critic                                     │  │
│  │                    (1-shot)                                  │  │
│  │                      │                                      │  │
│  │              ┌───────┴───────┐                               │  │
│  │              ▼               ▼                               │  │
│  │        Adjudicator      Planner                              │  │
│  │        (ER challenge)   (strategy revision)                  │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│               ┌───────────────────────────┐                       │
│               │  ResearchState             │                       │
│               │  (source of truth for all  │                       │
│               │   agents, JSON + MD)       │                       │
│               └───────────────────────────┘                       │
└──────────────────────────────────────────────────────────────────┘
```

### Agents


| Agent            | Role                                                 | Mode                       | Context source                                             | Mutates                                            |
| ---------------- | ---------------------------------------------------- | -------------------------- | ---------------------------------------------------------- | -------------------------------------------------- |
| **Surveyor**     | Maps the research landscape before the main loop     | One-shot                   | Problem statement + ResearchState                          | `BackgroundSurvey` on ResearchState                |
| **Planner**      | Research strategy planning (initial + revision)      | One-shot                   | Problem statement + background survey (+ revision trigger) | Strategy, sanity checks on ResearchState           |
| **Orchestrator** | Plans next task, mutates state via tools             | Agentic (9 tools)          | ResearchState via renderers                                | ResearchState, `CURRENT_TASK.md`                   |
| **Researcher**   | Analytical reasoning, derivation                     | One-shot (structured JSON) | Task + target entity + method hints + light state          | Evidence on RQ/WH                                  |
| **Computer**     | Computational work via code                          | Agentic (4 tools)          | Task + target entity + method hints + light state          | Evidence on RQ/WH                                  |
| **Reviewer**     | Adversarial review without code                      | One-shot (structured JSON) | Focused package: WH + evidence + light state               | ReviewResult on WH                                 |
| **Deep Critic**  | Strategic audit — research direction, coherence      | One-shot (structured JSON) | ResearchState via `render_critic_context()`                | Critique objects (typed: er/strategy/coordination) |
| **Adjudicator**  | Independent evaluation of ER challenges from critic  | One-shot (structured JSON) | Claim + challenge + evidence + conventions + ERs           | ER demotion or critique dismissal                  |
| **Formatter**    | Produces clean `ANSWER.md` from final research state | One-shot                   | ResearchState via renderers                                | `ANSWER.md`                                        |


### Research Lifecycle

The typical lifecycle of a claim: **RQ → evidence → WH → review → ER**

1. Orchestrator creates a **Research Question** (RQ) and dispatches to researcher or computer
2. Researcher/Computer produces **Evidence** (reasoning or code+output), stored on the RQ
3. Orchestrator formulates a **Working Hypothesis** (WH) from the evidence (auto-copies evidence from RQ; auto-triggers review)
4. **Reviewer** examines the WH + evidence (auto-dispatched by the engine)
5. Reviewer submits review (verdict: VERIFIED/REFUTED/INCONCLUSIVE, summary, details)
6. If VERIFIED: engine auto-promotes WH to **Established Result** (ER) when all dependencies are met (cascades to other VERIFIED WHs). If REFUTED: WH is auto-abandoned and logged as a failed approach

Entity numbers are unified — RQ-003 → WH-003 → ER-003.

### Review Stack

Results go through layered review before being promoted to "Established":

1. **Reviewer** — adversarial examination of evidence, methodology, and logical consistency
2. **Strategic critique** — Deep Critic (Strategic Auditor) reviews research direction and inter-result coherence, filing typed critiques
3. **Critique routing** — ER-targeted critiques go to the **Adjudicator** for independent evaluation; strategy/coordination critiques trigger **Planner** revision
4. **Dependency tracking** — All prerequisite results must themselves be Established

Promotion from WH to ER is automatic: the engine's `_auto_promote` fires after a VERIFIED review when all dependencies are established, including cascading promotion of other VERIFIED WHs whose dependencies become satisfied. ERs are immutable — only the adjudicator can demote them (via valid critique). REFUTED WHs are auto-abandoned. Periodic critic passes run every N iterations. A post-integration validation pipeline (`validation.py`) runs 4 checks on ResearchState after every orchestrator pass — demoting unreviewed ERs, stripping phantom labels, and ensuring critique resolution consistency.

### LLM Failure Compensation

LLMs fail in predictable ways (hallucinating IDs, promoting unverified results, emitting malformed output, failing to terminate). The scaffolding compensates via ~40 mechanisms across four categories (defined in `categories.py` as `CompensationCategory`):

- `**call_reliability`** — making each LLM call succeed: transport retry, tool-call fallback, agent loop bailouts, tool execution guards
- `**state_invariants**` — keeping ResearchState consistent: post-integration validation pipeline (4 checks)
- `**loop_control**` — steering the main loop: forced critic, dispatch guards, verdict tracking, compute enrichment, termination gates
- `**output_normalization**` — cleaning agent output: per-agent response corrections, markdown parsing tolerance

All interventions are logged to `EVENT_LOG.jsonl` with category, event key, and detail.

### Workspace Files

All research state is persisted under `workspaces/<run>/` (each run gets a timestamped subdirectory, gitignored from this repo, has its own git):


| File                  | Purpose                                                                                              |
| --------------------- | ---------------------------------------------------------------------------------------------------- |
| `RESEARCH_STATE.md`   | Established results, working hypotheses, evidence, dead ends                                         |
| `CURRENT_TASK.md`     | Current task with YAML frontmatter + structured dispatch context                                     |
| `RESEARCH_GRAPH.json` | Authoritative structured state (ResearchState serialized as JSON)                                    |
| `EVIDENCE_LOG.md`     | Log of all evidence and review results                                                               |
| `CRITIQUE_LOG.md`     | All critiques with severity and resolution status                                                    |
| `METRICS.md`          | Token usage, alerts                                                                                  |
| `EVENT_LOG.jsonl`     | Unified event log — LLM call metadata + scaffolding intervention events                              |
| `ANSWER.md`           | Final formatted answer (written by formatter at end of run, or by the forced formatter on soft-exit) |
| `BEST_GUESS.md`       | Mid-run best-guess snapshot (only if `best_guess_every_n > 0`) — never read by agents                |
| `VERIFICATION.md`     | Independent verification report (written by `--write-report`)                                        |
| `computations/`       | Saved Python scripts from computer agent                                                             |
| `derivations/`        | Saved derivation files from researcher agent                                                         |


## Problem Definitions

Problems are defined in YAML files under `problems/`. Each file contains a `problem` field with the problem statement in plain language. Problems with a known `answer` field support auto-evaluation in one-shot mode.

- `problems/tier1/` — 10 core problems (Hawking temperature, QHO thermodynamics, 1D Ising, hydrogen fine structure, Casimir effect, perihelion precession, Berry phase, Chandrasekhar limit, path integral HO, φ⁴ renormalisation)
- `problems/tier2/` — 12 advanced problems (Aharonov-Bohm, bremsstrahlung, Dirac-Coulomb, H₂⁺, 2D Ising Onsager, Lamb shift, Schwinger, Stark effect, Thomas-Fermi, TOV-Buchdahl, Unruh, WKB quartic)
- `problems/critpt/` — Critical-path problems (quantum error correction decomposition)
- `problems/cfg/` — CFG/combinatorics problems

## Project Structure

```
src/physics_intern/
  main.py              — Entry point, CLI argument parsing (console_scripts: `physics_intern`)
  engine.py            — Main loop driver: orchestrate → validate → enrich → dispatch → route critiques → git (delegates to control/ modules)
  llm.py               — Provider-agnostic LLM wrapper (call_llm, run_agent_loop) with retry + audit logging
  config.default.yaml  — Single source of truth for all default values
  models.yaml          — Model registry (friendly keys → provider + model_id + env_key + cost)
  state/               — Authoritative structured state, shared dataclasses
    research_state.py  — ResearchState dataclass: hypotheses, evidence, critiques
    state_transitions.py — Mutation helpers for ResearchState
    loop_state.py      — `LoopState` dataclass + `reconstruct_loop_state`
    task.py            — Task dataclass + TaskType enum + TASK_TYPE_AGENT_MAP
    tool_call.py       — ToolCall dataclass shared across agents and LLM layer
  control/             — Loop-control modules (driven by engine.py)
    dispatcher.py      — Task dispatch and dispatch error handlers
    critique_routing.py — Critique routing + `auto_promote` cascading WH→ER promotion
    validation.py      — Post-integration checks (4 checks on ResearchState) + termination gates
    resume.py          — Workspace resume helpers
  core/                — Shared infrastructure
    config.py          — Config dataclass (model, provider, thresholds, timeouts)
    metrics.py         — MetricsTracker (token counts, alerts, Markdown rendering)
    workspace.py       — File I/O + git operations on workspace/ + log_scaffold_event() + log_llm_call()
    console.py         — Shared rich console + reporting helpers (progress callbacks, task summaries, final report)
  agents/
    base.py            — BaseAgent ABC with template method + retry + tool-use dispatch
    evidence_base.py   — EvidenceAgent base class shared by researcher and computer
    parsing.py         — JSON parsing utilities for structured agent output
    orchestrator/      — Plans tasks, mutates ResearchState via tools (agent.py, tools.py, tool_schemas.py, context.py, prompt.md)
    computer/          — Computational work via code execution (agent.py, tools.py, prompt.md)
    researcher/        — Analytical reasoning, one-shot structured JSON (agent.py, prompt.md)
    reviewer/          — Adversarial review, one-shot structured JSON (agent.py, prompt.md)
    critic/            — Strategic audit, one-shot structured JSON (agent.py, context.py, prompt.md)
    adjudicator/       — Independent ER challenge evaluation (agent.py, prompt.md)
    formatter/         — Produces ANSWER.md from final research state (agent.py, context.py, prompt.md)
    planner/           — Research strategy: initial + revision mode (agent.py, context.py, prompt.md, prompt_revise.md)
    surveyor/          — Background surveyor: maps the research landscape (agent.py, prompt.md)
  rendering/
    snapshots.py       — Snapshot renderers: state → Markdown (RESEARCH_STATE, EVIDENCE_LOG, CRITIQUE_LOG)
    shared.py          — Shared context primitives (XML wrappers, sanity-check rendering, problem guidelines)
                         (Per-agent context renderers now live in `agents/<name>/context.py`)
  verification/
    cli.py             — CLI entry for `python -m physics_intern.verification`
    diagnosis.py       — Unified diagnosis pass (replaces verify.py + process_auditor)
    diagnosis.md       — Diagnosis prompt
    evaluate.py        — Answer evaluation: symbolic (SymPy) and numerical comparison
    formal_eval.py     — Formal evaluation API shared by one_shot/RSA/autophysicist
    event_summary.py   — Event log summarisation for diagnosis
    workspace.py       — Workspace loading helpers for verification
  autophysicist/
    __main__.py        — Module entry point (`python -m physics_intern.autophysicist`)
    runner.py          — Autophysicist entry point: CLI, iteration loop, formal evaluation
    tools.py           — Tool executor: dispatch_subagent, memory writes, end_turn, submit_final_answer
    subagent.py        — Ephemeral sub-agent dispatch with optional sandboxed code execution
    memory.py          — PermanentMemory (append-only) and Scratchpad (rolling window)
    prompt.md          — Research Manager system prompt
  baselines/
    call.py            — Shared LLM call wrappers (`run_baseline_call`, `run_two_step_call`) used by one_shot, two_steps, and RSA
    cli.py             — Shared argparse + config loading for baseline runners
    prompts.py         — Shared baseline prompts (one-shot system prompt + critpt-verbatim two-step system / parse prompts)
  one_shot/
    runner.py          — One-shot LLM baseline runner (single call, no scaffolding, for benchmarking)
  two_steps/
    runner.py          — Two-step LLM baseline runner reproducing critpt's `parsing=False` flow (derive, then populate template)
  rsa/
    runner.py          — RSA (Recursive Self-Aggregation) runner
  providers/
    base.py            — LLMProvider ABC + ProviderResponse dataclass
    anthropic.py       — Anthropic Claude adapter
    openai.py          — OpenAI adapter
    google.py          — Google Gemini adapter
    huggingface.py     — HuggingFace Inference Providers adapter
    vllm.py            — vLLM (OpenAI-compatible) adapter for locally served models
    retry.py           — Shared transport retry / backoff helpers
    _openai_compat.py  — OpenAI-compatible request/response shims used by openai.py and vllm.py
  utils/
    markdown.py        — YAML frontmatter parsing, section extraction, critique helpers
    sandbox.py         — Python script execution with timeout
    categories.py      — CompensationCategory enum (call_reliability, state_invariants, loop_control, output_normalization)
scripts/
  run_and_verify.sh    — Run a problem then verify results in one command
  one_shot_batch.sh    — Batch-run one-shot baseline across multiple problems
  test_model.py        — Smoke-test a model's reasoning and tool-call support
  run_multiple.py      — Run N concurrent physics_intern instances for pass@k
  run_multiple_oneshot.py — Run N concurrent one-shot instances for pass@k
  run_multiple_rsa.py  — Run N concurrent RSA instances for pass@k
  run_multiple_autophysicist.py — Run N concurrent autophysicist instances for pass@k
  run_critpt_physics_intern.py — Batch-run CritPt problems through the full pipeline
  run_critpt_oneshot.py — Batch-run CritPt problems through one-shot baseline
  run_critpt_two_steps.py — Batch-run CritPt problems through two-step baseline
  run_critpt_rsa.py    — Batch-run CritPt problems through RSA
  analyze_batch.py     — Analyze token usage across a CritPt batch run
  fill_missing_critpt.py — Fill missing CritPt submissions with template answers
tests/                 — pytest tests
problems/
  tier1/               — 10 core problems
  tier2/               — 12 advanced problems
  critpt/              — Critical-path problems
  cfg/                 — CFG/combinatorics problems
```