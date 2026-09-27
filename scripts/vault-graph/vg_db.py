#!/usr/bin/env python3
"""vg_db.py — shared schema and access helpers for the vault-graph edge database (SQLite).

One module for every writer (build_graph.py, vault_graph_import.py) and for the
exporter (export_graph.py), so the schema lives in exactly one place:

  nodes(id, zone, group_name, val)
  edges(node_id, zone, edge_kind, target, weight)   -- edge_kind: wikilink | vector | entity

A "zone" is a free-form label for where the nodes come from (e.g. one zone per
vault). Topics are expressed through the node-id prefix; there are deliberately
no further columns.

Default database location: ./vault_graph.db next to this file, or the path in
the environment variable VAULT_GRAPH_DB.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = Path(os.environ.get("VAULT_GRAPH_DB", HERE / "vault_graph.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id         TEXT NOT NULL,
    zone       TEXT NOT NULL,
    group_name TEXT,
    val        INTEGER DEFAULT 0,
    PRIMARY KEY (zone, id)
);
CREATE TABLE IF NOT EXISTS edges (
    node_id   TEXT NOT NULL,
    zone      TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    target    TEXT NOT NULL,
    weight    REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_edges_zone_node   ON edges(zone, node_id);
CREATE INDEX IF NOT EXISTS idx_edges_zone_target ON edges(zone, target);
CREATE INDEX IF NOT EXISTS idx_edges_zone_kind   ON edges(zone, edge_kind);
CREATE INDEX IF NOT EXISTS idx_nodes_zone_group  ON nodes(zone, group_name);
"""


def connect(db: Path | str = DEFAULT_DB, read_only: bool = False) -> sqlite3.Connection:
    """Open the edge DB. Read-write connections create the schema if missing."""
    db = Path(db)
    if read_only:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    return con


def drop_orphan_edges(con: sqlite3.Connection, zone: str, keep_kind: str = "wikilink") -> int:
    """Remove non-wikilink edges (e.g. vector edges) whose endpoints are no longer
    nodes of the zone after a wikilink rebuild. Prevents dangling vector edges when
    notes were renamed or deleted. Returns the number of removed edges."""
    cur = con.execute(
        "DELETE FROM edges WHERE zone = ? AND edge_kind <> ? AND ("
        "  node_id NOT IN (SELECT id FROM nodes WHERE zone = ?) OR"
        "  target  NOT IN (SELECT id FROM nodes WHERE zone = ?))",
        (zone, keep_kind, zone, zone),
    )
    return cur.rowcount or 0
