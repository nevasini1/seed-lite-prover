# Seed-Lite-Prover

A Seed-Prover-1.5-inspired **test-time orchestration** layer for small open-weight Lean theorem provers, designed to run end-to-end on a 16 GB Apple Silicon Mac with **no GPU training**.

Claim: same small model, same MacBook, no fine-tuning — but a higher Lean-verified solve rate via search, retrieval, lemma decomposition, and error-aware repair.

## Stack

- **Primary prover**: `zeyu-zheng/BFS-Prover-V2-7B:q8_0` via Ollama (~8.1 GB, step-level Lean tactic generator)
- **Helper / sketch model**: `AI-MO/Kimina-Prover-RL-1.7B` (~1.8 GB, whole-proof + decomposition + repair)
- **Verifier**: Lean 4 + Mathlib (the ground truth — model proposes, Lean disposes)
- **Glue**: LLMLean (`llmstep`), Python orchestrator

## Pipeline (variants A–F ablation)

| Variant | Adds | Purpose |
|---|---|---|
| A | one-shot | floor baseline |
| B | + best-of-N sampling | sampling lift |
| C | + Lean-checked tactic search | structured search |
| D | + Mathlib retrieval | finds the right lemma names |
| E | + Seed-style lemma decomposition | handles longer proofs |
| F | + Lean-error repair loop | full system |

See `/Users/jalajupadhyay/.claude/plans/try-to-see-the-binary-cupcake.md` for the full plan and `docs/FINDINGS.md` for the running results dump.

## Reproduce

```bash
# 1. Install (one-time)
brew install ollama && brew services start ollama
ollama pull zeyu-zheng/BFS-Prover-V2-7B:q8_0
curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh

# 2. Lean project
cd lean_project && lake exe cache get && lake build

# 3. Run ablation
python scripts/run_ablation.py --variants A,B,C,D,E,F --benchmark minif2f_valid
python scripts/score.py results/ablation_*.jsonl
```

## Layout

```
seed_lite_prover/   orchestration package
lean_project/       Lean 4 + Mathlib + LLMLean
benchmarks/         MiniF2F-valid + held-out Mathlib + private theorems
results/            per-run JSONL output
configs/            llmlean.toml + ablation matrix
scripts/            run_ablation.py, score.py
docs/FINDINGS.md    running findings (versions, pass rates, surprises)
```
