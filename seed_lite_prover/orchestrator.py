"""High-level pipeline: a `LeanProblem` -> a Lean-verified proof.

Variants A-F are selected via flags on `Variant`. The orchestrator wires
together the sub-modules (tactic_search, retrieval, decompose, repair) and
records every attempt to the run log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .lean_runner import LeanRunner
from .lean_snippets import LeanProblem, wrap
from .memory import LemmaCache, VerifiedLemma
from .ollama_client import GenerateRequest, OllamaClient


SYMBOLIC_PREAMBLE_TACTICS = [
    "simp_all",
    "norm_num",
    "omega",
    "linarith",
    "nlinarith",
    "ring_nf",
    "field_simp",
    "aesop",
    "positivity",
    "tauto",
    "decide",
    # Induction-aware tactics — cheap on non-induction goals (fail fast),
    # but solve many MiniF2F induction problems outright. Base case usually
    # needs `decide`/`norm_num` (computes 0^k etc); succ case needs ih + simp.
    "induction n with | zero => decide | succ n ih => simp_all",
    "induction n with | zero => decide | succ n ih => omega",
    # Hand-verified: closes induction_sum_odd-style problems (Σ over range = polynomial)
    "induction n with | zero => simp | succ n ih => rw [Finset.sum_range_succ, ih]; ring",
    # Same but for real-valued sums with division (e.g. 1/(k*(k+1)))
    "induction n with | zero => simp | succ n ih => rw [Finset.sum_range_succ, ih]; field_simp; ring",
    # Same but with `omega` closer (handles Nat truncated subtraction)
    "induction n with | zero => simp | succ n ih => rw [Finset.sum_range_succ, ih]; omega",
    # Universal closer — try ring/omega/field_simp/linarith after the rewrite
    "induction n with | zero => simp | succ n ih => rw [Finset.sum_range_succ, ih]; (first | ring | omega | linarith | (field_simp; ring))",
    "induction n <;> (first | rfl | decide | norm_num | simp_all | omega | linarith | ring_nf)",
]


@dataclass
class Variant:
    name: str
    samples: int = 1
    use_search: bool = False
    use_retrieval: bool = False
    use_decomposition: bool = False
    use_repair: bool = False
    search_depth: int = 8
    search_budget_s: float = 300.0
    retrieval_k: int = 20
    decomp_max_depth: int = 2
    decomp_max_lemmas: int = 6
    repair_max_rounds: int = 3


@dataclass
class ProofAttempt:
    proof: str
    ok: bool
    elapsed_s: float
    error: str = ""
    source: str = ""


@dataclass
class TheoremResult:
    name: str
    statement: str
    solved: bool
    attempts: list[ProofAttempt] = field(default_factory=list)
    total_elapsed_s: float = 0.0
    winning_attempt_idx: int = -1


class Orchestrator:
    def __init__(
        self,
        variant: Variant,
        prover_model: str,
        helper_model: str,
        lean: LeanRunner,
        cache: LemmaCache,
        ollama: OllamaClient | None = None,
    ):
        self.v = variant
        self.prover_model = prover_model
        self.helper_model = helper_model
        self.lean = lean
        self.cache = cache
        self.ollama = ollama or OllamaClient()

    # -- whole-proof attempt (variants A, B) ---------------------------------

    def _whole_proof_attempt(self, problem: LeanProblem, temperature: float) -> ProofAttempt:
        # Prime BFS with the theorem signature; it expects to continue after `by`.
        prompt = (
            f"{problem.keyword} {problem.name} {problem.statement} := by\n"
        )
        req = GenerateRequest(
            model=self.prover_model,
            prompt=prompt,
            temperature=temperature,
            num_predict=512,
            stop=("\ntheorem ", "\nexample ", "\nlemma "),
        )
        start = time.time()
        body = self.ollama.generate(req).strip()
        snippet = wrap(problem, body)
        res = self.lean.check(snippet)
        return ProofAttempt(
            proof=body,
            ok=res.ok,
            elapsed_s=time.time() - start,
            error="" if res.ok else res.stderr[:1000],
            source="whole_proof",
        )

    # -- top-level dispatch --------------------------------------------------

    def prove(self, problem: LeanProblem, deadline: float | None = None) -> TheoremResult:
        """Try to prove `problem`. `deadline` is an absolute wall-clock cutoff
        (time.time() seconds); if set, every phase boundary and inner-loop
        iteration checks it and short-circuits with what has accumulated."""
        t0 = time.time()
        self._deadline = deadline  # picked up by sub-modules via getattr fallback
        result = TheoremResult(name=problem.name, statement=problem.statement, solved=False)

        # Step 0: cheap symbolic tactics (always on)
        for tac in SYMBOLIC_PREAMBLE_TACTICS:
            if deadline is not None and time.time() > deadline:
                break
            snippet = wrap(problem, tac)
            r = self.lean.check(snippet)
            attempt = ProofAttempt(
                proof=tac,
                ok=r.ok,
                elapsed_s=r.elapsed_s,
                error="" if r.ok else r.stderr[:1000],
                source=f"symbolic:{tac}",
            )
            result.attempts.append(attempt)
            if r.ok:
                result.solved = True
                result.winning_attempt_idx = len(result.attempts) - 1
                self._cache_if_new(problem.statement, attempt.proof, attempt.source)
                result.total_elapsed_s = time.time() - t0
                return result

        if deadline is not None and time.time() > deadline:
            result.total_elapsed_s = time.time() - t0
            return result

        # Best-of-N whole-proof (variant A: N=1, B: N=samples)
        temps = [0.2 + 0.05 * (i % 8) for i in range(self.v.samples)]
        for _i, temp in enumerate(temps):
            if deadline is not None and time.time() > deadline:
                break
            attempt = self._whole_proof_attempt(problem, temp)
            result.attempts.append(attempt)
            if attempt.ok:
                result.solved = True
                result.winning_attempt_idx = len(result.attempts) - 1
                self._cache_if_new(problem.statement, attempt.proof, attempt.source)
                result.total_elapsed_s = time.time() - t0
                return result

            # Cheap mechanical near-miss closer — fires when whole_proof
            # produced a correct skeleton but left one or more cases unclosed.
            # No LLM call; pure structural patch + Lean recheck.
            if attempt.error and "unsolved goals" in attempt.error.lower():
                from .case_closer import try_close_unsolved
                closer_attempts = try_close_unsolved(
                    self.lean, problem, attempt.proof, attempt.error, deadline=deadline
                )
                for ca in closer_attempts:
                    result.attempts.append(ca)
                    if ca.ok:
                        result.solved = True
                        result.winning_attempt_idx = len(result.attempts) - 1
                        self._cache_if_new(problem.statement, ca.proof, ca.source)
                        result.total_elapsed_s = time.time() - t0
                        return result

        if self.v.use_search and (deadline is None or time.time() < deadline):
            from .tactic_search import bfs_prove
            ok, proof, sub_attempts = bfs_prove(self, problem)
            result.attempts.extend(sub_attempts)
            if ok:
                result.solved = True
                result.winning_attempt_idx = len(result.attempts) - 1
                self._cache_if_new(problem.statement, proof, "bfs_search")

        if not result.solved and self.v.use_decomposition and (deadline is None or time.time() < deadline):
            from .decompose import decompose_and_prove
            ok, proof, sub_attempts = decompose_and_prove(self, problem)
            result.attempts.extend(sub_attempts)
            if ok:
                result.solved = True
                result.winning_attempt_idx = len(result.attempts) - 1
                self._cache_if_new(problem.statement, proof, "decomposition")

        if not result.solved and self.v.use_repair and (deadline is None or time.time() < deadline):
            from .repair import repair_last_failure
            ok, proof, sub_attempts = repair_last_failure(self, problem, result.attempts)
            result.attempts.extend(sub_attempts)
            if ok:
                result.solved = True
                result.winning_attempt_idx = len(result.attempts) - 1
                self._cache_if_new(problem.statement, proof, "repair")

        result.total_elapsed_s = time.time() - t0
        return result

    def _cache_if_new(self, statement: str, proof: str, source: str) -> None:
        try:
            self.cache.append(VerifiedLemma(statement=statement, proof=proof, source=source))
        except Exception:
            pass
