#!/bin/bash
# Run the full CritPt benchmark (70 problems) against one or more vLLM serve jobs.
#
# Usage (single serve job, backward-compatible):
#   ./serve/full_eval.slurm --model MODEL_KEY --serve-job JOB_ID
#
# Usage (multi-replica with auto load-balancer):
#   ./serve/full_eval.slurm --model MODEL_KEY --serve-job J1 --serve-job J2 --serve-job J3
#
# Usage (auto-discover already-running matching serve jobs):
#   ./serve/full_eval.slurm --model MODEL_KEY
#
# Usage (physics-intern multi-agent runner with config override):
#   ./serve/full_eval.slurm --model MODEL_KEY --serve-job J1 --serve-job J2 \
#     --runner physicsintern --config config.cluster.yaml
#
# Usage (fresh run with isolated workspace directory):
#   ./serve/full_eval.slurm --model MODEL_KEY --serve-job J1 --serve-job J2 \
#     --runner physicsintern --config config.cluster.yaml --fresh --workspace-base workspaces_run2
#
# Self-submits via sbatch when run outside Slurm. For multiple serve jobs, starts
# a load_balancer.py in the background and routes all traffic through it. The
# eval runner hosts the load balancer and PhysicsIntern subprocesses. It
# defaults to hopper-prod because hopper-cpu runs on spot nodes that can be
# preempted mid-eval; serve jobs still run on their configured GPU partition.

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
SERVE_JOBS=()
CONCURRENCY="64"
TIME_LIMIT="3-00:00:00"
RESUME_DIR=""
RUNNER="oneshot"
CONFIG_FILE=""
FRESH=""
WORKSPACE_BASE=""
NODES_PER_REPLICA=""
KEEP_SERVES=""
PARTITION="hopper-prod"
DISCOVER_INTERVAL="600"
MAX_ACTIVE_PER_BACKEND="8"
QUEUE_TIMEOUT="1800"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL_KEY="$2"; shift 2 ;;
    --serve-job) SERVE_JOBS+=("$2"); shift 2 ;;
    --concurrency) CONCURRENCY="$2"; shift 2 ;;
    --time) TIME_LIMIT="$2"; shift 2 ;;
    --resume) RESUME_DIR="$2"; shift 2 ;;
    --runner) RUNNER="$2"; shift 2 ;;
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --fresh) FRESH="1"; shift ;;
    --workspace-base) WORKSPACE_BASE="$2"; shift 2 ;;
    --nodes-per-replica) NODES_PER_REPLICA="$2"; shift 2 ;;
    --keep-serves) KEEP_SERVES="1"; shift ;;
    --partition) PARTITION="$2"; shift 2 ;;
    --discover-interval) DISCOVER_INTERVAL="$2"; shift 2 ;;
    --max-active-per-backend) MAX_ACTIVE_PER_BACKEND="$2"; shift 2 ;;
    --queue-timeout) QUEUE_TIMEOUT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$MODEL_KEY" ]]; then
  cat >&2 <<'USAGE'
Usage: full_eval.slurm --model MODEL_KEY [--serve-job JOB_ID [--serve-job JOB_ID2 ...]]
       [--concurrency N] [--time HH:MM:SS] [--resume DIR]
       [--runner oneshot|physicsintern] [--config CONFIG.yaml]
       [--fresh] [--workspace-base DIR] [--nodes-per-replica N]
       [--partition PARTITION] [--discover-interval SECONDS]
       [--max-active-per-backend N] [--queue-timeout SECONDS]
USAGE
  exit 1
fi

if [[ "$RUNNER" != "oneshot" && "$RUNNER" != "physicsintern" ]]; then
  echo "Invalid --runner: $RUNNER (must be 'oneshot' or 'physicsintern')" >&2
  exit 1
fi

if [[ -z "${SLURM_JOB_ID:-}" ]]; then
  MODEL_SLUG="${MODEL_KEY//[^[:alnum:]]/-}"
  while [[ "$MODEL_SLUG" == *--* ]]; do MODEL_SLUG="${MODEL_SLUG//--/-}"; done
  MODEL_SLUG="${MODEL_SLUG#-}"; MODEL_SLUG="${MODEL_SLUG%-}"
  JOB_NAME="critpt-${RUNNER}-${MODEL_SLUG}"
  mkdir -p "$LOG_DIR_DEFAULT"

  # For single serve-job, verify endpoint.env exists before submitting.
  # For multi-replica, skip — the load balancer waits inside the eval job.
  if [[ ${#SERVE_JOBS[@]} -eq 1 ]]; then
    ENDPOINT_ENV="${LOG_ROOT}/vllm/${SERVE_JOBS[0]}/endpoint.env"
    [[ -f "$ENDPOINT_ENV" ]] || { echo "endpoint.env not found: $ENDPOINT_ENV" >&2; exit 1; }
  fi

  if [[ ${#SERVE_JOBS[@]} -eq 0 ]]; then
    echo "Serve jobs: auto-discover"
  else
    echo "Serve jobs: ${SERVE_JOBS[*]}"
  fi
  echo "Runner:     ${RUNNER}"
  echo "Partition:  ${PARTITION}"
  [[ -n "$CONFIG_FILE" ]] && echo "Config:     ${CONFIG_FILE}"

  RESUME_ARG=()
  [[ -n "$RESUME_DIR" ]] && RESUME_ARG=(--resume "$RESUME_DIR")

  CONFIG_ARG=()
  [[ -n "$CONFIG_FILE" ]] && CONFIG_ARG=(--config "$CONFIG_FILE")

  FRESH_ARG=()
  [[ -n "$FRESH" ]] && FRESH_ARG=(--fresh)

  WS_ARG=()
  [[ -n "$WORKSPACE_BASE" ]] && WS_ARG=(--workspace-base "$WORKSPACE_BASE")

  NPR_ARG=()
  [[ -n "$NODES_PER_REPLICA" ]] && NPR_ARG=(--nodes-per-replica "$NODES_PER_REPLICA")

  KS_ARG=()
  [[ -n "$KEEP_SERVES" ]] && KS_ARG=(--keep-serves)

  LB_LIMIT_ARGS=(
    --max-active-per-backend "$MAX_ACTIVE_PER_BACKEND"
    --queue-timeout "$QUEUE_TIMEOUT"
  )

  SERVE_JOB_ARGS=()
  for jid in "${SERVE_JOBS[@]}"; do
    SERVE_JOB_ARGS+=(--serve-job "$jid")
  done

  exec sbatch --parsable \
    --job-name "$JOB_NAME" \
    --partition "$PARTITION" \
    --nodes 1 --ntasks-per-node 1 --cpus-per-task 16 --mem=64G \
    --time "$TIME_LIMIT" \
    --output "${LOG_DIR_DEFAULT}/%x-%j.out" \
    --error "${LOG_DIR_DEFAULT}/%x-%j.err" \
    "$SCRIPT_PATH" --model "$MODEL_KEY" \
                   "${SERVE_JOB_ARGS[@]}" \
                   --concurrency "$CONCURRENCY" --time "$TIME_LIMIT" \
                   --runner "$RUNNER" \
                   "${CONFIG_ARG[@]}" \
                   "${RESUME_ARG[@]}" \
                   "${FRESH_ARG[@]}" \
                   "${WS_ARG[@]}" \
                   "${NPR_ARG[@]}" \
                   "${KS_ARG[@]}" \
                   --partition "$PARTITION" \
                   --discover-interval "$DISCOVER_INTERVAL" \
                   "${LB_LIMIT_ARGS[@]}"
fi

# ── Inside Slurm ─────────────────────────────────────────────────────────────
[[ -f "$HOME/.bashrc" ]] && source "$HOME/.bashrc"
cd "$REPO_ROOT"

LB_PID=""
RUNNER_EXIT=1  # assume failure until proven otherwise
cleanup() {
  if [[ -n "$LB_PID" ]]; then
    echo "Shutting down load balancer (PID $LB_PID)..."
    kill "$LB_PID" 2>/dev/null || true
    wait "$LB_PID" 2>/dev/null || true
  fi
  # Cancel serve jobs when: (a) --keep-serves was NOT passed, or
  # (b) --keep-serves was passed but the runner finished successfully
  #     (exit 0 = natural completion, no need to keep serves for reruns).
  if [[ ${#SERVE_JOBS[@]} -gt 0 ]]; then
    if [[ -z "$KEEP_SERVES" || "$RUNNER_EXIT" -eq 0 ]]; then
      echo "Cancelling ${#SERVE_JOBS[@]} serve job(s): ${SERVE_JOBS[*]}"
      scancel "${SERVE_JOBS[@]}" 2>/dev/null || true
    else
      echo "Keeping ${#SERVE_JOBS[@]} serve job(s) alive (--keep-serves, runner exit=$RUNNER_EXIT)"
    fi
  fi
}
trap cleanup EXIT

if [[ ${#SERVE_JOBS[@]} -ne 1 ]]; then
  # Multi-replica: start load balancer and wait for it
  LB_ARGS=(
    "${SERVE_JOBS[@]}"
    --port 9000
    --health-timeout 3600
    --model "$MODEL_KEY"
    --discover-interval "$DISCOVER_INTERVAL"
    --max-active-per-backend "$MAX_ACTIVE_PER_BACKEND"
    --queue-timeout "$QUEUE_TIMEOUT"
  )
  [[ -n "$NODES_PER_REPLICA" ]] && LB_ARGS+=(--nodes-per-replica "$NODES_PER_REPLICA")
  if [[ ${#SERVE_JOBS[@]} -eq 0 ]]; then
    echo "Starting load balancer with auto-discovery every ${DISCOVER_INTERVAL}s..."
  else
    echo "Starting load balancer for ${#SERVE_JOBS[@]} replicas..."
  fi
  uv run --no-sync python serve/load_balancer.py "${LB_ARGS[@]}" &
  LB_PID=$!

  # Wait for load balancer to become healthy
  WAIT=0; MAX=14400
  until curl -sf "http://localhost:9000/health" >/dev/null 2>&1; do
    (( WAIT >= MAX )) && { echo "Load balancer timed out after ${MAX}s" >&2; exit 1; }
    sleep 10; WAIT=$(( WAIT + 10 ))
    if (( WAIT % 60 == 0 )); then echo "  ...waiting for load balancer (${WAIT}s)"; fi
  done
  echo "Load balancer ready after ${WAIT}s (PID ${LB_PID})"

  export VLLM_BASE_URL="http://localhost:9000/v1"
else
  # Single serve job: read endpoint directly
  ENDPOINT_ENV="${LOG_ROOT}/vllm/${SERVE_JOBS[0]}/endpoint.env"
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
fi

RESUME_ARG=()
[[ -n "$RESUME_DIR" ]] && RESUME_ARG=(--resume "$RESUME_DIR")

CONFIG_ARG=()
[[ -n "$CONFIG_FILE" ]] && CONFIG_ARG=(--config "$CONFIG_FILE")

FRESH_ARG=()
[[ -n "$FRESH" ]] && FRESH_ARG=(--fresh)

WS_ARG=()
[[ -n "$WORKSPACE_BASE" ]] && WS_ARG=(--workspace-base "$WORKSPACE_BASE")

if [[ "$RUNNER" == "physicsintern" ]]; then
  uv run --no-sync python scripts/run_critpt_physics_intern.py \
    --model "$MODEL_KEY" --concurrency "$CONCURRENCY" \
    "${CONFIG_ARG[@]}" \
    "${RESUME_ARG[@]}" \
    "${FRESH_ARG[@]}" \
    "${WS_ARG[@]}" && RUNNER_EXIT=0 || RUNNER_EXIT=$?
else
  uv run --no-sync python scripts/run_critpt_oneshot.py \
    --model "$MODEL_KEY" --concurrency "$CONCURRENCY" --timeout 3600 \
    "${RESUME_ARG[@]}" && RUNNER_EXIT=0 || RUNNER_EXIT=$?
fi
