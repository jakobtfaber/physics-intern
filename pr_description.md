# Add DeepSeek V4 Pro local serving setup

## Summary

- Add a DeepSeek V4 Pro serving configuration for the H100 Slurm cluster using four external replicas, vLLM native DP=4 per replica, TP=8, expert parallelism, FP8 KV cache, and DeepSeek-specific tokenizer/tool/reasoning parsers.
- Harden `serve.slurm` for the CUDA 12.9 vLLM stack by loading `glibc/2.38` and `cuda/12.9`, prepending venv NVIDIA wheel libraries, and pinning Triton, TorchInductor, and DeepGEMM JIT caches to job-local `/tmp`.
- Harden the multi-replica load balancer for max-think traffic by disabling read timeouts while keeping bounded connect/write/pool timeouts.
- Update the local dependency setup to use the checked-in DeepGEMM 2.5 wheel and document the exact cu129 vLLM nightly install path used for DeepSeek V4 Pro.
- Record throughput-mode benchmark results and the trade-off between aggregate tok/s and queue latency.

## Motivation

DeepSeek V4 Pro needs newer vLLM support, a compatible DeepGEMM backend, and careful CUDA library resolution on the cluster. The previous setup hit several startup/runtime failures: stale shared compile caches, inherited CUDA 12.1 libraries, unsupported pipeline parallelism, missing DeepGEMM FP8/FP4 MQA logits kernels, and CUDA graph startup stalls.

## Implementation Notes

- `serve/upgrade_vllm.sh` installs the tested cu129 vLLM nightly wheel with
  `uv pip install -U vllm --prerelease=allow --python-platform x86_64-manylinux_2_34 --torch-backend=cu129 --extra-index-url https://wheels.vllm.ai/nightly/cu129`, then patches `/fsx/joel_niklaus/projects/open-dirac/.venv/bin/python3.13` with `glibc-fix`.
- `src/physics_intern/models.yaml` defines DeepSeek V4 Pro as four external
  replicas of a 4-node native-DP serve: `replicas: 4`, `dp: 4`, `tp: 8`,
  `--enable-expert-parallel`, `--kv-cache-dtype fp8`, `--tokenizer-mode deepseek_v4`,
  throughput mode, and PIECEWISE CUDA graphs capped at 128.
- `serve/resolve_serve_config.py` now emits TP/PP/DP defaults from `models.yaml`, so the serve script does not accidentally fall back to unsupported pipeline parallelism.
- `serve/load_balancer.py` now allows long silent streamed generations, which is required for DeepSeek V4 Pro max-think PhysicsIntern traffic.
- The checked-in DeepGEMM 2.5 wheel provides the `fp8_fp4_mqa_logits` symbol needed by DeepSeek V4 sparse attention.

## Benchmark Results

Tested on one 4x8 H100 DeepSeek V4 Pro serve job with 256 output-token requests and throughput mode enabled:

| Concurrency | Requests | Aggregate tok/s | Median latency |
| ----------- | -------- | --------------- | -------------- |
| 64          | 64       | 221.1           | 74.1s          |
| 128         | 128      | 272.9           | 120.0s         |
| 256         | 256      | 300.6           | 217.5s         |

`128` concurrency is the practical operating point unless maximum aggregate short-output throughput matters more than latency. MTP speculative decoding reached graph capture but the API server died before health checks passed, and MXFP4 indexer cache fails on H100 because Triton emits e2m1x2 instructions unsupported by `sm_90a`. `--enable-dbo` is intentionally left off because vLLM requires a DeepEP all-to-all backend for DBO.

## Test Plan

- `uv run --no-sync ruff check serve/resolve_serve_config.py vllm/vllm/utils/deep_gemm.py`
- `uv run --no-sync python serve/resolve_serve_config.py deepseek-ai/DeepSeek-V4-Pro`
- `uv run --no-sync python serve/resolve_serve_config.py moonshotai/Kimi-K2.6`
- `uv run --no-sync python serve/resolve_serve_config.py zai-org/GLM-5.1`
- Lightweight current-env smoke test for Kimi K2.6 and GLM 5.1: imported the
  vLLM model classes/tool parsers and loaded cached HF configs without loading
  weights.
- Launched DeepSeek V4 Pro via `serve/serve.slurm`; job reached healthy `/metrics`.
- Verified successful `/v1/chat/completions` responses under load.
- Ran throughput benchmarks at 64, 128, and 256 concurrency with zero request failures.
- Tested MTP speculative decoding and MXFP4 indexer cache; both were rejected
  because they did not produce a healthy H100 endpoint.
- Launched and resumed the 70-problem PhysicsIntern CritPt run through four
  load-balanced DeepSeek V4 Pro max-think replicas.