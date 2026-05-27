#!/usr/bin/env python3
"""Walk Mathlib source and emit a retrieval-ready JSONL.

We index `theorem` / `lemma` declarations from .lake/packages/mathlib/Mathlib/
**/*.lean. We do NOT need a full Lean parser — Mathlib's source is regular
enough that a careful regex captures the name and type signature for the
overwhelming majority of declarations. That's enough for symbol-overlap
retrieval used by `seed_lite_prover/retrieval.py`.

Each output JSONL row:
    {
      "name": "Nat.add_comm",
      "kind": "theorem",
      "statement": "∀ (n m : ℕ), n + m = m + n",
      "file": "Mathlib/Data/Nat/Defs.lean",
      "ns": ["Nat"]
    }

Run from repo root:
    python scripts/build_mathlib_index.py

Writes to benchmarks/mathlib_index.jsonl.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATHLIB_SRC = ROOT / "lean_project" / ".lake" / "packages" / "mathlib" / "Mathlib"
OUTPUT = ROOT / "benchmarks" / "mathlib_index.jsonl"


# Declaration line. Mathlib declarations span multiple lines; we match the
# leading line `theorem|lemma <name> ... ` and then collect the type up to
# `:=`. `theorem` is the bulk of useful retrieval targets.
#
# Restrictions:
# - At line start (allow leading `@[...]` attributes and `protected`/`private`
#   modifiers — handled by stripping leading attrs first).
# - Name is dot-separated identifier or a single identifier.
DECL_START = re.compile(
    r"^(?P<lead>\s*)(?P<kind>theorem|lemma)\s+(?P<name>[A-Za-z_][\w.']*)",
)

# Allow these soft modifiers immediately before the kind keyword.
MOD_PREFIX = re.compile(r"^(?:@\[[^\]]*\]\s*|protected\s+|private\s+|noncomputable\s+|nonrec\s+)+", re.MULTILINE)

# Open namespace tracking — note we don't try to be perfect with multi-name
# `namespace A.B` or `end <name>` matching of nested scopes; we just track a
# stack to suffix names. Mathlib is mostly well-behaved.
NAMESPACE_OPEN = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.']*)\s*$")
NAMESPACE_CLOSE = re.compile(r"^\s*end\s+([A-Za-z_][\w.']*)\s*$")


def _strip_block_comments(text: str) -> str:
    # Lean uses /- ... -/ block comments. Drop them so we don't try to
    # parse declarations that live inside docstrings.
    out: list[str] = []
    i = 0
    depth = 0
    while i < len(text):
        if text[i:i+2] == "/-" and depth == 0:
            depth = 1
            i += 2
            continue
        if depth > 0:
            if text[i:i+2] == "-/":
                depth -= 1
                i += 2
                continue
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _extract_signature(lines: list[str], start_idx: int) -> tuple[str, int]:
    """From the line index where `theorem <name>` starts, gather the type
    signature until we see `:=` or `where` or end of declaration block."""
    parts: list[str] = []
    i = start_idx
    while i < len(lines):
        ln = lines[i]
        # Strip trailing comments.
        ln = re.sub(r"--.*$", "", ln)
        parts.append(ln)
        if ":=" in ln or " where" in ln or ln.rstrip().endswith("where"):
            break
        i += 1
        # Hard stop after 50 lines so we don't suck up the whole file
        # if the regex confused itself.
        if i - start_idx > 50:
            break
    joined = " ".join(p.strip() for p in parts)
    # Cut at `:=`. Keep `where` clause stripped.
    if ":=" in joined:
        joined = joined.split(":=", 1)[0]
    elif "where" in joined:
        joined = joined.split("where", 1)[0]
    return joined.rstrip(), i


def _parse_file(path: Path) -> list[dict]:
    raw = path.read_text(errors="replace")
    text = _strip_block_comments(raw)
    lines = text.splitlines()

    out: list[dict] = []
    ns_stack: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        m_open = NAMESPACE_OPEN.match(ln)
        if m_open:
            ns_stack.append(m_open.group(1))
            i += 1
            continue
        m_close = NAMESPACE_CLOSE.match(ln)
        if m_close:
            if ns_stack and ns_stack[-1] == m_close.group(1):
                ns_stack.pop()
            i += 1
            continue

        # Drop leading modifiers before checking for a declaration.
        stripped = MOD_PREFIX.sub("", ln)
        m = DECL_START.match(stripped)
        if not m:
            i += 1
            continue

        # We have a `theorem`/`lemma`. Walk forward to gather the signature.
        # Use `stripped` as the first line to skip leading attributes.
        gather: list[str] = [stripped]
        j = i + 1
        # Continuation lines are anything not starting at column 0 with a
        # new declaration keyword.
        while j < len(lines):
            cont = lines[j]
            if ":=" in cont or cont.lstrip().startswith("where"):
                gather.append(re.sub(r"--.*$", "", cont))
                break
            # Hard stop on next `theorem`/`lemma`/`def`/`instance` etc.
            if re.match(r"^(\s*)(theorem|lemma|def|instance|abbrev|structure|inductive|class|namespace|end)\b", cont):
                break
            gather.append(re.sub(r"--.*$", "", cont))
            j += 1
            if j - i > 50:
                break
        joined = " ".join(p.strip() for p in gather)
        if ":=" in joined:
            sig = joined.split(":=", 1)[0]
        elif "where" in joined:
            sig = joined.split("where", 1)[0]
        else:
            sig = joined

        # Strip the leading `theorem foo` (we want the binders+":"+type only)
        sig = re.sub(r"^(theorem|lemma)\s+[A-Za-z_][\w.']*\s*", "", sig).strip()
        if not sig:
            i = j + 1
            continue

        full_name = ".".join(ns_stack + [m.group("name")]) if ns_stack else m.group("name")
        rel = str(path.relative_to(MATHLIB_SRC.parent))
        out.append({
            "name": full_name,
            "kind": m.group("kind"),
            "statement": sig[:1000],   # cap absurdly long signatures
            "file": rel,
            "ns": list(ns_stack),
        })

        i = j + 1
    return out


def main() -> int:
    if not MATHLIB_SRC.exists():
        print(f"Mathlib source not found at {MATHLIB_SRC}", file=sys.stderr)
        return 1

    files = sorted(MATHLIB_SRC.rglob("*.lean"))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    t0 = time.time()
    with OUTPUT.open("w") as f:
        for k, p in enumerate(files):
            try:
                rows = _parse_file(p)
            except Exception as e:
                print(f"  WARN {p.name}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            for r in rows:
                f.write(json.dumps(r) + "\n")
            total += len(rows)
            if (k + 1) % 500 == 0:
                print(f"  {k+1}/{len(files)} files, {total} declarations indexed, {time.time()-t0:.1f}s elapsed", flush=True)
    print(f"wrote {total} declarations to {OUTPUT} in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
