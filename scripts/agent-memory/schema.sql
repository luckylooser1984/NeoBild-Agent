-- Minimal schema for the agent-memory scripts (SQLite).
-- The vector table (vec_embeddings) needs the sqlite-vec extension and is
-- created by `memdb.py init` only when sqlite-vec is installed.
--
-- Author: Lukas Weißmann
-- License: MIT

-- One row per remembered chunk (a chat turn, a note fragment, ...).
CREATE TABLE IF NOT EXISTS embeddings (
    id            INTEGER PRIMARY KEY,
    ts            TEXT    DEFAULT (datetime('now')),
    session_id    TEXT,
    role          TEXT    NOT NULL,          -- user | assistant | note ...
    text          TEXT    NOT NULL,
    importance    REAL    DEFAULT 5.0,       -- 1..10, see importance.py
    access_count  INTEGER DEFAULT 0,         -- reads since the last dream cycle
    last_accessed DATETIME
);

-- Distilled key/value facts with their own importance.
CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY,
    ts            TEXT    DEFAULT (datetime('now')),
    key           TEXT    NOT NULL UNIQUE,
    value         TEXT    NOT NULL,
    importance    REAL    DEFAULT 5.0,
    access_count  INTEGER DEFAULT 0,
    last_accessed DATETIME
);

-- What the agent has learned about its (single, local) user.
CREATE TABLE IF NOT EXISTS user_profile (
    id          INTEGER PRIMARY KEY,
    key         TEXT    NOT NULL UNIQUE,
    value       TEXT    NOT NULL,
    updated_at  TEXT    DEFAULT (datetime('now'))
);

-- Output of reflect.py ('auto') and dream.py ('dream_summary').
CREATE TABLE IF NOT EXISTS reflections (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    DEFAULT (datetime('now')),
    kind        TEXT    NOT NULL,
    content     TEXT    NOT NULL
);

-- Semantic neighbourhood between chunks (e.g. cosine distance below a
-- threshold), written by whatever ingests embeddings.
CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    src_id      INTEGER NOT NULL REFERENCES embeddings(id) ON DELETE CASCADE,
    dst_id      INTEGER NOT NULL REFERENCES embeddings(id) ON DELETE CASCADE,
    distance    REAL    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'semantic',
    created_at  TEXT    DEFAULT (datetime('now')),
    UNIQUE(src_id, dst_id)
);
