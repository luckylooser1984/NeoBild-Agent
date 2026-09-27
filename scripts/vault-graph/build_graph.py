#!/usr/bin/env python3
"""build_graph.py — parse an Obsidian-style Markdown vault (wikilinks) into the edge DB.

The SQLite edge DB (see vg_db.py) is the single place that gets written. This
script only rewrites the *wikilink* edges of one zone; vector edges added by
other tools are left untouched unless they point to a node that no longer
exists after the rebuild — those are removed and counted.

Usage:
  build_graph.py VAULT [--zone NAME] [--db PATH]
    VAULT   path to the Markdown vault (required)
    --zone  free-form label for this vault inside the DB (default: wiki)

Wikilink resolution (consistent with Obsidian):
  1. [[stem]]         -> unique note title (file stem); on stem collision no guessing -> external
  2. [[path/target]]  -> exact relative path without .md (Obsidian path semantics)
  3. [[target.md]]    -> same as above, the .md extension is stripped
Unresolved targets stay as nodes of the group "external" so dead links are visible.

Author: Lukas Weißmann
License: MIT
"""
import argparse
import re
import sys
from pathlib import Path

import vg_db

WIKILINK = re.compile(r"\[\[([^\[\]|#]+?)(?:\|[^\[\]]+?)?(?:#[^\[\]]*)?\]\]")
IGNORE = {".obsidian", ".git", ".trash", ".wiki-meta"}


def iter_md(root: Path):
    for p in root.rglob("*.md"):
        if any(part in IGNORE for part in p.parts):
            continue
        yield p


def main() -> int:
    ap = argparse.ArgumentParser(description="Write the wikilink edges of a Markdown vault into the edge DB")
    ap.add_argument("vault", help="path to the Markdown/Obsidian vault")
    ap.add_argument("--zone", default="wiki", help="zone label for this vault (default: wiki)")
    ap.add_argument("--db", default=str(vg_db.DEFAULT_DB), help="edge DB path (default: %(default)s)")
    a = ap.parse_args()

    vault = Path(a.vault).expanduser()
    if not vault.is_dir():
        print(f"ERROR: vault {vault} not found", file=sys.stderr)
        return 2

    files = list(iter_md(vault))
    stems: dict[str, list[str]] = {}
    for p in files:
        rel = p.relative_to(vault).with_suffix("")
        stems.setdefault(p.stem, []).append(rel.as_posix())

    def resolve(tgt: str):
        """Target file (Path) or None. No guessing on ambiguity."""
        t = tgt[:-3] if tgt.endswith(".md") else tgt
        if not t:
            return None
        if "/" in t:
            cand = vault / (t + ".md")
            return cand if cand.is_file() else None
        hits = stems.get(t, [])
        if len(hits) == 1:
            return vault / (hits[0] + ".md")
        return None  # ambiguous or unknown -> external

    nodes: dict[str, dict] = {}
    links: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    collisions: dict[str, list[str]] = {}
    for p in files:
        src = p.stem
        if len(stems.get(src, [])) > 1:
            collisions[src] = stems[src]
        nodes.setdefault(src, {"id": src, "group": p.parent.name, "val": 1})
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in WIKILINK.finditer(text):
            tgt = m.group(1).strip()
            if not tgt:
                continue
            dst = resolve(tgt)
            if dst is not None:
                dst_id, dst_group = dst.stem, dst.parent.name
            else:
                dst_id, dst_group = tgt, "external"
            nodes.setdefault(dst_id, {"id": dst_id, "group": dst_group, "val": 1})
            key = (src, dst_id)
            if key not in seen:
                seen.add(key)
                links.append(key)
    for _, tgt in links:                      # node size = number of incoming links
        nodes[tgt]["val"] = nodes[tgt].get("val", 1) + 1

    con = vg_db.connect(a.db)
    try:
        con.execute("DELETE FROM nodes WHERE zone = ?", (a.zone,))
        con.execute("DELETE FROM edges WHERE zone = ? AND edge_kind = 'wikilink'", (a.zone,))
        con.executemany(
            "INSERT INTO nodes(id, zone, group_name, val) VALUES (?,?,?,?)",
            [(n["id"], a.zone, n["group"], n["val"]) for n in nodes.values()],
        )
        con.executemany(
            "INSERT INTO edges(node_id, zone, edge_kind, target, weight) VALUES (?,?,'wikilink',?,1.0)",
            [(s, a.zone, t) for s, t in links],
        )
        orphaned = vg_db.drop_orphan_edges(con, a.zone)
        con.commit()
        kn = con.execute("SELECT COUNT(*) FROM nodes WHERE zone = ?", (a.zone,)).fetchone()[0]
        kw = con.execute("SELECT COUNT(*) FROM edges WHERE zone = ? AND edge_kind='wikilink'",
                         (a.zone,)).fetchone()[0]
        kv = con.execute("SELECT COUNT(*) FROM edges WHERE zone = ? AND edge_kind='vector'",
                         (a.zone,)).fetchone()[0]
    finally:
        con.close()

    internal = sum(1 for _, t in links if nodes[t]["group"] != "external")
    print(f"OK zone={a.zone}: {kn} nodes in DB, {kw} wikilink edges "
          f"({internal} to internal targets), {kv} vector edges unchanged"
          + (f", {orphaned} orphaned vector edge(s) removed" if orphaned else ""))
    if collisions:
        print(f"NOTE stem collisions ({len(collisions)}): {sorted(collisions)[:10]}", file=sys.stderr)
    print("Export for the 3D viewer: python3 export_graph.py --zone " + a.zone)
    return 0


if __name__ == "__main__":
    sys.exit(main())
