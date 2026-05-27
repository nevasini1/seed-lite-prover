"""Helpers to assemble Lean snippets we feed to `LeanRunner.check`.

Each MiniF2F problem file is self-contained:

    import Mathlib
    set_option maxHeartbeats 0
    open BigOperators Real Nat Topology Rat

    theorem <name> <statement> := by sorry

We treat the lines above the theorem as the **header** (carries all the
imports + opens needed to make `<statement>` typecheck) and reuse them
verbatim when building candidate proofs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lean_runner import LeanRunner


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_]")
# Match the start of the theorem block in a MiniF2F file. We allow leading
# whitespace + capture (kw, name, body) where body is everything from the
# binders/return type up to the first `:= by` (the proof body follows).
_THEOREM_BLOCK = re.compile(
    r"^(?P<indent> *)(?P<kw>theorem|lemma|example)\s+(?P<name>\S+)\s+(?P<body>.+?):=\s*by\s+sorry\s*$",
    re.DOTALL | re.MULTILINE,
)


@dataclass
class LeanProblem:
    """A MiniF2F-style problem parsed into header + theorem fields."""

    path: Path
    header: str          # everything before the theorem (imports, set_option, opens)
    keyword: str         # "theorem" / "lemma"
    name: str            # original theorem name
    statement: str       # binders + ":" + return type, trimmed; no `:= by ...`

    @property
    def header_with_blank(self) -> str:
        h = self.header.rstrip()
        return h + "\n\n" if h else ""


def safe_name(raw: str) -> str:
    s = _SAFE_NAME.sub("_", raw.strip())
    if not s or not (s[0].isalpha() or s[0] == "_"):
        s = "thm_" + s
    return s[:60]


def indent(text: str, n: int = 2) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def parse_file(path: Path) -> LeanProblem | None:
    """Parse a MiniF2F-style problem file. Returns None if the file's theorem
    block doesn't match the expected `... := by sorry` shape."""
    text = path.read_text()
    m = _THEOREM_BLOCK.search(text)
    if not m:
        return None
    statement = m.group("body").strip()
    # Drop a trailing `:` artifact-free `:` after the return type (rare, but possible).
    statement = statement.rstrip()
    return LeanProblem(
        path=path,
        header=text[: m.start()],
        keyword=m.group("kw"),
        name=m.group("name"),
        statement=statement,
    )


def wrap(problem: LeanProblem, body: str, fresh_name: str | None = None) -> str:
    """Build a complete Lean snippet for `LeanRunner.check`.

    Uses the problem's own header (imports/opens), reuses the original
    theorem name by default, and inserts `body` as the proof.
    """
    name = safe_name(fresh_name) if fresh_name else problem.name
    body = (body or "sorry").rstrip()
    return (
        f"{problem.header_with_blank}"
        f"{problem.keyword} {name} {problem.statement} := by\n"
        f"{indent(body, 2)}\n"
    )


def wrap_stmt(header: str, name: str, statement: str, body: str, kw: str = "theorem") -> str:
    """Variant of `wrap` for ad-hoc sub-lemmas (decomposition)."""
    body = (body or "sorry").rstrip()
    h = header.rstrip()
    head = (h + "\n\n") if h else ""
    return f"{head}{kw} {safe_name(name)} {statement.rstrip()} := by\n{indent(body, 2)}\n"


def parses_as_type(runner: "LeanRunner", header: str, statement: str) -> bool:
    """Does `<header>\\n example <statement> := by sorry` parse?

    `sorry` triggers a warning (not an error), so Lean accepts it. If the
    statement is fundamentally malformed, we see an `error:` in stderr.
    """
    stmt = statement.strip()
    if not stmt or ":=" in stmt or "\n" in stmt:
        return False
    h = header.rstrip()
    head = (h + "\n\n") if h else ""
    snippet = f"{head}example {stmt} := by\n  sorry\n"
    res = runner.check(snippet)
    return res.ok


def parses_in_parent(runner: "LeanRunner", problem: "LeanProblem", have_name: str, have_type: str) -> bool:
    """Stricter check: does `have <name> : <type> := by sorry` typecheck
    INSIDE the parent theorem body?

    This catches issues `parses_as_type` misses — namespace-context mismatches,
    implicit-binder shadowing, and other things that depend on being inside
    the goal context, not at top-level.
    """
    typ = have_type.strip()
    if not typ or ":=" in typ or "\n" in typ:
        return False
    body = f"have {have_name} : {typ} := by sorry\n  sorry"
    snippet = wrap(problem, body)
    res = runner.check(snippet)
    return res.ok
