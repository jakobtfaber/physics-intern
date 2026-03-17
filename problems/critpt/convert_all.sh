#!/usr/bin/env bash
# Convert all JSON files in json/ to YAML problem files.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for f in "$SCRIPT_DIR"/json/*.json; do
    echo "Converting: $(basename "$f")"
    python3 "$SCRIPT_DIR/json_to_yaml.py" "$f"
done

echo "Done."
