#!/usr/bin/env python3
"""export_graph.py — export one zone of the edge DB as JSON for a 3d-force-graph viewer.

Data flow:
  build_graph.py        -> writes wikilink edges into the DB
  vault_graph_import.py -> imports {nodes, links} JSON (e.g. with vector edges) into the DB
  export_graph.py       -> writes JSON *from* the DB for the browser viewer

The DB is the only source of truth; the JSON files are pure exports.
Output format: {"nodes": [{id, group, val}], "links": [{source, target[, kind, w]}]}
which 3d-force-graph / force-graph can load directly.

Usage:
  export_graph.py --zone wiki                    # -> public/graph-wiki.json
  export_graph.py --zone wiki --out x.json       # other file name (inside --public-dir)
  export_graph.py --zone wiki --check            # count only, write nothing

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import vg_db

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Export one zone of the edge DB as force-graph JSON")
    ap.add_argument("--zone", required=True, help="zone label to export")
    ap.add_argument("--db", default=str(vg_db.DEFAULT_DB), help="edge DB path (default: %(default)s)")
    ap.add_argument("--public-dir", default=str(HERE / "public"),
                    help="output directory (default: %(default)s)")
    ap.add_argument("--out", default=None, help="output file name (default: graph-<zone>.json)")
    ap.add_argument("--check", action="store_true", help="count only, write nothing")
    a = ap.parse_args()

    db = Path(a.db)
    if not db.is_file():
        sys.exit(f"ERROR: edge DB missing: {db}\nBuild it first with build_graph.py or vault_graph_import.py")
    out = Path(a.public_dir) / (a.out or f"graph-{a.zone}.json")

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        nodes = [{"id": str(i), "group": str(g or "?"), "val": int(v or 1)}
                 for i, g, v in con.execute(
                     "SELECT id, group_name, val FROM nodes WHERE zone = ? ORDER BY id", (a.zone,))]
        links: list[dict] = []
        for src, kind, tgt, w in con.execute(
                "SELECT node_id, edge_kind, target, weight FROM edges "
                "WHERE zone = ? ORDER BY edge_kind, node_id, target", (a.zone,)):
            e: dict = {"source": str(src), "target": str(tgt)}
            if kind != "wikilink":
                e["kind"] = str(kind)
                e["w"] = float(w)
            links.append(e)
    finally:
        con.close()

    ids = {n["id"] for n in nodes}
    dead = sum(1 for l in links if l["source"] not in ids or l["target"] not in ids)
    if not nodes:
        sys.exit(f"ERROR: zone '{a.zone}' is empty in the DB — nothing exported.")

    if a.check:
        print(f"check {a.zone}: {len(nodes)} nodes, {len(links)} edges, {dead} with dead target -> {out}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps({"nodes": nodes, "links": links}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)  # atomic
    print(f"export {a.zone}: {len(nodes)} nodes, {len(links)} edges "
          f"({dead} with dead target) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
