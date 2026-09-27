#!/usr/bin/env python3
"""add_vector_edges.py — add semantic (vector) edges to a force-graph JSON file.

Wikilink graphs only know explicit links. This tool embeds the notes of the
vault with a local embedding server (llama.cpp `llama-server --embedding`,
e.g. nomic-embed-text-v1.5) and adds cosine-similarity edges between notes.
Wikilink edges stay unchanged. Purely additive, nothing is deleted, the vault
is read-only. The original JSON is backed up once before the first write.

Usage:
  add_vector_edges.py VAULT GRAPH_JSON [--top-k 8] [--min-sim 0.60]
                      [--cache FILE] [--vectors NPZ] [--embed-url URL]
                      [--seed NODE] [--only LIST] [--report] [--dry-run]

Example:
  python3 add_vector_edges.py ~/notes public/graph-wiki.json --report --dry-run

Edge format: wikilink edges stay {"source","target"}; vector edges are added as
{"source","target","kind":"vector","w":<cosine>}. Import the result into the
edge DB with vault_graph_import.py.

--report prints concrete before/after graph metrics (degree, components, mean
shortest path, share of edges touching hub notes, PageRank concentration) and,
with --seed, how Personalized PageRank from that node changes.

Environment: EMBED_URL (default http://127.0.0.1:8081).

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np

IGNORE = {".obsidian", ".git", ".trash", ".wiki-meta", ".embed-cache"}
CHUNK_CHARS = 800      # stays safely below the 512-token limit of nomic-embed
CHUNK_OVERLAP = 100
EMBED_DIM = 768
HUB_NODES = ("index", "log")
DEFAULT_EMBED_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8081")

# ------------------------------------------------------------------ reading


def iter_md(root: Path):
    for p in sorted(root.rglob("*.md")):
        if any(part in IGNORE for part in p.parts):
            continue
        yield p


def chunk_text(text: str, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP):
    """Whitespace-normalised chunks, cut at word boundaries, with overlap."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + max_chars // 2, end)
            if cut > start:
                end = cut
        chunks.append(text[start:end])
        start = max(end - overlap, start + 1)
    return chunks


# ------------------------------------------------------------------ embedding


class Embedder:
    """Minimal client for the llama.cpp /embedding endpoint."""

    def __init__(self, url: str, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def health(self) -> int:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=3) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return 0

    def wait_ready(self, seconds: float = 30.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < seconds:
            if self.health() == 200:
                return True
            time.sleep(0.4)
        return False

    def _once(self, texts: list[str]) -> np.ndarray:
        req = urllib.request.Request(
            self.url + "/embedding",
            data=json.dumps({"content": texts}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            out = json.loads(resp.read())
        vecs = []
        for item in sorted(out, key=lambda x: x.get("index", 0)):
            emb = item["embedding"]
            if emb and isinstance(emb[0], list):
                emb = emb[0]
            vecs.append(np.asarray(emb, dtype=np.float32))
        return np.vstack(vecs)

    def embed(self, texts: list[str], batch: int = 8) -> np.ndarray:
        """(n, 768) float32, L2-normalised. Halves the batch on errors; a single
        text that hits the token limit is split at a word boundary and averaged."""
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        out = []
        for i in range(0, len(texts), batch):
            out.append(self._part(texts[i:i + batch]))
        m = np.vstack(out)
        norm = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.maximum(norm, 1e-9)

    def _part(self, part: list[str]) -> np.ndarray:
        for attempt in range(3):
            try:
                return self._once(part)
            except Exception:
                if attempt == 2 and len(part) > 1:
                    mid = len(part) // 2
                    return np.vstack([self._part(part[:mid]), self._part(part[mid:])])
                if attempt == 2:
                    return self._split_single(part[0])
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError("unreachable")

    def _split_single(self, text: str) -> np.ndarray:
        words = text.split(" ")
        if len(words) < 2:
            # Cannot be split further (empty/short text): a zero vector is more
            # honest than an endless retry loop.
            return np.zeros((1, EMBED_DIM), dtype=np.float32)
        mid = max(len(words) // 2, 1)
        a = self.embed([" ".join(words[:mid])])
        b = self.embed([" ".join(words[mid:])])
        v = (a + b) / 2.0
        return v / max(float(np.linalg.norm(v)), 1e-9)


def embed_vault(vault: Path, graph_ids: set[str], cache: Path, embedder: Embedder,
                log=print):
    """One vector per note stem; cache keyed by text hash (resume/idempotency)."""
    notes: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for p in iter_md(vault):
        if p.stem not in graph_ids:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        notes[p.stem] = txt
        hashes[p.stem] = hashlib.md5(txt.encode()).hexdigest()

    cached_ids: list[str] = []
    cached_mat = np.zeros((0, EMBED_DIM), dtype=np.float32)
    cached_hash: dict[str, str] = {}
    if cache.exists():
        try:
            z = np.load(cache, allow_pickle=False)
            cached_ids = [str(x) for x in z["ids"]]
            cached_mat = z["mat"]
            cached_hash = json.loads(str(z["hashes"][0])) if z["hashes"].size else {}
        except Exception as e:  # noqa: BLE001
            log(f"  cache unreadable ({e}) — rebuilding")

    keep_idx = [i for i, i_d in enumerate(cached_ids)
                if hashes.get(i_d) == cached_hash.get(i_d)]
    reuse = {cached_ids[i]: cached_mat[i] for i in keep_idx}

    new_ids = [i for i in notes if i not in reuse]
    log(f"  notes in graph: {len(notes)} | from cache: {len(reuse)} | new: {len(new_ids)}")
    if new_ids:
        texts, owner = [], []
        for i in new_ids:
            for c in chunk_text(notes[i]):
                texts.append(c)
                owner.append(i)
        log(f"  new chunks: {len(texts)}")
        t0 = time.time()
        mat = embedder.embed(texts)
        out = np.zeros((len(new_ids), EMBED_DIM), dtype=np.float32)
        pos = {i: k for k, i in enumerate(new_ids)}
        for k, ow in enumerate(owner):
            out[pos[ow]] += mat[k]
        cnt = np.zeros(len(new_ids), dtype=np.float32)
        for ow in owner:
            cnt[pos[ow]] += 1
        out /= np.maximum(cnt, 1)[:, None]
        for k, i in enumerate(new_ids):
            reuse[i] = out[k]
        log(f"  embedded in {time.time() - t0:.1f}s ({len(texts) / max(time.time() - t0, 1e-9):.1f} chunks/s)")

    ids = [i for i in notes if i in reuse]
    mat = np.vstack([reuse[i] for i in ids]) if ids else np.zeros((0, EMBED_DIM), dtype=np.float32)
    if not ids:
        return ids, mat
    mat = mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache, ids=np.array(ids), mat=mat, hashes=np.array([json.dumps(hashes)]))
    return ids, mat


# ------------------------------------------------------------------ graph metrics


def load_graph(path: Path):
    d = json.loads(path.read_text(encoding="utf-8"))
    return d["nodes"], d["links"]


def weighted_adj(nodes, links, use_vector=True):
    adj = {n["id"]: {} for n in nodes}
    for l in links:
        if l.get("kind") == "vector" and not use_vector:
            continue
        w = float(l.get("w", 1.0))
        s, t = l["source"], l["target"]
        if s == t or s not in adj or t not in adj:
            continue
        adj[s][t] = max(adj[s].get(t, 0.0), w)
        adj[t][s] = max(adj[t].get(s, 0.0), w)
    return adj


def pagerank(adj, alpha=0.85, iters=200):
    ids = sorted(adj)
    idx = {i: k for k, i in enumerate(ids)}
    n = len(ids)
    A = np.zeros((n, n))
    for s, nbrs in adj.items():
        for t, w in nbrs.items():
            A[idx[s], idx[t]] = w
    out = A.sum(axis=1)
    dead = out == 0
    A = A / np.maximum(out[:, None], 1e-12)
    pr = np.full(n, 1.0 / n)
    for _ in range(iters):
        nxt = alpha * (A.T @ pr) + alpha * pr[dead].sum() / n + (1 - alpha) / n
        if np.abs(nxt - pr).sum() < 1e-10:
            pr = nxt
            break
        pr = nxt
    return {i: float(pr[idx[i]]) for i in ids}


def ppr_from(adj, seed, alpha=0.85, iters=200):
    """Personalized PageRank with all restart mass on one seed node."""
    ids = sorted(adj)
    idx = {i: k for k, i in enumerate(ids)}
    n = len(ids)
    A = np.zeros((n, n))
    for s, nbrs in adj.items():
        for t, w in nbrs.items():
            A[idx[s], idx[t]] = w
    out = A.sum(axis=1)
    A = A / np.maximum(out[:, None], 1e-12)
    e = np.zeros(n)
    e[idx[seed]] = 1.0
    p = e.copy()
    for _ in range(iters):
        nxt = alpha * (A.T @ p) + (1 - alpha) * e
        if np.abs(nxt - p).sum() < 1e-12:
            p = nxt
            break
        p = nxt
    return {i: float(p[idx[i]]) for i in ids}


def components(adj):
    seen, comps = set(), []
    for s in adj:
        if s in seen:
            continue
        q, comp = deque([s]), []
        seen.add(s)
        while q:
            c = q.popleft()
            comp.append(c)
            for t in adj[c]:
                if t not in seen:
                    seen.add(t)
                    q.append(t)
        comps.append(comp)
    return comps


def mean_shortest_path(adj, limit=None):
    tot, cnt = 0, 0
    for s in adj:
        dist = {s: 0}
        q = deque([s])
        while q:
            c = q.popleft()
            for t in adj[c]:
                if t not in dist:
                    dist[t] = dist[c] + 1
                    q.append(t)
        for t, d in dist.items():
            if t != s:
                tot += d
                cnt += 1
    return (tot / cnt if cnt else 0.0), cnt


def stats(adj, nodes, links, group, label):
    deg = {i: len(v) for i, v in adj.items()}
    degs = np.array(list(deg.values()), dtype=float)
    pr = pagerank(adj)
    top = sorted(pr.items(), key=lambda kv: -kv[1])[:5]
    comps = components(adj)
    asp, pairs = mean_shortest_path(adj)
    vec_links = [l for l in links if l.get("kind") == "vector"]
    wl_links = [l for l in links if l.get("kind") != "vector"]
    hub_hit = sum(1 for l in links if l["source"] in HUB_NODES or l["target"] in HUB_NODES)
    same = lambda ls: (sum(1 for l in ls if group.get(l["source"]) == group.get(l["target"])) / len(ls)) if ls else 0.0
    iso = int((degs == 0).sum())
    leaf = int((degs == 1).sum())
    pr_vals = np.array(list(pr.values()))
    return {
        "label": label,
        "nodes": len(adj),
        "links": len(links),
        "wl": len(wl_links),
        "vec": len(vec_links),
        "deg_mean": round(float(degs.mean()), 2),
        "deg_median": float(np.median(degs)),
        "isolated": iso,
        "leaf": leaf,
        "comps": len(comps),
        "biggest_comp": max(len(c) for c in comps),
        "asp": round(asp, 3),
        "asp_pairs": pairs,
        "hub_share": round(hub_hit / max(len(links), 1), 4),
        "pr_index_share": round(pr.get("index", 0.0), 4),
        "pr_top5": [(i, round(v, 4)) for i, v in top],
        # n * sum(p^2): 1.0 = perfectly even PageRank, larger = more concentrated
        "pr_concentration": round(float((pr_vals ** 2).sum() * len(pr_vals)), 2),
        "same_group_wl": round(same(wl_links), 3),
        "same_group_vec": round(same(vec_links), 3),
    }


# ------------------------------------------------------------------ main


def load_vectors(path: Path):
    """Load a vector cache: own format (ids/mat) or the embed_batch.py format
    (ids/vecs). So the same cache can serve both batch embedding and edge building."""
    z = np.load(path.expanduser(), allow_pickle=False)
    ids = [str(x) for x in z["ids"]]
    key = "mat" if "mat" in z else ("vecs" if "vecs" in z else None)
    if key is None:
        raise SystemExit(f"ERROR: {path} has neither 'mat' nor 'vecs'")
    mat = np.asarray(z[key], dtype=np.float32)
    mat = mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)
    return ids, mat


def main():
    ap = argparse.ArgumentParser(description="Add vector (cosine) edges to a graph JSON")
    ap.add_argument("vault", help="path to the Markdown vault")
    ap.add_argument("graph", help="graph JSON ({nodes, links}) to extend")
    ap.add_argument("--top-k", type=int, default=8, help="max. vector neighbours per note")
    ap.add_argument("--min-sim", type=float, default=0.60, help="min. cosine similarity")
    ap.add_argument("--cache", default=None, help="vector cache (default: VAULT/.embed-cache/<graph>-vectors.npz)")
    ap.add_argument("--vectors", default=None,
                    help="ready-made vector cache (npz with ids + vecs/mat), e.g. from embed_batch.py")
    ap.add_argument("--embed-url", default=DEFAULT_EMBED_URL, help="embedding server (default: %(default)s)")
    ap.add_argument("--seed", default=None, help="node for the before/after PPR comparison in --report")
    ap.add_argument("--only", default=None,
                    help="file with relative paths or stems (one per line): only these notes get "
                         "vector edges. The exclusion applies ONLY to vector edges — all nodes "
                         "stay in the graph and remain findable with query_graph.py.")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--report", action="store_true", help="print before/after graph metrics")
    a = ap.parse_args()

    vault = Path(a.vault).expanduser()
    gpath = Path(a.graph)
    cache = Path(a.cache).expanduser() if a.cache else vault / ".embed-cache" / (gpath.stem + "-vectors.npz")
    if not vault.is_dir() or not gpath.is_file():
        print(f"ERROR: vault or graph missing ({vault} / {gpath})", file=sys.stderr)
        sys.exit(2)

    nodes, links = load_graph(gpath)
    group = {n["id"]: n.get("group", "?") for n in nodes}
    ids_graph = set(group)
    print(f"Graph: {len(nodes)} nodes, {len(links)} edges")

    if a.only:
        # Only notes from the list get vector edges (e.g. to skip stubs). The nodes
        # themselves stay in the graph and remain findable.
        want = {l.strip() for l in Path(a.only).read_text(encoding="utf-8").splitlines() if l.strip()}
        stems = {(w.rsplit("/", 1)[-1][:-3] if w.endswith(".md") else w.rsplit("/", 1)[-1]) for w in want}
        before = len(ids_graph)
        ids_graph = {i for i in ids_graph if i in want or i in stems}
        ids_graph = ids_graph or {"\0no-match"}  # an empty set must not let everything through
        print(f"Filter ({a.only}): {len(ids_graph)} of {before} nodes "
              f"get vector edges, {before - len(ids_graph)} skipped")

    if a.vectors:
        ids, mat = load_vectors(Path(a.vectors))
        # embed_batch.py uses relative paths ('concepts/foo.md') as ids, the graph
        # uses stems ('foo'). Map to stems and count collisions.
        canon = {}
        collisions = 0
        for k, i in enumerate(ids):
            s = i.rsplit("/", 1)[-1]
            s = s[:-3] if s.endswith(".md") else s
            if s in canon:
                collisions += 1
                continue
            canon[s] = k
        keep = [s for s in canon if s in ids_graph]
        if len(keep) != len(canon):
            print(f"  {len(canon) - len(keep)} cache entries without graph node, "
                  f"{collisions} stem collision(s) dropped")
        ids = keep
        mat = mat[[canon[s] for s in ids]]
        print(f"Vectors from {a.vectors}: {len(ids)} (mapped onto graph nodes)")
    else:
        emb = Embedder(a.embed_url)
        if emb.health() != 200:
            print(f"Embedding server {a.embed_url} not ready — waiting up to 30s ...")
            if not emb.wait_ready(30):
                print("ERROR: embedding server does not respond", file=sys.stderr)
                sys.exit(3)
        ids, mat = embed_vault(vault, ids_graph, cache, emb)
        print(f"Vectors: {len(ids)}")

    sim = mat @ mat.T
    np.fill_diagonal(sim, -1.0)
    k = min(a.top_k, len(ids))
    existing = {(l["source"], l["target"]) for l in links} | {(l["target"], l["source"]) for l in links}
    new = []
    for i, nid in enumerate(ids):
        order = np.argsort(-sim[i])[:k]
        for j in order:
            if sim[i, j] < a.min_sim:
                break
            other = ids[j]
            if (nid, other) in existing:
                continue
            existing.add((nid, other))
            existing.add((other, nid))
            s, t = sorted((nid, other))
            new.append({"source": s, "target": t, "kind": "vector", "w": round(float(sim[i, j]), 4)})
    print(f"New vector edges: {len(new)} (top-{k}, min-sim {a.min_sim})")

    adj_wl = weighted_adj(nodes, links, use_vector=False)
    adj_all = weighted_adj(nodes, links + new, use_vector=True)
    if a.report:
        st_before = stats(adj_wl, nodes, links, group, "before (wikilinks only)")
        st_after = stats(adj_all, nodes, links + new, group, "after (wikilinks + vector)")
        print("\n--- metrics ---")
        keys = ["nodes", "links", "wl", "vec", "deg_mean", "deg_median", "isolated", "leaf",
                "comps", "biggest_comp", "asp", "hub_share", "same_group_wl", "same_group_vec",
                "pr_index_share", "pr_concentration", "pr_top5"]
        for key in keys:
            print(f"  {key:16s} before: {st_before.get(key)}\n  {'':16s} after:  {st_after.get(key)}")
        if a.seed and a.seed in adj_wl:
            p1 = ppr_from(adj_wl, a.seed)
            p2 = ppr_from(adj_all, a.seed)
            t1 = [i for i, _ in sorted(p1.items(), key=lambda kv: -kv[1])[:10] if i != a.seed]
            t2 = [i for i, _ in sorted(p2.items(), key=lambda kv: -kv[1])[:10] if i != a.seed]
            print(f"\n--- PPR from seed '{a.seed}' ---")
            print(f"  top-10 before : {t1}")
            print(f"  top-10 after  : {t2}")
            print(f"  overlap       : {len(set(t1) & set(t2))}/10")
            print(f"  new after     : {[i for i in t2 if i not in t1]}")
            print(f"  dropped after : {[i for i in t1 if i not in t2]}")

    if a.dry_run:
        print("dry-run: nothing written")
        return
    bak = gpath.with_suffix(gpath.suffix + ".bak-before-vector-edges")
    if not bak.exists():
        bak.write_text(gpath.read_text(encoding="utf-8"), encoding="utf-8")
    gpath.write_text(json.dumps({"nodes": nodes, "links": links + new}, ensure_ascii=False), encoding="utf-8")
    print(f"written: {gpath} (+{len(new)} edges, backup {bak.name})")


if __name__ == "__main__":
    main()
