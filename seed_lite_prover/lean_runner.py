"""Lean verifier facade.

Two backends:

1. **REPL** (preferred) — a persistent `repl` process from
   `leanprover-community/repl`, primed with `import Mathlib` once. Per-check
   latency is sub-millisecond after the ~30 s warmup. Built via
   `lake build repl` in `lean_project/`.

2. **Subprocess** (fallback) — cold `lake env lean <file.lean>` per check.
   8–16 s per call due to Mathlib import. Used only if the REPL binary
   isn't available (e.g. before `lake build repl` has been run).

The orchestrator and search modules only ever see `LeanRunner.check()`.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LeanResult:
    ok: bool
    stdout: str
    stderr: str
    elapsed_s: float


class LeanRunner:
    """Compiles a Lean source snippet against the project's pre-built Mathlib.

    Prefers a persistent REPL process if available; otherwise falls back to
    cold `lake env lean` invocations. Toggle with `use_repl=False` to force
    the slow path (useful for benchmarking).
    """

    def __init__(
        self,
        project_dir: str | Path,
        timeout_s: float = 180.0,
        use_repl: bool = True,
        warmup_preamble: str = "import Mathlib\nopen BigOperators Real Nat Topology Rat\n",
    ):
        self.project_dir = Path(project_dir).resolve()
        self.timeout_s = timeout_s
        if not (self.project_dir / "lakefile.lean").exists() and not (self.project_dir / "lakefile.toml").exists():
            raise FileNotFoundError(f"no lakefile in {self.project_dir}")

        self._repl = None
        if use_repl:
            try:
                from .lean_repl import LeanRepl
                self._repl = LeanRepl(self.project_dir, timeout_s=timeout_s)
                self._repl.warmup(warmup_preamble)
            except FileNotFoundError:
                # REPL binary not built — silently fall back
                self._repl = None
            except Exception as e:
                # REPL failed to spawn or warm up — fall back, but warn
                import sys
                print(f"[LeanRunner] REPL warmup failed ({e!r}); falling back to subprocess mode", file=sys.stderr)
                self._repl = None

    @property
    def backend(self) -> str:
        return "repl" if self._repl is not None else "subprocess"

    @property
    def has_repl(self) -> bool:
        return self._repl is not None

    def start_proof(self, theorem_decl: str):
        """Façade for state-aware proof search. Returns a ProofState
        (REPL backend only). Raises RuntimeError in subprocess fallback."""
        if self._repl is None:
            raise RuntimeError("state-aware proof search requires REPL backend")
        return self._repl.start_proof(theorem_decl)

    def apply_tactic(self, proof_state: int, tactic: str):
        if self._repl is None:
            raise RuntimeError("state-aware proof search requires REPL backend")
        return self._repl.apply_tactic(proof_state, tactic)

    def check(self, lean_source: str, preamble: str = "") -> LeanResult:
        """Strict acceptance check: `ok=True` requires zero errors, zero
        `sorry`/`admit` placeholders in the source, and zero sorries in the
        REPL response. Use this for ANY candidate proof you'd accept as a
        Lean-verified solve. For parse-only shape validation use
        `check_parses` instead."""
        # In REPL mode the preamble (imports + opens) was applied at warmup.
        # In subprocess mode we still allow per-check preamble (kept "" by
        # default; MiniF2F problems carry their own header in lean_source).
        if self._repl is not None:
            return self._repl.check(lean_source)
        return self._subprocess_check(lean_source, preamble)

    def check_parses(self, lean_source: str, preamble: str = "") -> bool:
        """Lenient parse-only check: returns True if `lean_source` typechecks
        with no Lean errors, IGNORING the presence of `sorry`/`admit`. Use
        this for shape validation (does this statement type-check?) — NEVER
        for accepting a candidate proof.
        """
        if self._repl is not None:
            return self._repl.check_parses(lean_source)
        # Subprocess: same as strict check but ignore the sorry-rejection.
        import os, re as _re
        full = preamble + lean_source
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lean", dir=self.project_dir, delete=False,
        ) as f:
            f.write(full)
            path = Path(f.name)
        env = os.environ.copy()
        elan_bin = str(Path.home() / ".elan" / "bin")
        if elan_bin not in env.get("PATH", ""):
            env["PATH"] = elan_bin + os.pathsep + env.get("PATH", "")
        try:
            proc = subprocess.run(
                ["lake", "env", "lean", str(path)],
                cwd=self.project_dir, capture_output=True, text=True,
                timeout=self.timeout_s, env=env,
            )
            return proc.returncode == 0 and "error:" not in proc.stderr.lower()
        except subprocess.TimeoutExpired:
            return False
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def close(self) -> None:
        if self._repl is not None:
            self._repl.close()
            self._repl = None

    def _subprocess_check(self, lean_source: str, preamble: str) -> LeanResult:
        import os
        full = preamble + lean_source
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lean",
            dir=self.project_dir,
            delete=False,
        ) as f:
            f.write(full)
            path = Path(f.name)
        env = os.environ.copy()
        elan_bin = str(Path.home() / ".elan" / "bin")
        if elan_bin not in env.get("PATH", ""):
            env["PATH"] = elan_bin + os.pathsep + env.get("PATH", "")
        start = time.time()
        try:
            proc = subprocess.run(
                ["lake", "env", "lean", str(path)],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=env,
            )
            stderr = proc.stderr
            # SOUNDNESS: a proof containing `sorry` or `admit` is NOT valid.
            # `lake env lean` emits a *warning* ("declaration uses 'sorry'")
            # but exits 0; without these checks `ok` would be True. Reject
            # both the source-level keyword and Lean's own warning text.
            import re as _re
            source_has_sorry = bool(_re.search(r"\b(sorry|admit)\b", lean_source))
            warns_sorry = "uses 'sorry'" in (stderr.lower() + " " + proc.stdout.lower())
            ok = (
                proc.returncode == 0
                and "error:" not in stderr.lower()
                and not source_has_sorry
                and not warns_sorry
            )
            if source_has_sorry or warns_sorry:
                stderr = "error: proof contains `sorry`/`admit`\n" + stderr
            return LeanResult(ok=ok, stdout=proc.stdout, stderr=stderr, elapsed_s=time.time() - start)
        except subprocess.TimeoutExpired as e:
            return LeanResult(
                ok=False,
                stdout=(e.stdout or b"").decode("utf-8", errors="replace"),
                stderr=f"timeout after {self.timeout_s}s",
                elapsed_s=time.time() - start,
            )
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
