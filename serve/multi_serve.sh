#!/bin/bash
# Launch multiple independent vLLM serve replicas for external data parallelism.
#
# Each replica is a separate serve.slurm job with its own head node. After all
# replicas start, a combined endpoint file is written with all BASE_URLs.
#
# Usage:
#   ./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --replicas 4 --nodes-per-replica 4
#   ./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --replicas 8 --nodes-per-replica 4
#
# Output: serve/logs/vllm/multi-<TIMESTAMP>/endpoints.env
#   Contains BASE_URLS=url1,url2,...,urlN for the load balancer or benchmark script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVE_SCRIPT="${SCRIPT_DIR}/serve.slurm"
LOG_ROOT="${REPO_ROOT}/serve/logs"

MODEL=""
REPLICAS=""
NODES_PER_REPLICA=""
GPUS_PER_NODE="8"
TIME_LIMIT="24:00:00"
IDLE_SHUTDOWN="14400"
MAX_NUM_SEQS=""
EXTRA_ARGS=()

usage() {
  cat <<'USAGE'
Usage:
  ./serve/multi_serve.sh --model MODEL --replicas N --nodes-per-replica K [options] [-- ...extra vllm args]

Examples:
  # 4 replicas of Kimi (4 nodes each = 16 nodes total, default)
  ./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --replicas 4 --nodes-per-replica 4

  # 8 replicas of Kimi (4 nodes each = 32 nodes total)
  ./serve/multi_serve.sh --model moonshotai/Kimi-K2.6 --replicas 8 --nodes-per-replica 4

Options:
  --model MODEL               Model to serve (required).
  --replicas N                Number of independent replicas (required).
  --nodes-per-replica K       Nodes per replica (required).
  --gpus-per-node N           GPUs per node (default: 8).
  --time HH:MM:SS             Slurm time limit (default: 24:00:00).
  --idle-shutdown SECONDS     Idle watchdog timeout (default: 14400).
  --max-num-seqs N            Max concurrent sequences per replica.
  --                          Extra args passed through to serve.slurm.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --replicas) REPLICAS="$2"; shift 2 ;;
    --nodes-per-replica) NODES_PER_REPLICA="$2"; shift 2 ;;
    --gpus-per-node) GPUS_PER_NODE="$2"; shift 2 ;;
    --time) TIME_LIMIT="$2"; shift 2 ;;
    --idle-shutdown) IDLE_SHUTDOWN="$2"; shift 2 ;;
    --max-num-seqs) MAX_NUM_SEQS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; EXTRA_ARGS=("$@"); break ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$MODEL" || -z "$REPLICAS" || -z "$NODES_PER_REPLICA" ]]; then
  echo "Missing required arguments." >&2
  usage
  exit 1
fi

TOTAL_NODES=$(( REPLICAS * NODES_PER_REPLICA ))
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
MULTI_DIR="${LOG_ROOT}/vllm/multi-${TIMESTAMP}"
mkdir -p "$MULTI_DIR"

echo "Launching ${REPLICAS} replica(s) of ${MODEL}"
echo "  Nodes per replica: ${NODES_PER_REPLICA}"
echo "  Total nodes:       ${TOTAL_NODES}"
echo "  Output dir:        ${MULTI_DIR}"
echo "---"

JOB_IDS=()
for i in $(seq 1 "$REPLICAS"); do
  SERVE_ARGS=(
    "$SERVE_SCRIPT"
    --model "$MODEL"
    --nodes "$NODES_PER_REPLICA"
    --gpus-per-node "$GPUS_PER_NODE"
    --time "$TIME_LIMIT"
    --idle-shutdown "$IDLE_SHUTDOWN"
  )
  if [[ -n "$MAX_NUM_SEQS" ]]; then
    SERVE_ARGS+=(-- --max-num-seqs "$MAX_NUM_SEQS" "${EXTRA_ARGS[@]}")
  elif [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    SERVE_ARGS+=(-- "${EXTRA_ARGS[@]}")
  fi

  JOB_ID=$(_MULTI_SERVE_PARENT=1 "${SERVE_ARGS[@]}" 2>&1 | grep -oP 'Submitted \K\d+')
  JOB_IDS+=("$JOB_ID")
  echo "Replica ${i}/${REPLICAS}: job ${JOB_ID}"
done

# Write multi-serve metadata.
cat >"${MULTI_DIR}/multi_serve.env" <<EOF
MODEL=${MODEL}
REPLICAS=${REPLICAS}
NODES_PER_REPLICA=${NODES_PER_REPLICA}
TOTAL_NODES=${TOTAL_NODES}
JOB_IDS=${JOB_IDS[*]}
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
