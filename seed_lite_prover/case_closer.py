"""Cheap mechanical near-miss closer.

When a whole-proof attempt fails with `unsolved goals case <name>`, we don't
need an LLM to patch it — the failure mode is almost always "model produced
a correct induction skeleton but didn't close one of the explicit cases".
Parse the case names out of the error, append `case <name> => <battery>`
lines, re-check. Pure Python, no model call.

The battery is the same set of closing tactics our symbolic preamble uses,
applied inside each unclosed case. If even one combination closes all of
them, we win. If not, we record the attempts and fall through to the
heavier (and slower) repair/decompose machinery.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from .lean_snippets import LeanProblem, indent, wrap

if TYPE_CHECKING:
    from .orchestrator import ProofAttempt


# Lean's unsolved-goals error reports each unclosed case as a stanza:
#
#     error: unsolved goals
#     case zero
#     ⊢ ...
#     case succ
#     n : ℕ
#     ih : ...
#     ⊢ ...
#
# We extract the bare case names (`zero`, `succ`, etc.) in order. Mathlib
# style is lowercase or snake_case identifiers — we accept either.
_CASE_NAME = re.compile(r"^case\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", re.MULTILINE)


# Closing tactics, ordered cheap-to-expensive. Each is a single Lean line
# that closes a case when applied as the case body.
_CLOSERS = [
    "rfl",
    "simp",
    "decide",
    "norm_num",
    "omega",
    "linarith",
    "nlinarith",
    "simp_all",
    "aesop",
    "ring",
    "(simp [Finset.sum_range_succ]; ring)",
    "(simp_all; omega)",
    "(simp_all; ring)",
    "(field_simp; ring)",
    "tauto",
]


def _parse_unsolved_cases(stderr: str) -> list[str]:
    """Return ordered, deduplicated list of unsolved case names from a
    Lean stderr blob. Empty list if no `case` stanzas found."""
    if "unsolved goals" not in stderr.lower():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _CASE_NAME.finditer(stderr):
        nm = m.group(1)
        if nm in seen:
            continue
        seen.add(nm)
        out.append(nm)
    return out


_BATTERY = (
    "first "
    "| rfl "
    "| simp "
    "| decide "
    "| norm_num "
    "| omega "
    "| linarith "
    "| nlinarith "
    "| simp_all "
    "| ring "
    "| aesop "
    "| tauto"
)
# Sequential combinators are kept separate so they don't break `first | ...` parsing.
_SEQ_CLOSERS = [
    "simp [Finset.sum_range_succ]; ring",
    "rw [Finset.sum_range_succ]; ring",
    "simp_all; omega",
    "simp_all; ring",
    "field_simp; ring",
    "ring_nf; omega",
]


def _augment_with_case_closers(body: str, cases: list[str], closer: str) -> str:
    """Append explicit `case <name> => <closer>` lines after the body.

    Each case independently picks a closer from the battery via `first | ...`,
    so e.g. `case zero` can pick `simp` while `case succ` picks `(simp [...]; ring)`.
    """
    case_lines = "\n".join(f"case {nm} => ({closer})" for nm in cases)
    return body.rstrip() + "\n" + case_lines


# Tail closers — appended to the failing proof body verbatim, for failures
# that don't have a `case <name>` structure (BFS made progress but didn't
# close; goal state is simpler than before).
_TAIL_CLOSERS = [
    # Cheap all-goals batteries
    "all_goals (first | rfl | simp | decide | norm_num | omega | linarith | nlinarith | ring | aesop | tauto)",
    "all_goals (try simp_all); all_goals (try omega); all_goals (try linarith); all_goals (try nlinarith); all_goals (try ring); all_goals (try aesop)",
    # Single-step closers
    "nlinarith",
    "linarith",
    "polyrith",
    "positivity",
    "ring",
    "field_simp; ring",
    # Two-step combinations
    "simp_all; linarith",
    "simp_all; nlinarith",
    "simp_all; ring",
    "simp_all; omega",
    "simp_all; tauto",
    "simp_all; polyrith",
    "norm_num; nlinarith",
    "norm_num; linarith",
    "norm_num; ring_nf; linarith",
    "norm_num; ring_nf; nlinarith",
    "ring_nf; nlinarith",
    "ring_nf; linarith",
    "ring_nf; omega",
    "field_simp at *; ring",
    "field_simp at *; nlinarith",
    "field_simp at *; linarith",
    # Heavy-but-sometimes-effective
    "aesop",
    "omega",
    "decide",
    # Aesop with extensions
    "aesop (add safe linarith) (add safe nlinarith) (add safe ring) (add safe omega)",
    # Goal-state setup + close (handle real-valued olympiad problems)
    "nlinarith [sq_nonneg _, sq_nonneg (1 : ℝ)]",
    "nlinarith [sq_nonneg (1 : ℝ), mul_self_nonneg _]",
    # Combinations that handle Σ over Finset
    "simp [Finset.sum_range_succ]; ring",
    "simp [Finset.prod_range_succ]; ring",
    # interval_cases for bounded Nat goals
    "interval_cases n <;> simp_all",
    "interval_cases n <;> decide",
]


def try_close_unsolved(
    lean,                                  # LeanRunner
    problem: LeanProblem,
    failing_body: str,
    stderr: str,
    deadline: float | None = None,
) -> list["ProofAttempt"]:
    """Try mechanically closing `failing_body`. Two strategies:

    1. If the Lean error names unsolved `case <name>` stanzas, append explicit
       `case <name> => <battery>` patches and re-check.
    2. Otherwise (BFS made progress but the final goals aren't case-shaped),
       append a `<battery>` directly to the proof body and re-check.

    Returns every ProofAttempt made so they all appear in the per-attempt log.
    """
    from .orchestrator import ProofAttempt

    out: list[ProofAttempt] = []
    cases = _parse_unsolved_cases(stderr)

    # Strategy 2: no case structure — try tail closers directly.
    if not cases:
        if "unsolved goals" not in stderr.lower():
            return out
        for closer in _TAIL_CLOSERS:
            if deadline is not None and time.time() > deadline:
                break
            new_body = failing_body.rstrip() + "\n" + closer
            snippet = wrap(problem, new_body)
            t0 = time.time()
            res = lean.check(snippet)
            attempt = ProofAttempt(
                proof=new_body,
                ok=res.ok,
                elapsed_s=time.time() - t0,
                error="" if res.ok else res.stderr[:600],
                source=f"tail_closer:{closer[:30]}",
            )
            out.append(attempt)
            if res.ok:
                return out
        return out

    # First shot: per-case `first | ...` battery of single-step closers.
    # Each case independently picks the cheapest closer that works on it.
    new_body = _augment_with_case_closers(failing_body, cases, _BATTERY)
    snippet = wrap(problem, new_body)
    t0 = time.time()
    res = lean.check(snippet)
    attempt = ProofAttempt(
        proof=new_body,
        ok=res.ok,
        elapsed_s=time.time() - t0,
        error="" if res.ok else res.stderr[:600],
        source="case_closer:battery",
    )
    out.append(attempt)
    if res.ok:
        return out

    # Second shot: try each sequential combinator (e.g. `simp [Finset...]; ring`)
    # uniformly across all cases. These don't compose well with `first`, so
    # we run each as its own all-cases attempt.
    for seq in _SEQ_CLOSERS:
        if deadline is not None and time.time() > deadline:
            break
        new_body = _augment_with_case_closers(failing_body, cases, seq)
        snippet = wrap(problem, new_body)
        t0 = time.time()
        res = lean.check(snippet)
        attempt = ProofAttempt(
            proof=new_body,
            ok=res.ok,
            elapsed_s=time.time() - t0,
            error="" if res.ok else res.stderr[:600],
            source=f"case_closer:seq:{seq[:24]}",
        )
        out.append(attempt)
        if res.ok:
            return out

    # Final shot: try each cheap single-step closer uniformly (preserves prior
    # behaviour — closes problems where `first | ...` mis-elaborates).
    for closer in _CLOSERS:
        if deadline is not None and time.time() > deadline:
            break
        new_body = _augment_with_case_closers(failing_body, cases, closer)
        snippet = wrap(problem, new_body)
        t0 = time.time()
        res = lean.check(snippet)
        attempt = ProofAttempt(
            proof=new_body,
            ok=res.ok,
            elapsed_s=time.time() - t0,
            error="" if res.ok else res.stderr[:600],
            source=f"case_closer:{closer[:30]}",
        )
        out.append(attempt)
        if res.ok:
            return out
    return out
