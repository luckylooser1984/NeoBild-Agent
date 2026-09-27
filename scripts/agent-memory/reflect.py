#!/usr/bin/env python3
"""Reflection loop: synthesise recent memories into higher-level insights.

Loads the 30 most recent memories with importance >= 4 plus their 1-hop
neighbours from the `edges` graph (so thematically linked entries are
included, not only new ones), asks the local model for a short summary,
up to 3 insights and up to 3 open questions, and stores the result as JSON
in `reflections` (kind='auto'). get_latest_reflection() renders the newest
one as text for a system prompt.

Usage:
  reflect.py [--db memory.db] [--limit 30]

Environment: MEMORY_DB, LLM_BASE_URL, LLM_MODEL (see memdb.py).

Author: Lukas Weißmann
License: MIT
"""
import argparse
import json
import re
from pathlib import Path

from memdb import chat, connect

_REFLECT_PROMPT = """You are a self-reflecting AI agent. Analyse the following recent memory
entries and produce:
1. A short summary (2-3 sentences) of the most important topics
2. At most 3 insights about the user or the conversation patterns
3. Up to 3 questions you would ask to understand the user better

Return a JSON object (NO Markdown):
{{"summary": "...", "insights": ["...", "..."], "questions": ["...", "..."]}}

Entries:
{entries}
"""


def _load_recent(con, limit: int = 30) -> list[tuple]:
    """Top-N recent high-importance seeds plus their 1-hop neighbours from
    edges, de-duplicated."""
    seeds = con.execute(
        "SELECT id, role, text, importance FROM embeddings "
        "WHERE importance >= 4.0 ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not seeds:
        return []

    seed_ids = [row[0] for row in seeds]
    seen_ids = set(seed_ids)
    placeholders = ",".join("?" * len(seed_ids))
    try:
        neighbor_ids = con.execute(
            f"SELECT DISTINCT dst_id FROM edges WHERE src_id IN ({placeholders})",
            seed_ids,
        ).fetchall()
    except Exception:
        neighbor_ids = []

    extra_ids = [nid for (nid,) in neighbor_ids if nid not in seen_ids]
    rows = [(role, text, imp) for _id, role, text, imp in seeds]
    if extra_ids:
        placeholders = ",".join("?" * len(extra_ids))
        extra_rows = con.execute(
            f"SELECT role, text, importance FROM embeddings WHERE id IN ({placeholders})",
            extra_ids,
        ).fetchall()
        rows.extend(extra_rows)
    return rows


def reflect(limit: int = 30, db: Path | None = None) -> dict | None:
    """Run one reflection cycle. Returns reflection dict or None on failure."""
    con = connect(db)
    rows = _load_recent(con, limit)
    if not rows:
        con.close()
        return None

    entries_text = "\n".join(
        f"[{role}|imp={imp:.1f}] {text[:300]}"
        for role, text, imp in rows
    )

    try:
        raw = chat(_REFLECT_PROMPT.format(entries=entries_text),
                   max_tokens=512, temperature=0.4, timeout=120)
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
    except Exception as e:
        con.close()
        print(f"[reflect] error: {e}")
        return None

    con.execute(
        "INSERT INTO reflections(kind, content) VALUES (?, ?)",
        ("auto", json.dumps(result, ensure_ascii=False)),
    )
    con.commit()
    con.close()
    return result


def get_latest_reflection(db: Path | None = None) -> str:
    """Return the latest reflection summary as plain text for a system prompt."""
    con = connect(db)
    row = con.execute(
        "SELECT content FROM reflections ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    con.close()
    if not row:
        return ""
    try:
        data = json.loads(row[0])
        parts = ["[Latest reflection]"]
        if "summary" in data:
            parts.append(f"  Summary: {data['summary']}")
        for e in data.get("insights", []):
            parts.append(f"  - {e}")
        return "\n".join(parts)
    except Exception:
        return ""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()
    result = reflect(a.limit, a.db)
    if result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("No entries available for reflection.")
