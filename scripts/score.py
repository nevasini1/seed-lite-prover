#!/usr/bin/env python3
"""Score ablation JSONL files and emit a markdown summary."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", help="one or more ablation_*.jsonl files")
    parser.add_argument("--out", default=None, help="write markdown here (default: stdout)")
    args = parser.parse_args()

    rows: list[dict] = []
    for f in args.files:
        for line in Path(f).read_text().splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    by_variant: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_variant[r.get("variant", "?")].append(r)

    lines = ["# Ablation results", ""]
    lines.append("| Variant | Name | Problems | Solved | Pass rate | Avg s/problem |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for vk in sorted(by_variant):
        items = by_variant[vk]
        total = len(items)
        solved = sum(1 for r in items if r.get("solved"))
        avg_s = sum(r.get("elapsed_s", 0.0) for r in items) / max(1, total)
        name = items[0].get("variant_name", "")
        rate = (solved / total * 100.0) if total else 0.0
        lines.append(f"| {vk} | {name} | {total} | {solved} | {rate:.1f}% | {avg_s:.1f} |")

    lines.append("")
    lines.append("## Per-problem detail")
    lines.append("")
    lines.append("| Variant | Problem | Solved | Source | Elapsed |")
    lines.append("|---|---|---|---|---:|")
    for r in rows:
        lines.append(
            f"| {r.get('variant')} | {r.get('problem')} | "
            f"{'OK' if r.get('solved') else '--'} | "
            f"{r.get('winning_source', '')} | "
            f"{r.get('elapsed_s', 0.0):.1f}s |"
        )

    out = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
