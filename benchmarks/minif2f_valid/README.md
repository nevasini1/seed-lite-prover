# MiniF2F-validation slice

This directory holds the validation slice of [MiniF2F](https://github.com/openai/miniF2F)
(or the Lean 4 port, `yangky11/miniF2F-lean4`). License inherited from upstream.

Each problem is one `.lean` file containing a single `theorem <name> <stmt> := by sorry`.
The runner parses the first `theorem` line and extracts `<stmt>` as the body to attempt.

We do **not** vendor MiniF2F here automatically; run `scripts/fetch_minif2f.sh`
to clone the upstream repo (~50 MB) and copy the validation problems in.

For development / smoke tests in advance of the real fetch, see
`../toy/` for tiny self-contained problems that exercise the pipeline.
