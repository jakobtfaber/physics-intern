#!/bin/bash
# One-shot evaluation job for OpenDirac.
#
# Usage (self-submits via sbatch when run outside Slurm):
#   ./serve/eval.slurm --model MODEL_KEY --problem PROBLEM_YAML [options]
#
# For local vLLM models, pass --serve-job <JOB_ID>.  The script reads the
# head node IP from endpoint.env and exports VLLM_BASE_URL so the Python
# client connects to the correct node (no longer requires localhost).

set -euo pipefail

if [[ -n "${SLURM_SUBMIT_DIR:-}" ]]; then
  REPO_ROOT="$(cd "${SLURM_SUBMIT_DIR}" && pwd)"
  SCRIPT_PATH="${REPO_ROOT}/serve/eval.slurm"
else
  SCRIPT_PATH="$(realpath "${BASH_SOURCE[0]}")"
  REPO_ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"
fi
LOG_ROOT="${REPO_ROOT}/serve/logs"
LOG_DIR_DEFAULT="${LOG_ROOT}/slurm"

usage() {
  cat <<'USAGE'
Usage:
  ./serve/eval.slurm --model MODEL_KEY --problem PROBLEM_YAML [options]
  sbatch serve/eval.slurm --model MODEL_KEY --problem PROBLEM_YAML [options]

Examples:
  # Local vLLM model — pins job to the serve head node automatically
  ./serve/eval.slurm \
    --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16 \
    --problem problems/critpt/quantum_error_correction_main.yaml \
    --serve-job 22071810

  # Batch run (10 samples)
  ./serve/eval.slurm \
    --model qwen-3.5-0.8b-local \
    --problem problems/critpt/quantum_error_correction_main.yaml \
    --serve-job 22071812 \
    --runs 10

  # Remote API model (no --serve-job needed, runs on hopper-cpu)
  ./serve/eval.slurm \
    --model gemini-3-flash-preview \
    --problem problems/critpt/quantum_error_correction_main.yaml

Options:
  --model KEY          Model key from models.yaml (required).
  --problem PATH       Path to problem YAML file (required).
  --runs N             Number of runs for batch evaluation (default: 1).
  --serve-job JOB_ID   Serve job hosting the vLLM endpoint.
                       Reads serve/logs/vllm/<JOB_ID>/endpoint.env and
                       exports VLLM_BASE_URL to reach the head node IP.
  --partition NAME     Slurm partition (default: hopper-cpu; overridden to
                       hopper-prod when --serve-job is used).
  --time HH:MM:SS      Slurm time limit (default: 02:00:00).
  -h, --help           Show this message.
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

MODEL_KEY=""
PROBLEM=""
RUNS=""
SERVE_JOB=""
PARTITION=""
TIME_LIMIT="02:00:00"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      require_value "$1" "${2:-}"
      MODEL_KEY="$2"
      shift 2
      ;;
    --problem)
      require_value "$1" "${2:-}"
      PROBLEM="$2"
      shift 2
      ;;
    --runs)
      require_value "$1" "${2:-}"
      RUNS="$2"
      shift 2
      ;;
    --serve-job)
      require_value "$1" "${2:-}"
      SERVE_JOB="$2"
      shift 2
      ;;
    --partition)
      require_value "$1" "${2:-}"
      PARTITION="$2"
      shift 2
      ;;
    --time)
      require_value "$1" "${2:-}"
      TIME_LIMIT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$MODEL_KEY" ]]; then
  echo "Missing required --model argument." >&2
  usage
  exit 1
fi
if [[ -z "$PROBLEM" ]]; then
  echo "Missing required --problem argument." >&2
  usage
  exit 1
fi

# Self-submit with sbatch when run outside Slurm.
if [[ -z "${SLURM_JOB_ID:-}" ]]; then
  PROBLEM_SLUG="${PROBLEM//[^[:alnum:]]/-}"
  while [[ "$PROBLEM_SLUG" == *--* ]]; do PROBLEM_SLUG="${PROBLEM_SLUG//--/-}"; done
  PROBLEM_SLUG="${PROBLEM_SLUG#-}"; PROBLEM_SLUG="${PROBLEM_SLUG%-}"
  MODEL_SLUG="${MODEL_KEY//[^[:alnum:]]/-}"
  while [[ "$MODEL_SLUG" == *--* ]]; do MODEL_SLUG="${MODEL_SLUG//--/-}"; done
  MODEL_SLUG="${MODEL_SLUG#-}"; MODEL_SLUG="${MODEL_SLUG%-}"
  JOB_NAME="one-shot-${MODEL_SLUG}-${PROBLEM_SLUG}"
  mkdir -p "$LOG_DIR_DEFAULT"

  SUBMIT_ARGS=(
    sbatch
    --parsable
    --job-name "$JOB_NAME"
    --nodes 1
    --ntasks-per-node 1
    --cpus-per-task 8
    --mem=32G
    --time "$TIME_LIMIT"
    --output "${LOG_DIR_DEFAULT}/%x-%j.out"
    --error "${LOG_DIR_DEFAULT}/%x-%j.err"
  )

  # For local vLLM models: resolve serve head node endpoint.
  if [[ -n "$SERVE_JOB" ]]; then
    ENDPOINT_ENV="${LOG_ROOT}/vllm/${SERVE_JOB}/endpoint.env"
    if [[ ! -f "$ENDPOINT_ENV" ]]; then
      echo "endpoint.env not found for serve job ${SERVE_JOB}: ${ENDPOINT_ENV}" >&2
      echo "Has the serve job started yet? Check: squeue -j ${SERVE_JOB}" >&2
      exit 1
    fi
    # shellcheck disable=SC1090
    source "$ENDPOINT_ENV"
    echo "Serve job ${SERVE_JOB}: endpoint ${BASE_URL} (head node: ${HEAD_NODE})"
    SUBMIT_ARGS+=(--partition "${PARTITION:-hopper-prod}")
  else
    SUBMIT_ARGS+=(--partition "${PARTITION:-hopper-cpu}")
  fi

  SUBMIT_ARGS+=(
    "$SCRIPT_PATH"
    --model "$MODEL_KEY"
    --problem "$PROBLEM"
  )
  [[ -n "$RUNS" ]]      && SUBMIT_ARGS+=(--runs "$RUNS")
  [[ -n "$SERVE_JOB" ]] && SUBMIT_ARGS+=(--serve-job "$SERVE_JOB")
  [[ -n "$TIME_LIMIT" ]] && SUBMIT_ARGS+=(--time "$TIME_LIMIT")

  printf 'Submitting:'
  printf ' %q' "${SUBMIT_ARGS[@]}"
  printf '\n'
  JOB_ID="$("${SUBMIT_ARGS[@]}")"
  echo "Submitted ${JOB_ID}"
  exit 0
fi

# ── Running inside Slurm ──────────────────────────────────────────────────────

if [[ -f "$HOME/.bashrc" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.bashrc"
fi

cd "$REPO_ROOT"

if [[ -n "$SERVE_JOB" ]]; then
  ENDPOINT_ENV="${LOG_ROOT}/vllm/${SERVE_JOB}/endpoint.env"
  if [[ ! -f "$ENDPOINT_ENV" ]]; then
    echo "endpoint.env not found: ${ENDPOINT_ENV}" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$ENDPOINT_ENV"
  # Export VLLM_BASE_URL so the Python vLLM provider connects to the
  # serve job's head node IP instead of localhost.
  export VLLM_BASE_URL="${BASE_URL}"
  echo "Serve job ${SERVE_JOB}: endpoint ${BASE_URL}"

  # Wait for vLLM to be ready (models can take 10+ minutes to compile).
  echo "Waiting for vLLM endpoint to be ready..."
  HEALTH_URL="http://${HEAD_IP}:${PORT:-8000}/health"
  WAIT_SECS=0
  MAX_WAIT=1800  # 30 minutes
  until curl -sf "$HEALTH_URL" >/dev/null 2>&1; do
    if (( WAIT_SECS >= MAX_WAIT )); then
      echo "Timed out waiting for vLLM after ${MAX_WAIT}s" >&2
      exit 1
    fi
    sleep 15
    WAIT_SECS=$(( WAIT_SECS + 15 ))
    echo "  ...still waiting (${WAIT_SECS}s elapsed)"
  done
  echo "vLLM is ready after ${WAIT_SECS}s."
fi

ONE_SHOT_CMD=(
  uv run --no-sync python -m open_dirac.one_shot
  "$PROBLEM"
  --model "$MODEL_KEY"
)
[[ -n "$RUNS" ]] && ONE_SHOT_CMD+=(--runs "$RUNS")

echo "Model:   ${MODEL_KEY}"
echo "Problem: ${PROBLEM}"
[[ -n "$RUNS" ]] && echo "Runs:    ${RUNS}"
echo "---"

exec "${ONE_SHOT_CMD[@]}"
