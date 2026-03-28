#!/usr/bin/env bash
# Overlay proprietary engine files from private/ onto unchained_cli/.
# Run before building binaries or running with full engine support.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PRIVATE="$ROOT/private"
TARGET="$ROOT/unchained_cli"

FILES=(ddm_engine.py intel_engine.py)

for f in "${FILES[@]}"; do
    src="$PRIVATE/$f"
    dst="$TARGET/$f"
    if [ ! -f "$src" ]; then
        echo "ERROR: $src not found" >&2
        exit 1
    fi
    cp "$src" "$dst"
    echo "  Overlaid $f"
done

echo "Private core installed. Ready to build."
