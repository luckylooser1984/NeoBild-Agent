"""search.py — hybrid retrieval for a local knowledge base: FTS5 ∪ cosine kNN ∪ Personalized PageRank.

Three signals are fused with fixed weights:
  1. full-text search (SQLite FTS5 / BM25), rank-normalised as 1/(rank+1)
  2. cosine similarity between the query embedding and note embeddings
  3. Personalized PageRank over the note graph, seeded with the top-3 cosine
     hits plus notes that mention an entity found in the query (alias match)

The module is storage-agnostic: it only needs an object implementing the small
`Storage` protocol below and an embedder with `embed(list[str]) -> ndarray | None`.

Known limitation (kept on purpose to stay faithful to the original): query
terms are joined with AND for FTS, so long natural-language questions often
yield zero FTS hits; the cosine and PPR signals still work. An OR + BM25 query
is the obvious improvement.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

from typing import Any, Protocol

import numpy as np

try:
    from . import graph
except ImportError:  # used as plain script folder
    import graph

# Fusion weights: FTS5 | cosine | PPR
DEFAULT_FUSE_W = (0.30, 0.40, 0.30)


class Storage(Protocol):
    """What hybrid_search() needs from the note store (rows are dict-like)."""

    def fts_search(self, query: str, limit: int) -> list[Any]: ...        # rows with "id"
    def all_embeddings(self) -> list[tuple[str, np.ndarray]]: ...          # (note_id, vec)
    def known_entities(self) -> list[tuple[str, str, list[str]]]: ...      # (id, canonical, aliases)
    def note_ids_by_entity(self, entity_id: str) -> list[str]: ...
    def all_notes(self) -> list[Any]: ...                                  # rows with "id"
    def all_edges(self) -> list[tuple[str, str, str, float]]: ...          # (src, dst, kind, weight)
    def get_note(self, note_id: str) -> Any: ...                           # row: slug, title, summary
    def log_usage(self, note_id: str, context: str = "") -> None: ...


def cosine_knn(query_vec: np.ndarray, note_embeddings: list[tuple[str, np.ndarray]],
               top_k: int) -> list[tuple[str, float]]:
    """Brute-force cosine over all note embeddings (trivial for < 10k notes)."""
    if not note_embeddings:
        return []
    q = query_vec / (np.linalg.norm(query_vec) + 1e-12)
    sims = []
    for nid, vec in note_embeddings:
        v = vec / (np.linalg.norm(vec) + 1e-12)
        sims.append((nid, float(q @ v)))
    sims.sort(key=lambda x: x[1], reverse=True)
    return sims[:top_k]


def _query_entities(query: str, entities: list[tuple[str, str, list[str]]]) -> list[str]:
    """Query -> known entity ids (deterministic alias matching)."""
    ql = query.lower()
    hits = []
    for eid, _canonical, aliases in entities:
        if any(a in ql for a in aliases if len(a) > 2):
            hits.append(eid)
    return hits


def hybrid_search(storage: Storage, emb, query: str, top_k: int = 8, use_ppr: bool = True,
                  log_usage: bool = True,
                  fuse_w: tuple[float, float, float] = DEFAULT_FUSE_W) -> list[dict]:
    """Fusion of FTS5 (BM25) ∪ cosine ∪ PPR.
    Returns [{'id','slug','title','summary','score','sources'}]."""
    # 1) FTS5 — join query terms with AND, quoted to be robust against special chars
    terms = [t for t in query.lower().split() if t]
    fts_query = " AND ".join(f'"{t}"' for t in terms) if terms else ""
    fts_res = storage.fts_search(fts_query, top_k * 2) if fts_query else []

    # 2) cosine — embed the query (fallback: FTS only)
    note_embs = storage.all_embeddings()
    cos_res: list[tuple[str, float]] = []
    if note_embs:
        vecs = emb.embed([query])
        if vecs is not None:
            cos_res = cosine_knn(vecs[0], note_embs, top_k * 2)

    # 3) PPR — seeds: cosine top-3 + entity hits
    ppr_scores: dict[str, float] = {}
    if use_ppr and cos_res:
        seeds = {nid: w for nid, w in cos_res[:3]}
        for eid in _query_entities(query, storage.known_entities()):
            for nid in storage.note_ids_by_entity(eid):
                seeds[nid] = seeds.get(nid, 0.0) + 0.5
        ids = [r["id"] for r in storage.all_notes()]
        edges = storage.all_edges()
        if ids and seeds:
            ppr_scores = graph.ppr(ids, edges, seeds)

    # fusion
    scores: dict[str, dict] = {}
    for i, r in enumerate(fts_res):
        d = scores.setdefault(r["id"], {"fts": 0.0, "cos": 0.0, "ppr": 0.0})
        d["fts"] = 1.0 / (i + 1)                      # rank-based normalisation
    for nid, w in cos_res:
        d = scores.setdefault(nid, {"fts": 0.0, "cos": 0.0, "ppr": 0.0})
        d["cos"] = max(d["cos"], w)
    for nid, w in ppr_scores.items():
        d = scores.setdefault(nid, {"fts": 0.0, "cos": 0.0, "ppr": 0.0})
        d["ppr"] = max(d["ppr"], w)

    w_fts, w_cos, w_ppr = fuse_w
    fused = []
    for nid, d in scores.items():
        score = w_fts * d["fts"] + w_cos * d["cos"] + w_ppr * d["ppr"]
        fused.append((nid, score))
    fused.sort(key=lambda x: x[1], reverse=True)

    out = []
    for nid, score in fused[:top_k]:
        n = storage.get_note(nid)
        if n is None:
            continue
        if log_usage:
            storage.log_usage(nid, context=query)
        d = scores.get(nid, {})
        out.append({
            "id": nid, "slug": n["slug"], "title": n["title"],
            "summary": n["summary"], "score": round(score, 4),
            "sources": [k for k, v in d.items() if v > 0] if nid in scores else [],
        })
    return out
