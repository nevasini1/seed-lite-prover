-- Smoke test: must compile via `lake env lean LeanProject/Probe.lean`.
-- Confirms Mathlib is available and ‹by tactic› proofs close.
import Mathlib

example (a b : Nat) : a + b = b + a := by
  exact Nat.add_comm a b

example (n : Nat) : n * 0 = 0 := by
  exact Nat.mul_zero n

example : (2 : Nat) ≤ 3 := by
  decide
