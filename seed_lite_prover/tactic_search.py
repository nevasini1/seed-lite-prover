"""State-aware best-first Lean-checked proof search.

The previous implementation prompted BFS-Prover with a proof-prefix STRING
("theorem … := by\\n  induction n with …\\n  -- suggest tactic"). BFS-Prover-V2
was trained on **tactic states** — explicit `case zero\\n⊢ ...` style goals —
so the prefix-string prompt under-conditioned the model and we got 0/8 on
the induction slice.

This version uses the Lean REPL's `proofState` API: each tree node carries a
REPL proof-state id and the current `goals` text. Expansion prompts the
model with the actual `goals` block, applies each candidate tactic via
`apply_tactic`, and uses the returned state (or terminal status) to grow
the tree. Falls back to the old prefix-string approach if no REPL is
available.

The proof tree (priority queue, dedup, status propagation) was ported in
Phase H from `ByteDance-Seed/BFS-Prover-V2/src/search/proof_tree.py`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .lean_snippets import LeanProblem, wrap
from .ollama_client import GenerateRequest
from .proof_tree import Edge, InternalNode, ProofTree, Status, TerminalNode

if TYPE_CHECKING:
    from .orchestrator import Orchestrator, ProofAttempt


# Per-node: REPL proof-state id and goals text live in InternalNode's
# `_state_id` / `_goals` attribute (set ad-hoc; we don't want to break the
# heap comparison contract by adding compare=True fields).
_STATE_ID_ATTR = "_state_id"
_GOALS_ATTR = "_goals"


def _parse_tactic(raw: str) -> str:
    raw = raw.strip()
    for fence in ("```lean", "```"):
        if raw.startswith(fence):
            raw = raw[len(fence):].strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()
    return raw.splitlines()[0].strip() if raw else ""


def _format_goal_prompt(problem: LeanProblem, goals_text: str, retrieved: str = "") -> str:
    """BFS-Prover-V2-style: explicit tactic state, no proof-prefix string.

    Format approximates LeanDojo's TacticState rendering, which BFS-Prover
    was trained on.
    """
    parts: list[str] = []
    if retrieved:
        parts.append("Relevant Mathlib facts:\n" + retrieved + "\n")
    parts.append("Current Lean tactic state:")
    parts.append(goals_text.strip() if goals_text.strip() else "<no goals — proof complete?>")
    parts.append("\nProduce one Lean 4 tactic that makes progress on the FIRST goal above. Respond with the tactic only, no explanation, no code fences.")
    return "\n".join(parts)


def bfs_prove(orc: "Orchestrator", problem: LeanProblem) -> tuple[bool, str, list["ProofAttempt"]]:
    """State-aware best-first search. REPL backend is required for full
    functionality; otherwise falls back to prefix-string behavior."""
    from .orchestrator import ProofAttempt

    attempts: list[ProofAttempt] = []
    v = orc.v
    run_deadline = getattr(orc, "_deadline", None)
    local_deadline = time.time() + v.search_budget_s
    deadline = min(local_deadline, run_deadline) if run_deadline else local_deadline

    if not orc.lean.has_repl:
        return _bfs_prove_prefix_string(orc, problem, deadline, attempts)

    retrieved = ""
    if v.use_retrieval:
        from .retrieval import retrieve_for_goal
        retrieved = retrieve_for_goal(orc, problem.statement, k=v.retrieval_k)

    # Open the initial proof state.
    decl = f"{problem.keyword} {problem.name} {problem.statement}"
    initial = orc.lean.start_proof(decl)
    if initial.id is None or not initial.ok:
        return False, "", attempts

    tree = ProofTree()
    setattr(tree.root, _STATE_ID_ATTR, initial.id)
    setattr(tree.root, _GOALS_ATTR, initial.goals)

    # Standard budget. Each search step expands one node by sampling k
    # tactics; we honour the per-search-and-run deadlines on every iteration.
    max_steps = max(1, v.search_depth) * max(1, v.samples)
    for _ in range(max_steps):
        if time.time() > deadline or tree.root.status == Status.PROVED:
            break
        node = tree.pop_best()
        if node is None:
            break
        if node.depth >= v.search_depth:
            tree.finalize(node, [])
            continue
        state_id = getattr(node, _STATE_ID_ATTR, None)
        goals_text = getattr(node, _GOALS_ATTR, "")
        if state_id is None:
            tree.finalize(node, [])
            continue

        prompt = _format_goal_prompt(problem, goals_text, retrieved)
        edges_built: list[Edge] = []
        for i in range(v.samples):
            if time.time() > deadline:
                break
            temp = 0.4 + 0.07 * i
            req = GenerateRequest(
                model=orc.prover_model,
                prompt=prompt,
                temperature=temp,
                num_predict=64,
                stop=("\n",),
            )
            tac = _parse_tactic(orc.ollama.generate(req))
            if not tac or tac.startswith("--"):
                continue

            t0 = time.time()
            ps = orc.lean.apply_tactic(state_id, tac)
            dt = time.time() - t0
            edge_logprob = -float(i) - (temp * 0.5)
            new_prefix = node.prefix + (tac,)

            if ps.done:
                # Tactic closed the proof. Record the winning prefix.
                attempt = ProofAttempt(
                    proof="\n".join(new_prefix),
                    ok=True,
                    elapsed_s=dt,
                    source=f"bfs_state:d{len(new_prefix)}",
                )
                attempts.append(attempt)
                child = tree.attach_child(
                    parent=node, tactic=tac, edge_logprob=edge_logprob,
                    elapsed_s=dt, ok=True, syntax_dead=False,
                )
                edges_built.append(Edge(tactic=tac, src=node, dst=child, elapsed_s=dt, logprob=edge_logprob))
                tree.finalize(node, edges_built)
                return True, "\n".join(new_prefix), attempts

            if not ps.ok or ps.id is None:
                # Lean error on this tactic — treat as a dead leaf.
                attempts.append(ProofAttempt(
                    proof="\n".join(new_prefix),
                    ok=False,
                    elapsed_s=dt,
                    error=_first_error(ps.messages),
                    source=f"bfs_state:d{len(new_prefix)}:err",
                ))
                child = tree.attach_child(
                    parent=node, tactic=tac, edge_logprob=edge_logprob,
                    elapsed_s=dt, ok=False, syntax_dead=True,
                    error=_first_error(ps.messages),
                )
                edges_built.append(Edge(tactic=tac, src=node, dst=child, elapsed_s=dt, logprob=edge_logprob))
                continue

            # Successful tactic but still have open goals — push child node.
            attempts.append(ProofAttempt(
                proof="\n".join(new_prefix),
                ok=False,
                elapsed_s=dt,
                source=f"bfs_state:d{len(new_prefix)}",
            ))
            child_obj = tree.attach_child(
                parent=node, tactic=tac, edge_logprob=edge_logprob,
                elapsed_s=dt, ok=False, syntax_dead=False,
            )
            if isinstance(child_obj, InternalNode):
                setattr(child_obj, _STATE_ID_ATTR, ps.id)
                setattr(child_obj, _GOALS_ATTR, ps.goals)
            edges_built.append(Edge(tactic=tac, src=node, dst=child_obj, elapsed_s=dt, logprob=edge_logprob))

        tree.finalize(node, edges_built)

    return False, "", attempts


def _first_error(messages: list[dict]) -> str:
    for m in messages:
        if m.get("severity") == "error":
            return str(m.get("data", ""))[:300]
    return ""


# ---------------------------------------------------------------------------
# Legacy fallback: when no REPL is available, fall back to the old prefix-
# string search (still does best-first over the heap, but feeds the model
# the wrong context — kept only for the subprocess backend's CI value).
# ---------------------------------------------------------------------------

def _bfs_prove_prefix_string(
    orc: "Orchestrator",
    problem: LeanProblem,
    deadline: float,
    attempts: list,
) -> tuple[bool, str, list]:
    from .orchestrator import ProofAttempt

    v = orc.v
    retrieved = ""
    if v.use_retrieval:
        from .retrieval import retrieve_for_goal
        retrieved = retrieve_for_goal(orc, problem.statement, k=v.retrieval_k)

    tree = ProofTree()
    for _ in range(v.search_depth * max(1, v.samples)):
        if time.time() > deadline or tree.root.status == Status.PROVED:
            break
        node = tree.pop_best()
        if node is None:
            break
        if node.depth >= v.search_depth:
            tree.finalize(node, [])
            continue

        pfx = "\n".join(f"  {t}" for t in node.prefix) if node.prefix else "  -- (empty)"
        prompt_parts = []
        if retrieved:
            prompt_parts.append("Relevant Mathlib facts:\n" + retrieved)
        prompt_parts.append(
            f"{problem.keyword} {problem.name} {problem.statement} := by\n{pfx}\n"
            "-- Suggest one Lean tactic to make progress. Respond with the tactic only."
        )
        prompt = "\n\n".join(prompt_parts)

        edges_built: list[Edge] = []
        for i in range(v.samples):
            if time.time() > deadline:
                break
            temp = 0.4 + 0.07 * i
            req = GenerateRequest(
                model=orc.prover_model,
                prompt=prompt,
                temperature=temp,
                num_predict=64,
                stop=("\n",),
            )
            tac = _parse_tactic(orc.ollama.generate(req))
            if not tac or tac.startswith("--"):
                continue
            new_prefix = node.prefix + (tac,)
            if new_prefix in tree.seen and tree.seen[new_prefix] is not node:
                continue
            body = "\n".join(new_prefix)
            snippet = wrap(problem, body)
            t0 = time.time()
            res = orc.lean.check(snippet)
            dt = time.time() - t0
            edge_logprob = -float(i) - (temp * 0.5)
            attempt = ProofAttempt(
                proof=body, ok=res.ok, elapsed_s=dt,
                error="" if res.ok else res.stderr[:500],
                source=f"bfs:d{len(new_prefix)}",
            )
            attempts.append(attempt)
            child = tree.attach_child(
                parent=node, tactic=tac, edge_logprob=edge_logprob,
                elapsed_s=dt, ok=res.ok,
                error=res.stderr if not res.ok else "",
                syntax_dead=(not res.ok and ("unknown" in res.stderr.lower() or "unexpected" in res.stderr.lower())),
            )
            edges_built.append(Edge(tactic=tac, src=node, dst=child, elapsed_s=dt, logprob=edge_logprob))
            if res.ok:
                tree.finalize(node, edges_built)
                return True, body, attempts
        tree.finalize(node, edges_built)
    return False, "", attempts
