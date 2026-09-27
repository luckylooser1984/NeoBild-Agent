"""markdown.py — Markdown helpers for a local knowledge base (no PyYAML needed).

  - parse_frontmatter()  minimal YAML front-matter reader (flat keys only)
  - parse_evidence()     reads an `evidence:` claim list from front-matter ("evidence ledger")
  - scan_wikilinks()     all [[wikilinks]] of a body, ignoring code blocks and inline code
  - chunk_text()         fixed-size chunks with overlap for embedding models
  - strip_markdown()     reduce Markdown to searchable plain text (FTS / embeddings)
  - body_sha256()        content hash for change detection

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Evidence vocabulary for claims recorded in note front-matter
EVIDENCE_STRENGTHS = {"none", "anecdotal", "weak", "moderate", "strong", "very_strong"}
EVIDENCE_DIRECTIONS = {"support", "contradict", "neutral"}
EVIDENCE_REPLICATIONS = {"unknown", "single_study", "replicated", "contested", "not_replicated"}
EVIDENCE_KEYS = {"claim", "hypothesis", "strength", "direction", "replication",
                 "sources", "checked", "note", "url"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Read YAML front-matter (minimal, flat keys, no PyYAML dependency)."""
    m = FRONTMATTER_RE.match(text)
    meta: dict[str, Any] = {}
    body = text
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "- ", "related:", "source:", "contradictions:")):
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key in ("title", "summary", "status", "type", "confidence", "importance"):
                    meta[key] = val
                elif key in ("created", "updated", "checked"):
                    meta[key] = val
    return meta, body


def parse_evidence(text: str) -> list[dict]:
    """Read the `evidence:` block from the YAML front-matter (minimal YAML).

    Convention:
      evidence:
        - claim: "Local 3B models are good enough for note summarisation"
          hypothesis: H1
          strength: weak            # none|anecdotal|weak|moderate|strong|very_strong
          direction: support        # support|contradict|neutral
          replication: single_study # unknown|single_study|replicated|contested|not_replicated
          sources: 3                # number of independent sources
          checked: 2026-08-25       # date of last check (ISO)
          note: optional comment
          url: https://…            # primary source (optional)

    Invalid values are not dropped but set to defaults — so the claim stays
    visible in the ledger (the linter reports the deviation).
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return []
    claims: list[dict] = []
    cur: dict | None = None
    in_block = False
    for line in m.group(1).splitlines():
        if line.startswith((" ", "\t")):
            s = line.strip()
            if s.startswith("- claim:"):
                if cur:
                    claims.append(cur)
                cur = {"claim": s[len("- claim:"):].strip().strip("'\"")}
            elif cur is not None and s and ":" in s:
                key, _, val = s.partition(":")
                key = key.strip()
                if key in EVIDENCE_KEYS and key != "claim":
                    cur[key] = val.strip().strip("'\"")
        else:
            s = line.strip()
            if s == "evidence:" or s.startswith("evidence: "):
                in_block = True
                continue
            if in_block and s:  # top-level key outside the block -> end
                in_block = False
    if cur:
        claims.append(cur)

    # normalisation: sources -> int, vocabulary defaults
    out = []
    for c in claims:
        if not c.get("claim"):
            continue
        if c.get("strength") not in EVIDENCE_STRENGTHS:
            c["strength"] = "weak"
        if c.get("direction") not in EVIDENCE_DIRECTIONS:
            c["direction"] = "neutral"
        if c.get("replication") not in EVIDENCE_REPLICATIONS:
            c["replication"] = "unknown"
        try:
            c["sources"] = max(0, int(c.get("sources") or 0))
        except (TypeError, ValueError):
            c["sources"] = 0
        out.append(c)
    return out


def scan_wikilinks(body: str) -> list[str]:
    """All [[wikilinks]] of the body, normalised (file name without path/extension).
    Code blocks (```…```) and inline code are excluded, because [[…]] there is
    usually an example or schema, not a real link."""
    body = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    body = re.sub(r"`[^`]*`", " ", body)
    out = []
    for m in WIKILINK_RE.finditer(body):
        target = m.group(1).strip()
        if not target or target.startswith("http"):
            continue
        out.append(target.split("/")[-1])
    return out


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def slug_from_path(path: Path) -> str:
    return path.stem


def chunk_text(text: str, max_chars: int = 900, overlap: int = 50) -> list[str]:
    """Chunking for embeddings (~512 tokens ≈ 900 characters of German text).
    Note: some llama.cpp embedding setups fail with HTTP 500 on inputs longer
    than ~1400 characters, so keep max_chars conservative."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks = []
    i = 0
    step = max_chars - overlap
    while i < len(text):
        chunks.append(text[i:i + max_chars])
        i += step
    return chunks


def strip_markdown(text: str, max_len: int = 2000) -> str:
    """Reduce Markdown to searchable text (FTS5 / embeddings)."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_`|~\-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:max_len]


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: markdown.py NOTE.md   # prints front-matter, wikilinks and evidence claims")
    raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)
    print(json.dumps({"meta": meta, "wikilinks": scan_wikilinks(body),
                      "evidence": parse_evidence(raw), "sha256": body_sha256(body)},
                     ensure_ascii=False, indent=2))
