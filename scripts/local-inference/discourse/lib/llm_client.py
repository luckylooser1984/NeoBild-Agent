"""HTTP client for a local llama-server /v1/chat/completions endpoint.

stdlib only. Never raises on network errors: failures come back as a
ChatResult with finish_reason="error" and the message in `error`.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

THINK_PREFILL = "<think>\n\n</think>\n\n"
_PREFILL_RE = re.compile(r"^\s*<think>\s*</think>\s*\n*")


@dataclass
class ChatResult:
    content: str
    raw_content: str
    prompt_tokens: int
    completion_tokens: int
    tokens_per_second: float
    duration_ms: float
    finish_reason: str
    model: str
    temperature: float
    seed: int
    max_tokens: int
    error: str | None = None


def chat_completion(
    base_url: str,
    messages: list[dict],
    *,
    model: str,
    temperature: float,
    seed: int,
    max_tokens: int,
    timeout_s: float = 120.0,
) -> ChatResult:
    """One chat-completion request with the Qwen3 thinking block suppressed.

    Qwen3 emits a <think>...</think> block before every answer; with a small
    max_tokens that block eats the whole budget and `content` comes back
    empty (finish_reason=length) -- observed against llama-server with
    Qwen3-1.7B. Appending an already-closed thinking block as an assistant
    prefill skips the thinking phase, but llama-server echoes the prefill
    text back in the response, so it is stripped here centrally instead of
    in every caller.
    """
    payload_messages = list(messages) + [{"role": "assistant", "content": THINK_PREFILL}]
    body = json.dumps({
        "model": model,
        "messages": payload_messages,
        "temperature": temperature,
        "seed": seed,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        duration_ms = (time.monotonic() - start) * 1000
        return ChatResult(
            content="", raw_content="", prompt_tokens=0, completion_tokens=0,
            tokens_per_second=0.0, duration_ms=duration_ms, finish_reason="error",
            model=model, temperature=temperature, seed=seed, max_tokens=max_tokens,
            error=str(exc),
        )
    duration_ms = (time.monotonic() - start) * 1000

    choice = data["choices"][0]
    raw_content = choice["message"]["content"]
    content = _PREFILL_RE.sub("", raw_content, count=1)
    usage = data.get("usage", {})
    completion_tokens = usage.get("completion_tokens", 0)

    timings = data.get("timings", {})
    tokens_per_second = timings.get("predicted_per_second")
    if tokens_per_second is None:
        tokens_per_second = completion_tokens / (duration_ms / 1000) if duration_ms > 0 else 0.0

    return ChatResult(
        content=content,
        raw_content=raw_content,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=completion_tokens,
        tokens_per_second=tokens_per_second,
        duration_ms=duration_ms,
        finish_reason=choice.get("finish_reason", "unknown"),
        model=model, temperature=temperature, seed=seed, max_tokens=max_tokens,
        error=None,
    )
