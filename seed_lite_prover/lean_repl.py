"""Persistent Lean REPL driver.

Wraps a long-lived `repl` process (leanprover-community/repl). Loads
Mathlib once into the REPL's environment, then reuses that env for every
subsequent `check()` so we pay the import cost a single time instead of
on every `lake env lean` cold-start.

Protocol: JSON-on-stdin, JSON-on-stdout, one request per line, blank
line between requests.

Public surface mirrors `LeanRunner.check()` so the orchestrator can swap
between the two via the same `check(snippet) -> LeanResult` API.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_HEADER_LINE = re.compile(r"^\s*(?:import\s|set_option\s|open\s|namespace\s|universe\s)")


def _strip_file_header(source: str) -> str:
    """Drop file-header lines (import / set_option / open / namespace /
    universe) until we hit the first non-header, non-blank line. The REPL
    rejects `import` mid-session and any `open` / `set_option` we strip
    here is assumed to have been applied at warmup."""
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or _HEADER_LINE.match(lines[i]):
            i += 1
            continue
        break
    return "\n".join(lines[i:])


@dataclass
class _ReplResult:
    """Internal: one REPL response."""
    env: int | None
    messages: list[dict[str, Any]]
    sorries: list[dict[str, Any]]
    raw: dict[str, Any]


@dataclass
class ProofState:
    """An open Lean tactic state inside the REPL. `goals` is the human-
    readable rendering REPL returns; `id` is the integer handle to use in
    follow-up `{tactic, proofState}` calls. `done` means no goals remain."""
    id: int | None
    goals: str
    done: bool
    messages: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """No `error` messages and (either done or has a state id)."""
        if any(m.get("severity") == "error" for m in self.messages):
            return False
        return self.done or self.id is not None


@dataclass
class LeanResult:
    """Same shape as lean_runner.LeanResult so callers can be agnostic."""
    ok: bool
    stdout: str
    stderr: str
    elapsed_s: float


class LeanRepl:
    """Persistent Lean REPL over JSON / stdio.

    Usage:

        repl = LeanRepl("/path/to/lean_project", repl_bin="/path/to/repl")
        repl.warmup("import Mathlib\\nopen BigOperators Real Nat Topology Rat\\n")
        result = repl.check("theorem t : 1 + 1 = 2 := by decide")
        repl.close()

    Or as a context manager:

        with LeanRepl("/path/to/lean_project") as repl:
            repl.warmup(...)
            result = repl.check(...)
    """

    def __init__(
        self,
        project_dir: str | Path,
        repl_bin: str | Path | None = None,
        timeout_s: float = 180.0,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.timeout_s = timeout_s
        # If not given, look for a repl binary at the standard build location
        # in the user's lean_project: `.lake/packages/repl/.lake/build/bin/repl`.
        if repl_bin is None:
            candidate = self.project_dir / ".lake" / "packages" / "repl" / ".lake" / "build" / "bin" / "repl"
            if not candidate.exists():
                raise FileNotFoundError(
                    f"repl binary not found at {candidate}; either pass repl_bin "
                    f"explicitly or `lake build` the repl dep in {self.project_dir}"
                )
            self.repl_bin = candidate
        else:
            self.repl_bin = Path(repl_bin).resolve()

        # Spawn `lake env` so the REPL inherits Mathlib's elaborator + paths.
        # Ensure elan's bin is on PATH so `lake` is findable when launched
        # from a Python process that doesn't inherit the user's shell PATH.
        import os
        env = os.environ.copy()
        elan_bin = str(Path.home() / ".elan" / "bin")
        if elan_bin not in env.get("PATH", ""):
            env["PATH"] = elan_bin + os.pathsep + env.get("PATH", "")
        self.proc = subprocess.Popen(
            ["lake", "env", str(self.repl_bin)],
            cwd=self.project_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line buffered
            env=env,
        )
        self._warmup_env: int | None = None

    def __enter__(self) -> "LeanRepl":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- public API ----------------------------------------------------------

    def warmup(self, preamble: str) -> _ReplResult:
        """Submit a preamble (typically `import Mathlib` + `open ...`) to
        prime the REPL env. The resulting `env` id is reused on every
        subsequent `check()` so Mathlib only loads once per process."""
        res = self._send({"cmd": preamble})
        self._warmup_env = res.env
        return res

    def check(self, lean_source: str, preamble_env: int | None = None) -> LeanResult:
        """Compile `lean_source` against the REPL env (defaulting to the
        warmed-up env). Returns a LeanResult that mirrors LeanRunner's.

        The REPL rejects mid-session `import` / `set_option header` lines —
        these must appear at file start. Since warmup already established
        Mathlib + standard `open`s, we strip them before sending. If a
        problem needs an extra `open`, include it in the warmup preamble
        instead of the per-check source.
        """
        env = preamble_env if preamble_env is not None else self._warmup_env
        body = _strip_file_header(lean_source)
        req: dict[str, Any] = {"cmd": body}
        if env is not None:
            req["env"] = env
        t0 = time.time()
        try:
            res = self._send(req)
        except Exception as e:
            return LeanResult(ok=False, stdout="", stderr=f"repl: {e}", elapsed_s=time.time() - t0)
        elapsed = time.time() - t0
        errors = [m for m in res.messages if m.get("severity") == "error"]
        warnings = [m for m in res.messages if m.get("severity") == "warning"]
        ok = len(errors) == 0
        stdout = json.dumps(res.raw)
        stderr_parts = []
        for m in errors + warnings:
            sev = m.get("severity", "")
            msg = m.get("data", "")
            pos = m.get("pos", {})
            stderr_parts.append(f"{sev}: line {pos.get('line', '?')}: {msg}")
        # Mirror lean_runner's convention: "error:" in stderr means failure.
        stderr = "\n".join(stderr_parts)
        return LeanResult(ok=ok, stdout=stdout, stderr=stderr, elapsed_s=elapsed)

    def start_proof(self, theorem_decl: str) -> ProofState:
        """Submit `<theorem_decl> := by sorry` and return the initial proof
        state for the body. The decl must NOT include `import` lines
        (warmup already established Mathlib + opens).

        Example: theorem_decl = "theorem t (n : ℕ) : 3 ∣ n^3 + 2*n"
        We send: cmd = "<theorem_decl> := by sorry"
        and pull the proofState id from the response's `sorries` list.
        """
        cmd = f"{theorem_decl} := by sorry"
        body = _strip_file_header(cmd)
        req: dict[str, Any] = {"cmd": body}
        if self._warmup_env is not None:
            req["env"] = self._warmup_env
        try:
            res = self._send(req)
        except Exception as e:
            return ProofState(id=None, goals="", done=False, messages=[{"severity": "error", "data": str(e)}])
        # `sorries` contains one entry per `sorry` in the snippet; the first
        # one corresponds to our injected `:= by sorry`.
        if not res.sorries:
            # Either the theorem already typechecked (unlikely) or there's
            # a parse error before we got to `sorry`.
            return ProofState(id=None, goals="", done=False, messages=res.messages, raw=res.raw)
        sorry = res.sorries[0]
        return ProofState(
            id=sorry.get("proofState"),
            goals=sorry.get("goal", "") or "",
            done=False,
            messages=res.messages,
            raw=res.raw,
        )

    def apply_tactic(self, proof_state: int, tactic: str) -> ProofState:
        """Apply `tactic` to the open proof state, return the new state.
        `done` is True iff all goals are closed (proof finished)."""
        req = {"tactic": tactic, "proofState": proof_state}
        try:
            res = self._send(req)
        except Exception as e:
            return ProofState(id=None, goals="", done=False, messages=[{"severity": "error", "data": str(e)}])
        raw = res.raw
        new_id = raw.get("proofState")
        goals_field = raw.get("goals")
        if isinstance(goals_field, list):
            goals_text = "\n".join(goals_field)
        else:
            goals_text = goals_field or ""
        has_error = any(m.get("severity") == "error" for m in res.messages)
        done = (not has_error) and (goals_text.strip() == "" or goals_text.strip().lower() in ("no goals", "[]"))
        return ProofState(
            id=new_id if not done else None,
            goals=goals_text,
            done=done and not has_error,
            messages=res.messages,
            raw=raw,
        )

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    # -- internals -----------------------------------------------------------

    def _send(self, request: dict[str, Any]) -> _ReplResult:
        """Send one JSON request, read one JSON response."""
        if self.proc.poll() is not None:
            raise RuntimeError(f"repl process died (exit {self.proc.returncode})")
        assert self.proc.stdin is not None and self.proc.stdout is not None
        line = json.dumps(request)
        # The REPL expects each request on a single line followed by a blank line.
        self.proc.stdin.write(line + "\n\n")
        self.proc.stdin.flush()

        # Read JSON response: REPL emits objects separated by blank lines.
        buf: list[str] = []
        deadline = time.time() + self.timeout_s
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"repl read timeout after {self.timeout_s}s")
            ln = self.proc.stdout.readline()
            if not ln:
                raise RuntimeError("repl stdout closed unexpectedly")
            if ln.strip() == "" and buf:
                break
            buf.append(ln)
            # Try parsing what we have; if it parses cleanly we're done.
            try:
                obj = json.loads("".join(buf))
                return _ReplResult(
                    env=obj.get("env"),
                    messages=obj.get("messages", []) or [],
                    sorries=obj.get("sorries", []) or [],
                    raw=obj,
                )
            except json.JSONDecodeError:
                continue
        # Hit the blank line without a successful parse — try once more.
        text = "".join(buf)
        obj = json.loads(text)
        return _ReplResult(
            env=obj.get("env"),
            messages=obj.get("messages", []) or [],
            sorries=obj.get("sorries", []) or [],
            raw=obj,
        )
