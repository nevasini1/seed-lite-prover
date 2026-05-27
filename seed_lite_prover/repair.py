"""Error-aware repair loop.

Two phases:
1. Targeted fix — feed (failing proof, Lean error) to Kimina, ask for the
   minimal correction. Up to `repair_max_rounds - 1` rounds.
2. Dynamic Replanning (final round) — switch to the BFS-Prover-V2-style
   replan prompt: produce a fresh have-chain that bridges the existing
   progress to the goal. Adapted from
   `ByteDance-Seed/BFS-Prover-V2/src/plan/prompt.yaml` (Apache-2.0).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .bfs_prover_prompts import REPLAN_TEMPLATE
from .lean_snippets import LeanProblem, indent, wrap
from .ollama_client import GenerateRequest

if TYPE_CHECKING:
    from .orchestrator import Orchestrator, ProofAttempt


_REPAIR_PROMPT = """The following Lean 4 proof of

  {kw} {name} {statement} := by

failed.

Proof body:
{proof}

Lean error:
{error}

Modify ONLY the failing part. Return the full corrected proof body, no
fences, no commentary, no leading `theorem ... := by` line.
"""


# Patterns used to score "closeness" of a failing attempt — see _closeness_score.
_UNSOLVED_CASES_RE = re.compile(r"^case\s+[A-Za-z_][\w']*\s*$", re.MULTILINE)
_UNKNOWN_IDENT_RE = re.compile(r"\bunknown identifier\b|\bunknown constant\b", re.IGNORECASE)
_UNEXPECTED_RE = re.compile(r"\bunexpected token\b|\bsyntax error\b", re.IGNORECASE)
_GOAL_TURNSTILE_RE = re.compile(r"^\s*⊢\s+", re.MULTILINE)


def _closeness_score(attempt) -> float:
    """Higher = closer to a clean Lean proof. Used to pick the most-promising
    failing attempt as the seed for repair. Signals (in priority order):
       * type errors that are merely 'unsolved goals' (not unknown-ident /
         syntax) score highest — the model got the structure right
       * fewer unsolved cases is better
       * fewer remaining goals (estimated from `⊢` lines) is better
       * shorter error message is better (a tight error means a small fix)
       * non-empty proof body required (already filtered upstream)
    """
    err = attempt.error or ""
    score = 0.0
    if not err:
        return -1e6
    # Heavy penalty: model output never even compiled in any way
    if _UNEXPECTED_RE.search(err):
        score -= 100.0
    if _UNKNOWN_IDENT_RE.search(err):
        score -= 50.0
    # Reward: stuck on the structured 'unsolved goals' shape (most patchable)
    if "unsolved goals" in err.lower():
        score += 100.0
    # Fewer unsolved cases is closer to done
    n_cases = len(_UNSOLVED_CASES_RE.findall(err))
    score -= 10.0 * n_cases
    # Fewer remaining goal turnstiles is closer to done
    n_goals = len(_GOAL_TURNSTILE_RE.findall(err))
    score -= 5.0 * n_goals
    # Shorter error = tighter fix target
    score -= min(len(err) / 100.0, 20.0)
    return score


_FENCE_LEAD = re.compile(r"^```(?:lean)?\s*", re.IGNORECASE)
_FENCE_TAIL = re.compile(r"\s*```\s*$")
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _clean(raw: str) -> str:
    raw = _THINK_BLOCK.sub("", raw)
    raw = _FENCE_LEAD.sub("", raw.strip())
    raw = _FENCE_TAIL.sub("", raw)
    return raw.rstrip()


def _strip_common_indent(body: str) -> str:
    """If every non-empty line starts with the same leading whitespace, drop it.
    Avoids feeding double-indented bodies through `wrap()`'s own indenter."""
    lines = body.splitlines()
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return body
    common = min(len(ln) - len(ln.lstrip(" ")) for ln in nonempty)
    if common == 0:
        return body
    return "\n".join((ln[common:] if ln.strip() else ln) for ln in lines)


def repair_last_failure(
    orc: "Orchestrator",
    problem: LeanProblem,
    prior_attempts: list["ProofAttempt"],
) -> tuple[bool, str, list["ProofAttempt"]]:
    from .orchestrator import ProofAttempt

    out: list[ProofAttempt] = []

    candidates = [a for a in prior_attempts if not a.ok and a.error and a.proof.strip()]
    if not candidates:
        return False, "", out

    # Pick the attempt that's CLOSEST to done, not the longest one (per the
    # review: length is a poor proxy for closeness; a long hallucinated proof
    # is much worse than a short proof with one unsolved goal). We score by
    # Lean signals visible in the per-attempt error: fewer unsolved goals,
    # latest successful prefix, no syntax error, no unknown identifier.
    seed = max(candidates, key=_closeness_score)

    current_proof = seed.proof
    current_error = seed.error

    import time as _t
    deadline = getattr(orc, "_deadline", None)

    for round_idx in range(orc.v.repair_max_rounds):
        if deadline is not None and _t.time() > deadline:
            break
        is_final = round_idx == orc.v.repair_max_rounds - 1
        if is_final:
            # Final round: escalate to Dynamic Replanning instead of another
            # targeted patch. Ask for a fresh have-chain that connects
            # progress to the goal. We feed `current_proof` (the
            # most-recent failing body) as the "stuck subgoal" context.
            prompt = REPLAN_TEMPLATE.format(
                theorem=f"{problem.keyword} {problem.name} {problem.statement} := by",
                proven_subgoals="(none — repair could not isolate proven steps)",
                stuck_subgoal=current_proof,
            )
            req_source = "repair:replan"
        else:
            prompt = _REPAIR_PROMPT.format(
                kw=problem.keyword,
                name=problem.name,
                statement=problem.statement,
                proof=current_proof,
                error=current_error[:1500],
            )
            req_source = f"repair:r{round_idx}"
        req = GenerateRequest(
            model=orc.helper_model,
            prompt=prompt,
            temperature=0.3 if is_final else 0.2,
            num_predict=3072,
            chat=True,
            stop=("\ntheorem ", "\nexample ", "\nlemma "),
        )
        # Hard per-call timeout based on remaining deadline.
        per_call_timeout = None
        if deadline is not None:
            per_call_timeout = max(1.0, deadline - _t.time())
        chat_resp = orc.ollama.chat(req, timeout=per_call_timeout)
        repaired = _clean(chat_resp.content)
        if not repaired and chat_resp.thinking:
            # Kimina spent all its budget thinking; nothing actionable. Skip.
            out.append(ProofAttempt(
                proof="", ok=False, elapsed_s=0.0,
                error="repair: empty content (only thinking)",
                source=f"repair:r{round_idx}:empty",
            ))
            break
        repaired = _strip_common_indent(repaired)

        # NOTE (soundness): a previous version of this code appended
        # `:= by sorry` to unproved `have` signatures from the replan output,
        # then submitted the snippet for acceptance. That was unsound — a
        # have-chain ending in `sorry` is not a Lean-verified proof. The
        # strict `lean.check` now rejects any source containing `sorry`,
        # but we also drop the auto-sorry-injection here so we never emit
        # such snippets in the first place. If the replan returns a bare
        # have-chain (no `by`), the strict check below will fail it; that
        # is the correct behaviour.

        snippet = wrap(problem, repaired)
        res = orc.lean.check(snippet)
        attempt = ProofAttempt(
            proof=repaired,
            ok=res.ok,
            elapsed_s=res.elapsed_s,
            error="" if res.ok else res.stderr[:1000],
            source=req_source,
        )
        out.append(attempt)
        if res.ok:
            return True, repaired, out
        current_proof = repaired
        current_error = res.stderr

    return False, "", out
