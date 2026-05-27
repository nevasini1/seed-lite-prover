#!/usr/bin/env python3
"""REPL ↔ subprocess parity test.

The REPL backend strips `import` / `set_option` / `open` / `namespace` /
`universe` header lines because the warmup env already has them applied.
That can silently change semantics for snippets needing problem-specific
opens / universes / options. This script catches divergences by running
a fixture through BOTH backends and comparing the `ok` verdict.

Known divergences as of 2026-05-27 (filed as issues, not blocking the
soundness fix this script was written to support):
- `mathlib_uses_omega`: REPL marks `:= by omega` as failed when import is
  stripped; subprocess accepts it. Suggests the REPL warmup env is missing
  some `Mathlib.Tactic.*` modules even though `import Mathlib` was applied.
- `syntax_error` (`:= by zzqq_not_a_tactic`): REPL emits a warning but
  reports `ok=True` (Lean elaborator treats the bad identifier as a no-op
  somehow); subprocess reports `ok=False`. Worth investigating Lean-side.

Run from repo root:
    python scripts/test_repl_subprocess_parity.py

Exits 0 if every fixture's verdict matches across backends, 1 otherwise.
The known divergences above currently cause exit 1 — that's expected
until those Lean-API surface issues are resolved.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seed_lite_prover.lean_runner import LeanRunner


# Each fixture: (label, snippet, expected_ok). expected_ok is what BOTH
# backends should return; the parity check is that they agree, regardless
# of which one matches expected_ok.
FIXTURES = [
    (
        "trivial_rfl",
        "theorem fix_rfl (n : Nat) : n + 0 = n := by rfl",
        True,
    ),
    (
        "sorry_must_be_rejected",
        "theorem fix_sorry (n : Nat) : n + 0 = n := by sorry",
        False,
    ),
    (
        "admit_must_be_rejected",
        "theorem fix_admit (n : Nat) : n + 0 = n := by admit",
        False,
    ),
    (
        "mathlib_uses_omega",
        "import Mathlib\n\ntheorem fix_omega (n : Nat) : 0 < n + 1 := by omega",
        True,
    ),
    (
        "mathlib_opens",
        # Uses `BigOperators` notation `∑`. Subprocess needs the open;
        # REPL strips it but warmup should have established it.
        "import Mathlib\nopen BigOperators\n\nexample (n : Nat) : ∑ k ∈ Finset.range n, (1 : Nat) = n := by\n  induction n with\n  | zero => simp\n  | succ k ih => rw [Finset.sum_range_succ, ih]",
        True,
    ),
    (
        "syntax_error",
        "theorem fix_syntax (n : Nat) : n + 0 = n := by zzqq_not_a_tactic",
        False,
    ),
    (
        "unknown_identifier",
        "import Mathlib\n\ntheorem fix_unknown (n : Nat) : n + 0 = n := by exact Nat.foobar_baz n",
        False,
    ),
]


@dataclass
class Outcome:
    backend: str
    ok: bool
    err_head: str


def _run(runner: LeanRunner, source: str) -> Outcome:
    res = runner.check(source)
    return Outcome(
        backend=runner.backend,
        ok=res.ok,
        err_head="\n".join(res.stderr.splitlines()[:3]),
    )


def main() -> int:
    project = ROOT / "lean_project"
    print(f"=== REPL ↔ subprocess parity test ===\nProject: {project}\n")

    # REPL backend
    repl_runner = LeanRunner(str(project), timeout_s=120)
    if not repl_runner.has_repl:
        print("REPL backend not available (lean_project/repl not built?)")
        print("Run `cd lean_project && lake update && lake build` and retry.")
        return 2

    # Subprocess backend — explicitly disable REPL by patching _repl=None
    sub_runner = LeanRunner(str(project), timeout_s=120)
    sub_runner.close()
    # Force subprocess mode for the comparator
    sub_runner._repl = None  # type: ignore[attr-defined]
    assert sub_runner.backend == "subprocess", sub_runner.backend

    print(f"{'Fixture':<30} {'expected':>8}  {'REPL':>8}  {'subproc':>8}  {'agree?':>7}")
    print("-" * 75)
    mismatches = 0
    for label, source, expected in FIXTURES:
        repl_out = _run(repl_runner, source)
        sub_out = _run(sub_runner, source)
        agree = repl_out.ok == sub_out.ok
        if not agree:
            mismatches += 1
        marker = "✓" if agree else "✗"
        print(
            f"{label:<30} {str(expected):>8}  {str(repl_out.ok):>8}  "
            f"{str(sub_out.ok):>8}  {marker:>7}"
        )
        if not agree:
            print(f"    REPL err: {repl_out.err_head[:160]}")
            print(f"    sub  err: {sub_out.err_head[:160]}")

    print("-" * 75)
    if mismatches:
        print(f"\n{mismatches} mismatch(es) — backends disagree on the snippets above.")
        return 1
    print(f"\nAll {len(FIXTURES)} fixtures agree across backends.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
