"""SQLite persistence layer (no ORM - plain sqlite3 with row factories)."""
import sqlite3
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT DEFAULT '',
    brand         TEXT DEFAULT '',
    price         REAL DEFAULT 0,
    commission    REAL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    keyword     TEXT NOT NULL,
    UNIQUE(product_id, keyword)
);

CREATE TABLE IF NOT EXISTS threads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT DEFAULT '',
    subreddit    TEXT DEFAULT '',
    traffic      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS opportunities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    thread_id    INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    keyword      TEXT NOT NULL,
    position     INTEGER DEFAULT 0,
    relevance    REAL DEFAULT 0,
    score        REAL DEFAULT 0,
    status       TEXT DEFAULT 'New',
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(product_id, thread_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT DEFAULT 'running',
    detail      TEXT DEFAULT ''
);
"""

VALID_STATUSES = {"New", "Approved", "Commented", "Rejected", "On Hold"}


def get_conn() -> sqlite3.Connection:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
