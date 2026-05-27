#!/usr/bin/env python3
"""Score ablation JSONL files and emit a markdown summary.

Adds:
- 95% Wilson confidence intervals on per-variant pass rates.
- Paired McNemar-style comparison when ≥2 variants ran on the same
  problem set: per-problem deltas + a discordant-pair count.
- Skips the `_type: run_metadata` header line at the top of each JSONL.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% confidence interval for a binomial proportion.
    Returns (lo, hi) on the [0, 1] scale. Handles k=0 and k=n cleanly,
    which the normal-approximation interval does not."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", help="one or more ablation_*.jsonl files")
    parser.add_argument("--out", default=None, help="write markdown here (default: stdout)")
    args = parser.parse_args()

    rows: list[dict] = []
    metadata: list[dict] = []
    for f in args.files:
        for line in Path(f).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("_type") == "run_metadata":
                metadata.append(obj)
                continue
            rows.append(obj)

    by_variant: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_variant[r.get("variant", "?")].append(r)

    lines = ["# Ablation results", ""]
    if metadata:
        m = metadata[0]
        lines.append(
            f"_Run {m.get('timestamp', '?')} · git {m.get('git_sha', '?')[:8]}"
            f"{' (dirty)' if m.get('git_dirty') else ''} · "
            f"lean {(m.get('lean_version') or '?').splitlines()[0][:40] if m.get('lean_version') else m.get('lean_toolchain', '?')} · "
            f"backend {m.get('lean_backend', '?')} · "
            f"cache-mode {m.get('cache_mode', '?')} · "
            f"prover {m.get('prover_model', '?')}_"
        )
        lines.append("")

    lines.append("## Per-variant pass rates (95% Wilson CI)")
    lines.append("")
    lines.append("| Variant | Name | n | Solved | Pass rate | 95% CI | Avg s/problem |")
    lines.append("|---|---|---:|---:|---:|---|---:|")
    for vk in sorted(by_variant):
        items = by_variant[vk]
        total = len(items)
        solved = sum(1 for r in items if r.get("solved"))
        avg_s = sum(r.get("elapsed_s", 0.0) for r in items) / max(1, total)
        name = items[0].get("variant_name", "")
        rate = (solved / total * 100.0) if total else 0.0
        lo, hi = wilson_ci(solved, total)
        lines.append(
            f"| {vk} | {name} | {total} | {solved} | "
            f"{rate:.1f}% | [{lo*100:.1f}, {hi*100:.1f}] | {avg_s:.1f} |"
        )

    # Paired comparison when ≥2 variants share a problem set.
    variant_keys = sorted(by_variant.keys())
    if len(variant_keys) >= 2:
        lines.append("")
        lines.append("## Paired comparison (same problems, per-variant deltas)")
        lines.append("")
        # Problem set = problems attempted by ALL variants in this run.
        per_v_problems: dict[str, set[str]] = {v: {r["problem"] for r in by_variant[v]} for v in variant_keys}
        shared = set.intersection(*per_v_problems.values()) if per_v_problems else set()
        if not shared:
            lines.append("_No problems attempted by all variants — paired comparison skipped._")
        else:
            # Map (variant, problem) -> solved
            solved_map: dict[tuple[str, str], bool] = {}
            for v in variant_keys:
                for r in by_variant[v]:
                    if r["problem"] in shared:
                        solved_map[(v, r["problem"])] = bool(r.get("solved"))

            # All pairwise comparisons in a compact summary
            lines.append("| A vs B | n_shared | A-only wins | B-only wins | both | neither | A→B Δ (pp) |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for i, a in enumerate(variant_keys):
                for b in variant_keys[i + 1:]:
                    a_only = sum(1 for p in shared if solved_map.get((a, p)) and not solved_map.get((b, p)))
                    b_only = sum(1 for p in shared if not solved_map.get((a, p)) and solved_map.get((b, p)))
                    both = sum(1 for p in shared if solved_map.get((a, p)) and solved_map.get((b, p)))
                    neither = len(shared) - a_only - b_only - both
                    a_rate = (a_only + both) / len(shared)
                    b_rate = (b_only + both) / len(shared)
                    delta_pp = (b_rate - a_rate) * 100.0
                    lines.append(
                        f"| {a} → {b} | {len(shared)} | {a_only} | {b_only} | "
                        f"{both} | {neither} | {delta_pp:+.1f} |"
                    )

            lines.append("")
            lines.append(
                "_Discordant pairs (A-only + B-only) drive the McNemar test. "
                "For a defensible claim of difference, expect discordant ≥ 6 and a clear majority direction._"
            )

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
