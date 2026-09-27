#!/usr/bin/env python3
"""Shared helpers for the agent-memory scripts: DB access + local LLM call.

Usage:
  memdb.py init [--db PATH]     create the schema (schema.sql, plus the
                                vec_embeddings table if sqlite-vec is installed)

Environment:
  MEMORY_DB     path to the SQLite memory DB (default: ./memory.db)
  LLM_BASE_URL  OpenAI-compatible endpoint (default: http://127.0.0.1:8080/v1)
  LLM_MODEL     model name sent to the endpoint (default: qwen)

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

try:  # optional: only needed for the vector table
    import sqlite_vec  # type: ignore
except ImportError:  # pragma: no cover
    sqlite_vec = None

HERE = Path(__file__).resolve().parent
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen")

# Qwen3 thinks before answering; a closed <think> block as assistant prefill
# skips that phase. llama-server echoes the prefill, so it is stripped again.
THINK_PREFIX = "<think>\n\n</think>\n\n"


def db_path() -> Path:
    return Path(os.environ.get("MEMORY_DB", "memory.db"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or db_path())
    if sqlite_vec is not None:
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
    return con


def has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def chat(prompt: str, *, max_tokens: int, temperature: float, timeout: float) -> str:
    """One prompt -> answer text from the local model (thinking suppressed)."""
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": THINK_PREFIX},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        LLM_BASE_URL.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    raw = data["choices"][0]["message"]["content"]
    return raw.removeprefix(THINK_PREFIX).strip()


def init_db(path: Path, dim: int = 768) -> None:
    con = connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    if sqlite_vec is not None:
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0("
            f"embedding_id INTEGER PRIMARY KEY, embedding FLOAT[{int(dim)}])")
    con.commit()
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create the schema")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--dim", type=int, default=768, help="embedding dimension")
    a = ap.parse_args()
    target = a.db or db_path()
    init_db(target, a.dim)
    print(f"initialised {target} (vector table: {'yes' if sqlite_vec else 'no, sqlite-vec missing'})")
