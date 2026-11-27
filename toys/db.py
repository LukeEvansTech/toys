"""The app's schema. The run ledger, the seen-before store and the model answer cache come
from collectorkit."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from collectorkit import llm_cache, runs, seen
from collectorkit.db import DB

SCHEMA = runs.SCHEMA + seen.SCHEMA + llm_cache.SCHEMA + """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    price TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    -- NULL until the model has answered; a listing is shown only once it has.
    liked INTEGER,
    dropped INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    judged TEXT,
    mailed TEXT,
    photo TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE (source, key)
);
CREATE TABLE IF NOT EXISTS collection (
    id TEXT PRIMARY KEY,
    item TEXT NOT NULL,
    maker TEXT NOT NULL DEFAULT '',
    vendor TEXT NOT NULL DEFAULT '',
    order_ref TEXT NOT NULL DEFAULT '',
    bought TEXT NOT NULL DEFAULT '',
    gbp_paid REAL,
    currency TEXT NOT NULL DEFAULT '',
    amount REAL,
    seller_status TEXT NOT NULL DEFAULT '',
    franchise TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    delivered INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '[]',
    parcels TEXT NOT NULL DEFAULT '[]'
);
"""


def migrate(c: sqlite3.Connection) -> None:
    """Columns added after the first release go here, one ``add_column`` each."""
    del c


def open_db(path: Path) -> DB:
    db = DB(path, SCHEMA, migrate)
    runs.mark_interrupted(db)
    return db
