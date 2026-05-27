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
    """Extract a tactic from a model response.

    Accepts multi-line tactic blocks (the previous single-line behaviour
    discarded valid `induction ... with | zero => ... | succ ... => ...`
    blocks). Strategy:
      1. Strip leading/trailing code fences.
      2. If the raw content is a fenced block, take it whole.
      3. Otherwise, take consecutive non-empty lines from the start until
         we hit a blank line, an `--` comment-only line, or obvious chatter
         ("Note:", "Here", "We", etc. starting a line).
      4. Trim trailing blank lines and any trailing prose.
    """
    raw = raw.strip()
    # Fenced block: extract the content between ```lean / ``` markers
    if raw.startswith("```"):
        # Skip the opening fence line
        nl = raw.find("\n")
        if nl >= 0:
            inner = raw[nl + 1:]
        else:
            inner = raw[3:]
        # Drop trailing fence
        if "```" in inner:
            inner = inner[: inner.index("```")]
        raw = inner.strip()

    out_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            if out_lines:
                break  # blank line ends the tactic
            continue
        # Obvious prose markers — stop accumulating
        if any(stripped.startswith(prefix) for prefix in (
            "Note:", "Here ", "We ", "This ", "The ", "Explanation:", "Hence", "Therefore",
        )) and out_lines:
            break
        # Pure comment lines
        if stripped.startswith("--") and not out_lines:
            continue
        out_lines.append(line.rstrip())
    return "\n".join(out_lines).strip()


def _goal_count(goals_text: str) -> int:
    """Estimate remaining goal count from REPL's rendered goal text.
    Each goal block begins with `⊢ ...`; counting turnstiles is robust."""
    return sum(1 for ln in (goals_text or "").splitlines() if ln.lstrip().startswith("⊢"))


def _progress_score(parent_goals: str, child_goals: str, child_done: bool, elapsed_s: float) -> float:
    """Higher = better. Used to rank ProofTree nodes for best-first expansion.

    Replaces the temperature-ladder logprob proxy. Signals:
      * If the tactic CLOSED the proof → very high
      * Fewer remaining goals than parent → reward proportional to delta
      * Shorter total goal text → reward (proxy for "simpler state")
      * Faster Lean check → small reward (cheap nodes preferred)
    Negative on regressions (more goals than parent).
    """
    if child_done:
        return 1e6
    pg = _goal_count(parent_goals)
    cg = _goal_count(child_goals)
    score = 100.0 * (pg - cg)                # +100 per goal closed
    score -= 0.01 * max(0, len(child_goals or ""))   # smaller state = better
    score -= max(0.0, elapsed_s) * 0.5       # cheaper tactic = slightly better
    return score


def _format_goal_prompt(problem: LeanProblem, goals_text: str, retrieved: str = "") -> str:
    """BFS-Prover-V2-style: explicit tactic state, no proof-prefix string.

    Parses `goals_text` through the structured `tactic_state` module to
    normalise hypothesis lists and case labels — closer to the LeanDojo
    TacticState format BFS-Prover-V2 was trained against. Falls back to the
    raw text on any parse error.
    """
    from . import tactic_state as _ts
    try:
        state = _ts.parse(goals_text)
        normalised = state.render() if state.goal_count else (goals_text.strip() or "<no goals — proof complete?>")
    except Exception:
        normalised = goals_text.strip() or "<no goals — proof complete?>"

    parts: list[str] = []
    if retrieved:
        parts.append("Relevant Mathlib facts (cite by name in your tactic):\n" + retrieved + "\n")
    parts.append("Current Lean tactic state:")
    parts.append(normalised)
    parts.append(
        "\nProduce one Lean 4 tactic that makes progress on the FIRST goal above. "
        "Respond with the tactic only, no explanation, no code fences."
    )
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

        # Refresh retrieval against the LIVE tactic-state symbols (so the
        # hints evolve as the proof unfolds), not just the top-line theorem.
        node_retrieved = retrieved
        if v.use_retrieval and goals_text:
            try:
                from . import tactic_state as _ts
                from .retrieval import retrieve_for_state
                live_state = _ts.parse(goals_text)
                if live_state.symbols:
                    node_retrieved = retrieve_for_state(orc, live_state.symbols, k=v.retrieval_k) or retrieved
            except Exception:
                pass  # fall back to top-line retrieval

        prompt = _format_goal_prompt(problem, goals_text, node_retrieved)
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
            # Progress-based score replaces the temperature-ladder logprob
            # proxy. Higher = better; used to rank nodes for best-first
            # expansion via ProofTree.attach_child(edge_logprob=...).
            parent_goals = getattr(node, _GOALS_ATTR, "") or ""
            child_goals = ps.goals if ps else ""
            edge_logprob = _progress_score(parent_goals, child_goals, bool(ps.done if ps else False), dt)
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
