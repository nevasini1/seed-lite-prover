"""BFS-Prover-V2-style decomposition.

Replaces the earlier list-of-separate-sub-lemmas approach. The planner
(Kimina via /api/chat) is asked for a chain of `have` *signatures* — no
proofs — that, when proven sequentially, imply the theorem. Each `have`
is then proven by re-entering the orchestrator (depth-limited, with
decomposition + repair off so it doesn't recurse forever). Successful
have-proofs are spliced into the parent theorem and closed with a small
defensive tactic.

Prompts are adapted from `ByteDance-Seed/BFS-Prover-V2/src/plan/prompt.yaml`
(Apache-2.0). See `bfs_prover_prompts.py`.
"""

from __future__ import annotations

import re
from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING

from .bfs_prover_prompts import (
    INITIAL_PLANNING_EXAMPLES,
    INITIAL_PLANNING_SYSTEM,
    INITIAL_PLANNING_TEMPLATE,
)
from .lean_snippets import LeanProblem, parses_in_parent, wrap
from .ollama_client import GenerateRequest

if TYPE_CHECKING:
    from .orchestrator import Orchestrator, ProofAttempt


_FENCE_LEAD = re.compile(r"^```(?:lean)?\s*", re.IGNORECASE)
_FENCE_TAIL = re.compile(r"\s*```\s*$")
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# Match a `have <body>` OR `suffices <body>` block extending to the next
# `have`/`suffices`/`ASSEMBLY:` marker, or end of text. Supports the
# `suffices` style: `suffices h : T` is the goal-reducing dual of `have`.
_HAVE_BLOCK = re.compile(
    r"(?:have|suffices)\s+(.*?)(?=\n*\s*(?:have|suffices)\s+|\n*\s*ASSEMBLY\s*:|\Z)",
    re.DOTALL,
)
_NAME_AND_TYPE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_']*)\s*:\s*(?P<type>.+)$", re.DOTALL)
_ASSEMBLY_LINE = re.compile(r"^\s*ASSEMBLY\s*:\s*(.+?)\s*$", re.MULTILINE)


def _clean(raw: str) -> str:
    raw = _THINK_BLOCK.sub("", raw)
    raw = _FENCE_LEAD.sub("", raw.strip())
    raw = _FENCE_TAIL.sub("", raw)
    return raw.strip()


def _parse_have_chain(raw: str, max_lemmas: int) -> tuple[list[tuple[str, str]], str]:
    """Return (haves, assembly) from the planner output.

    - haves: list of (name, type) from `have <name> : <type>` /
             `suffices <name> : <type>` lines.
    - assembly: the tactic line following `ASSEMBLY:` if present; "" otherwise.

    Drops trailing `:= ...` (rule 2 forbids it but models slip up).
    """
    out: list[tuple[str, str]] = []
    text = _clean(raw)
    for m in _HAVE_BLOCK.finditer(text):
        body = m.group(1).strip()
        body = re.sub(r"\s*:=.*$", "", body, flags=re.DOTALL).strip()
        body = re.sub(r"\s+", " ", body)
        nt = _NAME_AND_TYPE.match(body)
        if not nt:
            continue
        name = nt.group("name").strip()
        typ = nt.group("type").strip().rstrip(",;.")
        if not name or not typ:
            continue
        out.append((name, typ))
        if len(out) >= max_lemmas:
            break
    asm_m = _ASSEMBLY_LINE.search(text)
    assembly = asm_m.group(1).strip() if asm_m else ""
    return out, assembly


def _format_theorem_for_planner(problem: LeanProblem) -> str:
    return f"{problem.keyword} {problem.name} {problem.statement} := by"


def decompose_and_prove(
    orc: "Orchestrator",
    problem: LeanProblem,
    depth: int = 0,
) -> tuple[bool, str, list["ProofAttempt"]]:
    import time as _t  # used by deadline-aware Ollama timeouts + recursion budget

    from .orchestrator import ProofAttempt

    attempts: list[ProofAttempt] = []
    if depth >= orc.v.decomp_max_depth:
        return False, "", attempts

    prompt = INITIAL_PLANNING_TEMPLATE.format(
        examples=INITIAL_PLANNING_EXAMPLES,
        theorem=_format_theorem_for_planner(problem),
    )
    req = GenerateRequest(
        model=orc.helper_model,
        prompt=prompt,
        system=INITIAL_PLANNING_SYSTEM,
        temperature=0.5,
        num_predict=3072,
        chat=True,
    )
    # Hard per-call timeout based on remaining deadline.
    deadline_for_planner = getattr(orc, "_deadline", None)
    per_call_timeout = None
    if deadline_for_planner is not None:
        per_call_timeout = max(1.0, deadline_for_planner - _t.time())
    raw = orc.ollama.generate(req, timeout=per_call_timeout)
    haves, assembly = _parse_have_chain(raw, orc.v.decomp_max_lemmas)
    # A single useful `have` + assembly is enough; the prior >= 2 cutoff
    # discarded plans like `have h : <key fact>; ASSEMBLY: linarith [h]`.
    if not haves:
        return False, "", attempts

    # Pre-filter: drop have-signatures that don't typecheck inside the parent
    # theorem body. Catches namespace-context issues `parses_as_type` misses.
    kept: list[tuple[str, str]] = []
    for name, typ in haves:
        if parses_in_parent(orc.lean, problem, name, typ):
            kept.append((name, typ))
    if not kept:
        return False, "", attempts

    # Recurse with decomposition + repair disabled, search optional.
    sub_variant = dc_replace(orc.v, use_decomposition=False, use_repair=False)
    sub_orc = type(orc)(
        variant=sub_variant,
        prover_model=orc.prover_model,
        helper_model=orc.helper_model,
        lean=orc.lean,
        cache=orc.cache,
        ollama=orc.ollama,
    )

    deadline = getattr(orc, "_deadline", None)

    proven: list[tuple[str, str, str]] = []  # (name, type, body)
    for name, typ in kept:
        if deadline is not None and _t.time() > deadline:
            break
        sub_problem = LeanProblem(
            path=problem.path,
            header=problem.header,
            keyword="theorem",
            name=name,
            statement=": " + typ,
        )
        sub_result = sub_orc.prove(sub_problem, deadline=deadline)
        attempts.extend(sub_result.attempts)
        if sub_result.solved and sub_result.winning_attempt_idx >= 0:
            proven.append((name, typ, sub_result.attempts[sub_result.winning_attempt_idx].proof))

    # Need at least 1 successful have to attempt assembly.
    if not proven:
        return False, "", attempts

    # Stitch: emit each proven have, then close the goal. We try TWO
    # assembly strategies in order:
    #   1. The planner's own ASSEMBLY tactic (if it provided one)
    #   2. The generic glue-combinator fallback (legacy behaviour)
    # Both attempts are recorded so attribution shows which one earned the win.
    have_lines: list[str] = []
    for nm, typ, body in proven:
        have_lines.append(f"have {nm} : {typ} := by")
        for ln in body.splitlines():
            have_lines.append("  " + ln)
    names_csv = ", ".join(n for n, _, _ in proven)

    candidate_assemblies: list[tuple[str, str]] = []
    if assembly:
        # Sanitise: drop trailing comments / fences if any
        clean_asm = assembly.split("--", 1)[0].strip().rstrip("`")
        if clean_asm:
            candidate_assemblies.append(("planner_assembly", clean_asm))
    fallback_glue = (
        f"first\n"
        f"  | (exact ⟨{names_csv}⟩)\n"
        f"  | (constructor <;> assumption)\n"
        f"  | (simp_all)\n"
        f"  | (aesop)\n"
        f"  | tauto\n"
        f"  | (linarith [{names_csv}])\n"
        f"  | omega"
    )
    candidate_assemblies.append(("fallback_glue", fallback_glue))

    final = None
    for tag, asm in candidate_assemblies:
        if deadline is not None and _t.time() > deadline:
            break
        body = "\n".join(have_lines + [asm])
        snippet = wrap(problem, body)
        res = orc.lean.check(snippet)
        attempt = ProofAttempt(
            proof=body,
            ok=res.ok,
            elapsed_s=res.elapsed_s,
            error="" if res.ok else res.stderr[:1000],
            source=f"decompose:d{depth}:{tag}",
        )
        attempts.append(attempt)
        if res.ok:
            return True, body, attempts
        # Save the last attempt for the legacy return contract
        final = attempt

    # All assembly attempts already recorded above. Neither closed the parent.
    return False, "", attempts
