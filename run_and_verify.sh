#!/usr/bin/env bash
# Run a SciRalph research session, then verify the results with Claude Opus.
#
# Usage:
#   ./run_and_verify.sh <problem.yaml> [run-options...] [-- verify-options...]
#
# Examples:
#   ./run_and_verify.sh problems/hawking_temperature.yaml --max-iterations 10
#   ./run_and_verify.sh problems/qho.yaml --max-iterations 5 -- --rerun-computations
#
# Everything before "--" is passed to sciralph.main.
# Everything after "--" is passed to sciralph.verify (--write-report is always on).

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <problem.yaml> [run-options...] [-- verify-options...]"
    exit 1
fi

# Split args at "--"
run_args=()
verify_args=()
past_separator=false

for arg in "$@"; do
    if [ "$arg" = "--" ]; then
        past_separator=true
        continue
    fi
    if $past_separator; then
        verify_args+=("$arg")
    else
        run_args+=("$arg")
    fi
done

problem_file="${run_args[0]}"
if [ ! -f "$problem_file" ]; then
    echo "Error: problem file not found: $problem_file"
    exit 1
fi

# Determine workspace dir (same logic as main.py)
timestamp=$(date -u +"%Y%m%d_%H%M%S")
stem=$(basename "$problem_file" .yaml)
workspace_dir="workspaces/${timestamp}_${stem}"

# Check if a --workspace-dir was passed in run_args
for i in "${!run_args[@]}"; do
    if [ "${run_args[$i]}" = "--workspace-dir" ]; then
        workspace_dir="${run_args[$((i + 1))]}"
        break
    fi
done

# If no explicit --workspace-dir, inject ours so we know where to look
has_ws_flag=false
for arg in "${run_args[@]}"; do
    if [ "$arg" = "--workspace-dir" ]; then
        has_ws_flag=true
        break
    fi
done
if ! $has_ws_flag; then
    run_args+=("--workspace-dir" "$workspace_dir")
fi

echo "=== SciRalph: Run + Verify ==="
echo "Problem:   $problem_file"
echo "Workspace: $workspace_dir"
echo ""

# --- Phase 1: Research run ---
echo "--- Phase 1: Research run ---"
uv run python -m sciralph.main "${run_args[@]}"
echo ""

# --- Phase 2: Verification ---
echo "--- Phase 2: Verification (Claude Opus) ---"
uv run python -m sciralph.verify "$workspace_dir" --write-report ${verify_args[@]+"${verify_args[@]}"}
