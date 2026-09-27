# vault-graph

Turns a Markdown/Obsidian vault into a queryable graph: wikilinks become edges in a
small SQLite database, optional embedding-based "vector" edges add semantic links,
and the result can be explored from the CLI or in a 3D force-graph viewer.
Everything runs locally; embeddings come from a local llama.cpp embedding server.

| File | Purpose |
|---|---|
| `vg_db.py` | Shared SQLite schema (`nodes`, `edges` with `zone` + `edge_kind`) and helpers |
| `build_graph.py` | Parse a vault's wikilinks (Obsidian resolution rules) into the DB |
| `query_graph.py` | Read-only CLI: `stats`, `find`, `neigh`, `path`, `sub`, `rank` (PageRank), `--json` |
| `export_graph.py` | Export one zone as `{nodes, links}` JSON for 3d-force-graph / force-graph |
| `vault_graph_import.py` | Import a `{nodes, links}` JSON (e.g. with vector edges) back into the DB |
| `embed_batch.py` | Batch-embed all notes with resumable cache and a systemd service watchdog |
| `add_vector_edges.py` | Add top-k cosine edges to a graph JSON, with before/after graph metrics |
| `eval_vector_quality.py` | Measure how well embeddings separate groups (kNN accuracy vs. baseline) |
| `cosine_stats.py` | Detect embedding "saturation" (template-like notes collapsing onto one point) |

Requirements: Python 3.10+, `numpy` (embedding/metrics scripts only). An embedding
server such as `llama-server --embedding -m nomic-embed-text-v1.5.Q8_0.gguf --port 8081`
for `embed_batch.py` / `add_vector_edges.py`.

## Typical run

```bash
export VAULT_GRAPH_DB=./vault_graph.db          # optional, default: next to the scripts
python3 build_graph.py ~/notes --zone wiki
python3 query_graph.py --zone wiki stats
python3 query_graph.py --zone wiki path "note-a" "note-b"
python3 export_graph.py --zone wiki             # -> public/graph-wiki.json

# optional: semantic edges
python3 embed_batch.py ~/notes --no-manage-service --embed-url http://127.0.0.1:8081
python3 add_vector_edges.py ~/notes public/graph-wiki.json \
        --vectors ~/notes/.embed-cache/vectors.npz --report --dry-run
python3 add_vector_edges.py ~/notes public/graph-wiki.json --vectors ~/notes/.embed-cache/vectors.npz
python3 vault_graph_import.py --zone wiki --json public/graph-wiki.json
```

Note: `build_graph.py` rewrites only wikilink edges of a zone and keeps vector
edges whose endpoints still exist; `vault_graph_import.py` replaces the whole zone.

Author: Lukas Weißmann · License: MIT
