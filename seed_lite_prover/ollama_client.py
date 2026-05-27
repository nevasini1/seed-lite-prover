"""Thin HTTP client for a local Ollama server (default :11434).

Supports both /api/generate (raw-prompt models like BFS-Prover-V2-7B) and
/api/chat (chat-template models like Kimina-Prover-RL-1.7B, which is a
reasoning model whose useful output lands in `message.content` only after
a long `message.thinking` preamble).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import urllib.request
import urllib.error


@dataclass
class GenerateRequest:
    model: str
    prompt: str
    temperature: float = 0.7
    top_p: float = 0.95
    num_predict: int = 256
    stop: tuple[str, ...] = ()
    # Set chat=True for reasoning / chat-template models. Then `prompt` is
    # used as the single user message.
    chat: bool = False
    system: str = ""


@dataclass
class ChatResponse:
    content: str
    thinking: str
    done_reason: str
    eval_count: int


class OllamaClient:
    def __init__(self, host: str = "http://localhost:11434", timeout: float = 600.0):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def generate(self, req: GenerateRequest, timeout: float | None = None) -> str:
        """Returns the model's textual output (chat: content only).

        `timeout`: per-call wall-clock cap. If unset, falls back to the
        constructor `timeout`. Callers with a deadline should pass
        `max(1, deadline - time.time())` to bound the call.
        """
        if req.chat:
            return self.chat(req, timeout=timeout).content
        body = {
            "model": req.model,
            "prompt": req.prompt,
            "stream": False,
            "options": {
                "temperature": req.temperature,
                "top_p": req.top_p,
                "num_predict": req.num_predict,
                "stop": list(req.stop),
            },
        }
        return self._post_json("/api/generate", body, timeout=timeout)["response"]

    def chat(self, req: GenerateRequest, timeout: float | None = None) -> ChatResponse:
        messages = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.append({"role": "user", "content": req.prompt})
        body = {
            "model": req.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": req.temperature,
                "top_p": req.top_p,
                "num_predict": req.num_predict,
                "stop": list(req.stop),
            },
        }
        out = self._post_json("/api/chat", body, timeout=timeout)
        msg = out.get("message", {}) or {}
        return ChatResponse(
            content=msg.get("content", "") or "",
            thinking=msg.get("thinking", "") or "",
            done_reason=out.get("done_reason", "") or "",
            eval_count=out.get("eval_count", 0) or 0,
        )

    def sample_n(self, req: GenerateRequest, n: int) -> list[str]:
        return [self.generate(req) for _ in range(n)]

    def unload(self, model: str) -> None:
        self._post_json("/api/generate", {"model": model, "keep_alive": 0})

    def _post_json(self, path: str, body: dict, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode()
        request = urllib.request.Request(
            self.host + path,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        effective_timeout = self.timeout if timeout is None else max(1.0, timeout)
        try:
            with urllib.request.urlopen(request, timeout=effective_timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Ollama {path} {e.code}: {e.read().decode()[:300]}") from e
