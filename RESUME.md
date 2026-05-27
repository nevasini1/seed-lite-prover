# Resume notes — Seed-Lite-Prover bring-up

Use this if you close the session and come back later. The plan lives at
`/Users/jalajupadhyay/.claude/plans/try-to-see-the-binary-cupcake.md`.

## What is running in the background

1. **Ollama BFS-Prover-V2-7B q8_0 pull** — detached via `nohup`, logging to
   `results/ollama_pull_bfs.log`. Resumable on retry. ETA ~27 h at the
   observed ~78 KB/s.

   Check progress:
   ```bash
   ollama list                                           # appears once complete
   tail -1 results/ollama_pull_bfs.log
   du -sh ~/.ollama/models                                # grows toward ~8.1 GB
   pgrep -afl "ollama pull"                               # is the puller alive?
   ```
   If it died, simply re-run — Ollama resumes from the existing chunks:
   ```bash
   nohup ollama pull zeyu-zheng/BFS-Prover-V2-7B:q8_0 \
       >> results/ollama_pull_bfs.log 2>&1 &
   disown
   ```

2. **Lean toolchain install** via `elan` — may still be downloading/extracting
   into `~/.elan/toolchains/`. To force a retry:
   ```bash
   rm -f ~/.elan/toolchains/leanprover--lean4---v4.29.lock
   export PATH="$HOME/.elan/bin:$PATH"
   elan toolchain install leanprover/lean4:stable
   ```

## Status checklist (tick as you go)

- [x] Disk cleanup (5.3 GB recovered)
- [x] Ollama installed and service running on :11434
- [x] elan installed (~/.elan/bin)
- [ ] Lean stable toolchain fully extracted (`lean --version` succeeds)
- [ ] BFS-Prover-V2-7B:q8_0 pulled (`ollama list` shows it)
- [ ] Kimina helper model available (Ollama or local GGUF via llama-server)
- [ ] `lean_project/` bootstrapped with Mathlib + LLMLean
- [ ] `lake build` succeeds
- [ ] LLMLean canary file compiles
- [ ] First baseline (`scripts/run_ablation.py --variants A --benchmark toy --n 3`) runs

## Order of operations after the model finishes downloading

```bash
cd /Users/jalajupadhyay/Documents/okay123
export PATH="$HOME/.elan/bin:$PATH"

# 1. Sanity-check Lean
lean --version
lake --version

# 2. Bootstrap the Lean project (one-time)
cd lean_project
lake new . math              # OR: rm files first and use `lake new lean_project math` one dir up
# Add the LLMlean dep to lakefile.toml (see lean_project/lakefile.toml comments)
lake update
lake exe cache get           # avoids hours of Mathlib compilation
lake build

# 3. Wire LLMLean config
mkdir -p ~/.config/llmlean
cp ../configs/llmlean.toml ~/.config/llmlean/config.toml

# 4. Smoke tests
ollama run zeyu-zheng/BFS-Prover-V2-7B:q8_0 "hello"   # responds
lake env lean LeanProject/Probe.lean                  # compiles

# 5. Tiny baseline run
cd ..
python scripts/run_ablation.py --variants A --benchmark toy --n 3

# 6. Real benchmark (only after smoke + baseline OK)
./scripts/fetch_minif2f.sh
python scripts/run_ablation.py --variants A,B,C,D,E,F \
    --benchmark minif2f_valid --n 50
python scripts/score.py results/ablation_*.jsonl > docs/FINDINGS.md
```

## Disk budget

Approximate sizes after everything is installed:

| Item | Size |
|---|---|
| `~/.ollama/models` (BFS q8 + Kimina 1.7B) | ~10 GB |
| `~/.elan/toolchains/leanprover--lean4---v4.29` | ~700 MB |
| `lean_project/.lake/` (Mathlib + LLMLean, cached) | ~5–8 GB |
| Python venv (if any) + caches | ~500 MB |
| Total | ~16–19 GB |

Started with 22 GB free; expect ~3–6 GB free after full bring-up. If
disk gets tight, see `docs/FINDINGS.md` for what to prune first.
