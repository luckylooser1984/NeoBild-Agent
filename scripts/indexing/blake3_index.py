#!/usr/bin/env python3
"""blake3_index.py — incremental BLAKE3 file index: deterministic, offline, no LLM.

Pure Python (stdlib + the `blake3` package), no network, incremental: a file is
only re-hashed when its size or mtime changed. Results live in a SQLite
database, so later runs skip unchanged files and duplicates can be found by
grouping on the hash (duplicates are only reported, nothing is ever deleted).

Intended as the cheap, deterministic bottom layer of a local knowledge/memory
pipeline: an agent can ask "what changed?" without reading any file content.

Usage:
    python3 blake3_index.py [--roots DIR [DIR ...]] [--db PATH] [--max-seconds N] [--quiet]

Default roots: $BLAKE3_INDEX_ROOTS (space-separated) or the current directory.
Default DB:    $BLAKE3_INDEX_DB or ~/.local/state/blake3-index/index.db
Side outputs next to the DB: status.json, duplicates_latest.json

Requires: pip install blake3

Author: Lukas Weißmann
License: MIT
"""
import argparse
import fnmatch
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    import blake3
except ImportError:
    sys.exit("Missing dependency: pip install blake3")

EXCLUDE_DIR_PATTERNS = [
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".cache", "cache", "Cache", "browser-profiles", "audio_cache",
    ".Trash*", "backups",
]
DEFAULT_DB = Path(os.environ.get(
    "BLAKE3_INDEX_DB",
    Path.home() / ".local" / "state" / "blake3-index" / "index.db",
))
CHUNK_SIZE = 1024 * 1024  # 1 MiB


def should_skip_dir(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_DIR_PATTERNS)


def hash_file(path: Path) -> str | None:
    h = blake3.blake3()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                h.update(chunk)
    except (PermissionError, OSError):
        return None
    return h.hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            hash TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash)")
    conn.commit()


def walk_roots(roots: list[Path]):
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
            for name in filenames:
                yield Path(dirpath) / name


def run(roots: list[Path], db_path: Path, max_seconds: float | None, quiet: bool) -> dict:
    try:
        os.nice(15)  # background job: be polite to interactive work
    except OSError:
        pass

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start = time.monotonic()
    seen = hashed = skipped = errors = 0
    stopped_early = False

    cur = conn.cursor()
    for path in walk_roots(roots):
        if max_seconds is not None and time.monotonic() - start > max_seconds:
            stopped_early = True
            break
        try:
            st = path.stat()
        except OSError:
            errors += 1
            continue
        seen += 1
        key = str(path)
        row = cur.execute(
            "SELECT size, mtime FROM files WHERE path = ?", (key,)
        ).fetchone()
        if row and row[0] == st.st_size and row[1] == st.st_mtime:
            skipped += 1
            cur.execute("UPDATE files SET last_seen = ? WHERE path = ?", (now, key))
            continue
        digest = hash_file(path)
        if digest is None:
            errors += 1
            continue
        hashed += 1
        cur.execute(
            """INSERT INTO files (path, size, mtime, hash, first_seen, last_seen)
               VALUES (?, ?, ?, ?, COALESCE(
                   (SELECT first_seen FROM files WHERE path = ?), ?), ?)
               ON CONFLICT(path) DO UPDATE SET
                   size=excluded.size, mtime=excluded.mtime, hash=excluded.hash,
                   last_seen=excluded.last_seen""",
            (key, st.st_size, st.st_mtime, digest, key, now, now),
        )
        if hashed % 2000 == 0:
            conn.commit()
    conn.commit()

    duplicates = cur.execute(
        """SELECT hash, COUNT(*) c, GROUP_CONCAT(path, '|') paths
           FROM files GROUP BY hash HAVING c > 1"""
    ).fetchall()
    conn.close()

    elapsed = time.monotonic() - start
    dup_path = db_path.parent / "duplicates_latest.json"
    if duplicates:
        dup_path.write_text(json.dumps(
            [{"hash": h, "count": c, "paths": p.split("|")} for h, c, p in duplicates],
            indent=2, ensure_ascii=False,
        ))

    status = {
        "ts": now,
        "roots": [str(r) for r in roots],
        "db": str(db_path),
        "files_seen": seen,
        "hashed_new_or_changed": hashed,
        "skipped_unchanged": skipped,
        "errors": errors,
        "duplicate_groups": len(duplicates),
        "elapsed_seconds": round(elapsed, 1),
        "stopped_early": stopped_early,
    }
    (db_path.parent / "status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False))

    if not quiet:
        print(
            f"BLAKE3 index {now}: {seen} files seen, {hashed} new/changed hashed, "
            f"{skipped} unchanged skipped, {errors} errors, "
            f"{len(duplicates)} duplicate groups, {elapsed:.1f}s"
            + (" [time limit reached]" if stopped_early else "")
        )
    return status


def main():
    ap = argparse.ArgumentParser(description="Incremental BLAKE3 file index (SQLite).")
    default_roots = os.environ.get("BLAKE3_INDEX_ROOTS", "").split() or ["."]
    ap.add_argument("--roots", nargs="+", default=default_roots,
                    help="directories to index (default: $BLAKE3_INDEX_ROOTS or .)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite path (default: {DEFAULT_DB})")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="stop after N seconds; the next run continues incrementally")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    roots = [Path(r).expanduser() for r in args.roots]
    run(roots, Path(args.db).expanduser(), args.max_seconds, args.quiet)


if __name__ == "__main__":
    main()
