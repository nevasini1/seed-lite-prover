"""Planner prompts adapted from ByteDance-Seed/BFS-Prover-V2 (Apache-2.0).

Upstream: github.com/ByteDance-Seed/BFS-Prover-V2/blob/main/src/plan/prompt.yaml

We use these for `decompose.py` (Initial Planning) and `repair.py`'s
re-planning fallback (Dynamic Replanning). Rules 16 / 17 / 18 are slightly
trimmed to fit our smaller helper model (Kimina-Prover-RL-1.7B); the
Lean-4.10.0 target version mention is left as a note rather than rewritten,
since the syntactic rules still apply to Lean 4.29.1.
"""

INITIAL_PLANNING_SYSTEM = """\
You are an expert assistant specializing in Math Olympiads and the Lean 4 theorem prover.
Your primary goal is to generate **syntactically perfect, type-checkable** Lean 4
intermediate steps for a given theorem. Strictly adhere to the rules in the user
message. ANY violation is an error. While ensuring correctness, generate as many
intermediate steps as possible.
"""


INITIAL_PLANNING_TEMPLATE = """\
# Task
Given the following theorem statement in Lean 4, your job is to **plan the
complete proof** by analyzing the theorem statement and generating a coherent
sequence of `have` statements. These statements should form a clear chain of
reasoning that bridges the theorem's assumptions to its final claim, breaking
down complex arguments into simpler components.

# Mandatory Rules

1. **Critical: Explicitly Specify Set/Finset Types**
   - Correct: `({{-1, 0, 1}} : Set ℤ)`, `(Finset.Icc 1 42 : Finset ℕ)`
   - Incorrect: `{{-1, 0, 1}}`, `Finset.Icc 1 42`
2. **Omit the Proof**: Never provide `:= by ...`. Only state the `have` signature.
3. **Valid Lean 4 Code**: must type-check under standard Mathlib `open` directives.
4. **Use Existing Names**: use exact `mathlib` lemma / definition names. Do not invent.
5. **No Undeclared Variables**: do not introduce variables not in the theorem statement.
6. **Explicit Multiplication**: always use `*`. `a * x`, not `ax`.
7. **No Chained Inequalities**: split with `∧`. `a ≤ x ∧ x ≤ b`, not `a ≤ x ≤ b`.
8. **Logarithm**: `Real.log` is natural log. For other bases use `Real.logb`.
9. **Factorial**: write `(n)!` or `Nat.factorial n`, not `n!`.
10. **Numeric Types Annotated**: annotate at least one operand of division /
    subtraction with its type, e.g. `(a : ℝ) / b`, `(n : ℤ) - m`.
11. **Interval Notation**: prefer inequalities over `Icc`/`Ioo`/`Ico`/`Ioc`.
12. **Complex Numbers**: use `Complex.I` and `Complex.abs`.
13. **Avoid heavyweight Inequality Theorems**: prefer simple simplification steps.
14. **Equivalences as Implications**: split `↔` into two `have`s if needed.
15. **Real.pi**: write `Real.pi`, not `π`.
16. **Final Check**: before outputting, re-check every rule above.

# Output Format
First, lines starting with `have <name> : <type>` (or `suffices <name> : <type>`).
NO `:=`, NO `by`, NO commentary inside those lines.

Then, on a NEW line, the literal token `ASSEMBLY:` followed by a one-line Lean
tactic that closes the **original goal** assuming each `<name>` above is in
scope as a proven hypothesis. Examples of useful assembly tactics:
  ASSEMBLY: exact h1.trans h2
  ASSEMBLY: linarith [h1, h2, h3]
  ASSEMBLY: simp [h1, h2]
  ASSEMBLY: omega

The assembly is REQUIRED — without it the plan cannot be checked.

# Examples
{examples}

# Input
{theorem}
"""


INITIAL_PLANNING_EXAMPLES = """\
Input:
theorem singapore2019_r1_p7 (x : ℝ) (hx : Real.tan x = 5) :
  (6 + Real.sin (2 * x)) / (1 + Real.cos (2 * x)) = 83 := by
Output:
have h1 : Real.sin x = 5 * Real.cos x
have h2 : Real.sin x ^ 2 = 25 * Real.cos x ^ 2
have h3 : 26 * Real.cos x ^ 2 = 1
have hsin2x_val : Real.sin (2 * x) = (5 : ℝ) / (13 : ℝ)
have hcos2x_val : Real.cos (2 * x) = -(12 : ℝ) / (13 : ℝ)
ASSEMBLY: rw [hsin2x_val, hcos2x_val]; norm_num
"""


REPLAN_TEMPLATE = """\
# Task
Given the theorem, the already-proven subgoals, and the currently stuck subgoal,
**replan the remaining proof** by either correcting the stuck subgoal or breaking
it into smaller intermediate `have` statements. The plan must keep all proven
subgoals in their original order and position, and only insert new `have`
statements **immediately after** them.

# Mandatory Rules
Same 16 rules as the Initial Planning prompt. Additionally:

17. **Insert After Proven Steps**: keep proven subgoals exactly as given, in
    order, with no edits inside them.
18. **Output the Whole Plan**: emit ALL `have` statements (proven + new), not
    only the new ones.
19. **Logical Continuity**: do not immediately repeat the stuck subgoal — insert
    new intermediates between the proven ones and the goal.

# Output Format
Same as Initial Planning: `have <name> : <type>` lines, no `:= by`, no commentary.

# Input
Theorem:
{theorem}

Proven Subgoals:
{proven_subgoals}

Stuck Subgoal:
{stuck_subgoal}
"""
