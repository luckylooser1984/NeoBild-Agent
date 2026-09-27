#!/usr/bin/env python3
"""llm_router — deterministic model routing for small tasks, local-first.

Routes a prompt between two local models behind an OpenAI-compatible
endpoint (e.g. llama-swap serving the aliases "qwen" and "coder") and an
optional OpenAI-compatible cloud model (tier 3). No LLM is involved in the
routing decision itself -- it is plain keyword/length logic, so it is
predictable and unit-testable.

Routing order (see classify()):
  1. Privacy first: if --source matches a private marker, the task ALWAYS
     stays local -- this overrides --cloud and the "complex" escalation.
  2. Cloud escalation only if --cloud is set or the task is "complex"
     (long, or contains planning hints) AND a cloud API key is present.
     Without a key: documented fallback to local, not an error.
  3. Otherwise a local model by task type / keywords (code -> "coder",
     everything else -> "qwen").

Usage:
  router.py "Write a Python script that computes Fibonacci" --task code
  router.py "Summarize the following text: ..." --task summarize
  router.py "..." --source notes/private/x.md      # forced local
  router.py "long analysis ..." --cloud            # cloud if key is set
  router.py --proxy --port 8090                    # OpenAI-compatible proxy

The model answer goes to stdout, a JSON metadata line to stderr.
Exit codes: 0 ok, 1 router/network error, 2 missing task text.

Environment:
  LLM_ROUTER_LOCAL_URL       default http://127.0.0.1:8080/v1/chat/completions
  LLM_ROUTER_CLOUD_URL       OpenAI-compatible chat completions URL (no default)
  LLM_ROUTER_CLOUD_MODEL     cloud model name (no default)
  LLM_ROUTER_CLOUD_API_KEY   cloud API key (never logged)
                             Cloud escalation is off unless all three are set.
  LLM_ROUTER_PRIVATE_MARKERS comma-separated path fragments that force local
                             (default: /private/,confidential,embargoed)

stdlib only (urllib/json/argparse/http.server).

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

LOCAL_URL = os.environ.get(
    "LLM_ROUTER_LOCAL_URL", "http://127.0.0.1:8080/v1/chat/completions")
# No cloud provider is built in: cloud escalation stays off until the user
# configures URL, model and key explicitly.
CLOUD_URL = os.environ.get("LLM_ROUTER_CLOUD_URL", "")
CLOUD_MODEL = os.environ.get("LLM_ROUTER_CLOUD_MODEL", "")
CLOUD_KEY_ENV = "LLM_ROUTER_CLOUD_API_KEY"

DEFAULT_TIMEOUT_S = 30
SWAP_RETRY_TIMEOUT_S = 300  # a model swap in llama-swap needs load time
COMPLEX_LEN_THRESHOLD = 3000

# Keyword lists are constants so tests can check them without any network.
# They are bilingual (English/German) on purpose.
CODER_KEYWORDS = (
    "python", "bash", "skript", "script", "bug", "fix", "refactor",
    "regex", "json", "code", "funktion", "klasse", "unittest", "pytest",
)
QWEN_KEYWORDS = (
    "zusammenfass", "summar", "klassifi", "classif", "kategoris", "categoris",
    "extrahier", "extract", "übersetz", "translate", "label", "tag",
)
PLAN_HINT_MARKERS = (
    "plan:", "schritt 1", "schritt 2", "mehrere dateien", "mehrstufig",
    "step 1", "multi-file",
)
# Privacy rule -- hard, overrides --cloud and "complex".
PRIVATE_MARKERS = tuple(
    m.strip() for m in os.environ.get(
        "LLM_ROUTER_PRIVATE_MARKERS", "/private/,confidential,embargoed"
    ).split(",") if m.strip()
)

TASK_TYPES = ("auto", "code", "chat", "summarize")


class RouterError(Exception):
    """Explicit failure instead of a silent one -- leads to exit code != 0."""


@dataclass(frozen=True)
class RouteDecision:
    model: str          # "qwen" | "coder" | CLOUD_MODEL
    tier: int           # 1 (qwen), 2 (coder), 3 (cloud)
    target: str         # "local" | "cloud"
    local: bool
    private: bool
    complex_: bool
    reason: str


# ---------------------------------------------------------------------------
# Classification -- pure functions, no I/O, testable with a fake client.
# ---------------------------------------------------------------------------

def is_private(source: str) -> bool:
    return any(marker in source for marker in PRIVATE_MARKERS)


def is_complex(task: str) -> bool:
    if len(task) > COMPLEX_LEN_THRESHOLD:
        return True
    lower = task.lower()
    return any(marker in lower for marker in PLAN_HINT_MARKERS)


def detect_model(task: str) -> str:
    """Deterministic keyword classification. Default: qwen."""
    lower = task.lower()
    if any(kw in lower for kw in CODER_KEYWORDS):
        return "coder"
    if any(kw in lower for kw in QWEN_KEYWORDS):
        return "qwen"
    return "qwen"


def classify(task: str, task_type: str = "auto", source: str = "",
             cloud_flag: bool = False, api_key_present: bool = False) -> RouteDecision:
    """Complete, pure routing decision.

    Order (not negotiable): privacy first (hard, always local), then explicit
    --cloud / complex escalation, otherwise a local model by task type/keyword.
    """
    if task_type not in TASK_TYPES:
        raise RouterError(f"Unknown --task type: {task_type!r} (allowed: {TASK_TYPES})")

    private = is_private(source)

    if task_type == "code":
        local_model = "coder"
    elif task_type in ("chat", "summarize"):
        local_model = "qwen"
    else:
        local_model = detect_model(task)

    complex_ = is_complex(task)

    if private:
        # Privacy rule is hard: NEVER cloud, not even with --cloud or complex.
        return RouteDecision(
            model=local_model, tier=(2 if local_model == "coder" else 1),
            target="local", local=True, private=True, complex_=complex_,
            reason="private source: forced local",
        )

    wants_cloud = cloud_flag or complex_
    if wants_cloud and api_key_present:
        return RouteDecision(
            model=CLOUD_MODEL, tier=3, target="cloud", local=False,
            private=False, complex_=complex_,
            reason="explicit --cloud" if cloud_flag else "complex (>3000 chars or planning hint)",
        )
    if wants_cloud and not api_key_present:
        # No key -> the cloud wish cannot be fulfilled, fall back to local.
        return RouteDecision(
            model=local_model, tier=(2 if local_model == "coder" else 1),
            target="local", local=True, private=False, complex_=complex_,
            reason="cloud requested but not configured (URL/model/key) -> local fallback",
        )

    return RouteDecision(
        model=local_model, tier=(2 if local_model == "coder" else 1),
        target="local", local=True, private=False, complex_=complex_,
        reason="default routing",
    )


# ---------------------------------------------------------------------------
# Live calls (network) -- deliberately separate from classify(), so tests can
# inject fake clients instead of these functions.
# ---------------------------------------------------------------------------

def call_local(model: str, messages: list[dict],
               timeout_s: int = DEFAULT_TIMEOUT_S,
               retry_timeout_s: int = SWAP_RETRY_TIMEOUT_S) -> tuple[dict, int]:
    """Call the local endpoint. On timeout/connection error, retry ONCE with a
    long timeout -- a model swap (e.g. llama-swap with an exclusive group)
    first has to load the backend; that is not an error."""
    body = json.dumps({"model": model, "messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        LOCAL_URL, data=body,
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RouterError(f"local model HTTP error {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError):
        try:
            with urllib.request.urlopen(req, timeout=retry_timeout_s) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            raise RouterError(
                f"local model call failed (also after retry with {retry_timeout_s}s): {exc}"
            ) from exc
    duration_ms = int((time.monotonic() - start) * 1000)
    if "error" in data:
        raise RouterError(f"local model reported an error: {data['error']}")
    return data, duration_ms


def call_cloud(messages: list[dict], api_key: str,
               timeout_s: int = 60) -> tuple[dict, int]:
    body = json.dumps({"model": CLOUD_MODEL, "messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        CLOUD_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Never print the key in an error message.
        raise RouterError(f"cloud HTTP error {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RouterError(f"cloud call failed: {exc}") from exc
    duration_ms = int((time.monotonic() - start) * 1000)
    return data, duration_ms


def extract_content(response: dict) -> str:
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RouterError(f"unexpected response format: {response!r}") from exc


# ---------------------------------------------------------------------------
# Orchestration -- thin layer over classify()/call_*(), injectable via
# function arguments so it stays testable without a network.
# ---------------------------------------------------------------------------

def run(task: str, task_type: str, source: str, cloud_flag: bool,
        call_local=call_local, call_cloud_fn=call_cloud) -> tuple[str, dict]:
    api_key = os.environ.get(CLOUD_KEY_ENV)
    cloud_ready = bool(api_key and CLOUD_URL and CLOUD_MODEL)
    decision = classify(task, task_type, source, cloud_flag, api_key_present=cloud_ready)
    messages = [{"role": "user", "content": task}]

    if decision.target == "cloud":
        response, duration_ms = call_cloud_fn(messages, api_key)
    else:
        response, duration_ms = call_local(decision.model, messages)

    content = extract_content(response)
    meta = {
        "model": decision.model,
        "tier": decision.tier,
        "local": decision.local,
        "private": decision.private,
        "reason": decision.reason,
        "duration_ms": duration_ms,
    }
    return content, meta


# ---------------------------------------------------------------------------
# Proxy mode -- OpenAI-compatible endpoint, so an agent can use the router as
# just another provider.
# ---------------------------------------------------------------------------

_last_model_loaded = "unknown"


def _openai_chat_response(content: str, model: str) -> dict:
    return {
        "id": f"llmrouter-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # less noise, no secrets in the log
        sys.stderr.write("[proxy] " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "model_loaded": _last_model_loaded})
        elif self.path == "/v1/models":
            data = [{"id": m, "object": "model", "owned_by": "llm_router"}
                    for m in ("auto", "qwen", "coder")]
            self._send_json(200, {"object": "list", "data": data})
        else:
            self._send_json(404, {"error": f"unknown path: {self.path}"})

    def do_POST(self):
        global _last_model_loaded
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": f"unknown path: {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw)
            messages = body.get("messages", [])
            task = next(
                (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
                "",
            )
            requested_model = body.get("model", "auto")
            source = body.get("source", "")
            if requested_model == "coder":
                task_type = "code"
            elif requested_model == "qwen":
                task_type = "chat"
            else:
                task_type = "auto"

            content, meta = run(task, task_type, source, cloud_flag=False)
            _last_model_loaded = meta["model"]
            sys.stderr.write(json.dumps(meta) + "\n")
            self._send_json(200, _openai_chat_response(content, meta["model"]))
        except RouterError as exc:
            self._send_json(502, {"error": str(exc)})
        except Exception as exc:  # explicit failure instead of a silent crash
            self._send_json(500, {"error": f"internal error: {exc}"})


def run_proxy(port: int):
    server = HTTPServer(("127.0.0.1", port), ProxyHandler)
    sys.stderr.write(f"[proxy] llm_router listening on http://127.0.0.1:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="router.py",
        description="Local-first model routing for small agent tasks.",
    )
    p.add_argument("task", nargs="?", help="task text (not needed with --proxy)")
    p.add_argument("--task", dest="task_type", choices=TASK_TYPES, default="auto",
                   help="force a task type instead of keyword detection")
    p.add_argument("--source", default="", help="source path (for the privacy rule)")
    p.add_argument("--cloud", action="store_true", help="explicitly request cloud escalation")
    p.add_argument("--proxy", action="store_true", help="start the OpenAI-compatible proxy server")
    p.add_argument("--port", type=int, default=8090, help="proxy port (default 8090)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.proxy:
        run_proxy(args.port)
        return 0

    if not args.task:
        sys.stderr.write("Error: task text missing (or use --proxy).\n")
        return 2

    try:
        content, meta = run(args.task, args.task_type, args.source, args.cloud)
    except RouterError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    print(content)
    sys.stderr.write(json.dumps(meta) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
