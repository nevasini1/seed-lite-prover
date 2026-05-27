#!/usr/bin/env python3
"""Run one or more ablation variants over a benchmark slice.

Usage:
    python scripts/run_ablation.py --variants A,B,F --benchmark minif2f_valid --n 50

Writes two JSONL files under results/:
    ablation_<ts>.jsonl   one row per (variant, problem) summary
    attempts_<ts>.jsonl   one row per attempt (used by score.py for attribution)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seed_lite_prover.lean_runner import LeanRunner
from seed_lite_prover.lean_snippets import LeanProblem, parse_file
from seed_lite_prover.memory import LemmaCache
from seed_lite_prover.ollama_client import OllamaClient
from seed_lite_prover.orchestrator import Orchestrator, Variant


def load_variants(path: Path) -> tuple[dict[str, Variant], dict[str, str]]:
    cfg = yaml.safe_load(path.read_text())
    variants = {}
    for key, v in cfg["variants"].items():
        kwargs = {k2: v2 for k2, v2 in v.items() if k2 != "name"}
        variants[key] = Variant(name=v["name"], **kwargs)
    return variants, cfg["models"]


def load_benchmark(bench_dir: Path) -> list[LeanProblem]:
    """Load every .lean file in bench_dir that matches the MiniF2F shape."""
    out: list[LeanProblem] = []
    for p in sorted(bench_dir.glob("*.lean")):
        prob = parse_file(p)
        if prob is not None:
            out.append(prob)
    return out


def load_statements_jsonl(jsonl_path: Path, header: str) -> list[LeanProblem]:
    """Load problems from a BFS-Prover-V2-style statements JSONL.

    Each line: {"name": str, "statement": str, ...}. The supplied `header`
    is reused verbatim for every problem (BFS-Prover-V2's MiniF2F dump
    assumes the standard MiniF2F header — `import Mathlib`, `set_option
    maxHeartbeats 0`, `open BigOperators Real Nat Topology Rat`).
    """
    out: list[LeanProblem] = []
    for line in Path(jsonl_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        name = obj.get("name") or obj.get("theorem_name")
        stmt = obj.get("statement") or obj.get("formal_statement")
        if not name or not stmt:
            continue
        stmt = stmt.strip()
        # Drop a leading `theorem <name> ` and trailing `:= by sorry` if present.
        if stmt.startswith("theorem "):
            tail = stmt[len("theorem "):]
            sp = tail.find(" ")
            if sp > 0:
                tail = tail[sp + 1:]
            stmt = tail
        if ":=" in stmt:
            stmt = stmt.split(":=", 1)[0].rstrip()
        out.append(LeanProblem(
            path=Path(jsonl_path),
            header=header,
            keyword="theorem",
            name=name,
            statement=stmt,
        ))
    return out


_DEFAULT_MINIF2F_HEADER = """\
import Mathlib
set_option maxHeartbeats 0
open BigOperators Real Nat Topology Rat
"""


def _attempt_record(variant_key: str, variant_name: str, problem: LeanProblem, idx: int, a) -> dict:
    err_head = (a.error or "").splitlines()[:3]
    return {
        "variant": variant_key,
        "variant_name": variant_name,
        "problem": problem.name,
        "idx": idx,
        "source": a.source,
        "proof_head": "\n".join(a.proof.splitlines()[:6]),
        "ok": a.ok,
        "error_head": "\n".join(err_head),
        "elapsed_s": a.elapsed_s,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variants", default="A")
    parser.add_argument("--benchmark", default="minif2f_valid")
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--lean-project", default=str(ROOT / "lean_project"))
    parser.add_argument("--matrix", default=str(ROOT / "configs" / "ablation_matrix.yaml"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--lean-timeout", type=float, default=180.0)
    parser.add_argument("--problem-budget-s", type=float, default=600.0)
    parser.add_argument(
        "--statements-jsonl",
        default=None,
        help="Optional BFS-Prover-V2-style statements JSONL; takes precedence over --benchmark.",
    )
    args = parser.parse_args()

    variants, models = load_variants(Path(args.matrix))
    keys = [k.strip() for k in args.variants.split(",") if k.strip()]
    for k in keys:
        if k not in variants:
            print(f"unknown variant: {k} (have {list(variants)})", file=sys.stderr)
            return 2

    if args.statements_jsonl:
        problems = load_statements_jsonl(Path(args.statements_jsonl), _DEFAULT_MINIF2F_HEADER)[: args.n]
        bench_label = f"jsonl:{Path(args.statements_jsonl).name}"
    else:
        bench_dir = ROOT / "benchmarks" / args.benchmark
        problems = load_benchmark(bench_dir)[: args.n]
        bench_label = args.benchmark
    if not problems:
        print(f"no problems found ({bench_label})", file=sys.stderr)
        return 2

    ts = time.strftime("%Y%m%dT%H%M%S")
    summary_path = Path(args.out) if args.out else (ROOT / "results" / f"ablation_{ts}.jsonl")
    attempts_path = summary_path.with_name(summary_path.name.replace("ablation_", "attempts_"))
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    lean = LeanRunner(args.lean_project, timeout_s=args.lean_timeout)
    cache = LemmaCache(ROOT / "results" / "verified_lemmas.jsonl")
    ollama = OllamaClient()

    with summary_path.open("a") as sf, attempts_path.open("a") as af:
        for vk in keys:
            variant = variants[vk]
            orc = Orchestrator(
                variant=variant,
                prover_model=models["prover"],
                helper_model=models["helper"],
                lean=lean,
                cache=cache,
                ollama=ollama,
            )
            for problem in problems:
                deadline = time.time() + args.problem_budget_s
                t_problem_start = time.time()
                try:
                    res = orc.prove(problem, deadline=deadline)
                    record = {
                        "variant": vk,
                        "variant_name": variant.name,
                        "problem": problem.name,
                        "statement": problem.statement,
                        "solved": res.solved,
                        "attempts": len(res.attempts),
                        "elapsed_s": res.total_elapsed_s,
                        "winning_source": (
                            res.attempts[res.winning_attempt_idx].source
                            if res.solved and res.winning_attempt_idx >= 0
                            else ""
                        ),
                    }
                    for i, a in enumerate(res.attempts):
                        af.write(json.dumps(_attempt_record(vk, variant.name, problem, i, a)) + "\n")
                    af.flush()
                except Exception as e:
                    record = {
                        "variant": vk,
                        "variant_name": variant.name,
                        "problem": problem.name,
                        "statement": problem.statement,
                        "solved": False,
                        "error": f"{type(e).__name__}: {e}",
                        "elapsed_s": time.time() - t_problem_start,
                    }
                sf.write(json.dumps(record) + "\n")
                sf.flush()
                status = "OK" if record.get("solved") else "--"
                print(f"[{vk}] {status} {problem.name} ({record.get('elapsed_s', 0):.1f}s, {record.get('attempts', '?')} attempts)", flush=True)
                # honour problem budget across attempts; the orchestrator does
                # its own per-step budgeting too, so this is a hard ceiling.
                if time.time() > deadline:
                    pass  # already over budget; orchestrator returned what it had

    print(f"wrote {summary_path}")
    print(f"wrote {attempts_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
