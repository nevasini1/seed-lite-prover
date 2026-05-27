"""Lean-checked proof-search tree, ported from `ByteDance-Seed/BFS-Prover-V2`
(`src/search/proof_tree.py`, Apache-2.0) and stripped of the LeanDojo
dependency.

We do not have a true tactic-state surface (LeanDojo's `TacticState`),
because our verifier is the line-oriented Lean REPL — we only know
whether a full proof prefix compiles. So each `InternalNode` here
represents a **proof prefix** (tuple of tactics applied so far) rather
than a tactic state. Two nodes are equal iff their prefixes are equal.

Priority: nodes ordered by descending `cumulative_logprob`. We don't get
real logprobs from Ollama, so we synthesise a proxy at edge-construction
time (see `tactic_search.py` — typically `-temperature_index`).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering
from typing import Iterable, Optional


class Status(Enum):
    PROVED = "Proved"
    FAILED = "Failed"
    OPEN = "Open"


@dataclass
class TerminalNode:
    """Leaf of the tree: either a successful proof or a syntactic dead end."""
    status: Status
    depth: int
    error: str = ""
    is_terminal: bool = True
    distance_to_proof: float = 0.0  # overridden for FAILED below

    def __post_init__(self) -> None:
        if self.status == Status.FAILED:
            self.distance_to_proof = math.inf


@total_ordering
@dataclass(unsafe_hash=True)
class InternalNode:
    """Non-terminal node, identified by its proof prefix."""
    prefix: tuple[str, ...] = field(compare=True)
    depth: int = field(default=0, compare=False)
    cumulative_logprob: float = field(default=0.0, compare=False, repr=False)
    in_edges: list["Edge"] = field(default_factory=list, init=False, compare=False, repr=False)
    _out_edges: Optional[list["Edge"]] = field(default=None, init=False, compare=False, repr=False)
    _status: Status = field(default=Status.OPEN, init=False, compare=False)
    _distance_to_proof: float = field(default=math.inf, init=False, compare=False, repr=False)
    is_terminal: bool = field(default=False, init=False)

    @property
    def out_edges(self) -> Optional[list["Edge"]]:
        return self._out_edges

    @out_edges.setter
    def out_edges(self, edges: Iterable["Edge"]) -> None:
        if self._out_edges is not None:
            raise RuntimeError("node already explored")
        self._out_edges = list(edges)
        self._recompute_status()
        self._recompute_distance()

    @property
    def is_explored(self) -> bool:
        return self._out_edges is not None

    @property
    def status(self) -> Status:
        return self._status

    @property
    def distance_to_proof(self) -> float:
        return self._distance_to_proof

    def _recompute_status(self) -> None:
        assert self._out_edges is not None
        if self._status != Status.OPEN:
            return
        if any(e.dst.status == Status.PROVED for e in self._out_edges):
            self._status = Status.PROVED
        elif self._out_edges and all(e.dst.status == Status.FAILED for e in self._out_edges):
            self._status = Status.FAILED
        if self._status != Status.OPEN:
            for parent_edge in self.in_edges:
                parent_edge.src._recompute_status()

    def _recompute_distance(self) -> None:
        d = math.inf
        if self._out_edges:
            d = min(e.distance_to_proof() for e in self._out_edges)
        if d < self._distance_to_proof:
            self._distance_to_proof = d
            for parent_edge in self.in_edges:
                parent_edge.src._recompute_distance()

    # heapq is a *min*-heap; we want highest cumulative_logprob first,
    # so node_a < node_b iff a.cumulative_logprob > b.cumulative_logprob.
    def __lt__(self, other: "InternalNode") -> bool:
        return self.cumulative_logprob > other.cumulative_logprob

    def extract_proof(self) -> Optional[list[str]]:
        """Return the winning tactic sequence (the prefix at the proved leaf)."""
        if self._status != Status.PROVED or self._out_edges is None:
            return None
        # Find the child that reaches the PROVED leaf the cheapest.
        winning_edge = min(
            (e for e in self._out_edges if e.dst.status == Status.PROVED),
            key=lambda e: e.distance_to_proof(),
            default=None,
        )
        if winning_edge is None:
            return None
        if winning_edge.dst.is_terminal:
            return list(self.prefix) + [winning_edge.tactic]
        assert isinstance(winning_edge.dst, InternalNode)
        child = winning_edge.dst.extract_proof()
        return child if child is not None else None


@dataclass
class Edge:
    tactic: str
    src: InternalNode = field(repr=False)
    dst: object = field(repr=False)  # InternalNode | TerminalNode
    elapsed_s: float = 0.0
    logprob: float = 0.0

    def distance_to_proof(self) -> float:
        return 1.0 + self.dst.distance_to_proof  # type: ignore[attr-defined]


class ProofTree:
    """Best-first search over proof prefixes.

    Owns the priority queue and the dedup index. Callers push tactics onto
    nodes via `attach_child`; the heap and status propagation are bookkeeping.
    """

    def __init__(self) -> None:
        self.root = InternalNode(prefix=())
        self.heap: list[InternalNode] = [self.root]
        self.seen: dict[tuple[str, ...], InternalNode] = {(): self.root}

    def pop_best(self) -> Optional[InternalNode]:
        while self.heap:
            node = heapq.heappop(self.heap)
            if node.is_explored or node.status != Status.OPEN:
                continue
            return node
        return None

    def attach_child(
        self,
        parent: InternalNode,
        tactic: str,
        edge_logprob: float,
        elapsed_s: float,
        ok: bool,
        error: str = "",
        syntax_dead: bool = False,
    ) -> object:
        """Create the edge + child node, push the (still-open) child onto the
        frontier, and let status / distance propagate. Returns the new node
        (TerminalNode if ok or syntax_dead, otherwise InternalNode)."""
        new_prefix = parent.prefix + (tactic,)
        cumlog = parent.cumulative_logprob + edge_logprob
        depth = parent.depth + 1

        if ok:
            child: object = TerminalNode(status=Status.PROVED, depth=depth)
        elif syntax_dead:
            child = TerminalNode(status=Status.FAILED, depth=depth, error=error)
        else:
            # Reuse an existing InternalNode with the same prefix, if any.
            existing = self.seen.get(new_prefix)
            if existing is not None:
                child = existing
            else:
                child = InternalNode(prefix=new_prefix, depth=depth, cumulative_logprob=cumlog)
                self.seen[new_prefix] = child
                heapq.heappush(self.heap, child)

        edge = Edge(tactic=tactic, src=parent, dst=child, elapsed_s=elapsed_s, logprob=edge_logprob)
        # InternalNode tracks in_edges for status / distance propagation.
        if isinstance(child, InternalNode):
            child.in_edges.append(edge)
        return child

    def finalize(self, parent: InternalNode, edges: list[Edge]) -> None:
        """Mark `parent` as explored once all candidate children have been
        attached (so status / distance recomputes only run once)."""
        parent.out_edges = edges
