"""Lightweight retrieval over Mathlib + the verified-lemma cache.

We avoid heavy embeddings. Strategy:
- Symbols are pulled from the goal statement (`extract_symbols`).
- Score each indexed statement by the size of the symbol-set overlap, with
  a small idf weighting computed on first index load.
- Top-k statements (names + signatures) are formatted into a short list and
  inlined into the BFS prompt.

The Mathlib index source is `mathlib_index.jsonl`, expected at
`benchmarks/mathlib_index.jsonl`. Each line: {"name": str, "statement": str}.
You can produce it from a Mathlib build with a small lake script (deferred);
for now the index file may simply not exist, in which case retrieval falls
back to the cache only.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .memory import extract_symbols

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


@dataclass
class IndexedFact:
    name: str
    statement: str
    symbols: frozenset[str]


_MATHLIB: list[IndexedFact] | None = None
_IDF: dict[str, float] | None = None
_DEFAULT_INDEX = Path("benchmarks/mathlib_index.jsonl")


def _load_mathlib(path: Path = _DEFAULT_INDEX) -> list[IndexedFact]:
    global _MATHLIB, _IDF
    if _MATHLIB is not None:
        return _MATHLIB
    facts: list[IndexedFact] = []
    if path.exists():
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                facts.append(
                    IndexedFact(
                        name=obj["name"],
                        statement=obj["statement"],
                        symbols=frozenset(extract_symbols(obj["statement"])),
                    )
                )
    _MATHLIB = facts

    df: Counter[str] = Counter()
    for f_ in facts:
        for s in f_.symbols:
            df[s] += 1
    n = max(1, len(facts))
    _IDF = {s: math.log((n + 1) / (c + 1)) + 1.0 for s, c in df.items()}
    return facts


def _score(fact: IndexedFact, goal_symbols: frozenset[str]) -> float:
    overlap = fact.symbols & goal_symbols
    if not overlap:
        return 0.0
    idf = _IDF or {}
    return sum(idf.get(s, 1.0) for s in overlap)


def retrieve_for_goal(orc: "Orchestrator", statement: str, k: int = 20) -> str:
    facts = _load_mathlib()
    goal_symbols = frozenset(extract_symbols(statement))

    scored: list[tuple[float, str, str, str]] = []
    for f_ in facts:
        s = _score(f_, goal_symbols)
        if s > 0:
            scored.append((s, "mathlib", f_.name, f_.statement))

    for lemma in orc.cache.load():
        lemma_syms = frozenset(extract_symbols(lemma.statement))
        overlap = lemma_syms & goal_symbols
        if overlap:
            scored.append((float(len(overlap)) + 0.5, "cache", "cached_lemma", lemma.statement))

    scored.sort(key=lambda x: x[0], reverse=True)
    lines = [f"{i+1}. [{src}] {name} : {stmt}" for i, (_, src, name, stmt) in enumerate(scored[:k])]
    return "\n".join(lines)
