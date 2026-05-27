# Copyright 2025 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import time

from enum import Enum
from loguru import logger
from lean_dojo import (
    Dojo,
    TacticState,
    LeanError,
    ProofGivenUp,
    ProofFinished,
    Theorem,
)
from abc import ABC, abstractmethod
from functools import total_ordering
from dataclasses import dataclass, field
from typing import Optional, List, Iterable, Union, Tuple


class Status(Enum):
    """Status of a node or a proof search."""

    PROVED = "Proved"  # This node (or search) has at least one known proof.
    FAILED = "Failed"  # This node (or search) has exhausted its options and cannot be proved within the current run.
    OPEN = "Open"  # This node (or search) has not been proven or given up on yet.


class Node(ABC):
    @property
    @abstractmethod
    def status(self) -> Status:
        raise NotImplementedError

    @property
    @abstractmethod
    def distance_to_proof(self) -> int:
        "The smallest number of steps to a proof."
        raise NotImplementedError

    @property
    @abstractmethod
    def is_terminal(self) -> bool:
        raise NotImplementedError


@dataclass
class ProofFinishedNode(Node):
    inner: Union[ProofFinished, ProofGivenUp]
    status = Status.PROVED
    distance_to_proof = 0
    is_terminal = True
    depth: int


@dataclass
class ErrorNode(Node):
    inner: LeanError
    status = Status.FAILED
    distance_to_proof = math.inf
    is_terminal = True
    depth: int


@total_ordering
@dataclass(unsafe_hash=True)
class InternalNode(Node):
    """
    An internal node in the search tree, representing a nonterminal state.

    Nodes are sorted by _inverse_ priority, for compatibility with the `heapq` library.
    That is, node_a < node_b is true if node_a has _higher_ priority than node_b.
    """

    # Goal state this node represents. Two nodes are considered equal if their states
    # are equal; this is the only hashed field and must not be changed.
    state: TacticState = field(compare=True)

    # Depth of the node in the search tree
    depth: int

    # The sum of action logprobs along edges from the root to this node
    cumulative_logprob: float = field(compare=False, repr=False)

    # All edges known to lead to this node.
    # May change at any time as other nodes are explored.
    in_edges: List["Edge"] = field(
        default_factory=list, init=False, compare=False, repr=False
    )

    # All edges out of this node that we've considered, or None for unexplored nodes.
    # When a node is explored, this list is populated, and must not change after that.
    _out_edges: Optional[List["Edge"]] = field(
        default=None, init=False, compare=False, repr=False
    )

    # A node is proved if any child is proved, and failed if every child is failed
    # (or there are no children). A node that is proved or failed cannot change status
    # because nothing is ever added to out_edges. _status is recomputed on an as-needed
    # basis by children, since proving or failing a child may prove or fail this node.
    _status: Status = field(default=Status.OPEN, init=False, compare=False, repr=True)

    is_terminal = False  # type: ignore[override]

    # Number of steps separating this node from the end of a proof along the
    # optimal path. If unproved, infinity. Updated as needed by children.
    _distance_to_proof: float = field(
        default=math.inf, init=False, compare=False, repr=False
    )

    @property
    def out_edges(self):
        return self._out_edges

    # This setter implements exploring this node
    @out_edges.setter
    def out_edges(self, out_edges: Iterable["Edge"]) -> Optional[List["Edge"]]:
        if self.is_explored:
            raise RuntimeError("Node is already explored.")

        self._out_edges = list(out_edges)
        self._recompute_status()
        self._recompute_distance_to_proof()

    # A node is considered explored if we've evaluated the actor in the node to generate
    # a list of candidate children. Explored nodes are never re-searched.
    @property
    def is_explored(self) -> bool:
        return self.out_edges is not None

    @property
    def status(self) -> Status:
        return self._status

    @status.setter
    def status(self, s):
        self._status = s

    def _recompute_status(self):
        """
        Recursively update the status of the current node and its ancestors.
        """
        assert self.is_explored and self.out_edges is not None

        # If this node is proved or failed, nothing can change that
        if self._status != Status.OPEN:
            return

        # If any child is proved, this node is proved, and so are parents recursively
        if any(edge.dst.status == Status.PROVED for edge in self.out_edges):
            self._status = Status.PROVED

        # If all children failed, this node is failed. This may fail some parents too.
        if all(edge.dst.status == Status.FAILED for edge in self.out_edges):
            self._status = Status.FAILED

        # If this node was proved or failed, parents may need to recompute.
        # This is guaranteed to terminate because only open nodes can change, and
        # there are a finite number of open nodes in the tree.
        if self._status != Status.OPEN:
            for edge in self.in_edges:
                edge.src._recompute_status()

    @property
    def distance_to_proof(self) -> float:
        return self._distance_to_proof

    def _recompute_distance_to_proof(self):
        """
        Recursively update the distance_to_proof of the current node and its ancestors.
        """
        if self.out_edges:
            distance = min(edge.distance_to_proof() for edge in self.out_edges)
        else:
            distance = math.inf

        if distance < self._distance_to_proof:
            self._distance_to_proof = distance
            for edge in self.in_edges:
                edge.src._recompute_distance_to_proof()

    # NOTE: Nodes are compared by _negative_ priority, to make heapq act as a max-priority-queue.
    @property
    def priority(self) -> float:
        return self.cumulative_logprob

    def __lt__(self, other: "InternalNode") -> bool:
        return self.priority > other.priority

    def extract_proof(self) -> Optional[List["Edge"]]:
        """
        Extract a proof of the current node as a sequence of edges.
        """
        if self.status != Status.PROVED:
            return None
        assert self.is_explored

        proving_edge = min(
            self.out_edges,
            key=Edge.distance_to_proof,
        )

        if proving_edge.dst.is_terminal:
            # Base case: this edge is all that's required to finish the proof
            assert isinstance(proving_edge.dst, ProofFinishedNode)
            return [proving_edge]
        else:
            # Recursive case: prove the child, then add this edge
            assert isinstance(proving_edge.dst, InternalNode)
            child_proof = proving_edge.dst.extract_proof()
            assert child_proof
            return [proving_edge, *child_proof]

    def extract_trace(self) -> List["Edge"]:
        """
        Extract the sequence of tactics from the last marker to this node.
        """
        if getattr(self, '_marker', False):
            return []

        parent_edge = self.in_edges[0]
        return [*parent_edge.src.extract_trace(), parent_edge]


@dataclass
class Edge:
    """An edge in the search tree, representing a tactic."""

    tactic: str
    src: InternalNode = field(repr=False)
    dst: Node = field(repr=False)
    time: float = field(repr=False)
    logprob: float = field(repr=False)

    def distance_to_proof(self) -> float:
        return 1 + self.dst.distance_to_proof



def validate_proof(theorem: Theorem, trajectory: List[str], cls, focus_mode: bool = False) -> bool:
    """Validate the proof. Return True if the proof is valid."""
    assert theorem is not None, "Theorem must be provided for validation"
    state = None
    start_time = time.time()
    try:
        with Dojo(theorem, timeout=600) as (dojo, state):
            for idx, tactic in enumerate(trajectory):
                #tactic only applies to the current goal in agentic mode
                tac = f"focus {tactic}" if focus_mode else tactic
                state = dojo.run_tac(state, tac)
                if idx != len(trajectory) - 1 and not isinstance(state, TacticState):
                    logger.warning(
                        f"Proof validation failed: replay {tactic} failed at step {idx}, expected TacticState but got {type(state).__name__}, trajectory: {trajectory}"
                    )
                    return False
    except Exception as ex:
        logger.warning(f"Proof validation failed: {ex}, trajectory: {trajectory}")
        return False
    if isinstance(state, ProofFinished):
        logger.info(f"Found a proof!")
    logger.info(
        f"Proof validation took {time.time() - start_time:.2f}s for theorem {theorem.full_name}, result: {type(state).__name__}, trajectory: {trajectory}"
    )
    return isinstance(state, cls)

def extract_proof_data(
    proof_edges: Optional[List] = None,
    focus_mode: bool = False,
) -> Tuple[Optional[Tuple[Tuple[str, str]]], Optional[Tuple[Tuple[float, float]]], Optional[Tuple[Tuple[str, str, str]]]]:
    """Extract proof, proof_stats and preference pairs from the proof edges."""
    if not proof_edges:
        return None, None, None
    
    # Extract proof and proof_stats
    if focus_mode:
        proof = tuple((edge.src.state.pp1, edge.tactic) for edge in proof_edges)
    else:
        proof = tuple((edge.src.state.pp, edge.tactic) for edge in proof_edges)
    proof_stats = tuple((edge.time, edge.logprob) for edge in proof_edges)
    
    # Extract preference pairs
    preference_pairs = []
    for edge in proof_edges:
        for out_edge in edge.src.out_edges:
            if isinstance(out_edge.dst, ErrorNode):
                if focus_mode:
                    preference_pairs.append((edge.src.state.pp1, edge.tactic, out_edge.tactic))
                else:
                    preference_pairs.append((edge.src.state.pp, edge.tactic, out_edge.tactic))
                logger.info(f"Found preference pair: <win> '{edge.tactic}' </win> ; <lose> '{out_edge.tactic}' </lose>")
    
    preference_pairs = tuple(preference_pairs) if preference_pairs else None
    
    return proof, proof_stats, preference_pairs
