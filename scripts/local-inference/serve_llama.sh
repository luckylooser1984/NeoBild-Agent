#!/usr/bin/env bash
# serve_llama.sh — start llama.cpp's llama-server as a local, OpenAI-compatible
# HTTP API (bound to localhost only) for scripts that want to talk to a model.
#
# Tested setup: Qwen3-1.7B Q8_0 on a CPU-only laptop (8 threads, no GPU),
# ~13-15 tokens/s generation. Thinking mode is on via the default chat
# template; it can be disabled per request with
#   "chat_template_kwargs": {"enable_thinking": false}
# or by the closed-<think> prefill trick used in discourse/lib/llm_client.py.
#
# Usage:  serve_llama.sh [extra llama-server args...]
#
# Environment (all optional):
#   LLAMA_BIN_DIR  directory containing llama-server (default: ./llama.cpp/build/bin)
#   LLAMA_MODEL    path to the .gguf model (default: ./models/Qwen3-1.7B-Q8_0.gguf)
#   LLAMA_HOST     bind address (default 127.0.0.1 — keep it local)
#   LLAMA_PORT     port (default 8080)
#   LLAMA_CTX      context size (default 8192)
#
# Author: Lukas Weißmann
# License: MIT
set -euo pipefail

LLAMA_BIN_DIR="${LLAMA_BIN_DIR:-./llama.cpp/build/bin}"
LLAMA_MODEL="${LLAMA_MODEL:-./models/Qwen3-1.7B-Q8_0.gguf}"
LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
LLAMA_CTX="${LLAMA_CTX:-8192}"

[ -x "${LLAMA_BIN_DIR}/llama-server" ] || { echo "llama-server not found in ${LLAMA_BIN_DIR}" >&2; exit 1; }
[ -f "${LLAMA_MODEL}" ] || { echo "model not found: ${LLAMA_MODEL}" >&2; exit 1; }

export LD_LIBRARY_PATH="${LLAMA_BIN_DIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
exec "${LLAMA_BIN_DIR}/llama-server" \
  -m "${LLAMA_MODEL}" \
  --host "${LLAMA_HOST}" --port "${LLAMA_PORT}" \
  --jinja \
  -c "${LLAMA_CTX}" \
  --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0 \
  "$@"
