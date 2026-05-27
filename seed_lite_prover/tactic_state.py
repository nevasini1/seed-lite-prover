"""LeanDojo-style structured tactic-state surface, built on top of the
leanprover-community/repl raw goal-text output.

The `repl` project emits goal state as rendered text — typically a series
of hypothesis lines followed by a `⊢ <goal>` line, repeated per open goal,
separated by blank lines:

    n m : ℕ
    h₁ : n + 1 ≤ m
    h₂ : 0 < m
    ⊢ n < m

    case succ
    k : ℕ
    ih : k < k + 1
    ⊢ k + 1 < k + 2

BFS-Prover-V2 was trained on LeanDojo's `TacticState`, which exposes the
hypothesis list as a structured `(name, type)` table. We don't have access
to LeanDojo (Lean version mismatch + LeanDojo's setup is GPU-heavy), but
we can recover most of the structure by parsing the rendered text. This
gives:

  * Better BFS prompts (structured "given hypotheses ...; prove ...")
  * A real symbol set for retrieval (extracted from the goal AND the
    hypothesis types, not just the original theorem statement)
  * A goal-count signal that's more robust than counting `⊢` characters
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Hypothesis line: `name : type` (possibly multi-name `a b c : Type`).
# Names: Lean identifiers including unicode subscripts (₁, ✝, etc.).
# Type: everything after the FIRST top-level `:` until end-of-line.
# `top-level` here means not inside a (...) / [...] / {...} group on the
# same line — but for the parser's purposes we just use the first `:` since
# Lean's pretty-printer puts the binder colon at the start of the type.
# Lean identifiers include Unicode letters (ℕ, ℝ, ℂ, α, β, …), digits, primes,
# dots (namespace separator), and the ✝ marker on hidden hypotheses. We use
# Python's \w with re.UNICODE plus an explicit character class for the math
# unicode block we care about.
_IDENT = re.compile(r"[A-Za-z_ℕℤℝℚℂΑ-ωℐ-∀][\w'.✝₀-₉ℐ-∀ℕℤℝℚℂΑ-ω]*", re.UNICODE)
_HYP_LINE = re.compile(
    r"^("
    r"[A-Za-z_ℕℤℝℚℂΑ-ωℐ-∀][\w'.✝₀-₉ℐ-∀ℕℤℝℚℂΑ-ω]*"
    r"(?:\s+[A-Za-z_ℕℤℝℚℂΑ-ωℐ-∀][\w'.✝₀-₉ℐ-∀ℕℤℝℚℂΑ-ω]*)*"
    r")\s*:\s*(.+?)\s*$",
    re.UNICODE,
)
_CASE_LINE = re.compile(r"^case\s+([A-Za-z_][\w'.]*)\s*$")
_GOAL_LINE = re.compile(r"^⊢\s+(.+?)\s*$")


@dataclass
class Hypothesis:
    """One hypothesis line: `<names> : <type>`. Multi-name binders like
    `n m k : ℕ` become a single Hypothesis with `names=['n','m','k']`."""
    names: list[str]
    type_str: str

    @property
    def symbols(self) -> set[str]:
        """All identifiers appearing in the type — used for retrieval."""
        return set(_IDENT.findall(self.type_str))


@dataclass
class Goal:
    """One open goal: optional case label, hypothesis list, goal type."""
    case: str = ""            # empty if no `case <name>` header
    hypotheses: list[Hypothesis] = field(default_factory=list)
    goal_type: str = ""

    @property
    def symbols(self) -> set[str]:
        """Goal-type symbols + all hypothesis-type symbols, for retrieval."""
        out: set[str] = set(_IDENT.findall(self.goal_type))
        for h in self.hypotheses:
            out |= h.symbols
        return out


@dataclass
class TacticState:
    """Structured form of the REPL's rendered goal text."""
    goals: list[Goal] = field(default_factory=list)

    @property
    def goal_count(self) -> int:
        return len(self.goals)

    @property
    def symbols(self) -> set[str]:
        """Union of every goal's symbols — the natural query set for
        retrieval at any open goal state."""
        out: set[str] = set()
        for g in self.goals:
            out |= g.symbols
        return out

    def render(self) -> str:
        """Re-render structured state into a BFS-prompt-friendly form.

        Format matches what BFS-Prover-V2 saw during training (rendered
        rather than JSON), so the policy model treats it the same way. Mostly
        a normalised echo of the original input, but with consistent
        whitespace and case-label placement.
        """
        if not self.goals:
            return "<no goals — proof complete>"
        chunks: list[str] = []
        for i, g in enumerate(self.goals):
            lines: list[str] = []
            if g.case:
                lines.append(f"case {g.case}")
            for h in g.hypotheses:
                lines.append(f"{' '.join(h.names)} : {h.type_str}")
            lines.append(f"⊢ {g.goal_type}")
            chunks.append("\n".join(lines))
        return "\n\n".join(chunks)

    def first_goal_render(self) -> str:
        """Just the first goal, for narrow BFS prompts."""
        return self.render() if not self.goals else self.render().split("\n\n", 1)[0]


def parse(repl_goal_text: str) -> TacticState:
    """Parse the REPL's rendered goal text into a TacticState.

    Robust to leading/trailing whitespace, multiple goals separated by blank
    lines, optional `case <name>` headers, and multi-name hypothesis lines.
    Anything we can't parse becomes a hypothesis with empty names + the line
    text as the type — so we lose nothing and the rendered output remains
    informative even on edge cases.
    """
    if not repl_goal_text or not repl_goal_text.strip():
        return TacticState(goals=[])

    # Split into per-goal blocks at blank lines
    blocks: list[list[str]] = [[]]
    for ln in repl_goal_text.splitlines():
        if not ln.strip():
            if blocks[-1]:
                blocks.append([])
        else:
            blocks[-1].append(ln)
    blocks = [b for b in blocks if b]

    state = TacticState()
    for block in blocks:
        goal = Goal()
        for ln in block:
            stripped = ln.strip()
            mc = _CASE_LINE.match(stripped)
            if mc:
                goal.case = mc.group(1)
                continue
            mg = _GOAL_LINE.match(stripped)
            if mg:
                goal.goal_type = mg.group(1)
                continue
            mh = _HYP_LINE.match(stripped)
            if mh:
                names = mh.group(1).split()
                type_str = mh.group(2)
                goal.hypotheses.append(Hypothesis(names=names, type_str=type_str))
                continue
            # Couldn't parse — preserve as a raw "type" hypothesis with no
            # binder names so it still contributes symbols to retrieval.
            goal.hypotheses.append(Hypothesis(names=[], type_str=stripped))
        if goal.goal_type or goal.hypotheses or goal.case:
            state.goals.append(goal)
    return state
