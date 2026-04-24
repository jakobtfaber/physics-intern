#!/bin/bash
# Run the full CritPt benchmark (70 problems) against a vLLM serve job.
#
# Usage:
#   ./serve/full_eval.slurm --model MODEL_KEY --serve-job JOB_ID [--concurrency N] [--time HH:MM:SS]
#
# Self-submits via sbatch when run outside Slurm. Reads endpoint.env from the
# serve job and exports VLLM_BASE_URL so the parallel client connects to the
# serve head node.

set -euo pipefail

if [[ -n "${SLURM_SUBMIT_DIR:-}" ]]; then
  REPO_ROOT="$(cd "${SLURM_SUBMIT_DIR}" && pwd)"
  SCRIPT_PATH="${REPO_ROOT}/serve/full_eval.slurm"
else
  SCRIPT_PATH="$(realpath "${BASH_SOURCE[0]}")"
  REPO_ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"
fi
LOG_ROOT="${REPO_ROOT}/serve/logs"
LOG_DIR_DEFAULT="${LOG_ROOT}/slurm"

MODEL_KEY=""
SERVE_JOB=""
CONCURRENCY="10"
TIME_LIMIT="08:00:00"
RESUME_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL_KEY="$2"; shift 2 ;;
    --serve-job) SERVE_JOB="$2"; shift 2 ;;
    --concurrency) CONCURRENCY="$2"; shift 2 ;;
    --time) TIME_LIMIT="$2"; shift 2 ;;
    --resume) RESUME_DIR="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$MODEL_KEY" || -z "$SERVE_JOB" ]]; then
  echo "Usage: $0 --model MODEL_KEY --serve-job JOB_ID [--concurrency N] [--time HH:MM:SS] [--resume DIR]" >&2
  exit 1
fi

if [[ -z "${SLURM_JOB_ID:-}" ]]; then
  MODEL_SLUG="${MODEL_KEY//[^[:alnum:]]/-}"
  while [[ "$MODEL_SLUG" == *--* ]]; do MODEL_SLUG="${MODEL_SLUG//--/-}"; done
  MODEL_SLUG="${MODEL_SLUG#-}"; MODEL_SLUG="${MODEL_SLUG%-}"
  JOB_NAME="critpt-full-${MODEL_SLUG}"
  mkdir -p "$LOG_DIR_DEFAULT"

  ENDPOINT_ENV="${LOG_ROOT}/vllm/${SERVE_JOB}/endpoint.env"
  [[ -f "$ENDPOINT_ENV" ]] || { echo "endpoint.env not found: $ENDPOINT_ENV" >&2; exit 1; }
  # shellcheck disable=SC1090
  source "$ENDPOINT_ENV"
  echo "Serve job ${SERVE_JOB}: endpoint ${BASE_URL}"

  RESUME_ARG=()
  [[ -n "$RESUME_DIR" ]] && RESUME_ARG=(--resume "$RESUME_DIR")

  exec sbatch --parsable \
    --job-name "$JOB_NAME" \
    --partition hopper-prod \
    --nodes 1 --ntasks-per-node 1 --cpus-per-task 16 --mem=64G \
    --time "$TIME_LIMIT" \
    --output "${LOG_DIR_DEFAULT}/%x-%j.out" \
    --error "${LOG_DIR_DEFAULT}/%x-%j.err" \
    "$SCRIPT_PATH" --model "$MODEL_KEY" --serve-job "$SERVE_JOB" \
                   --concurrency "$CONCURRENCY" --time "$TIME_LIMIT" \
                   "${RESUME_ARG[@]}"
fi

# ── Inside Slurm ─────────────────────────────────────────────────────────────
[[ -f "$HOME/.bashrc" ]] && source "$HOME/.bashrc"
cd "$REPO_ROOT"

ENDPOINT_ENV="${LOG_ROOT}/vllm/${SERVE_JOB}/endpoint.env"
# shellcheck disable=SC1090
source "$ENDPOINT_ENV"
export VLLM_BASE_URL="${BASE_URL}"

echo "Waiting for vLLM endpoint ${BASE_URL}..."
HEALTH_URL="http://${HEAD_IP}:${PORT:-8000}/health"
WAIT=0; MAX=14400
until curl -sf "$HEALTH_URL" >/dev/null 2>&1; do
  (( WAIT >= MAX )) && { echo "Timed out after ${MAX}s" >&2; exit 1; }
  sleep 15; WAIT=$(( WAIT + 15 ))
done
echo "vLLM ready after ${WAIT}s."

RESUME_ARG=()
[[ -n "$RESUME_DIR" ]] && RESUME_ARG=(--resume "$RESUME_DIR")

exec uv run --no-sync python scripts/run_critpt_oneshot.py \
  --model "$MODEL_KEY" --concurrency "$CONCURRENCY" --timeout 3600 \
  "${RESUME_ARG[@]}"
