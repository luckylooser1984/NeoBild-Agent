#!/usr/bin/env python3
"""Dream cycle: nightly importance decay and memory consolidation.

Meant to run once per night (cron / systemd timer). Per run:
  1. back up the DB (skipped with --dry-run),
  2. boost every memory by its accesses since the last run, then decay it
     by 5 %; memories that fall below 2.0 are deleted (forgetting),
  3. build connected components over the `edges` graph among the
     high-importance memories (>= 7.0) and let the local model summarise
     each cluster of >= 3 members -- only the N clusters with the most new
     edges since the last run ("dirtiest first"), to bound LLM work,
  4. apply the same boost/decay to the `facts` table.
Summaries are stored in `reflections` with kind='dream_summary'.

Usage:
  dream.py [--db memory.db] [--dry-run]

Environment: MEMORY_DB, LLM_BASE_URL, LLM_MODEL (see memdb.py).

Author: Lukas Weißmann
License: MIT
"""
import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from memdb import chat, connect, db_path, has_table

DECAY_RATE       = 0.05   # -5% per night
ACCESS_BOOST     = 0.5    # +0.5 per access since last dream
DELETE_BELOW     = 2.0    # delete memories below this score
SUMMARIZE_ABOVE  = 7.0    # cluster + summarise memories above this

CLUSTER_MIN_SIZE = 3
CLUSTER_TOP_N    = 5      # only summarise the N "dirtiest" clusters per night

_SUMMARIZE_PROMPT = """Condense the following memory entries into ONE compact summary
(max. 3 sentences). No list, flowing text.

Entries:
{entries}
"""


def backup_db(db: Path) -> Path:
    """Back up the DB before a dream run writes anything."""
    backup_path = db.with_name(f"{db.name}.bak.{datetime.now().strftime('%Y%m%d')}")
    shutil.copy2(db, backup_path)
    return backup_path


class _UnionFind:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _cluster_high_importance(con, ids: list[int], last_run_ts: str | None) -> list[tuple]:
    """Connected components over the edges table, restricted to the given
    high-importance ids. Returns (root, member_ids, dirtiness), sorted by
    dirtiness (number of new edges since the last dream run), only clusters
    >= CLUSTER_MIN_SIZE, at most CLUSTER_TOP_N."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    edge_rows = con.execute(
        f"SELECT src_id, dst_id, created_at FROM edges "
        f"WHERE src_id IN ({placeholders}) AND dst_id IN ({placeholders})",
        ids + ids,
    ).fetchall()

    uf = _UnionFind(ids)
    for src, dst, _created_at in edge_rows:
        uf.union(src, dst)

    clusters: dict[int, list[int]] = {}
    for i in ids:
        clusters.setdefault(uf.find(i), []).append(i)

    dirtiness: dict[int, int] = {root: 0 for root in clusters}
    for src, _dst, created_at in edge_rows:
        root = uf.find(src)
        if last_run_ts is None or (created_at and created_at >= last_run_ts):
            dirtiness[root] += 1

    result = [
        (root, members, dirtiness.get(root, 0))
        for root, members in clusters.items()
        if len(members) >= CLUSTER_MIN_SIZE
    ]
    result.sort(key=lambda x: x[2], reverse=True)
    return result[:CLUSTER_TOP_N]


def _summarize(entries: list[str]) -> str:
    text = "\n".join(f"- {e[:300]}" for e in entries)
    try:
        return chat(_SUMMARIZE_PROMPT.format(entries=text),
                    max_tokens=200, temperature=0.3, timeout=60)
    except Exception:
        return " | ".join(e[:100] for e in entries[:3])


def run_decay(db: Path, dry_run: bool = False) -> dict:
    """Apply nightly decay and consolidation. Returns stats dict."""
    if not dry_run:
        backup_db(db)

    con = connect(db)
    now = datetime.now().isoformat()

    last_run_ts = con.execute(
        "SELECT MAX(ts) FROM reflections WHERE kind='dream_summary'"
    ).fetchone()[0]

    rows = con.execute(
        "SELECT id, text, importance, access_count, last_accessed FROM embeddings"
    ).fetchall()

    stats = {"decayed": 0, "boosted": 0, "deleted": 0, "summarized": 0}
    to_delete = []
    high_importance = []

    for row_id, text, importance, access_count, _last_accessed in rows:
        new_imp = importance

        # Boost for accesses since the last dream
        if access_count > 0:
            new_imp = min(10.0, new_imp + ACCESS_BOOST * access_count)
            stats["boosted"] += 1

        # Apply decay
        new_imp = max(0.0, new_imp * (1 - DECAY_RATE))

        if new_imp < DELETE_BELOW:
            to_delete.append(row_id)
            stats["deleted"] += 1
        else:
            if new_imp != importance:
                stats["decayed"] += 1
            if not dry_run:
                con.execute(
                    "UPDATE embeddings SET importance=?, access_count=0, last_accessed=? WHERE id=?",
                    (round(new_imp, 3), now, row_id),
                )
            if new_imp >= SUMMARIZE_ABOVE:
                high_importance.append((row_id, text, new_imp))

    # Forget
    if to_delete and not dry_run:
        placeholders = ",".join("?" * len(to_delete))
        if has_table(con, "vec_embeddings"):
            con.execute(f"DELETE FROM vec_embeddings WHERE embedding_id IN ({placeholders})", to_delete)
        con.execute(f"DELETE FROM embeddings WHERE id IN ({placeholders})", to_delete)

    # Consolidation: connected components over edges instead of one global
    # blob. Only connected clusters (>= CLUSTER_MIN_SIZE) are summarised, each
    # on its own, at most the CLUSTER_TOP_N with the most new edges since the
    # last dream run.
    hi_ids = [row_id for row_id, _text, _imp in high_importance]
    hi_text_by_id = {row_id: text for row_id, text, _imp in high_importance}
    clusters = _cluster_high_importance(con, hi_ids, last_run_ts)

    for _root, member_ids, dirtiness in clusters:
        texts = [hi_text_by_id[mid] for mid in member_ids]
        summary = _summarize(texts)
        if not dry_run:
            con.execute(
                "INSERT INTO reflections(kind, content) VALUES(?, ?)",
                ("dream_summary", json.dumps({
                    "summary": summary,
                    "source_ids": member_ids,
                    "source_count": len(member_ids),
                    "dirtiness": dirtiness,
                }, ensure_ascii=False)),
            )
        stats["summarized"] += len(member_ids)

    # Facts decay
    fact_rows = con.execute("SELECT id, importance, access_count FROM facts").fetchall()
    for fid, fimp, facc in fact_rows:
        new_fimp = min(10.0, fimp + ACCESS_BOOST * facc) if facc > 0 else fimp
        new_fimp = max(0.0, new_fimp * (1 - DECAY_RATE))
        if not dry_run:
            con.execute(
                "UPDATE facts SET importance=?, access_count=0 WHERE id=?",
                (round(new_fimp, 3), fid),
            )

    if not dry_run:
        con.commit()
    con.close()
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=None,
                        help="memory DB (default: $MEMORY_DB or ./memory.db)")
    parser.add_argument("--dry-run", action="store_true", help="simulate without writing")
    args = parser.parse_args()
    db = args.db or db_path()
    if not os.path.exists(db):
        parser.error(f"DB not found: {db} (create it with: memdb.py init --db {db})")

    tag = "DRY RUN " if args.dry_run else ""
    print(f"[dream] {tag}start: {datetime.now().isoformat()}")
    stats = run_decay(db, dry_run=args.dry_run)
    print(f"[dream] decayed={stats['decayed']} boosted={stats['boosted']} "
          f"deleted={stats['deleted']} summarized={stats['summarized']}")
    print(f"[dream] {tag}end: {datetime.now().isoformat()}")
