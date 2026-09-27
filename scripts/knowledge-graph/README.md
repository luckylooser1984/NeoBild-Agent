# knowledge-graph

Building blocks of a local, LLM-free knowledge-base engine: graph ranking,
Markdown parsing with an "evidence ledger", hybrid retrieval and linting.
They are libraries (import them), not a complete application — the storage
layer is left to you via small `Protocol` interfaces.

| File | Purpose |
|---|---|
| `graph.py` | numpy PageRank, Personalized PageRank (HippoRAG-style), cosine matrix, Adamic/Adar, label propagation. `python3 graph.py` runs a tiny demo |
| `markdown.py` | Front-matter parser without PyYAML, wikilink scanner (ignores code), chunking, evidence-claim parser. `python3 markdown.py NOTE.md` prints what it finds |
| `search.py` | Hybrid search: FTS5/BM25 ∪ cosine kNN ∪ PPR with weighted fusion |
| `lint.py` | Orphans, broken links, stale notes, duplicate suspicion, evidence-claim hygiene |

### Evidence ledger

Notes can record claims in their front-matter:

```yaml
evidence:
  - claim: "Local 3B models are good enough for note summarisation"
    strength: weak          # none|anecdotal|weak|moderate|strong|very_strong
    direction: support      # support|contradict|neutral
    replication: unknown
    sources: 1
    checked: 2026-09-01
```

`lint.py` then flags claims that are rated strong with too few sources, have no
sources at all, or were not re-checked for a long time.

### Known limitation

`search.py` joins FTS terms with AND, so natural-language questions often get no
FTS hits (cosine + PPR still contribute). Switching to OR + BM25 ranking is the
obvious next step. On dense entity graphs, PageRank can also degrade into noise
when hub entities connect almost every note — dampen hubs (top-k or IDF weighting)
before relying on the graph signal.

Requirements: Python 3.10+, `numpy`.

Author: Lukas Weißmann · License: MIT
