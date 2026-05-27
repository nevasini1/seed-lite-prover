#!/usr/bin/env bash
# Clone the Lean 4 port of MiniF2F at a PINNED commit and copy its
# per-theorem validation files into benchmarks/minif2f_valid/.
# Each upstream file is self-contained, so we just `cp` them. Idempotent.
# Run from repo root.
set -euo pipefail

REPO_URL="${MINIF2F_REPO:-https://github.com/yangky11/miniF2F-lean4.git}"
# Pinned upstream commit — reproducibility. Override at your own risk.
MINIF2F_REF="${MINIF2F_REF:-5746b7d6c47855ce1294bed87329618ff7f1bc31}"
TMP="${TMPDIR:-/tmp}/miniF2F-lean4"
DEST="$(cd "$(dirname "$0")/.." && pwd)/benchmarks/minif2f_valid"

if [[ ! -d "$TMP/.git" ]]; then
    git clone "$REPO_URL" "$TMP"
fi
git -C "$TMP" fetch origin "$MINIF2F_REF" 2>/dev/null || git -C "$TMP" fetch origin
git -C "$TMP" checkout --detach "$MINIF2F_REF"

SRC="$TMP/MiniF2F/Valid"
if [[ ! -d "$SRC" ]]; then
    echo "expected $SRC; upstream layout may have changed" >&2
    exit 1
fi

mkdir -p "$DEST"
# Wipe any previously-copied .lean files so removed-upstream problems
# don't linger and cause attribution drift between runs.
find "$DEST" -maxdepth 1 -name "*.lean" -delete

count=0
for f in "$SRC"/*.lean; do
    cp "$f" "$DEST/"
    count=$((count + 1))
done
echo "copied $count problem files to $DEST (pinned at $MINIF2F_REF)"
