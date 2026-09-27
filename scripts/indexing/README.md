# indexing/

Deterministic, offline file indexing — the cheap bottom layer of a local
knowledge pipeline. No LLM involved; an agent can query the results instead
of reading files.

| Script | What it does |
|---|---|
| `blake3_index.py` | Incremental BLAKE3 hash index in SQLite. Re-hashes only files whose size/mtime changed, supports a time budget (`--max-seconds`) and reports duplicate groups. Needs `pip install blake3`. |
| `workspace_hygiene.py` | Read-only scan for unrotated logs, piling-up backups, exact duplicates, large untracked binaries in git repos and `__pycache__` sprawl. Writes a Markdown + JSON report, deletes nothing. |

```bash
python3 blake3_index.py --roots ~/projects ~/notes --max-seconds 600
python3 workspace_hygiene.py --root ~/projects --out /tmp/hygiene.md
```
