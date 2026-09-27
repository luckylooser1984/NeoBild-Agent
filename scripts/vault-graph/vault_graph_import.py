#!/usr/bin/env python3
"""vault_graph_import.py — import a force-graph JSON file into one zone of the edge DB.

Accepts the {nodes, links} format that export_graph.py writes and that
add_vector_edges.py extends with vector edges ({"kind": "vector", "w": cos}).
The import is idempotent (DELETE + INSERT per zone). With --expect it checks the
node/edge counts first and aborts instead of overwriting if they differ — useful
as a guard when migrating existing data.

Usage:
  vault_graph_import.py --zone wiki --json public/graph-wiki.json [--expect 204,1666] [--db PATH]

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import vg_db


def import_zone(con: sqlite3.Connection, zone: str, path: Path, expect: tuple[int, int] | None):
    d = json.loads(path.read_text(encoding="utf-8"))
    nodes = d.get("nodes", [])
    links = d.get("links", [])

    if expect is not None:
        en, ee = expect
        if (len(nodes), len(links)) != expect:
            sys.exit(f"ABORT: {zone}: expected {en} nodes / {ee} edges, "
                     f"found {len(nodes)} / {len(links)} — resolve first, not overwriting.")

    con.execute("DELETE FROM nodes WHERE zone = ?", (zone,))
    con.execute("DELETE FROM edges WHERE zone = ?", (zone,))
    con.executemany(
        "INSERT INTO nodes(id, zone, group_name, val) VALUES (?, ?, ?, ?)",
        [(str(n["id"]), zone, str(n.get("group", "?")), int(n.get("val", 0) or 0)) for n in nodes],
    )
    rows = []
    for e in links:
        kind = str(e.get("kind") or "wikilink")
        w = e.get("w", e.get("weight", 1.0))
        if kind == "wikilink" and "w" not in e:
            w = 1.0
        rows.append((str(e["source"]), zone, kind, str(e["target"]), float(w)))
    con.executemany(
        "INSERT INTO edges(node_id, zone, edge_kind, target, weight) VALUES (?, ?, ?, ?, ?)", rows
    )
    con.commit()

    # Cross-check against the DB itself
    kn = con.execute("SELECT COUNT(*) FROM nodes WHERE zone = ?", (zone,)).fetchone()[0]
    ke = con.execute("SELECT COUNT(*) FROM edges WHERE zone = ?", (zone,)).fetchone()[0]
    kinds = con.execute(
        "SELECT edge_kind, COUNT(*) FROM edges WHERE zone = ? GROUP BY edge_kind ORDER BY 2 DESC",
        (zone,)).fetchall()
    print(f"{zone:6s} -> {kn} nodes, {ke} edges  {dict(kinds)}")
    return kn, ke


def main():
    ap = argparse.ArgumentParser(description="Import a {nodes, links} graph JSON into the edge DB")
    ap.add_argument("--zone", required=True, help="zone label to (re)write")
    ap.add_argument("--json", required=True, help="graph JSON file to import")
    ap.add_argument("--db", default=str(vg_db.DEFAULT_DB), help="edge DB path (default: %(default)s)")
    ap.add_argument("--expect", default=None, help="expected 'nodes,edges' — abort on mismatch")
    a = ap.parse_args()

    expect = None
    if a.expect:
        n, e = a.expect.split(",")
        expect = (int(n), int(e))

    con = vg_db.connect(a.db)
    import_zone(con, a.zone, Path(a.json), expect)

    total = con.execute("SELECT zone, edge_kind, COUNT(*) FROM edges GROUP BY zone, edge_kind").fetchall()
    print("DB total:")
    for z, k, c in total:
        print(f"  {z:6s} {k:9s} {c}")
    con.close()
    print(f"written: {a.db}")


if __name__ == "__main__":
    main()
