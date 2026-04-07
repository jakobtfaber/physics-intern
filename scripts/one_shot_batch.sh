#!/usr/bin/env bash
# Run one-shot baseline over all problem YAMLs in a folder.
#
# Usage:
#   ./one_shot_batch.sh <model> <problem_folder> [runs]
#
# Examples:
#   ./one_shot_batch.sh claude-sonnet-4.6 problems/tier1
#   ./one_shot_batch.sh gpt-5.4-high problems/tier2 10

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <model> <problem_folder> [runs]" >&2
    echo "  model          — model key from models.yaml (e.g. claude-sonnet-4.6)" >&2
    echo "  problem_folder — directory containing problem YAML files" >&2
    echo "  runs           — number of runs per problem (default: 5)" >&2
    exit 1
fi

MODEL="$1"
FOLDER="$2"
RUNS="${3:-5}"

if [[ ! -d "$FOLDER" ]]; then
    echo "Error: directory not found: $FOLDER" >&2
    exit 1
fi

# Collect YAML files
shopt -s nullglob
FILES=("$FOLDER"/*.yaml "$FOLDER"/*.yml)
shopt -u nullglob

if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "Error: no YAML files found in $FOLDER" >&2
    exit 1
fi

echo "Model:    $MODEL"
echo "Folder:   $FOLDER"
echo "Runs:     $RUNS per problem"
echo "Problems: ${#FILES[@]}"
echo "---"

PASSED=0
FAILED=0

for f in "${FILES[@]}"; do
    NAME="$(basename "$f")"
    echo ""
    echo "=== $NAME ==="
    if uv run python -m open_dirac.one_shot "$f" --model "$MODEL" --runs "$RUNS"; then
        PASSED=$((PASSED + 1))
    else
        echo "  *** FAILED: $NAME ***" >&2
        FAILED=$((FAILED + 1))
    fi
done

echo ""
echo "=== Done ==="
echo "Completed: $PASSED/${#FILES[@]}  |  Failed: $FAILED"
