#!/usr/bin/env python3
"""eval_vector_quality.py — measure how well note embeddings separate content (numbers, no opinion).

Question it answers: do the embedding vectors separate notes in a meaningful
way — and does that also hold for code-heavy or template-like notes, or only
for prose?

Usage:
  eval_vector_quality.py CACHE_NPZ [--json GRAPH_JSON | --labels LABELS_JSON] [--k 8] [--examples 4]

Metrics:
  - intra/inter: mean cosine within the same group vs. between groups
    (group = folder/group from the graph JSON, or a label). Difference = separation.
  - knn_acc: leave-one-out accuracy of predicting a node's group from the
    majority of its k nearest neighbours, next to the majority-class baseline
    (a predictor that always guesses the largest group).
  - balanced accuracy (macro recall) and accuracy without the largest class,
    because plain accuracy is misleading with very unbalanced groups.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

import numpy as np


def main():
    ap = argparse.ArgumentParser(description="Measure the separation power of note embeddings")
    ap.add_argument("cache", help="vector cache (.npz with ids + mat/vecs)")
    ap.add_argument("--json", default=None, help="graph JSON; node 'group' is used as class")
    ap.add_argument("--labels", default=None,
                    help="JSON {stem: class} — content labels instead of folder groups")
    ap.add_argument("--k", type=int, default=8, help="neighbours for the kNN vote")
    ap.add_argument("--examples", type=int, default=4, help="random example notes to print")
    a = ap.parse_args()

    z = np.load(Path(a.cache).expanduser(), allow_pickle=False)
    key = "mat" if "mat" in z else ("vecs" if "vecs" in z else None)
    if key is None:
        raise SystemExit("cache has neither 'mat' nor 'vecs'")
    # Map ids to stems ('concepts/foo.md' -> 'foo') so they match graph nodes
    # (build_graph.py uses stems).
    raw = [str(x) for x in z["ids"]]
    mat = np.asarray(z[key], dtype=np.float32)
    ids, seen = [], {}
    for k, i in enumerate(raw):
        s = i.rsplit("/", 1)[-1]
        s = s[:-3] if s.endswith(".md") else s
        if s in seen:
            continue
        seen[s] = k
        ids.append(s)
    mat = mat[[seen[i] for i in ids]]
    mat = mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)
    lab: dict[str, str] = {}
    if a.labels:
        # Content labels instead of folder groups. Notes without a label are left
        # out of the measurement so the classes stay balanced.
        lab = json.loads(Path(a.labels).read_text(encoding="utf-8"))
        keep = [k for k, i in enumerate(ids) if i in lab]
        ids = [ids[k] for k in keep]
        mat = mat[keep]
        n = len(ids)
        print(f"label mode: {n} labelled notes, {len(set(lab[i] for i in ids))} classes")
    else:
        n = len(ids)
    sim = mat @ mat.T
    np.fill_diagonal(sim, -1.0)
    print(f"nodes: {n} | dimension: {mat.shape[1]}")

    group = {}
    if a.labels:
        group = {i: lab[i] for i in ids}
    elif a.json:
        d = json.loads(Path(a.json).read_text(encoding="utf-8"))
        group = {x["id"]: x.get("group", "?") for x in d["nodes"]}
    if not group:
        print("no graph JSON / labels: neighbour examples only")
    else:
        g = np.array([group.get(i, "?") for i in ids])
        same = g[:, None] == g[None, :]
        off = ~np.eye(n, dtype=bool)
        intra = sim[same & off]
        inter = sim[~same & off]
        print(f"intra-group mean cos: {intra.mean():.4f} (n={intra.size})")
        print(f"inter-group mean cos: {inter.mean():.4f} (n={inter.size})")
        print(f"separation (intra-inter): {intra.mean() - inter.mean():+.4f}")

        k = min(a.k, n - 1)
        nb = np.argsort(-sim, axis=1)[:, :k]
        pred = np.array([collections.Counter(g[r]).most_common(1)[0][0] for r in nb])
        acc = float(np.mean(pred == g))
        base = collections.Counter(g).most_common(1)[0][1] / n
        print(f"knn_acc (k={k}, leave-one-out): {acc:.3f}  | majority-class baseline: {base:.3f}")
        print(f"improvement over baseline: {acc - base:+.3f}")
        recalls = []
        for cls in set(g):
            m = g == cls
            recalls.append(float(np.mean(pred[m] == g[m])) if m.any() else 0.0)
        big = collections.Counter(g).most_common(1)[0][0]
        keep = g != big
        print(f"balanced accuracy (macro recall): {np.mean(recalls):.3f}  (chance ~{1 / len(set(g)):.3f})")
        print(f"knn_acc without largest class '{big}': {float(np.mean(pred[keep] == g[keep])):.3f} (n={int(keep.sum())})")
        print("group distribution:", dict(collections.Counter(g).most_common()))

    ex = min(a.examples, n)
    print("\nexample neighbours (top-3, cosine):")
    rng = np.random.default_rng(7)
    pick = list(rng.choice(n, size=ex, replace=False))
    for i in pick:
        row = np.argsort(-sim[i])[:3]
        nbs = ", ".join(f"{ids[j]} ({sim[i, j]:.3f}{'/' + group.get(ids[j], '?') if group else ''})" for j in row)
        print(f"  {ids[i]} [{group.get(ids[i], '?')}] -> {nbs}")


if __name__ == "__main__":
    main()
