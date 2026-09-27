#!/usr/bin/env python3
"""cosine_stats.py — saturation and top-1 hit rate of one or more vector caches.

Question it answers: are the notes in a corpus actually distinguishable for the
embedding model, or does it map template-like documents (advisories, generated
reports, boilerplate) onto almost the same point? Compare e.g. a prose wiki
against a template-heavy corpus.

Usage:
  cosine_stats.py NAME=CACHE.npz [NAME=CACHE.npz ...] [--labels LABELS_JSON --labels-for NAME]

Per cache it prints: share of pairs above cos 0.99/0.95/0.90, median/min top-1
cosine, the number of practically identical notes (top-1 cos >= 0.999), and —
with labels — how often the nearest neighbour has the same class.

Author: Lukas Weißmann
License: MIT
"""
import argparse
import json
from pathlib import Path

import numpy as np


def load(path):
    z = np.load(path, allow_pickle=False)
    key = "mat" if "mat" in z else "vecs"
    ids = []
    for x in z["ids"]:
        s = str(x).rsplit("/", 1)[-1]
        ids.append(s[:-3] if s.endswith(".md") else s)
    m = np.asarray(z[key], dtype=np.float32)
    m = m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    return ids, m


def stats(name, ids, m, labels=None):
    sim = m @ m.T
    np.fill_diagonal(sim, -1.0)
    n = len(ids)
    iu = np.triu_indices(n, 1)
    p = sim[iu]
    top1 = sim.argmax(axis=1)
    print(f"\n--- {name} ({n} notes, {p.size} pairs) ---")
    for t in (0.99, 0.95, 0.90):
        print(f"  pairs with cos >= {t:.2f}: {int((p >= t).sum())} ({(p >= t).mean() * 100:.1f} %)")
    print(f"  top-1 cos: median {np.median(sim.max(axis=1)):.4f}, min {sim.max(axis=1).min():.4f}")
    if labels:
        lab = np.array([labels.get(i, "?") for i in ids])
        hit = (lab[top1] == lab)
        sel = lab != "?"
        print(f"  top-1 neighbour in same class: {hit[sel].mean() * 100:.1f} % "
              f"(n={int(sel.sum())}, chance {max(np.bincount(np.unique(lab[sel], return_inverse=True)[1])) / sel.sum():.3f})")
    # How many notes are practically duplicates (top-1 >= 0.999)?
    dup = int((sim.max(axis=1) >= 0.999).sum())
    print(f"  notes with top-1 cos >= 0.999 (practically identical): {dup} ({dup / n * 100:.1f} %)")


def main():
    ap = argparse.ArgumentParser(description="Saturation / top-1 statistics of vector caches")
    ap.add_argument("caches", nargs="+", metavar="NAME=CACHE", help="named vector caches (.npz)")
    ap.add_argument("--labels", default=None, help="JSON {stem: class}")
    ap.add_argument("--labels-for", default=None, help="NAME of the cache the labels belong to")
    a = ap.parse_args()
    lab = json.loads(Path(a.labels).read_text(encoding="utf-8")) if a.labels else None
    for spec in a.caches:
        name, path = spec.split("=", 1)
        ids, m = load(path)
        use = lab if (lab and (a.labels_for is None or a.labels_for == name)) else None
        stats(name, ids, m, use)


if __name__ == "__main__":
    main()
