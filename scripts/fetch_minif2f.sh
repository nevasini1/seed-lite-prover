#!/usr/bin/env bash
# Clone the public Lean 4 port of MiniF2F and copy its per-theorem validation
# files into benchmarks/minif2f_valid/. Each upstream file is self-contained
# (own `import Mathlib`, `open ...`, and one `theorem <name> ... := by sorry`),
# so we just `cp` them. Idempotent. Run from repo root.
set -euo pipefail

REPO_URL="${MINIF2F_REPO:-https://github.com/yangky11/miniF2F-lean4.git}"
TMP="${TMPDIR:-/tmp}/miniF2F-lean4"
DEST="$(cd "$(dirname "$0")/.." && pwd)/benchmarks/minif2f_valid"

if [[ ! -d "$TMP/.git" ]]; then
    git clone --depth 1 "$REPO_URL" "$TMP"
else
    git -C "$TMP" pull --ff-only || true
fi

SRC="$TMP/MiniF2F/Valid"
if [[ ! -d "$SRC" ]]; then
    echo "expected $SRC; upstream layout may have changed" >&2
    exit 1
fi

mkdir -p "$DEST"
count=0
for f in "$SRC"/*.lean; do
    cp "$f" "$DEST/"
    count=$((count + 1))
done
echo "copied $count problem files to $DEST"
