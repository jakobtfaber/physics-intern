#!/bin/bash

set -euo pipefail

if [[ -n "${SLURM_SUBMIT_DIR:-}" ]]; then
  REPO_ROOT="$(cd "${SLURM_SUBMIT_DIR}" && pwd)"
  SCRIPT_DIR="${REPO_ROOT}/serve"
  SCRIPT_PATH="${SCRIPT_DIR}/serve.slurm"
else
  SCRIPT_PATH="$(realpath "${BASH_SOURCE[0]}")"
  SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi
LOG_ROOT="${REPO_ROOT}/serve/logs"
LOG_DIR_DEFAULT="${LOG_ROOT}/slurm"
RUNTIME_DIR_ROOT="${LOG_ROOT}/vllm"
PARSER_DIR="${SCRIPT_DIR}/reasoning_parsers"
PIDS=()

usage() {
  cat <<'USAGE'
Usage:
  ./serve/serve.slurm --model MODEL [options] [-- ...extra vllm args]
  sbatch serve/serve.slurm --model MODEL [options] [-- ...extra vllm args]

Examples:
  ./serve/serve.slurm --model Qwen/Qwen3.5-4B --nodes 1 --gpus-per-node 1
  ./serve/serve.slurm --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 --nodes 1 --gpus-per-node 1
  ./serve/serve.slurm --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16 --nodes 2 --gpus-per-node 8
  # GLM auto-launches 8 replicas (PP=3 each, 24 nodes total) via multi_serve.sh
  ./serve/serve.slurm --model zai-org/GLM-5.1
  # Kimi auto-launches 4 replicas (PP=4 each, 16 nodes total) via multi_serve.sh
  ./serve/serve.slurm --model moonshotai/Kimi-K2.6
  ./serve/serve.slurm --model Qwen/Qwen3.5-4B --nodes 1 --gpus-per-node 1 --reasoning-parser qwen3

  # Data parallel: 4 replicas on 4 nodes (1 node each, TP=8 within node)
  # Only works for models that fit on a single node (TP=8, PP=1).
  ./serve/serve.slurm --model some/model --nodes 4 --gpus-per-node 8 --dp 4 --tp 8

Options:
  --model MODEL                    Hugging Face model repo id to serve.
  --nodes N                        Node count. Defaults to serve.nodes or 1 when self-submitting.
  --gpus-per-node N                GPU count per node. Defaults are model-specific.
  --partition NAME                 Slurm partition (default: hopper-prod).
  --cpus-per-task N                CPUs per task. Defaults to 11 * gpus-per-node.
  --time HH:MM:SS                  Slurm time limit (default: 24:00:00).
  --port PORT                      vLLM API port (default: 8000).
  --served-model-name NAME         Model name exposed by the API (default: MODEL).
  --max-model-len TOKENS           Max model length (default: 262144).
  --tp N                           Tensor parallel size.
  --tensor-parallel-size N         Alias for --tp.
  --pp N                           Pipeline parallel size.
  --pipeline-parallel-size N       Alias for --pp.
  --dp N                           Data parallel size (native vLLM DP).
  --data-parallel-size N           Alias for --dp.
  --reasoning-parser NAME          Explicit reasoning parser override.
  --idle-shutdown SECONDS          Auto-cancel the job if no requests AND
                                   head-node GPU util is 0 for SECONDS
                                   (default: 14400 = 4h; 0 disables).
  --max-restarts N                 Restart vLLM up to N times on crash
                                   (default: VLLM_MAX_RESTARTS or 3).
  -h, --help                       Show this message.
  --                               Extra args passed through to vllm serve.

Notes:
  - The script self-submits with sbatch when run outside Slurm.
  - On multi-node PP jobs, rank 0 serves the OpenAI-compatible API and nonzero
    ranks join in --headless mode using vLLM's multiprocessing backend.
  - With --dp, each node runs a DP replica (TP within node, DP across nodes).
    DP and PP > 1 are mutually exclusive (vLLM v1 constraint).
  - The idle watchdog only arms once /metrics is reachable, so model load
    time never counts against the idle window.
USAGE
}

require_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" ]]; then
    echo "Missing value for ${option}." >&2
    usage
    exit 1
  fi
}

is_pos_int() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

slugify() {
  local value="$1"
  value="${value//[^[:alnum:]]/-}"
  while [[ "$value" == *--* ]]; do
    value="${value//--/-}"
  done
  value="${value#-}"
  value="${value%-}"
  if [[ -z "$value" ]]; then
    value="model"
  fi
  printf '%s\n' "$value"
}

detect_gpu_count() {
  if [[ -n "${SLURM_GPUS_ON_NODE:-}" ]]; then
    if [[ "${SLURM_GPUS_ON_NODE}" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "${SLURM_GPUS_ON_NODE}"
      return 0
    fi
    if [[ "${SLURM_GPUS_ON_NODE}" =~ gpu:([0-9]+) ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"
      return 0
    fi
  fi

  if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi -L 2>/dev/null | wc -l | tr -d ' '
    return 0
  fi

  return 1
}

has_option_arg() {
  local option="$1"
  shift
  local arg
  for arg in "$@"; do
    case "$arg" in
      "${option}"|${option}=*)
        return 0
        ;;
    esac
  done
  return 1
}

print_command() {
  local label="$1"
  shift
  printf '%s' "${label}:"
  printf ' %q' "$@"
  printf '\n'
}

cleanup() {
  local pid
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
}

# Background watchdog: cancels the Slurm job if vLLM has been idle for too long.
# "Idle" = the Prometheus counter `vllm:request_success_total` has not advanced
# AND the max GPU utilization on the head node has stayed at 0% for the whole
# window. The timer is only armed after /metrics responds, so model loading
# never trips the kill switch.
start_idle_watchdog() {
  local job_id="$1"
  local port="$2"
  local idle_seconds="$3"
  local poll_interval="$4"
  local metrics_url="http://localhost:${port}/metrics"
  local now last_activity_ts=0 last_total="" total gpu activity
  local server_seen=0

  # Disable errexit/pipefail inside the watchdog: curl failing while vLLM is
  # still loading is expected, and we don't want a transient pipe failure to
  # take down the whole serve job.
  set +e +o pipefail

  echo "[watchdog] enabled: idle_seconds=${idle_seconds}, poll_interval=${poll_interval}, metrics=${metrics_url}" >&2

  while sleep "$poll_interval"; do
    now=$(date +%s)
    total="$(curl -fsS --max-time 5 "$metrics_url" 2>/dev/null \
      | awk '/^vllm:request_success_total[ {]/ { sum += $NF; ok = 1 } END { if (ok) print sum }')"
    gpu="$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null \
      | awk 'BEGIN { m = 0 } { v = $1 + 0; if (v > m) m = v } END { print m + 0 }')"

    if [[ -z "$total" ]]; then
      # vLLM not yet serving; hold the timer so model load never trips us.
      last_activity_ts=$now
      continue
    fi

    if (( server_seen == 0 )); then
      server_seen=1
      last_activity_ts=$now
      last_total="$total"
      echo "[watchdog] vLLM /metrics reachable; idle timer armed for ${idle_seconds}s." >&2
      continue
    fi

    activity=0
    if [[ "$total" != "$last_total" ]]; then
      activity=1
    fi
    if [[ "$gpu" =~ ^[0-9]+$ ]] && (( gpu > 0 )); then
      activity=1
    fi
    last_total="$total"

    if (( activity == 1 )); then
      last_activity_ts=$now
      continue
    fi

    if (( now - last_activity_ts >= idle_seconds )); then
      echo "[watchdog] no requests and GPU util=0 for $((now - last_activity_ts))s (>= ${idle_seconds}s); cancelling job ${job_id}." >&2
      scancel "$job_id" >/dev/null 2>&1 || true
      return 0
    fi
  done
}

on_signal() {
  echo "Received termination signal; stopping vLLM ranks." >&2
  exit 0
}

trap cleanup EXIT
trap on_signal INT TERM

ORIGINAL_ARGS=("$@")

MODEL=""
NODES=""
GPUS_PER_NODE=""
PARTITION="hopper-prod"
CPUS_PER_TASK=""
TIME_LIMIT="24:00:00"
PORT="8000"
SERVED_MODEL_NAME=""
MAX_MODEL_LEN="262144"
MAX_MODEL_LEN_EXPLICIT=""
TP=""
PP=""
DP=""
REASONING_PARSER=""
IDLE_SHUTDOWN_SECONDS="${WATCHDOG_IDLE_SECONDS:-14400}"
IDLE_POLL_INTERVAL="${WATCHDOG_POLL_INTERVAL:-60}"
EXTRA_VLLM_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      require_value "$1" "${2:-}"
      MODEL="$2"
      shift 2
      ;;
    --nodes)
      require_value "$1" "${2:-}"
      NODES="$2"
      shift 2
      ;;
    --gpus-per-node)
      require_value "$1" "${2:-}"
      GPUS_PER_NODE="$2"
      shift 2
      ;;
    --partition)
      require_value "$1" "${2:-}"
      PARTITION="$2"
      shift 2
      ;;
    --cpus-per-task)
      require_value "$1" "${2:-}"
      CPUS_PER_TASK="$2"
      shift 2
      ;;
    --time)
      require_value "$1" "${2:-}"
      TIME_LIMIT="$2"
      shift 2
      ;;
    --port)
      require_value "$1" "${2:-}"
      PORT="$2"
      shift 2
      ;;
    --served-model-name)
      require_value "$1" "${2:-}"
      SERVED_MODEL_NAME="$2"
      shift 2
      ;;
    --max-model-len)
      require_value "$1" "${2:-}"
      MAX_MODEL_LEN="$2"
      MAX_MODEL_LEN_EXPLICIT=1
      shift 2
      ;;
    --tp|--tensor-parallel-size)
      require_value "$1" "${2:-}"
      TP="$2"
      shift 2
      ;;
    --pp|--pipeline-parallel-size)
      require_value "$1" "${2:-}"
      PP="$2"
      shift 2
      ;;
    --dp|--data-parallel-size)
      require_value "$1" "${2:-}"
      DP="$2"
      shift 2
      ;;
    --reasoning-parser)
      require_value "$1" "${2:-}"
      REASONING_PARSER="$2"
      shift 2
      ;;
    --idle-shutdown)
      require_value "$1" "${2:-}"
      IDLE_SHUTDOWN_SECONDS="$2"
      shift 2
      ;;
    --max-restarts)
      require_value "$1" "${2:-}"
      export VLLM_MAX_RESTARTS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      EXTRA_VLLM_ARGS=("$@")
      break
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$MODEL" ]]; then
  echo "Missing required --model argument." >&2
  usage
  exit 1
fi

MODEL_SLUG="$(slugify "$MODEL")"

# Resolve model-specific serve defaults from models.yaml
_RESOLVE_SCRIPT="${SCRIPT_DIR}/resolve_serve_config.py"
eval "$(uv run --no-sync python "$_RESOLVE_SCRIPT" "$MODEL")"

# If models.yaml vllm_args includes --max-model-len and the user didn't
# explicitly pass it on the CLI, adopt the models.yaml value.
if [[ -z "$MAX_MODEL_LEN_EXPLICIT" ]]; then
  _skip_next=0
  for _arg in "${DEFAULT_VLLM_ARGS[@]}"; do
    if (( _skip_next )); then
      MAX_MODEL_LEN="$_arg"
      _skip_next=0
      break
    fi
    [[ "$_arg" == "--max-model-len" ]] && _skip_next=1
  done
fi

if [[ -z "${SLURM_JOB_ID:-}" ]]; then
  if [[ -z "$NODES" ]]; then
    NODES="$DEFAULT_NODES"
  fi
  if [[ -z "$NODES" ]]; then
    NODES="1"
  fi
  if [[ -z "$GPUS_PER_NODE" ]]; then
    GPUS_PER_NODE="$DEFAULT_GPUS_PER_NODE"
  fi

  # Auto-dispatch to multi_serve.sh for models with replicas > 1
  # (e.g. Kimi-K2.6 uses 4 external-DP replicas with PP=2 each).
  # Suppressed when called from multi_serve.sh to avoid infinite recursion.
  REPLICAS="${DEFAULT_REPLICAS:-1}"
  if [[ -z "${_MULTI_SERVE_PARENT:-}" ]] && is_pos_int "$REPLICAS" && (( REPLICAS > 1 )); then
    MULTI_SERVE="${SCRIPT_DIR}/multi_serve.sh"
    MULTI_ARGS=(
      "$MULTI_SERVE"
      --model "$MODEL"
      --replicas "$REPLICAS"
      --nodes-per-replica "$NODES"
      --gpus-per-node "$GPUS_PER_NODE"
      --time "$TIME_LIMIT"
      --idle-shutdown "$IDLE_SHUTDOWN_SECONDS"
    )
    if [[ ${#EXTRA_VLLM_ARGS[@]} -gt 0 ]]; then
      MULTI_ARGS+=(-- "${EXTRA_VLLM_ARGS[@]}")
    fi
    exec "${MULTI_ARGS[@]}"
  fi

  if [[ -z "$CPUS_PER_TASK" ]]; then
    CPUS_PER_TASK="$((11 * GPUS_PER_NODE))"
  fi

  if ! is_pos_int "$NODES"; then
    echo "--nodes must be a positive integer." >&2
    exit 1
  fi
  if ! is_pos_int "$GPUS_PER_NODE"; then
    echo "--gpus-per-node must be a positive integer." >&2
    exit 1
  fi
  if ! is_pos_int "$CPUS_PER_TASK"; then
    echo "--cpus-per-task must be a positive integer." >&2
    exit 1
  fi
  if ! is_pos_int "$PORT"; then
    echo "--port must be a positive integer." >&2
    exit 1
  fi
  if [[ -n "$TP" ]] && ! is_pos_int "$TP"; then
    echo "--tp must be a positive integer." >&2
    exit 1
  fi
  if [[ -n "$PP" ]] && ! is_pos_int "$PP"; then
    echo "--pp must be a positive integer." >&2
    exit 1
  fi
  if [[ -n "$DP" ]] && ! is_pos_int "$DP"; then
    echo "--dp must be a positive integer." >&2
    exit 1
  fi

  mkdir -p "$LOG_DIR_DEFAULT"
  JOB_NAME="vllm-serve-$(slugify "${SERVED_MODEL_NAME:-$MODEL}")"
  SUBMIT_CMD=(
    sbatch
    --parsable
    --job-name "$JOB_NAME"
    --partition "$PARTITION"
    --nodes "$NODES"
    --ntasks-per-node 1
    --gpus-per-node "$GPUS_PER_NODE"
    --cpus-per-task "$CPUS_PER_TASK"
    --time "$TIME_LIMIT"
    --output "${LOG_DIR_DEFAULT}/%x-%j.out"
    --error "${LOG_DIR_DEFAULT}/%x-%j.err"
    "$SCRIPT_PATH"
    "${ORIGINAL_ARGS[@]}"
  )
  print_command "Submitting" "${SUBMIT_CMD[@]}"
  JOB_ID="$("${SUBMIT_CMD[@]}")"
  echo "Submitted ${JOB_ID}"
  exit 0
fi

if [[ -f "$HOME/.bashrc" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.bashrc"
fi

if type module >/dev/null 2>&1; then
  module load cuda/12.9 || true
fi

export TORCH_COMPILE_DISABLE=1
export VLLM_ENGINE_READY_TIMEOUT_S="${VLLM_ENGINE_READY_TIMEOUT_S:-1800}"

# DeepGEMM JIT compiles sparse-attention kernels (used by GLM-5.1 and DSv3-class
# MoE models) at first call. Put cubins in the shared HF cache when available so
# all Slurm ranks see the same writable path and kernels survive across jobs.
# Also point JIT at the CUDA toolkit just loaded above.
SHARED_CACHE_ROOT="${HUGGINGFACE_HUB_CACHE:-${REPO_ROOT}/.cache}"
export DG_JIT_CACHE_DIR="${DG_JIT_CACHE_DIR:-${SHARED_CACHE_ROOT}/deep_gemm_jit}"
mkdir -p "$DG_JIT_CACHE_DIR"
if [[ -n "${CUDA_HOME:-}" && -x "${CUDA_HOME}/bin/nvcc" ]]; then
  export DG_JIT_NVCC_COMPILER="${CUDA_HOME}/bin/nvcc"
fi

if ! uv run --no-sync hf auth whoami >/dev/null 2>&1; then
  echo "Hugging Face auth missing. Run 'uv run hf auth login' first." >&2
  exit 1
fi

ALLOCATED_NODES="${SLURM_JOB_NUM_NODES:-1}"
if [[ -z "${SLURM_JOB_NODELIST:-}" ]]; then
  echo "SLURM_JOB_NODELIST is not set." >&2
  exit 1
fi

mapfile -t NODE_NAMES < <(scontrol show hostnames "$SLURM_JOB_NODELIST")
if [[ "${#NODE_NAMES[@]}" -eq 0 ]]; then
  echo "Unable to resolve allocated nodes." >&2
  exit 1
fi

HEAD_NODE="${NODE_NAMES[0]}"
HEAD_IP="$(
  srun --nodes=1 --ntasks=1 -w "$HEAD_NODE" bash -lc \
    "hostname -I | awk '{print \$1}'" 2>/dev/null | head -n1
)"
if [[ -z "$HEAD_IP" ]]; then
  HEAD_IP="$(getent hosts "$HEAD_NODE" | awk '{print $1}' | head -n1)"
fi
if [[ -z "$HEAD_IP" ]]; then
  echo "Unable to determine head node IP address." >&2
  exit 1
fi

ALLOCATED_GPUS_PER_NODE="$(srun --nodes=1 --ntasks=1 -w "$HEAD_NODE" bash -lc 'if [[ -n "${SLURM_GPUS_ON_NODE:-}" ]]; then echo "${SLURM_GPUS_ON_NODE}"; else nvidia-smi -L | wc -l; fi' | head -n1 | sed -E 's/.*gpu:([0-9]+).*/\1/')"
if ! is_pos_int "${ALLOCATED_GPUS_PER_NODE:-0}"; then
  ALLOCATED_GPUS_PER_NODE="$(srun --nodes=1 --ntasks=1 -w "$HEAD_NODE" bash -lc 'nvidia-smi -L | wc -l' | head -n1 | tr -d ' ')"
fi
if ! is_pos_int "${ALLOCATED_GPUS_PER_NODE:-0}"; then
  echo "Unable to detect GPUs per node." >&2
  exit 1
fi

if [[ -z "$NODES" ]]; then
  NODES="$ALLOCATED_NODES"
elif [[ "$NODES" != "$ALLOCATED_NODES" ]]; then
  echo "--nodes=${NODES} does not match allocation (${ALLOCATED_NODES})." >&2
  exit 1
fi

if [[ -z "$GPUS_PER_NODE" ]]; then
  GPUS_PER_NODE="$ALLOCATED_GPUS_PER_NODE"
elif ! is_pos_int "$GPUS_PER_NODE" || (( GPUS_PER_NODE > ALLOCATED_GPUS_PER_NODE )); then
  echo "--gpus-per-node=${GPUS_PER_NODE} exceeds allocation (${ALLOCATED_GPUS_PER_NODE})." >&2
  exit 1
fi

if [[ -z "$CPUS_PER_TASK" ]]; then
  CPUS_PER_TASK="${SLURM_CPUS_PER_TASK:-$((11 * GPUS_PER_NODE))}"
fi

if ! is_pos_int "$CPUS_PER_TASK"; then
  echo "--cpus-per-task must be a positive integer." >&2
  exit 1
fi
if ! is_pos_int "$PORT"; then
  echo "--port must be a positive integer." >&2
  exit 1
fi
if [[ ! "$IDLE_SHUTDOWN_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "--idle-shutdown must be a non-negative integer (got: ${IDLE_SHUTDOWN_SECONDS})." >&2
  exit 1
fi
if [[ ! "$IDLE_POLL_INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "WATCHDOG_POLL_INTERVAL must be a positive integer (got: ${IDLE_POLL_INTERVAL})." >&2
  exit 1
fi

if [[ -z "$SERVED_MODEL_NAME" ]]; then
  # Default the served name to the upstream HF repo (`model_id` from
  # models.yaml) so OpenAI clients can call the API with the same name they
  # use everywhere else. This matters for alias entries like
  # `zai-org/GLM-5.1-runai` whose `model_id` is `zai-org/GLM-5.1`.
  SERVED_MODEL_NAME="${DEFAULT_MODEL_ID:-$MODEL}"
fi
# Use the real HF repo id for vLLM's `serve` argument; the alias is only
# meaningful inside open-dirac's models.yaml.
MODEL_ID="${DEFAULT_MODEL_ID:-$MODEL}"
if [[ -z "$REASONING_PARSER" ]]; then
  REASONING_PARSER="$DEFAULT_REASONING_PARSER"
fi
if [[ -z "$DP" ]]; then
  DP="1"
fi
if ! is_pos_int "$DP"; then
  echo "--dp must be a positive integer." >&2
  exit 1
fi

if [[ -z "$TP" ]]; then
  TP="$GPUS_PER_NODE"
fi
if [[ -z "$PP" ]]; then
  if (( DP > 1 )); then
    PP="1"
  else
    PP="$NODES"
  fi
fi
if ! is_pos_int "$TP"; then
  echo "--tp must be a positive integer." >&2
  exit 1
fi
if ! is_pos_int "$PP"; then
  echo "--pp must be a positive integer." >&2
  exit 1
fi

# DP and PP > 1 are mutually exclusive (vLLM v1 constraint).
if (( DP > 1 && PP > 1 )); then
  echo "Cannot combine --dp > 1 with --pp > 1 (vLLM v1 does not support DP+PP)." >&2
  exit 1
fi

TOTAL_ASSIGNED_GPUS=$((NODES * GPUS_PER_NODE))
if (( DP > 1 )); then
  if (( DP * TP != TOTAL_ASSIGNED_GPUS )); then
    echo "Expected dp * tp to equal nodes * gpus-per-node (${TOTAL_ASSIGNED_GPUS}), got $((DP * TP))." >&2
    exit 1
  fi
  if (( DP % NODES != 0 )); then
    echo "--dp (${DP}) must be evenly divisible by --nodes (${NODES})." >&2
    exit 1
  fi
else
  if (( TP * PP != TOTAL_ASSIGNED_GPUS )); then
    echo "Expected tp * pp to equal nodes * gpus-per-node (${TOTAL_ASSIGNED_GPUS}), got $((TP * PP))." >&2
    exit 1
  fi
fi

VLLM_ARGS=(
  serve "$MODEL_ID"
  --host 0.0.0.0
  --port "$PORT"
  --served-model-name "$SERVED_MODEL_NAME"
  --max-model-len "$MAX_MODEL_LEN"
  --tensor-parallel-size "$TP"
)

if (( DP > 1 )); then
  # Native vLLM data parallelism: each DP replica is a separate core engine
  # process. DP replicas are distributed across nodes with TP within each node.
  DP_LOCAL=$(( DP / NODES ))
  DP_RPC_PORT=$(( 13345 + (SLURM_JOB_ID % 1000) ))
  VLLM_ARGS+=(
    --data-parallel-size "$DP"
    --data-parallel-size-local "$DP_LOCAL"
    --data-parallel-address "$HEAD_IP"
    --data-parallel-rpc-port "$DP_RPC_PORT"
  )
  # Scale API server count for large DP to avoid head-node bottleneck.
  # Stored separately: only the head node gets this (--headless is incompatible).
  HEAD_EXTRA_ARGS=()
  if (( DP >= 8 )); then
    HEAD_EXTRA_ARGS+=(--api-server-count 4)
  elif (( DP >= 4 )); then
    HEAD_EXTRA_ARGS+=(--api-server-count 2)
  fi
else
  VLLM_ARGS+=(--pipeline-parallel-size "$PP")
  if (( NODES > 1 )); then
    # Derive a unique master port from the job ID to avoid collisions when
    # multiple multi-node jobs share a node.
    MASTER_PORT=$(( 29500 + (SLURM_JOB_ID % 1000) ))
    VLLM_ARGS+=(
      --distributed-executor-backend mp
      --nnodes "$NODES"
      --master-port "$MASTER_PORT"
    )
  fi
fi

if [[ -n "$REASONING_PARSER" && "$REASONING_PARSER" != "none" && "$REASONING_PARSER" != "off" ]]; then
  VLLM_ARGS+=(--reasoning-parser "$REASONING_PARSER")
  if ! has_option_arg --reasoning-parser-plugin "${EXTRA_VLLM_ARGS[@]}"; then
    case "$REASONING_PARSER" in
      nano_v3)
        PARSER_PLUGIN_PATH="${PARSER_DIR}/nano_v3_reasoning_parser.py"
        ;;
      super_v3)
        PARSER_PLUGIN_PATH="${PARSER_DIR}/super_v3_reasoning_parser.py"
        ;;
      *)
        PARSER_PLUGIN_PATH=""
        ;;
    esac
    if [[ -n "${PARSER_PLUGIN_PATH:-}" ]]; then
      if [[ ! -f "$PARSER_PLUGIN_PATH" ]]; then
        echo "Missing reasoning parser plugin: $PARSER_PLUGIN_PATH" >&2
        exit 1
      fi
      VLLM_ARGS+=(--reasoning-parser-plugin "$PARSER_PLUGIN_PATH")
    fi
  fi
fi

# Filter --max-model-len from DEFAULT_VLLM_ARGS (it's already in the base
# VLLM_ARGS via $MAX_MODEL_LEN) to prevent duplication.
FILTERED_DEFAULT_ARGS=()
_skip_next=0
for _arg in "${DEFAULT_VLLM_ARGS[@]}"; do
  if (( _skip_next )); then
    _skip_next=0
    continue
  fi
  case "$_arg" in
    --max-model-len|--max-model-len=*)
      if [[ "$_arg" == "--max-model-len" ]]; then _skip_next=1; fi
      ;;
    *)
      FILTERED_DEFAULT_ARGS+=("$_arg")
      ;;
  esac
done
VLLM_ARGS+=("${FILTERED_DEFAULT_ARGS[@]}")
VLLM_ARGS+=("${EXTRA_VLLM_ARGS[@]}")

JOB_RUNTIME_DIR="${RUNTIME_DIR_ROOT}/${SLURM_JOB_ID}"
mkdir -p "$JOB_RUNTIME_DIR"
cat >"${JOB_RUNTIME_DIR}/endpoint.env" <<EOF
MODEL=${MODEL}
SERVED_MODEL_NAME=${SERVED_MODEL_NAME}
HEAD_NODE=${HEAD_NODE}
HEAD_IP=${HEAD_IP}
PORT=${PORT}
BASE_URL=http://${HEAD_IP}:${PORT}/v1
EOF

if (( DP > 1 )); then
  echo "Serving ${MODEL} on ${NODES} node(s) with ${GPUS_PER_NODE} GPU(s) per node (DP=${DP}, TP=${TP})."
else
  echo "Serving ${MODEL} on ${NODES} node(s) with ${GPUS_PER_NODE} GPU(s) per node (TP=${TP}, PP=${PP})."
fi
echo "Head node: ${HEAD_NODE} (${HEAD_IP})"
echo "Endpoint: http://${HEAD_IP}:${PORT}/v1"

launch_pp_rank() {
  local node="$1"
  local rank="$2"
  local -a rank_cmd=(uv run --no-sync vllm "${VLLM_ARGS[@]}")
  if (( NODES > 1 )); then
    rank_cmd+=(--node-rank "$rank" --master-addr "$HEAD_IP")
    if (( rank > 0 )); then
      rank_cmd+=(--headless)
    fi
  fi

  print_command "PP rank ${rank} on ${node}" "${rank_cmd[@]}"
  srun \
    --nodes=1 \
    --ntasks=1 \
    --cpus-per-task "$CPUS_PER_TASK" \
    --gpus-per-node "$GPUS_PER_NODE" \
    --exact \
    -w "$node" \
    bash -lc "cd '$REPO_ROOT' && exec $(printf '%q ' "${rank_cmd[@]}")" &
  PIDS+=("$!")
}

launch_dp_node() {
  local node="$1"
  local node_idx="$2"
  local start_rank=$(( node_idx * DP_LOCAL ))
  local -a rank_cmd=(uv run --no-sync vllm "${VLLM_ARGS[@]}")
  if (( node_idx == 0 )); then
    rank_cmd+=("${HEAD_EXTRA_ARGS[@]}")
  else
    rank_cmd+=(
      --headless
      --data-parallel-start-rank "$start_rank"
    )
  fi

  print_command "DP node ${node_idx} (ranks ${start_rank}..$(( start_rank + DP_LOCAL - 1 ))) on ${node}" "${rank_cmd[@]}"
  srun \
    --nodes=1 \
    --ntasks=1 \
    --cpus-per-task "$CPUS_PER_TASK" \
    --gpus-per-node "$GPUS_PER_NODE" \
    --exact \
    -w "$node" \
    bash -lc "cd '$REPO_ROOT' && exec $(printf '%q ' "${rank_cmd[@]}")" &
  PIDS+=("$!")
}

MAX_RESTARTS="${VLLM_MAX_RESTARTS:-3}"
RESTART_COOLDOWN="${VLLM_RESTART_COOLDOWN:-30}"
ATTEMPT=0

launch_all() {
  PIDS=()
  if (( DP > 1 )); then
    for idx in "${!NODE_NAMES[@]}"; do
      launch_dp_node "${NODE_NAMES[$idx]}" "$idx"
    done
  else
    for idx in "${!NODE_NAMES[@]}"; do
      launch_pp_rank "${NODE_NAMES[$idx]}" "$idx"
    done
  fi

  if (( IDLE_SHUTDOWN_SECONDS > 0 )); then
    start_idle_watchdog "$SLURM_JOB_ID" "$PORT" "$IDLE_SHUTDOWN_SECONDS" "$IDLE_POLL_INTERVAL" &
    PIDS+=("$!")
  else
    echo "Idle watchdog disabled (--idle-shutdown 0)." >&2
  fi
}

while true; do
  ATTEMPT=$(( ATTEMPT + 1 ))
  echo "[serve] Attempt ${ATTEMPT}/$((MAX_RESTARTS + 1))"
  launch_all

  if wait -n "${PIDS[@]}"; then
    STATUS=0
  else
    STATUS=$?
  fi

  # Clean up surviving processes before deciding whether to restart.
  cleanup

  if (( STATUS == 0 )); then
    echo "[serve] vLLM exited cleanly."
    exit 0
  fi

  echo "[serve] vLLM crashed with status ${STATUS} (attempt ${ATTEMPT})." >&2
  if (( ATTEMPT > MAX_RESTARTS )); then
    echo "[serve] Exhausted ${MAX_RESTARTS} restart(s); giving up." >&2
    exit "$STATUS"
  fi

  echo "[serve] Restarting in ${RESTART_COOLDOWN}s..." >&2
  sleep "$RESTART_COOLDOWN"
done
