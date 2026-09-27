#!/usr/bin/env python3
"""query_graph.py — read-only queries against the vault graph (neighbours, path, subgraph, rank).

Gives agents and humans programmatic access to the note graph instead of only a
visualisation: they can ask for neighbours, shortest paths or the PageRank top
list without writing ad-hoc code each time.

Reads the SQLite edge DB (see vg_db.py). It NEVER writes: no vault access, no
network, the connection is opened read-only.

Usage (--zone / --db / --json are GLOBAL and go BEFORE the sub-command):
  query_graph.py --zone wiki stats
  query_graph.py --zone wiki find  <substring> [--limit 20]
  query_graph.py --zone wiki neigh <node> [--depth 1]         # neighbours
  query_graph.py --zone wiki path  <node-a> <node-b>          # shortest path
  query_graph.py --zone wiki sub   <node|topic> [--depth 2]   # subgraph / surroundings
  query_graph.py --zone wiki rank  [--top 20]                 # PageRank

Counting: `edges` in `stats` are UNDIRECTED pairs. The DB stores directed edges,
so the number can be slightly lower than in the JSON export (mutual links and
self-links collapse). No data loss, only a different way of counting.

Nodes are resolved by exact id or by a unique substring; on ambiguity the tool
lists the candidates instead of guessing. Output is plain sentences (TTS
friendly); use --json for machine-readable output.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import deque
from pathlib import Path

import vg_db


def load(db: Path, zone: str):
    """Read nodes and edges of one zone from the edge DB as an undirected adjacency."""
    if not db.is_file():
        sys.exit(f"ERROR: edge DB missing: {db}\n"
                 f"Build it first with build_graph.py or vault_graph_import.py")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        adj: dict[str, dict[str, int]] = {}
        groups: dict[str, str] = {}
        for nid, grp in con.execute(
                "SELECT id, group_name FROM nodes WHERE zone = ?", (zone,)):
            adj.setdefault(str(nid), {})
            groups[str(nid)] = str(grp or "?")
        for src, tgt in con.execute(
                "SELECT node_id, target FROM edges WHERE zone = ?", (zone,)):
            a, b = str(src), str(tgt)
            adj.setdefault(a, {})[b] = adj.get(a, {}).get(b, 0) + 1
            adj.setdefault(b, {})[a] = adj.get(b, {}).get(a, 0) + 1
            groups.setdefault(a, "external")
            groups.setdefault(b, "external")
    finally:
        con.close()
    return adj, groups


def resolve(adj, needle: str) -> str:
    """Exact id first, then unique substring; otherwise list candidates."""
    if needle in adj:
        return needle
    hits = [k for k in adj if needle.lower() in k.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit(f"No node matches '{needle}'.")
    hits.sort(key=len)
    print(f"'{needle}' is ambiguous ({len(hits)} hits), please be more specific:", file=sys.stderr)
    for h in hits[:25]:
        print(f"  {h}", file=sys.stderr)
    sys.exit(2)


def split_avoid(spec: str) -> set[str]:
    """'index,log' -> {'index','log'} (mask hub nodes)."""
    return {x.strip() for x in (spec or "").split(",") if x.strip()}


def bfs(adj, start: str, depth: int, avoid: set[str] | None = None):
    avoid = avoid or set()
    seen = {start: 0}
    order = [start]
    q = deque([start])
    while q:
        cur = q.popleft()
        if seen[cur] >= depth:
            continue
        for nb in adj.get(cur, {}):
            if nb in avoid and nb != start:
                continue
            if nb not in seen:
                seen[nb] = seen[cur] + 1
                q.append(nb)
                order.append(nb)
    return seen, order


def bfs_path(adj, a: str, b: str, avoid: set[str] | None = None):
    avoid = avoid or set()
    prev: dict[str, str | None] = {a: None}
    q = deque([a])
    while q:
        cur = q.popleft()
        if cur == b:
            out = []
            while cur is not None:
                out.append(cur)
                cur = prev[cur]
            return list(reversed(out))
        for nb in adj.get(cur, {}):
            if nb in prev or (nb in avoid and nb != b):
                continue
            prev[nb] = cur
            q.append(nb)
    return None


def pagerank(adj, alpha=0.85, iters=60):
    """Plain-Python PageRank on the undirected adjacency (dangling mass spread evenly)."""
    n = len(adj)
    if not n:
        return {}
    r = {k: 1.0 / n for k in adj}
    for _ in range(iters):
        nxt = {k: (1 - alpha) / n for k in adj}
        dshare = alpha * sum(r[k] for k, v in adj.items() if not v) / n
        for k, v in adj.items():
            if not v:
                continue
            share = alpha * r[k] / len(v)
            for nb in v:
                nxt[nb] += share
        for k in nxt:
            nxt[k] += dshare
        r = nxt
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="query-graph",
                                 description="Read-only queries against the vault graph.")
    ap.add_argument("--zone", default="wiki", help="zone label (default: wiki)")
    ap.add_argument("--db", default=str(vg_db.DEFAULT_DB), help="edge DB path (default: %(default)s)")
    ap.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="size, groups, isolated nodes")
    f = sub.add_parser("find", help="find nodes by substring")
    f.add_argument("needle"); f.add_argument("--limit", type=int, default=20)
    nb = sub.add_parser("neigh", help="neighbours of a node")
    nb.add_argument("node"); nb.add_argument("--depth", type=int, default=1)
    nb.add_argument("--avoid", default="", help="nodes to skip (comma-separated), e.g. index,log")
    pa = sub.add_parser("path", help="shortest path between two nodes")
    pa.add_argument("a"); pa.add_argument("b")
    pa.add_argument("--avoid", default="index,log", help="nodes to skip (comma-separated)")
    sg = sub.add_parser("sub", help="subgraph around a node")
    sg.add_argument("node"); sg.add_argument("--depth", type=int, default=2)
    sg.add_argument("--avoid", default="", help="nodes to skip (comma-separated)")
    rk = sub.add_parser("rank", help="PageRank top list")
    rk.add_argument("--top", type=int, default=20)

    args = ap.parse_args(argv)
    db = Path(args.db)
    adj, groups = load(db, args.zone)

    if args.cmd == "stats":
        deg = sorted((len(v) for v in adj.values()), reverse=True)
        internal = sum(1 for g in groups.values() if g != "external")
        data = {
            "zone": args.zone, "db": str(db),
            "nodes": len(adj), "edges": sum(deg) // 2,
            "internal_nodes": internal,
            "external_nodes": len(adj) - internal,
            "isolated": sum(1 for d in deg if d == 0),
            "max_degree": deg[0] if deg else 0,
            "groups": {g: sum(1 for x in groups.values() if x == g)
                       for g in sorted(set(groups.values()))},
        }
        if args.as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"Zone {data['zone']}: {data['nodes']} nodes, {data['edges']} edges "
                  f"({data['internal_nodes']} internal, {data['external_nodes']} dead targets, "
                  f"{data['isolated']} isolated, max. degree {data['max_degree']}).")
            print("Groups: " + ", ".join(f"{g}={c}" for g, c in data["groups"].items()))
        return 0

    if args.cmd == "find":
        hits = sorted((k for k in adj if args.needle.lower() in k.lower()),
                      key=lambda k: (-len(adj[k]), k))[:args.limit]
        if args.as_json:
            print(json.dumps([{"id": k, "group": groups[k], "neighbours": len(adj[k])}
                              for k in hits], ensure_ascii=False, indent=2))
        elif not hits:
            print(f"Nothing matches '{args.needle}'.")
        else:
            for k in hits:
                print(f"{k}  [{groups[k]}, {len(adj[k])} neighbours]")
        return 0

    if args.cmd == "neigh":
        start = resolve(adj, args.node)
        seen, order = bfs(adj, start, args.depth, split_avoid(args.avoid))
        near = [k for k in order if k != start]
        if args.as_json:
            print(json.dumps({"start": start, "group": groups[start],
                              "neighbours": [{"id": k, "distance": seen[k],
                                              "shared": adj[start].get(k, 0)} for k in near]},
                             ensure_ascii=False, indent=2))
        else:
            print(f"{start} [{groups[start]}] has {len(adj[start])} direct neighbours"
                  + (f", {len(near)} nodes within distance {args.depth}."
                     if args.depth > 1 else "."))
            for k in sorted(near, key=lambda x: (seen[x], x)):
                print(f"  d{seen[k]}  {k}  [{groups[k]}]")
        return 0

    if args.cmd == "path":
        a, b = resolve(adj, args.a), resolve(adj, args.b)
        chain = bfs_path(adj, a, b, split_avoid(args.avoid))
        if args.as_json:
            print(json.dumps({"from": a, "to": b, "path": chain,
                              "length": (len(chain) - 1) if chain else None},
                             ensure_ascii=False, indent=2))
        elif chain is None:
            print(f"No path between {a} and {b} (separate components).")
        else:
            print(f"Shortest path, {len(chain)-1} steps:")
            for i, k in enumerate(chain):
                print(f"  {i}: {k} [{groups[k]}]")
        return 0

    if args.cmd == "sub":
        start = resolve(adj, args.node)
        seen, order = bfs(adj, start, args.depth, split_avoid(args.avoid))
        outside = sum(1 for k in order if k != start)
        if args.as_json:
            print(json.dumps({"center": start, "depth": args.depth,
                              "nodes": [{"id": k, "distance": seen[k], "group": groups[k]}
                                        for k in order]},
                             ensure_ascii=False, indent=2))
        else:
            print(f"Subgraph around {start} (depth {args.depth}): {len(order)} nodes, "
                  f"{outside} besides the center.")
            by_grp: dict[str, list[str]] = {}
            for k in order:
                by_grp.setdefault(groups[k], []).append(k)
            for g in sorted(by_grp):
                print(f"  {g}: {len(by_grp[g])} -> " + ", ".join(sorted(by_grp[g])[:12])
                      + (" ..." if len(by_grp[g]) > 12 else ""))
        return 0

    if args.cmd == "rank":
        r = pagerank(adj)
        top = sorted(r.items(), key=lambda kv: -kv[1])[:args.top]
        if args.as_json:
            print(json.dumps([{"id": k, "pagerank": round(v, 6), "group": groups[k]}
                              for k, v in top], ensure_ascii=False, indent=2))
        else:
            print(f"PageRank (zone {args.zone}), top {len(top)}:")
            for k, v in top:
                print(f"  {v:.4f}  {k}  [{groups[k]}]")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
