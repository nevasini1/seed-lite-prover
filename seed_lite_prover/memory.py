"""Verified-lemma cache: append-only JSONL."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.']*")


@dataclass
class VerifiedLemma:
    statement: str
    proof: str
    symbols: list[str] = field(default_factory=list)
    tactics: list[str] = field(default_factory=list)
    source: str = "local_search_verified"
    timestamp: float = field(default_factory=time.time)


def extract_symbols(statement: str) -> list[str]:
    return sorted(set(_IDENT.findall(statement)))


def extract_tactics(proof: str) -> list[str]:
    return [t for t in re.findall(r"\b([a-z_][a-z0-9_]*)\b", proof) if len(t) > 1]


class LemmaCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, lemma: VerifiedLemma) -> None:
        if not lemma.symbols:
            lemma.symbols = extract_symbols(lemma.statement)
        if not lemma.tactics:
            lemma.tactics = extract_tactics(lemma.proof)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(lemma)) + "\n")

    def load(self) -> list[VerifiedLemma]:
        out: list[VerifiedLemma] = []
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                out.append(VerifiedLemma(**json.loads(line)))
        return out
