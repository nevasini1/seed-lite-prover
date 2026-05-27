"""Okapi BM25 retrieval over Mathlib + the verified-lemma cache.

Previous version used naive symbol-set overlap + IDF (no term frequency,
no document-length normalisation — which is NOT BM25). This module now
implements proper Okapi BM25 with the standard parameters (k1=1.5, b=0.75)
and uses the structured `tactic_state` symbol set as the query when called
from inside BFS search.

Index source: `benchmarks/mathlib_index.jsonl` produced by
`scripts/build_mathlib_index.py`. Each line: {"name": str, "statement": str}.
Path is repo-root anchored so retrieval works from any CWD.
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


# BM25 hyperparameters (standard Okapi defaults; tuned later if needed)
_BM25_K1 = 1.5
_BM25_B = 0.75


@dataclass
class IndexedFact:
    name: str
    statement: str
    tokens: list[str]          # bag of tokens — preserves multiplicity for TF
    tf: dict[str, int]         # term -> count in this doc
    length: int                # |doc| in tokens


_MATHLIB: list[IndexedFact] | None = None
_IDF: dict[str, float] | None = None
_AVGDL: float = 0.0
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_INDEX = _REPO_ROOT / "benchmarks" / "mathlib_index.jsonl"


def _tokenise(text: str) -> list[str]:
    """Token stream from a Mathlib statement: every identifier-shaped match,
    with multiplicity preserved (so `f x + f y` yields `[f, x, f, y]`)."""
    return extract_symbols_multi(text)


def extract_symbols_multi(text: str) -> list[str]:
    """Same identifier alphabet as `memory.extract_symbols` but preserves
    multiplicity (BM25 needs term frequency). Inlined to avoid changing the
    memory module's set-based contract."""
    import re
    # Same alphabet as tactic_state._IDENT — Unicode-aware
    _IDENT = re.compile(r"[A-Za-z_ℕℤℝℚℂΑ-ωℐ-∀][\w'.✝₀-₉ℐ-∀ℕℤℝℚℂΑ-ω]*", re.UNICODE)
    return _IDENT.findall(text)


def _load_mathlib(path: Path = _DEFAULT_INDEX) -> list[IndexedFact]:
    """Load and tokenise the Mathlib index; compute IDF and average doc
    length for BM25. Cached module-level after first call."""
    global _MATHLIB, _IDF, _AVGDL
    if _MATHLIB is not None:
        return _MATHLIB
    facts: list[IndexedFact] = []
    if not path.exists():
        print(
            f"[retrieval] WARNING: index not found at {path}. "
            f"Variant D will fall back to cache-only retrieval. "
            f"Run `python scripts/build_mathlib_index.py` to regenerate.",
            flush=True,
        )
        _MATHLIB = []
        _IDF = {}
        _AVGDL = 0.0
        return _MATHLIB

    total_len = 0
    df: Counter[str] = Counter()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            tokens = _tokenise(obj["statement"])
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            for t in tf:
                df[t] += 1
            total_len += len(tokens)
            facts.append(IndexedFact(
                name=obj["name"],
                statement=obj["statement"],
                tokens=tokens,
                tf=tf,
                length=len(tokens),
            ))
    n = max(1, len(facts))
    _MATHLIB = facts
    # BM25 IDF: log((N - n_t + 0.5) / (n_t + 0.5) + 1)
    # The `+ 1` outside makes it always ≥ 0, avoiding negative scores
    # for terms in ≥ half the docs (the classic BM25 idf can go negative).
    _IDF = {
        t: math.log((n - c + 0.5) / (c + 0.5) + 1.0)
        for t, c in df.items()
    }
    _AVGDL = total_len / n if n > 0 else 1.0

    print(
        f"[retrieval] loaded {n} Mathlib decls from {path} "
        f"(avg_doc_len={_AVGDL:.1f}, vocab={len(_IDF)})",
        flush=True,
    )
    return facts


def _bm25_score(fact: IndexedFact, query_terms: list[str]) -> float:
    """Okapi BM25 score for `fact` given a tokenised query."""
    if _IDF is None or not query_terms:
        return 0.0
    score = 0.0
    norm_factor = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (fact.length / max(_AVGDL, 1.0)))
    for t in query_terms:
        idf = _IDF.get(t, 0.0)
        if idf == 0.0:
            continue
        tf = fact.tf.get(t, 0)
        if tf == 0:
            continue
        score += idf * (tf * (_BM25_K1 + 1.0)) / (tf + norm_factor)
    return score


def retrieve_for_goal(orc: "Orchestrator", statement: str, k: int = 20) -> str:
    """Top-k Mathlib + cache lemmas, ranked by BM25 against the goal text.

    `statement` is the original theorem statement. When called from BFS
    inside an open proof, the caller can pass the current goal text instead
    — BM25 handles both equally (it's just a query).
    """
    facts = _load_mathlib()
    query = _tokenise(statement)
    if not query:
        return ""

    scored: list[tuple[float, str, str, str]] = []
    for fact in facts:
        s = _bm25_score(fact, query)
        if s > 0:
            scored.append((s, "mathlib", fact.name, fact.statement))

    # Cache results are added with their own BM25 score so they compete on
    # equal footing with Mathlib (no fixed-weight inflation).
    cache_facts: list[IndexedFact] = []
    for lemma in orc.cache.load():
        tokens = _tokenise(lemma.statement)
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        cache_facts.append(IndexedFact(
            name="cached_lemma",
            statement=lemma.statement,
            tokens=tokens,
            tf=tf,
            length=len(tokens) or 1,
        ))
    for fact in cache_facts:
        s = _bm25_score(fact, query)
        if s > 0:
            scored.append((s, "cache", fact.name, fact.statement))

    scored.sort(key=lambda x: x[0], reverse=True)
    lines = [
        f"{i+1}. [{src}] {name} : {stmt}"
        for i, (_, src, name, stmt) in enumerate(scored[:k])
    ]
    return "\n".join(lines)


def retrieve_for_state(orc: "Orchestrator", state_symbols: set[str], k: int = 20) -> str:
    """BFS-search hook: retrieve against the structured tactic-state symbol
    set instead of the static theorem statement. This is what makes
    retrieval respond to the EVOLVING proof state rather than the unchanging
    top-line."""
    query = list(state_symbols)
    facts = _load_mathlib()
    if not facts or not query:
        return ""
    scored: list[tuple[float, str, str, str]] = []
    for fact in facts:
        s = _bm25_score(fact, query)
        if s > 0:
            scored.append((s, "mathlib", fact.name, fact.statement))
    scored.sort(key=lambda x: x[0], reverse=True)
    return "\n".join(
        f"{i+1}. [{src}] {name} : {stmt}"
        for i, (_, src, name, stmt) in enumerate(scored[:k])
    )
