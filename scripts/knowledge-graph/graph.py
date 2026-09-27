"""graph.py — small graph algorithms on numpy for a note/knowledge graph.

  - pagerank()          global PageRank (alpha = 0.85), dangling nodes spread evenly
  - ppr()               Personalized PageRank per query (HippoRAG style: restart on query seeds)
  - cosine_matrix()     pairwise cosine similarity of embedding vectors
  - adamic_adar()       link-prediction score over shared neighbours
  - label_propagation() near-linear community detection (e.g. for "map of content" candidates)

Edges are tuples (src, dst, kind, weight). Dense matrices are used on purpose:
for personal knowledge bases (< ~10k notes) this is simple and fast enough.
No dependencies besides numpy.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np


def _index(ids: list[str]) -> dict[str, int]:
    return {nid: i for i, nid in enumerate(ids)}


def adjacency(ids: list[str], edges: list[tuple[str, str, str, float]]):
    """Directed adjacency as numpy matrix (row = src, col = dst)."""
    idx = _index(ids)
    n = len(ids)
    A = np.zeros((n, n), dtype=np.float64)
    for src, dst, _kind, weight in edges:
        if src in idx and dst in idx:
            A[idx[src], idx[dst]] += weight
    return A, idx


def pagerank(ids: list[str], edges: list[tuple[str, str, str, float]],
             alpha: float = 0.85, iterations: int = 40, tol: float = 1e-6) -> dict[str, float]:
    """Global PageRank. Dangling nodes -> uniform distribution (classic)."""
    n = len(ids)
    if n == 0:
        return {}
    A, idx = adjacency(ids, edges)
    out_deg = A.sum(axis=1)
    P = np.zeros((n, n))
    nz = out_deg > 0
    P[nz] = A[nz] / out_deg[nz, None]
    dangling = np.ones(n) / n
    teleport = np.ones(n) / n
    pr = teleport.copy()
    for _ in range(iterations):
        # dangling contribution: all dangling nodes spread their share evenly
        new_pr = alpha * (P.T @ pr) + alpha * (pr * (~nz)).sum() * dangling + (1 - alpha) * teleport
        if np.abs(new_pr - pr).sum() < tol:
            pr = new_pr
            break
        pr = new_pr
    return {nid: float(pr[i]) for nid, i in idx.items()}


def ppr(ids: list[str], edges: list[tuple[str, str, str, float]],
        seed_weights: dict[str, float], alpha: float = 0.85,
        iterations: int = 40) -> dict[str, float]:
    """Personalized PageRank (HippoRAG style): restart distribution = query seeds."""
    n = len(ids)
    if n == 0:
        return {}
    A, idx = adjacency(ids, edges)
    out_deg = A.sum(axis=1)
    P = np.zeros((n, n))
    nz = out_deg > 0
    P[nz] = A[nz] / out_deg[nz, None]
    dangling = np.ones(n) / n
    v = np.zeros(n)
    for nid, w in seed_weights.items():
        if nid in idx:
            v[idx[nid]] = w
    s = v.sum()
    if s == 0:
        v = np.ones(n) / n
    else:
        v /= s
    pr = v.copy()
    for _ in range(iterations):
        new_pr = alpha * (P.T @ pr) + alpha * (pr * (~nz)).sum() * dangling + (1 - alpha) * v
        pr = new_pr
    return {nid: float(pr[i]) for nid, i in idx.items()}


def cosine_matrix(vecs: list[np.ndarray]) -> np.ndarray:
    """Pairwise cosine similarity (n x n); vectors are normalised here."""
    M = np.stack(vecs)
    M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-12
    return M @ M.T


def adamic_adar(ids: list[str], edges: list[tuple[str, str, str, float]]) -> dict[tuple[str, str], float]:
    """Adamic/Adar over shared neighbours (treated as undirected). Returns pairs with score > 0.5."""
    nb = defaultdict(set)
    for src, dst, _k, _w in edges:
        nb[src].add(dst)
        nb[dst].add(src)
    out: dict[tuple[str, str], float] = {}
    for u in ids:
        for v in ids:
            if u >= v:
                continue
            common = nb[u] & nb[v]
            if common:
                score = sum(1.0 / np.log(len(nb[c]) + 1) for c in common)
                if score > 0.5:
                    out[(u, v)] = score
    return out


def label_propagation(ids: list[str], edges: list[tuple[str, str, str, float]],
                      iterations: int = 20) -> dict[str, int]:
    """Community detection via label propagation (near-linear)."""
    nb = defaultdict(set)
    for src, dst, _k, _w in edges:
        if src != dst:
            nb[src].add(dst)
            nb[dst].add(src)
    labels = {nid: i for i, nid in enumerate(ids)}
    for _ in range(iterations):
        changed = False
        for nid in ids:
            if not nb[nid]:
                continue
            cnt = Counter(labels[x] for x in nb[nid])
            new_label = cnt.most_common(1)[0][0]
            if new_label != labels[nid]:
                labels[nid] = new_label
                changed = True
        if not changed:
            break
    # compact re-indexing
    remap = {lbl: i for i, lbl in enumerate(sorted(set(labels.values())))}
    return {nid: remap[lbl] for nid, lbl in labels.items()}


if __name__ == "__main__":
    # Tiny self-demo: two triangles joined by one bridge.
    ids = ["a", "b", "c", "x", "y", "z"]
    E = [(s, d, "link", 1.0) for s, d in
         [("a", "b"), ("b", "c"), ("c", "a"), ("x", "y"), ("y", "z"), ("z", "x"), ("c", "x")]]
    print("pagerank :", {k: round(v, 3) for k, v in pagerank(ids, E).items()})
    print("ppr(a)   :", {k: round(v, 3) for k, v in ppr(ids, E, {"a": 1.0}).items()})
    print("adamic   :", {k: round(float(v), 3) for k, v in adamic_adar(ids, E).items()})
    print("labels   :", label_propagation(ids, E))
