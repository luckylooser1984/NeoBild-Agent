"""lint.py — health checks for a local knowledge base.

Findings (list of dicts with level/kind/slug/msg):
  - orphan               note without incoming edges
  - broken-link          [[target]] without a matching note file
  - stale                note not updated for longer than the recency half-life
  - duplicate            cosine similarity of two note embeddings above a threshold
  - claim-stale          evidence claim not re-checked for too long
  - evidence-overclaim   claim rated strong/very_strong with too few sources
  - evidence-no-sources  claim above "anecdotal" without any source

Storage-agnostic: pass any object implementing the `Storage` protocol below.
Thresholds are parameters of `LintConfig`.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np

try:
    from . import graph
except ImportError:  # used as plain script folder
    import graph


@dataclass
class LintConfig:
    recency_halflife_days: float = 180.0   # notes older than this are "stale"
    cosine_dup_threshold: float = 0.95     # duplicate suspicion above this cosine
    evidence_stale_days: int = 90          # claim check older than this -> warning
    evidence_overclaim_sources: int = 2    # strong claims need at least N sources


class Storage(Protocol):
    def all_notes(self) -> list[Any]: ...                          # rows: id, slug, updated_ts
    def all_edges(self) -> list[tuple[str, str, str, float]]: ...
    def note_exists(self, slug: str) -> bool: ...
    def get_note(self, note_id: str) -> Any: ...                   # row with source_path
    def all_embeddings(self) -> list[tuple[str, np.ndarray]]: ...
    def all_claims(self) -> list[Any]: ...                         # rows: slug, claim, strength, sources, checked_ts


def lint(storage: Storage, root: Path, cfg: LintConfig | None = None) -> list[dict]:
    cfg = cfg or LintConfig()
    findings: list[dict] = []
    notes = storage.all_notes()
    ids = [n["id"] for n in notes]
    edges = storage.all_edges()

    # 1) orphans — no incoming edges
    inbound = {nid: 0 for nid in ids}
    for _src, dst, _k, _w in edges:
        inbound[dst] = inbound.get(dst, 0) + 1
    for n in notes:
        if inbound.get(n["id"], 0) == 0:
            findings.append({"level": "info", "kind": "orphan",
                             "slug": n["slug"],
                             "msg": "no incoming edges (orphan)"})

    # 2) broken wikilinks — [[target]] without a file below root
    existing_files = {p.stem for p in root.rglob("*.md")}
    for n in notes:
        body = _body_for(storage, n["id"])
        for target in _extract_links(body):
            if target not in existing_files and not storage.note_exists(target):
                findings.append({"level": "warn", "kind": "broken-link",
                                 "slug": n["slug"],
                                 "msg": f"[[{target}]] points to a non-existing note"})

    # 3) stale — last update older than the half-life
    cutoff = time.time() - cfg.recency_halflife_days * 86400
    for n in notes:
        try:
            up = datetime.fromisoformat(n["updated_ts"].replace("Z", "+00:00"))
            if up.timestamp() < cutoff:
                findings.append({"level": "warn", "kind": "stale",
                                 "slug": n["slug"],
                                 "msg": f"last updated {n['updated_ts'][:10]} (> {int(cfg.recency_halflife_days)} d)"})
        except (ValueError, TypeError, AttributeError):
            pass

    # 4) duplicate suspicion — cosine above threshold over note embeddings
    embs = storage.all_embeddings()
    if len(embs) > 1:
        cm = graph.cosine_matrix([v for _, v in embs])
        for i in range(len(embs)):
            for j in range(i + 1, len(embs)):
                if cm[i][j] > cfg.cosine_dup_threshold:
                    findings.append({"level": "warn", "kind": "duplicate",
                                     "slug": embs[i][0],
                                     "msg": f"cosine {cm[i][j]:.2f} to {embs[j][0]} (possible duplicate)"})

    # 5) evidence ledger — claim hygiene
    for c in storage.all_claims():
        try:
            chk = datetime.fromisoformat((c["checked_ts"] or "").replace("Z", "+00:00"))
            if time.time() - chk.timestamp() > cfg.evidence_stale_days * 86400:
                findings.append({"level": "warn", "kind": "claim-stale",
                                 "slug": c["slug"],
                                 "msg": f"claim checked {c['checked_ts'][:10]} > "
                                        f"{cfg.evidence_stale_days} d ago: \"{(c['claim'] or '')[:60]}\""})
        except (ValueError, TypeError):
            pass
        if c["strength"] in ("strong", "very_strong") and \
                (c["sources"] or 0) < cfg.evidence_overclaim_sources:
            findings.append({"level": "warn", "kind": "evidence-overclaim",
                             "slug": c["slug"],
                             "msg": f"\"{(c['claim'] or '')[:60]}\" rated {c['strength']} "
                                    f"but only {c['sources']} source(s)"})
        if (c["sources"] or 0) == 0 and c["strength"] not in ("none", "anecdotal"):
            findings.append({"level": "info", "kind": "evidence-no-sources",
                             "slug": c["slug"],
                             "msg": f"\"{(c['claim'] or '')[:60]}\" without sources"})

    return findings


def _extract_links(body: str) -> list[str]:
    # exclude code blocks (consistent with markdown.scan_wikilinks)
    body = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    return re.findall(r"\[\[([^\]|#]+)", body)


def _body_for(storage: Storage, note_id: str) -> str:
    # the body is not stored in the DB (only its hash) — read it from the file
    n = storage.get_note(note_id)
    if n and n["source_path"]:
        p = Path(n["source_path"])
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    return ""
