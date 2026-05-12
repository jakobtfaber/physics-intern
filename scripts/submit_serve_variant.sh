#!/bin/bash
# Submit one DeepSeek-V4-Pro-high serve replica with explicit PP / cudagraph
# / scheduler overrides. Wraps serve/serve.slurm with the right env so the
# job appears in `serve/logs/vllm/<job>/endpoint.env` and can be reused by
# `scripts/stress_test.py --serve-job <id>`.
#
# Usage:
#   submit_serve_variant.sh <pp> <nodes> <max_num_seqs> <max_num_batched_tokens> <cudagraph_mode> [extra_compile_kv ...]
#
# Example (manueldomke recipe):
#   submit_serve_variant.sh 4 4 32 8192 PIECEWISE '"custom_ops":["all"]'
set -euo pipefail
cd "$(dirname "$0")/.."

PP="${1:?pp}"
NODES="${2:?nodes}"
MAX_SEQS="${3:?max_num_seqs}"
BATCHED="${4:?max_num_batched_tokens}"
CG_MODE="${5:?cudagraph_mode}"
shift 5
EXTRA_COMPILE_KV=("$@")

# Always keep the inductor flag the existing models.yaml relies on; tack on
# extra compile-config keys (e.g. custom_ops:["all"]) when requested. The
# composed JSON has to fit on one line because serve.slurm forwards via argv.
# CG_MODE=EAGER is a sentinel: forward `--enforce-eager` and skip the compile
# config entirely. This is the maximally-stable "no cuda graphs at all"
# baseline used to bound the cost of cudagraph-related instability.
EAGER_MODE=0
if [[ "$CG_MODE" == "EAGER" ]]; then
  EAGER_MODE=1
else
  COMPILE_PARTS=("\"cudagraph_mode\":\"${CG_MODE}\"" "\"max_cudagraph_capture_size\":128" "\"inductor_compile_config\":{\"benchmark_combo_kernel\":false}")
  for kv in "${EXTRA_COMPILE_KV[@]}"; do
    COMPILE_PARTS+=("$kv")
  done
  COMPILE_JSON="{$(IFS=,; echo "${COMPILE_PARTS[*]}")}"
fi

# Stability env knobs picked to mirror run_pp_sweep.py's submission path so
# all our experiments share the same engine-side timeouts.
export VLLM_RPC_TIMEOUT="${VLLM_RPC_TIMEOUT:-600000}"
export VLLM_ENGINE_READY_TIMEOUT_S="${VLLM_ENGINE_READY_TIMEOUT_S:-3600}"
# Tell serve.slurm we're intentionally launching a single-replica job so it
# does not delegate to multi_serve.sh and add opportunistic low-QOS replicas.
export _MULTI_SERVE_PARENT=1

SERVE_TAIL=(--max-num-seqs "$MAX_SEQS" --max-num-batched-tokens "$BATCHED")
if (( EAGER_MODE )); then
  SERVE_TAIL+=(--enforce-eager)
else
  SERVE_TAIL+=(--compilation-config "$COMPILE_JSON")
fi

exec serve/serve.slurm \
  --model deepseek-ai/DeepSeek-V4-Pro-high \
  --nodes "$NODES" \
  --gpus-per-node 8 \
  --tp 8 \
  --pp "$PP" \
  --qos low \
  --idle-shutdown 14400 \
  --time 12:00:00 \
  -- \
  "${SERVE_TAIL[@]}"
