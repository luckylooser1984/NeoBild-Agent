# agent-memory

A small, fully local long-term memory for a personal agent, backed by one
SQLite file and a small local model. The idea: memories carry an
*importance* score that is set on ingest, rises when a memory is used and
decays every night — unused trivia fades away, while clusters of important,
related memories get consolidated into summaries.

| File | What it does |
|---|---|
| `schema.sql`, `memdb.py` | Schema, DB connection (sqlite-vec optional), local LLM call helper; `memdb.py init` creates a DB. |
| `importance.py` | Rates a text 1–10 for long-term relevance; ≥5 → embed, ≥8 → store as fact. |
| `dream.py` | Nightly "dream cycle": access boost, 5 % decay, forgetting below 2.0, cluster summaries over the `edges` graph (dirtiest clusters first). |
| `reflect.py` | Synthesises recent memories + their graph neighbours into a summary, insights and open questions. |
| `user_profile.py` | Extracts structured user facts from a dialog into `user_profile`; renders them for a system prompt. |

The scripts do **not** create embeddings or `edges` themselves — whatever
ingests the conversation fills `embeddings` (and optionally
`vec_embeddings` / `edges`). The consolidation only needs `edges`.

## Quick start

```bash
export MEMORY_DB=./memory.db LLM_BASE_URL=http://127.0.0.1:8080/v1
python3 memdb.py init
python3 importance.py "I prefer answers in German."
python3 dream.py --dry-run      # safe preview
python3 dream.py                # backs up the DB first, then writes
python3 reflect.py
```

Optional dependency: `sqlite-vec` (only for the `vec_embeddings` table).
Everything else is Python stdlib.

**Privacy:** the profile and reflections describe the user. They are meant
to stay on the local machine; don't sync the DB to shared storage.

## Tests

```bash
python3 -m unittest discover -s tests
```

Author: Lukas Weißmann · License: MIT
