"""SQLite access layer: connection handling, schema and seed data."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    name       TEXT NOT NULL DEFAULT 'New Event',
    show_name  TEXT NOT NULL DEFAULT '',
    logo_path  TEXT,
    starts_on  TEXT,
    ends_on    TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    stock_mode TEXT NOT NULL DEFAULT 'shared',   -- shared | specific
    rtsp_url   TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS techs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'tech',    -- tech | lead
    site_id      INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT,
    name        TEXT NOT NULL,
    dept        TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    bolero_id   TEXT NOT NULL DEFAULT '',   -- the person's Bolero / comms ID; stays with them across pack swaps
    faceplate   TEXT NOT NULL DEFAULT '',   -- faceplate colour (hex) from the allocation sheet, e.g. 00B050
    allocation  TEXT NOT NULL DEFAULT '',   -- planned kit as text from the allocation sheet, e.g. "Bolero · Pro-D1"
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS people_external_id ON people(external_id) WHERE external_id IS NOT NULL AND external_id <> '';
CREATE INDEX IF NOT EXISTS people_name ON people(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS people_phone ON people(phone);

CREATE TABLE IF NOT EXISTS device_types (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    short_code TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 100,
    active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    si            TEXT NOT NULL UNIQUE,              -- barcode / easyjob SI number
    type_id       INTEGER NOT NULL REFERENCES device_types(id),
    label         TEXT NOT NULL DEFAULT '',          -- user facing, e.g. "Bolero 61"
    site_id       INTEGER REFERENCES sites(id) ON DELETE SET NULL,  -- NULL = shared stock
    status        TEXT NOT NULL DEFAULT 'in',        -- in | out | lost | damaged
    holder_id     INTEGER REFERENCES people(id) ON DELETE SET NULL,
    linked_id     TEXT NOT NULL DEFAULT '',          -- e.g. the holder's Bolero ID this pack is configured as while out
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_event_at TEXT
);
CREATE INDEX IF NOT EXISTS devices_holder ON devices(holder_id);
CREATE INDEX IF NOT EXISTS devices_status ON devices(status);
CREATE INDEX IF NOT EXISTS devices_type ON devices(type_id);

CREATE TABLE IF NOT EXISTS assignments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  INTEGER NOT NULL UNIQUE REFERENCES devices(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assignments_person ON assignments(person_id);

CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    action            TEXT NOT NULL,
    device_id         INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    person_id         INTEGER REFERENCES people(id) ON DELETE SET NULL,
    tech_id           INTEGER REFERENCES techs(id) ON DELETE SET NULL,
    site_id           INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    reason            TEXT NOT NULL DEFAULT '',
    note              TEXT NOT NULL DEFAULT '',
    related_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS transactions_ts ON transactions(ts);
CREATE INDEX IF NOT EXISTS transactions_device ON transactions(device_id);
CREATE INDEX IF NOT EXISTS transactions_person ON transactions(person_id);

CREATE TABLE IF NOT EXISTS kiosk_state (
    site_id    INTEGER PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_DEVICE_TYPES = [
    ("Bolero Beltpack", "BOL", 10),
    ("Bolero Antenna", "ANT", 20),
    ("Radio", "RAD", 30),
    ("RSM", "RSM", 40),
    ("Pro-D2 Headset", "HS", 50),
    ("Pro-D1 Headset", "HS", 51),
    ("Air-D1 Headset", "HS", 52),
    ("Covert Headset", "HS", 53),
    ("Max-E1 Headset", "HS", 54),
    ("Headset", "HS", 55),
    ("Earpiece", "EAR", 60),
    ("Charger", "CHG", 70),
    ("Battery", "BAT", 80),
    ("Other", "OTH", 999),
]

# Human readable names for transaction actions, used in the UI and exports.
ACTION_LABELS = {
    "out": "Signed out",
    "in": "Signed in",
    "in_noop": "Scanned in (already in)",
    "out_noop": "Scanned out (already with person)",
    "out_reassign": "Reassigned (was out)",
    "swap_out": "Swap: returned",
    "swap_in": "Swap: issued",
    "lost": "Marked lost",
    "damaged": "Marked damaged",
    "restored": "Restored to stock",
    "created": "Device created",
    "imported": "Device imported",
    "assigned": "Pre-assigned device",
    "unassigned": "Pre-assignment removed",
    "note": "Note",
}


def now_iso() -> str:
    """Local time, second precision, ISO format. Stored as text in SQLite."""
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")   # every commit reaches the disk before we answer
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # Some network file systems cannot lock for WAL; the default journal still works.
        pass
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


MIGRATIONS = [
    # (table, column, definition) added after the first release
    ("people", "bolero_id", "TEXT NOT NULL DEFAULT ''"),
    ("devices", "linked_id", "TEXT NOT NULL DEFAULT ''"),
    ("people", "faceplate", "TEXT NOT NULL DEFAULT ''"),
    ("people", "allocation", "TEXT NOT NULL DEFAULT ''"),
]
DROPPED_TABLES = ["plans"]   # short-lived 0.3.0 feature, replaced by the person's Bolero ID alone


def migrate(db: sqlite3.Connection) -> None:
    for table, column, definition in MIGRATIONS:
        cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for table in DROPPED_TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")


def init_db() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)
        migrate(db)
        seed(db)


def seed(db: sqlite3.Connection) -> None:
    ts = now_iso()
    if db.execute("SELECT 1 FROM event WHERE id = 1").fetchone() is None:
        db.execute(
            "INSERT INTO event (id, name, show_name, updated_at) VALUES (1, 'New Event', '', ?)",
            (ts,),
        )
    if db.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 0:
        db.execute("INSERT INTO sites (name, stock_mode, sort_order) VALUES ('Main RDR', 'shared', 0)")
    if db.execute("SELECT COUNT(*) FROM device_types").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO device_types (name, short_code, sort_order) VALUES (?, ?, ?)",
            DEFAULT_DEVICE_TYPES,
        )
    if db.execute("SELECT COUNT(*) FROM techs").fetchone()[0] == 0:
        db.execute(
            "INSERT INTO techs (username, display_name, role) VALUES ('tech', 'RDR Tech', 'lead')"
        )


def reset_db() -> None:
    """Drop everything and start a brand new database. A snapshot is taken first."""
    from .services.snapshots import snapshot
    snapshot("reset")
    with get_db() as db:
        db.execute("PRAGMA foreign_keys = OFF")
        rows = db.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for name, kind in rows:
            db.execute(f"DROP {kind} IF EXISTS \"{name}\"")
    init_db()


# -- settings helpers -------------------------------------------------------

def get_setting(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
