# Findings — running log

Populated as Seed-Lite-Prover is built and benchmarked. Newest entries first.

---

## 2026-05-24 — Phase 0 install (overnight)

### System

- Host: Apple M3, 16 GB RAM, arm64, macOS 15.3.2 (Build 24D81)
- Free disk before cleanup: 19–22 GB (fluctuated)
- Old `~/.ollama/models` from Sep 2025 contained a llama3.1 8B q5_K_M blob (5.3 GB) — removed
- Free disk after cleanup: 24 GB
- **Network**: ~67–78 KB/s sustained on Ollama CDN; ~2–21 KB/s on github.com; 100–180 ms ping. Constrained link.

### Versions

| Component | Version | Source |
|---|---|---|
| Ollama | 0.24.0 | `brew install ollama` |
| elan | 4.2.1 (2026-03-18) | `elan-init.sh` |
| Lean toolchain | leanprover/lean4:stable (4.29.1, downloading) | elan |
| Mathlib | (TBD, pinned via lake) | lake update |
| LLMLean | (TBD, latest main) | lake require |

### Models

- `zeyu-zheng/BFS-Prover-V2-7B:q8_0` — **detached pull running overnight**, log at `results/ollama_pull_bfs.log`. ~500 MB / 8.1 GB at session pause; ETA ~27–31 h at observed throughput.
- `AI-MO/Kimina-Prover-RL-1.7B` — pull deferred until BFS finishes; will fall back to llama.cpp GGUF if not on Ollama registry.

### Surprises so far

- `~/.ollama/models` retained 5.3 GB of blobs even though the `ollama` CLI was uninstalled. Worth checking on future fresh-Ollama setups so disk doesn't quietly grow.
- The stable Lean toolchain (4.29.1) auto-downloads on first `lean --version` invocation under elan; harmless but adds a few minutes to first-use latency.
- elan's toolchain lockfile (`~/.elan/toolchains/leanprover--lean4---v4.29.lock`) is a stale PID file: if elan is killed mid-install the lock is **not** removed and the next install hangs forever waiting on a dead PID. Manual `rm` is needed before retry.
- Original plan estimate (~30 min total install) was off by 1–2 orders of magnitude on this network — bandwidth dominated everything. Lesson for future no-GPU plans on constrained links: bake an explicit network-speed check into Phase 0 step 1 and warn early.

### What was completed offline (Python only)

- Full `seed_lite_prover/` package: orchestrator, tactic search (variant C), retrieval (D), decomposition (E), repair (F), Ollama client, Lean runner, lemma cache.
- `scripts/run_ablation.py`, `scripts/score.py`, `scripts/fetch_minif2f.sh`.
- `configs/ablation_matrix.yaml`, `configs/llmlean.toml`.
- Toy benchmark (3 problems) under `benchmarks/toy/` for smoke-testing once Lean + a model are live.
- All modules import cleanly; config and benchmark loaders verified.

### What is queued for after downloads finish (see `RESUME.md`)

1. Verify `lean --version` succeeds → toolchain extracted.
2. `lake new lean_project math` → add LLMLean dep → `lake exe cache get` → `lake build`.
3. Copy `configs/llmlean.toml` to `~/.config/llmlean/config.toml`.
4. Smoke tests: `ollama run BFS "hi"`, `lake env lean Probe.lean`, LLMLean canary.
5. `python scripts/run_ablation.py --variants A --benchmark toy --n 3` → sanity baseline.
6. Fetch MiniF2F-valid → run A–F ablation → score → append to this file.

---

## 2026-05-25 — Phase 0e/0f surprises

### Mathlib cache fetch went unexpectedly well

Despite the slow link, `lake exe cache get` for Mathlib v4.29.1 (8232 .olean files) finished in a few minutes — burst speeds up to ~2 MB/s on the Azure CDN that the cache uses. The model pulls clearly suffered worse routing than the cache did.

### `lake init <name> <template>` does NOT create a subdir

It treats the first arg as the project name and writes files into the **current** directory. To get them in `lean_project/`, either `cd lean_project && lake init . math` OR run `lake new lean_project math` from one level up. We hit the wrong behavior and had to manually move files (`.lake`, `.gitignore`, `LeanProject.lean`, `lakefile.toml`, etc.) into `lean_project/`.

### LLMLean: dropped from the plan

Our Python orchestrator drives Ollama directly via HTTP, so the in-Lean `llmstep` tactic is not on the critical path. We removed `import LLMlean` from `lean_runner.py`'s preamble and stopped adding LLMLean as a lake dep. Saves a dependency build and one round of `lake update`. Re-add later only if we want interactive `llmstep` in Lean files.

### Kimina is a reasoning model — and it shows

Kimina-Prover-RL-1.7B (mradermacher GGUF, Q8_0) on `/api/generate` returns **empty content** and only fills `message.thinking` until token budget is exhausted. Even "Hello, who are you?" gets interpreted as a Lean theorem to prove. Fixes:

- Use `/api/chat`, which applies its `<|im_start|>` Qwen chat template.
- Budget at least `num_predict: 2048–4096` to leave room for thinking + content.
- Read both `message.content` and `message.thinking` from the response.

`OllamaClient.chat()` was added for this; `decompose.py` and `repair.py` were updated to use `chat=True` with 3072 tokens.

### BFS-Prover-V2-7B works as advertised

On the tactic-state prompt `theorem t (a b : Nat) : a + b = b + a := by`, BFS returned (24 s, q8_0 cold-load):

```
  induction a with
  | zero => simp [Nat.zero_add, Nat.add_zero]
  | succ n ih =>
    rw [succ_add, add_succ, ih]
```

After a tiny qualifier patch (`succ_add` → `Nat.succ_add`), the proof compiles in Lean. The model produced a correct Lean 4 induction proof, not a Lean 3 one — important.

### **The real bottleneck: `import Mathlib` cold-start**

Per-snippet `lake env lean` invocations cold-load Mathlib every time:

| Preamble | Wall time per check |
|---|---:|
| `import Mathlib` | **16 s** |
| `import Mathlib.Tactic` | **8 s** |

At 8 s/check, a 50-problem × 6-variant ablation (estimated ~2k Lean checks) is a **4-hour** run. With `import Mathlib`, it doubles. Concrete options to address:

1. **Switch to `import Mathlib.Tactic`** in `lean_runner.py`'s default preamble. Half the per-check cost; we lose access to specific named lemmas, but `simp`/`omega`/`linarith`/`aesop`/`decide` still work.
2. **Persistent Lean REPL** (the proper fix): keep one Lean process alive and feed it goals over stdin. Lean has `Lean.Elab.Frontend` for this; the LLMLean / LeanDojo / `repl` projects all do this. Big engineering — a few hundred lines.
3. **Project-internal cached preamble**: put `import Mathlib` in `LeanProject/Probe.lean`, let lake build it once, then per-check files do `import LeanProject.Probe` — should reuse the already-loaded `.olean`. Untested.

For tonight: option 1 is the pragmatic choice. Option 2 is the right long-term move.

### Smoke-test summary

| Component | State |
|---|---|
| BFS-Prover-V2-7B:q8_0 via /api/generate | ✓ produces Lean 4 proofs |
| Kimina-Prover-RL-1.7B:Q8_0 via /api/chat | ✓ (with thinking-aware client) |
| Lean 4.29.1 + Mathlib v4.29.1 + Aesop + Plausible | ✓ builds and compiles toy proofs |
| `seed_lite_prover` Python package (all 8 modules) | ✓ imports + minimal flow exercised |
| Per-snippet `lake env lean` performance | ✗ 8–16 s/check; needs a fix before full ablation |

---

## 2026-05-25 — Pilot ablation (run `ablation_20260525T013111`)

**Setup:** 10 problems × 3 variants (A, C, F), 240 s per-problem budget, `lake env lean` with each problem's full `import Mathlib` header preserved (no `Mathlib.Tactic` shortcut — MiniF2F problems open `BigOperators Real Nat Topology Rat`).

**Slice:** 10 smallest MiniF2F-validation problems by file size → 9 short modular-arithmetic / factorial-mod theorems + 1 IMO-tier (`imo_1964_p1_2`).

| Variant | Wrapper | Solved | Pass rate | Avg s/problem |
|---|---|---:|---:|---:|
| A | one-shot + symbolic preamble | 9 / 10 | 90% | 75.7 |
| C | + BFS tactic search | 9 / 10 | 90% | 114.0 |
| F | + decomposition + repair (full Seed-Lite) | 9 / 10 | 90% | 104.1 |

**Pass criterion (harness validation): met.** No crashes across 30 problem-variant runs; F's solved count ≥ A's. Per-attempt JSONL captured for every attempt.

**Real takeaway: the slice was too easy to expose any lift.** All 9 solved problems were closed by the **symbolic preamble alone** — every winning attempt has `source = symbolic:<tactic>` (e.g. `simp_all`, `norm_num`, `omega`, `nlinarith`, `ring_nf`). The model never even ran on the easy problems. The hard problem (`imo_1964_p1_2`) defeated all three variants identically.

What the symbolic preamble caught:

| Problem | Winning tactic |
|---|---|
| mathd_numbertheory_81  | simp_all |
| mathd_numbertheory_102 | simp_all |
| mathd_numbertheory_132 | simp_all |
| mathd_numbertheory_200 | simp_all |
| mathd_numbertheory_961 | simp_all |
| mathd_numbertheory_252 | norm_num |
| mathd_numbertheory_739 | norm_num |
| mathd_numbertheory_198 | omega |
| mathd_numbertheory_101 | nlinarith (A and F), ring_nf (C — tactic ordering nondeterminism) |

**Why this matters:** to see the orchestration variants earn their lift, the slice has to contain problems where the symbolic preamble fails BUT the symbolic-+-LLM combination succeeds. Modular-arithmetic problems are a degenerate case — they're either trivial to `decide` or genuinely hard.

**Next step (Phase G in the plan):** before re-running, swap the decompose / repair prompts for the BFS-Prover-V2 planner prompts (have-chain output, Dynamic Replanning). Then pick a different MiniF2F slice — algebra / analysis / inequalities — where the symbolic preamble has a real failure rate. Otherwise the ablation will keep producing flat tables.

**Process notes:**
- Variant A averaged 75.7 s/problem; the 9 easy ones averaged ~17 s and the IMO timeout pulled the mean up. Realistic per-easy-problem cost is ~10–40 s.
- Variants C and F spent extra wall-clock time on the IMO problem (658 s on C; F shorter because budget-capped) without buying anything.
- The 240 s problem budget is honored cleanly — no runaway searches.

---

## 2026-05-25 — Phase G pilot, medium slice (run `ablation_20260525T022840`)

**Setup:** 10 smallest `mathd_algebra_*` problems × variants A, C, F. 300 s nominal budget; `--lean-timeout 60`. First run with the BFS-Prover-V2-adapted prompts and the Dynamic-Replanning fallback in `repair.py`.

| Variant | Wrapper | Solved | Pass rate | Avg s/problem |
|---|---|---:|---:|---:|
| A | one-shot + symbolic preamble | 6 / 10 | 60% | 149.7 |
| C | + BFS tactic search | 5 / 10 | 50% | 379.8 |
| F | + decomposition + repair (BFS-Prover-V2 prompts) | 6 / 10 | 60% | 589.8 |

**Pass criterion (F's solved ≥ A's): tied, not strictly greater.** No crashes; harness is sound. But the data exposes three concrete things:

### 1. The BFS-Prover-V2 have-chain decomposition didn't win anything (yet)
Every winning attempt across all three variants still has `source = symbolic:<tactic>` — never `decompose:*` or `repair:*`. The new prompts run (Kimina returns plausible have-chains; the `parses_as_type` filter accepts some; the sub-prover tries each), but the assembled proofs don't compile. Hypotheses to test next:
- Have-types pass `parses_as_type` (which only checks that wrapping in `example … := by sorry` typechecks) but fail under the actual proof context (different `open`-d namespaces, implicit-binder mismatches, etc.). Add a stricter filter that wraps as `have <name> : <type> := by sorry` *inside* the parent theorem body.
- The sub-prover gets the symbolic preamble for each have, but most algebra subgoals need targeted tactics that BFS-Prover doesn't emit when prompted with just the goal state. Try giving the BFS-Prover prompt the parent goal as additional context.
- The defensive closing combinator (`first | exact ⟨…⟩ | constructor <;> assumption | simp_all | aesop | tauto | linarith [...] | omega`) is still probably wrong for non-trivial algebra — for `quadratic ≥ 4` the right glue is `nlinarith [sq_nonneg (x - 3)]`, which we don't try.

### 2. C regressed from A on `mathd_algebra_455` — 60 s Lean timeout is too tight
A solved it via `symbolic:nlinarith` in 253.6 s (so ~50–60 s on the nlinarith check itself). C's same `nlinarith` check apparently **timed out** (60 s wall) before returning a positive verdict. Result: C lost a win A had purely to stochastic timing. Fix: raise default `--lean-timeout` to 120 s (or even 180 s for `nlinarith`-heavy slices). Confidence: high, since both runs share code paths up to that point.

### 3. F took 2843 s (47 min) on `mathd_algebra_59`, violating the 300 s nominal budget
The `--problem-budget-s` knob in `scripts/run_ablation.py` is currently advisory — phases (symbolic preamble, whole-proof, BFS search, decomposition, repair) all have their own internal budgets and don't check the run-level deadline. The 47 min was Kimina chewing through 65 attempts (likely many `thinking`-heavy generations) across decomposition + repair.

Fix path: have `Orchestrator.prove` accept an absolute `deadline` and short-circuit every phase boundary on it. Plumb `args.problem_budget_s` → `deadline = time.time() + budget` → pass to orchestrator → check before each phase + before each sub-attempt within a phase.

### Process notes (this run)
- Models loaded both BFS (8.1 GB) and Kimina (2.2 GB) into the q8_0 + Q8_0 mix; RAM held up fine, no swap thrash observed.
- The BFS-Prover-V2-adapted prompt for decomposition lands ~3–6 have-signatures per call, most well-typed; the 16-rule prompt body did seem to push Kimina to annotate types more diligently (anecdotal — we'd need to score parse-rate to confirm).

### Concrete next actions before any further ablation
1. **Raise `--lean-timeout` default to 120 s** — `LeanRunner(..., timeout_s=120.0)` and the CLI default.
2. **Enforce a hard run-level deadline** in `Orchestrator.prove` — at every phase boundary, return what we have.
3. **Stricter decompose filter**: instead of `parses_as_type`, wrap the candidate have inside the parent theorem body (`theorem T … := by have <name> : <type> := by sorry; sorry`) and check that compiles. Catches namespace-context issues the current filter misses.

Phase G itself is *complete* in code; phase G's measurable lift is *not yet visible* — these three follow-ups are needed before re-running. Recommend treating them as a Phase G1 (≈ 1 h) before re-pilot.

---

## 2026-05-25 — Phase G1 re-pilot (run `ablation_20260525T082508`)

**Setup:** same 10 `mathd_algebra_*` problems × A,C,F. `--lean-timeout 180`, `--problem-budget-s 480`. Code: G1.1 (timeout 60→180s) + G1.2 (deadline-aware orchestrator) + G1.3 (`parses_in_parent` filter that wraps candidate `have`s inside the parent theorem body).

| Variant | G pilot | G1 pilot | Δ |
|---|---:|---:|---:|
| A | 6 / 10 | 7 / 10 | +1 |
| C | 5 / 10 | 7 / 10 | +2 |
| F | 6 / 10 | 7 / 10 | +1 |

**Lift source attribution.** All three variants now solve the **same 7 problems**: {10, 104, 182, 190, 410, 455, 462}. The new solve across the board is `mathd_algebra_410` (`y = x² - 6x + 13 ≥ 4`), and crucially the winning source is **`whole_proof`** — the BFS-Prover-V2-7B model produced a compiling proof. Previously, A's whole-proof attempt was timing out at the Lean 60 s cap; with the cap at 180 s and the deadline-aware orchestrator giving the phase room to run, the model's proof now compiles.

**Conclusion: the G1 lift came from G1.1 + G1.2 (the harness fixes), not from G1.3 or Phase G's prompt adoption.** The stricter decomposition filter and BFS-Prover-V2-adapted have-chain prompt are now correct in code, but on this slice they still produce **zero** winning attempts. Every variant F win has `source = symbolic:*` or `source = whole_proof` — never `decompose:*` or `repair:*`.

**The three unsolved problems are not decomposition targets at this model scale.** They need mathematical insight Kimina-1.7B doesn't produce:

| Problem | Statement | What it needs |
|---|---|---|
| `mathd_algebra_22` | `Real.logb (5^2) (5^4) = 2` | log identity reduction or `Real.logb_self_rpow` etc. — name not in our retrieval |
| `mathd_algebra_59` | `4^b + 8 = 12 → b = 1` | exponential equation → case analysis on `b`; needs `(4 : ℝ) ^ b = 4 = 4 ^ 1`, then injectivity of `rpow` |
| `mathd_algebra_151` | `⌈√27⌉ - ⌊√26⌋ = 1` | `⌈√27⌉ = 6 ∧ ⌊√26⌋ = 5` then arithmetic — Kimina's decomposition produced sketches that didn't typecheck inside the parent context |

**Process notes (G1 pilot):**
- 480 s problem budget honored across all 30 runs (max single-problem elapsed = 624 s, only slightly over because the phase-final repair finishes its current Lean call before checking the deadline; acceptable slop).
- Variant F's average time per problem dropped from 590 s (G pilot, runaway) to 307 s (G1, deadline-bounded).
- `mathd_algebra_410` whole-proof attempts: 13 attempts at varied temperatures, succeeded on attempt 8.

### Implications & next direction

The orchestration variants will **not** lift solve rate further on the small-helper-model path without one of:
1. **A bigger / better helper model.** Kimina-Distill-8B reports 77.86% Pass@32 on MiniF2F (vs RL-1.7B at 76.63%) — marginal at this scale. The real jump would be a 32B-class reasoning model, which doesn't fit the 16 GB Mac constraint.
2. **A much faster verifier so we can do many-shot decomposition.** Currently every candidate have-chain costs O(N_haves × 60–180 s) just to filter and try-prove. With sub-second checks (Phase F: Lean REPL), we could afford to try 10–100× more candidates per problem.
3. **A different benchmark slice that exposes decomposition lift.** Long multi-step proofs (`putnam_*`, `aime_*`) where the symbolic preamble can't possibly win, only assembled have-chains can. But the per-problem cost will be punishing without Phase F.

Path (2) is what's next: build the persistent Lean REPL and re-test.

---

## 2026-05-25 — Phase F: Persistent Lean REPL (run `ablation_20260525T112847`)

**Setup:** added `leanprover-community/repl` rev `v4.29.0-rc8` as a lake dep, built the binary, wrote `seed_lite_prover/lean_repl.py` (JSON stdio driver), made `LeanRunner` prefer the REPL with `subprocess` fallback. Warmup applies `import Mathlib` + the standard MiniF2F `open` once; per-check headers are stripped before sending (REPL rejects mid-session `import` lines).

**Same 10 medium problems × A,C,F, same configs as G1, only the backend changed:**

| Variant | G1 (subprocess) avg s | F (REPL) avg s | speedup |
|---|---:|---:|---:|
| A | 173.0 | 1.8 | **96×** |
| C | 305.0 | 2.9 | **105×** |
| F | 307.4 | 42.4 | **7×** |

Solve count: 7/10 across all variants in both runs — same outcome, but the 30-minute pilot is now a 90-second pilot. F is "only" 7× faster because its bottleneck shifted from Lean to Kimina's reasoning latency (decompose + repair are inherently model-bound and don't benefit from REPL).

Concrete REPL economics:
- Warmup (one-time): ~30 s (cold), ~8 s (warm OS cache).
- Steady-state per check: ~10 ms — 1 s depending on tactic (decide on big naturals is the slow tail).
- Memory: REPL process holds Mathlib resident; observed RSS ~3 GB. Plus models in Ollama. Mac handled it fine.

This unlocks the **full 244-problem × 6-variant matrix as a ~1-hour job** instead of an overnight one.

---

## 2026-05-25 — Phase H: Best-first search via BFS-Prover-V2 proof tree (run `ablation_20260525T114118`)

**Setup:** ported `ByteDance-Seed/BFS-Prover-V2/src/search/proof_tree.py` (stripped of LeanDojo deps) into `seed_lite_prover/proof_tree.py`. Rewrote `tactic_search.bfs_prove` to use a priority queue ordered by a synthetic logprob proxy (greedy samples sort above higher-temperature siblings). Confidence proxy: edge logprob `= -i × 1.0 - temperature × 0.5` where `i` is the sample index. Reran the same medium slice × A,C,F.

| Variant | Solved | Avg s/problem | Median attempts-to-solve |
|---|---:|---:|---:|
| A | 7 / 10 | 1.8 | 4 |
| C | 7 / 10 | 2.8 | 4 |
| F | 7 / 10 | 58.2 | 4 |

**Pass criterion (C ≥ B, and median checks/solve drops ≥ 20%): partially met.**
- C ≥ B (functionally A here): tied at 7/10. ✓
- Median checks-to-first-solve dropped: not visible — every winning attempt came from the symbolic preamble (within the first 12 attempts before tactic search even runs), so best-first search never expanded a single node to a winning state. ✗

**The honest read.** On this 10-problem `mathd_algebra_*` slice, the symbolic preamble + single-shot whole-proof attempt is *already* strong enough to capture every solvable problem at this difficulty. The three unsolved problems (logb identity, exponential equation, ceiling/floor of sqrt) need multi-step mathematical reasoning that neither plain BFS nor best-first BFS produces from BFS-Prover-V2-7B at q8. Adopting better search structure correctly was a no-op on solve rate because the search branch isn't where the bottleneck lives on this slice.

**Where best-first search *would* show lift** is a slice that satisfies all three:
1. Symbolic preamble fails.
2. Single-shot whole-proof from BFS-Prover fails.
3. But a 3–5 tactic sequence exists that the model can produce one step at a time.

Olympiad-style algebraic-identity slices (e.g. `amc12*`, `algebra_*` involving named Mathlib lemmas) and `induction_*` problems are the most promising candidates. Pulling 10 random `induction_*` problems and rerunning is the next experiment if we choose to keep going.

**Net result of the session.** The end-to-end system now exists, with the right architecture and the right speed profile:
- BFS-Prover-V2-7B q8_0 + Kimina-Prover-RL-1.7B Q8_0 running locally via Ollama.
- Lean 4.29.1 + Mathlib via a persistent REPL — sub-second per check after a 30 s warmup.
- Variants A through F + the best-first proof tree from BFS-Prover-V2.
- BFS-Prover-V2's adapted planner prompts wired into decomposition + Dynamic Replanning repair.
- Per-attempt JSONL traces so any future ablation is auditable.

The remaining question — "does this orchestration beat the symbolic preamble?" — needs a harder slice to be answered honestly. As of 2026-05-25 the answer on `mathd_algebra_*` is "no observable lift"; on a different slice (induction or olympiad-tier algebra) it might be different.

---

## Template for a benchmark run entry

```
## YYYY-MM-DD HH:MM — ablation run <id>

Hardware/config: ...
Benchmark: minif2f_valid (n=50)
Per-problem budget: 600 s
Variant overrides: ...

| Variant | Pass rate | Avg s | Notes |
|---|---:|---:|---|
| A | x% | y | ... |
...

Per-problem highlights:
- problem_name — variant F solved via decomposition + repair where C/D timed out.
- ...

Dead ends:
- ...
```

---

## 2026-05-25 — Mitigating 0/0/0 on the induction slice

**Trigger**: prior Phase J pilot returned A=0/8, C=0/8, F=0/3 (F OOM-crashed) on `minif2f_induction`. User asked: "find out how to mitigate 0 for A,C,F".

### Diagnosis

Inspected the per-attempt JSONL traces (`results/attempts_*.jsonl`). Two recurring failure shapes:

1. **BFS-Prover-V2 produces a correct induction *skeleton*** in `whole_proof` but consistently fails to close the **base case**. Example for `induction_sum_odd`:
   ```
   proof: induction' n with nn nih
            simp [Finset.sum_range_succ]
            rw [nih]
   error: unsolved goals  case zero
          ⊢ ∑ k ∈ Finset.range 0, (2 * k + 1) = 0 ^ 2
   ```
   The model is one tactic away — `case zero => simp` would close it. Repair would normally fix this in variant F, but F's repair has been OOM-killed on every run.

2. **Symbolic preamble had no induction-aware combinator.** A slice literally named "induction" with no `induction n` in our 11-tactic preamble is the same as not trying.

3. **Logging bug**: symbolic attempts didn't propagate `LeanResult.stderr` into `ProofAttempt.error`, so `error_head` was empty in the per-attempt JSONL for everything except `whole_proof`. Hard to debug what failed. **Fixed** in `orchestrator.py`.

### Fix applied

Added induction-aware symbolic combinators to `SYMBOLIC_PREAMBLE_TACTICS`:

```python
"induction n with | zero => decide | succ n ih => simp_all",
"induction n with | zero => decide | succ n ih => omega",
"induction n with | zero => simp | succ n ih => rw [Finset.sum_range_succ, ih]; ring",
"induction n with | zero => simp | succ n ih => rw [Finset.sum_range_succ, ih]; field_simp; ring",
"induction n with | zero => simp | succ n ih => rw [Finset.sum_range_succ, ih]; omega",
"induction n with | zero => simp | succ n ih => rw [Finset.sum_range_succ, ih]; (first | ring | omega | linarith | (field_simp; ring))",
"induction n <;> (first | rfl | decide | norm_num | simp_all | omega | linarith | ring_nf)",
```

The `rw [Finset.sum_range_succ, ih]; ring` combinator was hand-verified against `induction_sum_odd` in an isolated Lean compile before being added — `simp [..., Nat.succ_eq_add_one]; ring` (the earlier guess) does not close, but `rw [..., ih]; ring` does.

### Result

| Variant | Before | After | Notes |
|---|---|---|---|
| A on `minif2f_induction` × 8 | 0/8 | **2/8** | `induction_sum_odd` (via new symbolic combinator, 2.3 s); `induction_divisibility_9div10tonm1` (whole_proof, 7.4 s — appears non-deterministically) |
| C | 0/8 | (not retested; inherits A's preamble — expected ≥ 2/8) | |
| F | 0/3 (crashed) | (not retested; same inheritance) | |

### Remaining 0s — diagnosis for next iteration

- `induction_sum_1oktkp1`, `induction_sum2kp1npqsqm1` — same shape as `sum_odd` but the closing tactic (`ring` / `field_simp; ring` / `omega`) doesn't close. Next idea: try `nlinarith` and `push_cast; ring` variants.
- `induction_divisibility_3divnto3m2n`, `induction_divisibility_3div2tooddnp1` — divisibility induction requires `Nat.dvd_add` style manipulation; no canned tactic likely to close. Real lift here needs either retrieval (variant D with the now-built lemma index) or repair on whole_proof near-misses.
- `induction_ineq_nsqlefactn` (`n^2 ≤ n!`) — inequality + factorial; needs `Nat.factorial_le` lemmas. Variant D.
- `induction_seq_mul2pnp1` — recursive sequence; harder.

### Architectural next step (not done this iteration)

The single biggest lift available would be **cheap repair on whole_proof near-misses in variant A**: parse the Lean error for `unsolved goals case <name>`, prepend `case <name> => <battery>` and re-check. No LLM call, pure mechanical fix-up. Estimated 30 lines of Python. Would likely close another 2–3 induction problems.

