"""SQLite storage: supplier list, offer cache, searches, comparisons/decisions and RFQs (spec 13.12)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

JSON_FIELDS = {"offers", "filters", "notes", "categories", "rating_reasons", "chosen_offer", "tags", "probe", "rating", "data", "extra"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 1,
    website TEXT DEFAULT '',
    vat_basis TEXT DEFAULT 'unknown',
    status TEXT DEFAULT 'active',
    categories TEXT DEFAULT '[]',
    rating REAL,
    rating_reasons TEXT DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers_cache (
    supplier TEXT NOT NULL,
    query TEXT NOT NULL,
    offers TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (supplier, query)
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT DEFAULT '',
    query TEXT NOT NULL,
    filters TEXT DEFAULT '{}',
    offers TEXT DEFAULT '[]',
    notes TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER REFERENCES searches(id),
    offers TEXT NOT NULL,
    chosen_offer TEXT,
    reason TEXT DEFAULT '',
    user TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS directory (
    id TEXT PRIMARY KEY,                 -- osm:node/123 or web:example.co.ke
    name TEXT NOT NULL,
    website TEXT DEFAULT '',
    domain TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    address TEXT DEFAULT '',
    town TEXT DEFAULT '',
    lat REAL,
    lon REAL,
    kind TEXT DEFAULT '',
    opening_hours TEXT DEFAULT '',
    source TEXT DEFAULT 'osm',          -- osm | added
    osm_url TEXT DEFAULT '',
    location_key TEXT DEFAULT '',
    tags TEXT DEFAULT '{}',
    probe TEXT DEFAULT '{}',            -- website check result
    probed_at TEXT DEFAULT '',
    rating TEXT DEFAULT '{}',           -- score, parts, reasons, flags, tier
    tier INTEGER DEFAULT 3,
    status TEXT DEFAULT 'active',       -- active | blocked
    added_by TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS directory_location ON directory(location_key);
CREATE TABLE IF NOT EXISTS places (
    query TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS directory_runs (
    location_key TEXT PRIMARY KEY,
    label TEXT DEFAULT '',
    status TEXT DEFAULT '',             -- running | ok | error
    detail TEXT DEFAULT '',
    count INTEGER DEFAULT 0,
    ran_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS debarred (
    name TEXT PRIMARY KEY,
    norm TEXT NOT NULL,
    debarred_on TEXT DEFAULT '',
    period TEXT DEFAULT '',
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
    url TEXT NOT NULL,
    supplier_key TEXT DEFAULT '',
    name TEXT DEFAULT '',
    price REAL,
    total REAL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS price_history_url ON price_history(url, fetched_at);
CREATE TABLE IF NOT EXISTS fetch_log (
    supplier_key TEXT NOT NULL,
    ok INTEGER NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rfqs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT DEFAULT '',
    item TEXT NOT NULL,
    specs TEXT DEFAULT '',
    quantity INTEGER DEFAULT 1,
    supplier TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    body TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release: (table, column, definition).
MIGRATIONS: list[tuple[str, str, str]] = [
    ("comparisons", "decided_at", "TEXT DEFAULT ''"),
    ("rfqs", "search_id", "INTEGER"),
    ("rfqs", "source", "TEXT DEFAULT 'template'"),
    ("searches", "extra", "TEXT DEFAULT '{}'"),
]


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        init()
    return _conn


def init(path: str | None = None) -> None:
    """Open (or reopen) the database and bring its tables up to date."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            have = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in have:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        _conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_FIELDS & d.keys():
        if isinstance(d[k], str) and d[k]:
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def _val(v):
    return json.dumps(v) if isinstance(v, (dict, list)) else v


def query(sql: str, args: tuple = ()) -> list[dict]:
    c = conn()
    with _lock:
        return [_row(r) for r in c.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    c = conn()
    with _lock:
        return _row(c.execute(sql, args).fetchone())


def execute(sql: str, args: tuple = ()) -> int:
    c = conn()
    with _lock:
        cur = c.execute(sql, tuple(_val(a) for a in args))
        c.commit()
        return cur.lastrowid


# ---- Helpers ------------------------------------------------------------------
def sync_suppliers(suppliers: list[config.Supplier]) -> None:
    """Mirror suppliers.yaml into the suppliers table (ratings come in Phase 2)."""
    for s in suppliers:
        execute("""INSERT INTO suppliers (id, name, tier, website, vat_basis, status, categories, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, tier=excluded.tier, website=excluded.website,
                   vat_basis=excluded.vat_basis, status=excluded.status, categories=excluded.categories,
                   updated_at=excluded.updated_at""",
                (s.key, s.name, s.tier, s.website, s.vat_basis, s.status, list(s.categories), now()))


def cached_offers(supplier: str, q: str, max_age_minutes: float) -> tuple[list[dict], str] | None:
    row = one("SELECT offers, fetched_at FROM offers_cache WHERE supplier = ? AND query = ?", (supplier, q))
    if not row:
        return None
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
    if age.total_seconds() > max_age_minutes * 60:
        return None
    return row["offers"], row["fetched_at"]


def store_offers(supplier: str, q: str, offers: list[dict]) -> None:
    execute("INSERT OR REPLACE INTO offers_cache (supplier, query, offers, fetched_at) VALUES (?, ?, ?, ?)",
            (supplier, q, offers, now()))


def clear_cache() -> None:
    execute("DELETE FROM offers_cache")
