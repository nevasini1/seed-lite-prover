-- Verify BFS's actual output from the smoke test compiles.
import Mathlib

theorem bfs_proof_test (a b : Nat) : a + b = b + a := by
  induction a with
  | zero => simp [Nat.zero_add, Nat.add_zero]
  | succ n ih =>
    rw [Nat.succ_add, Nat.add_succ, ih]
