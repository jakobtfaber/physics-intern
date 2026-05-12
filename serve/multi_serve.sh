#!/bin/bash
# Launch multiple independent vLLM serve replicas for external data parallelism.
#
# Each replica is a separate serve.slurm job with its own head node. After all
# replicas start, a combined endpoint file is written with all BASE_URLs.
#
# Usage:
#   ./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --normal-replicas 4 --nodes-per-replica 4
#   ./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --normal-replicas 4 --low-replicas auto --nodes-per-replica 4
#
# Output: serve/logs/vllm/multi-<TIMESTAMP>/endpoints.env
#   Contains BASE_URLS=url1,url2,...,urlN for the load balancer or benchmark script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVE_SCRIPT="${SCRIPT_DIR}/serve.slurm"
LOG_ROOT="${REPO_ROOT}/serve/logs"

MODEL=""
NORMAL_REPLICAS=""
LOW_REPLICAS="auto"
LOW_MIN_REPLICAS="2"
NODES_PER_REPLICA=""
GPUS_PER_NODE="8"
PARTITION="hopper-prod"
NORMAL_QOS="normal"
LOW_QOS="low"
TIME_LIMIT="24:00:00"
IDLE_SHUTDOWN="7200"
MAX_NUM_SEQS=""
EXTRA_ARGS=()

usage() {
  cat <<'USAGE'
Usage:
  ./serve/multi_serve.sh --model MODEL --normal-replicas N --low-replicas N|auto --nodes-per-replica K [options] [-- ...extra vllm args]

Examples:
  # 4 normal-QOS replicas plus auto-sized low-QOS replicas from idle capacity
  ./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --normal-replicas 4 --nodes-per-replica 4

  # 4 normal-QOS replicas only
  ./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --normal-replicas 4 --low-replicas 0 --nodes-per-replica 4

Options:
  --model MODEL               Model to serve (required).
  --normal-replicas N         Number of normal-QOS replicas.
  --low-replicas N|auto       Number of low-QOS replicas, or "auto" to use
                              idle nodes after normal replicas (default: auto).
  --low-min-replicas N        Minimum low-QOS replicas in auto mode (default: 2).
  --nodes-per-replica K       Nodes per replica (required).
  --gpus-per-node N           GPUs per node (default: 8).
  --partition NAME            Slurm partition (default: hopper-prod).
  --normal-qos NAME           QOS for normal replicas (default: normal).
  --low-qos NAME              QOS for opportunistic replicas (default: low).
  --time HH:MM:SS             Slurm time limit (default: 24:00:00).
  --idle-shutdown SECONDS     Idle watchdog timeout (default: 7200).
  --max-num-seqs N            Max concurrent sequences per replica.
  --                          Extra args passed through to serve.slurm.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --normal-replicas) NORMAL_REPLICAS="$2"; shift 2 ;;
    --low-replicas) LOW_REPLICAS="$2"; shift 2 ;;
    --low-min-replicas) LOW_MIN_REPLICAS="$2"; shift 2 ;;
    --nodes-per-replica) NODES_PER_REPLICA="$2"; shift 2 ;;
    --gpus-per-node) GPUS_PER_NODE="$2"; shift 2 ;;
    --partition) PARTITION="$2"; shift 2 ;;
    --normal-qos) NORMAL_QOS="$2"; shift 2 ;;
    --low-qos) LOW_QOS="$2"; shift 2 ;;
    --time) TIME_LIMIT="$2"; shift 2 ;;
    --idle-shutdown) IDLE_SHUTDOWN="$2"; shift 2 ;;
    --max-num-seqs) MAX_NUM_SEQS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; EXTRA_ARGS=("$@"); break ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$MODEL" || -z "$NORMAL_REPLICAS" || -z "$NODES_PER_REPLICA" ]]; then
  echo "Missing required arguments." >&2
  usage
  exit 1
fi

if [[ ! "$NORMAL_REPLICAS" =~ ^[0-9]+$ || "$NORMAL_REPLICAS" -lt 0 ]]; then
  echo "--normal-replicas must be a non-negative integer." >&2
  exit 1
fi
if [[ "$LOW_REPLICAS" != "auto" && ( ! "$LOW_REPLICAS" =~ ^[0-9]+$ || "$LOW_REPLICAS" -lt 0 ) ]]; then
  echo "--low-replicas must be a non-negative integer or 'auto'." >&2
  exit 1
fi
if [[ ! "$LOW_MIN_REPLICAS" =~ ^[0-9]+$ ]]; then
  echo "--low-min-replicas must be a non-negative integer." >&2
  exit 1
fi

IDLE_NODES=""
if [[ "$LOW_REPLICAS" == "auto" ]]; then
  IDLE_NODES="$(sinfo -h -p "$PARTITION" -t idle -o "%D" | awk '{ total += $1 } END { print total + 0 }')"
  RESERVED_NORMAL_NODES=$(( NORMAL_REPLICAS * NODES_PER_REPLICA ))
  REMAINING_NODES=$(( IDLE_NODES - RESERVED_NORMAL_NODES ))
  if (( REMAINING_NODES > 0 )); then
    LOW_REPLICAS=$(( REMAINING_NODES / NODES_PER_REPLICA ))
  else
    LOW_REPLICAS=0
  fi
  if (( LOW_REPLICAS < LOW_MIN_REPLICAS )); then
    LOW_REPLICAS="$LOW_MIN_REPLICAS"
  fi
fi

REPLICAS=$(( NORMAL_REPLICAS + LOW_REPLICAS ))
TOTAL_NODES=$(( REPLICAS * NODES_PER_REPLICA ))
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
MULTI_DIR="${LOG_ROOT}/vllm/multi-${TIMESTAMP}"
mkdir -p "$MULTI_DIR"

echo "Launching ${REPLICAS} replica(s) of ${MODEL}"
echo "  Normal replicas:   ${NORMAL_REPLICAS} (qos=${NORMAL_QOS})"
echo "  Low replicas:      ${LOW_REPLICAS} (qos=${LOW_QOS}, requeue=on)"
if [[ -n "$IDLE_NODES" ]]; then
  echo "  Idle ${PARTITION} nodes: ${IDLE_NODES}"
fi
echo "  Nodes per replica: ${NODES_PER_REPLICA}"
echo "  Total nodes:       ${TOTAL_NODES}"
echo "  Output dir:        ${MULTI_DIR}"
echo "---"

JOB_IDS=()
submit_replica() {
  local replica_idx="$1"
  local total_replicas="$2"
  local qos="$3"
  local requeue="$4"
  local label="$5"
  SERVE_ARGS=(
    "$SERVE_SCRIPT"
    --model "$MODEL"
    --nodes "$NODES_PER_REPLICA"
    --gpus-per-node "$GPUS_PER_NODE"
    --partition "$PARTITION"
    --qos "$qos"
    --time "$TIME_LIMIT"
    --idle-shutdown "$IDLE_SHUTDOWN"
  )
  if [[ "$requeue" == "1" ]]; then
    SERVE_ARGS+=(--requeue)
  fi
  if [[ -n "$MAX_NUM_SEQS" ]]; then
    SERVE_ARGS+=(-- --max-num-seqs "$MAX_NUM_SEQS" "${EXTRA_ARGS[@]}")
  elif [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    SERVE_ARGS+=(-- "${EXTRA_ARGS[@]}")
  fi

  JOB_ID=$(_MULTI_SERVE_PARENT=1 "${SERVE_ARGS[@]}" 2>&1 | grep -oP 'Submitted \K\d+')
  JOB_IDS+=("$JOB_ID")
  echo "Replica ${replica_idx}/${total_replicas} [${label}]: job ${JOB_ID}"
}

replica_i=0
for _ in $(seq 1 "$NORMAL_REPLICAS"); do
  replica_i=$((replica_i + 1))
  submit_replica "$replica_i" "$REPLICAS" "$NORMAL_QOS" "0" "normal"
done
for _ in $(seq 1 "$LOW_REPLICAS"); do
  replica_i=$((replica_i + 1))
  submit_replica "$replica_i" "$REPLICAS" "$LOW_QOS" "1" "low"
done

# Write multi-serve metadata.
cat >"${MULTI_DIR}/multi_serve.env" <<EOF
MODEL=${MODEL}
REPLICAS=${REPLICAS}
NORMAL_REPLICAS=${NORMAL_REPLICAS}
LOW_REPLICAS=${LOW_REPLICAS}
NODES_PER_REPLICA=${NODES_PER_REPLICA}
TOTAL_NODES=${TOTAL_NODES}
JOB_IDS=${JOB_IDS[*]}
PARTITION=${PARTITION}
NORMAL_QOS=${NORMAL_QOS}
LOW_QOS=${LOW_QOS}
MAX_NUM_SEQS=${MAX_NUM_SEQS:-default}
TIMESTAMP=${TIMESTAMP}
EOF

echo ""
echo "All ${REPLICAS} replicas submitted: ${JOB_IDS[*]}"
echo "Metadata: ${MULTI_DIR}/multi_serve.env"
echo ""
echo "Wait for all replicas to start, then collect endpoints:"
echo "  for jid in ${JOB_IDS[*]}; do"
echo "    cat serve/logs/vllm/\${jid}/endpoint.env"
echo "  done"
echo ""
echo "Start load balancer:"
echo "  uv run python serve/load_balancer.py ${JOB_IDS[*]}"
echo ""
echo "Or benchmark directly with client-side round-robin:"
echo "  uv run python scripts/benchmark_throughput.py --serve-job ${JOB_IDS[*]}"
