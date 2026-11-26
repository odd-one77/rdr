"""SQLite access layer: connection handling, schema and seed data."""
from __future__ import annotations

import contextvars
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

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
    ("Hytera PD682 Radio", "RAD", 31),
    ("Moto R7 Radio", "RAD", 32),
    ("RSM", "RSM", 40),
    ("Pro-D2 Headset", "HS", 50),
    ("Pro-D1 Headset", "HS", 51),
    ("Air-D1 Headset", "HS", 52),
    ("Covert Headset", "HS", 53),
    ("Max-E1 Headset", "HS", 54),
    ("Headset", "HS", 55),
    ("TS2 Headset", "HS", 56),
    ("Hytera TS2 Headset", "HS", 57),
    ("Moto R7 TS2 Headset", "HS", 58),
    ("Earpiece", "EAR", 60),
    ("Radio Headset Adapter", "ADP", 65),
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
    "assigned": "Device reserved",
    "unassigned": "Reservation removed",
    "transfer": "Stock transfer",
    "note": "Note",
}


def now_iso() -> str:
    """Local time, second precision, ISO format. Stored as text in SQLite."""
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# Which database this request works on. A site node and a standalone RDR always use DB_PATH; the hub
# keeps one database per event and points each request at the event it is working on.
_db_path: contextvars.ContextVar[Optional[Path]] = contextvars.ContextVar("rdr_db_path", default=None)
_machine_db: Optional[Path] = None   # hub only: where machine settings (the admin password) live


def current_db_path() -> Path:
    return _db_path.get() or DB_PATH


@contextmanager
def using_db(path) -> Iterator[None]:
    token = _db_path.set(Path(path))
    try:
        yield
    finally:
        _db_path.reset(token)


def set_db_path(path) -> None:
    """Point the rest of this request (or thread) at another database."""
    _db_path.set(Path(path) if path else None)


def set_machine_db(path) -> None:
    global _machine_db
    _machine_db = Path(path) if path else None


def open_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def connect() -> sqlite3.Connection:
    ensure_dirs()
    return open_db(current_db_path())   # every commit reaches the disk before we answer (synchronous=FULL)


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
    ("people", "import_name", "TEXT NOT NULL DEFAULT ''"),   # the name before Always use Bolero names renamed them
] + [(t, "uid", "TEXT") for t in ("sites", "device_types", "techs", "people", "devices", "assignments", "transactions")]

# -- sync with a hub -------------------------------------------------------------
# Every row that can be merged between a site node and the hub carries a uid that is the same in every copy.
# Integer ids differ between copies; the uid does not. A counter records that the data changed at all.
SYNCED_TABLES = ("sites", "device_types", "techs", "people", "devices", "assignments", "transactions")
# settings that belong to the machine, not the event: never synced, kept when an event is pulled
LOCAL_SETTING_KEYS = ("admin_password_hash",)
LOCAL_SETTING_PREFIXES = ("away:", "hub_")
_LOCAL_SQL = " AND ".join([f"{{row}}.key <> '{k}'" for k in LOCAL_SETTING_KEYS] + [f"{{row}}.key NOT LIKE '{p}%'" for p in LOCAL_SETTING_PREFIXES])


def is_local_setting(key: str) -> bool:
    return key in LOCAL_SETTING_KEYS or key.startswith(LOCAL_SETTING_PREFIXES)


def _sync_setup(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS sync_state (id INTEGER PRIMARY KEY CHECK (id = 1), changes INTEGER NOT NULL DEFAULT 0)")
    db.execute("INSERT OR IGNORE INTO sync_state (id, changes) VALUES (1, 0)")
    bump = "UPDATE sync_state SET changes = changes + 1 WHERE id = 1;"
    for t in SYNCED_TABLES:
        db.execute(f"UPDATE {t} SET uid = lower(hex(randomblob(16))) WHERE uid IS NULL OR uid = ''")
        db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {t}_uid ON {t}(uid)")
        db.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_uid_fill AFTER INSERT ON {t} WHEN NEW.uid IS NULL OR NEW.uid = '' "
                   f"BEGIN UPDATE {t} SET uid = lower(hex(randomblob(16))) WHERE rowid = NEW.rowid; END")
    for t in SYNCED_TABLES + ("event",):
        for op in ("INSERT", "UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_chg_{op.lower()} AFTER {op} ON {t} BEGIN {bump} END")
    for op, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS settings_chg_{op.lower()} AFTER {op} ON settings WHEN {_LOCAL_SQL.format(row=row)} BEGIN {bump} END")


def changes(db: sqlite3.Connection) -> int:
    """How many times the event data has changed (rows in synced tables, the event, shared settings)."""
    row = db.execute("SELECT changes FROM sync_state WHERE id = 1").fetchone()
    return int(row[0]) if row else 0
# types added to the defaults after a database may already exist: offered once, then the list is the user's
ADDED_DEVICE_TYPES = {"0.9.5": ["Hytera PD682 Radio", "Moto R7 Radio", "TS2 Headset", "Hytera TS2 Headset", "Moto R7 TS2 Headset"]}
DROPPED_TABLES = ["plans"]   # short-lived 0.3.0 feature, replaced by the person's Bolero ID alone


def migrate(db: sqlite3.Connection) -> None:
    for table, column, definition in MIGRATIONS:
        cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for table in DROPPED_TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    # faceplate colours end up in style attributes: anything but six hex digits from older versions goes
    db.execute("UPDATE people SET faceplate = '' WHERE faceplate <> '' AND (length(faceplate) <> 6 OR faceplate GLOB '*[^0-9A-Fa-f]*')")
    _sync_setup(db)


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
    seen = db.execute("SELECT value FROM settings WHERE key = 'types_added'").fetchone()
    seen = set((seen["value"] or "").split(",")) if seen else set()
    for release, names in ADDED_DEVICE_TYPES.items():
        if release in seen:
            continue
        for row in DEFAULT_DEVICE_TYPES:
            if row[0] in names:
                db.execute("INSERT INTO device_types (name, short_code, sort_order) SELECT ?, ?, ? "
                           "WHERE NOT EXISTS (SELECT 1 FROM device_types WHERE lower(name) = lower(?))", (*row, row[0]))
        seen.add(release)
        db.execute("INSERT INTO settings (key, value) VALUES ('types_added', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (",".join(sorted(x for x in seen if x)),))
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
            if kind in ("table", "index"):
                quoted = '"' + name.replace('"', '""') + '"'
                db.execute(f"DROP {kind} IF EXISTS {quoted}")
    init_db()


# -- settings helpers -------------------------------------------------------

MACHINE_SETTING_KEYS = ("admin_password_hash",)   # on the hub these live in the hub database, not an event's


def get_setting(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    if _machine_db is not None and key in MACHINE_SETTING_KEYS:
        own = open_db(_machine_db)
        try:
            row = own.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        finally:
            own.close()
        return row["value"] if row else default
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db: sqlite3.Connection, key: str, value: str | None) -> None:
    if _machine_db is not None and key in MACHINE_SETTING_KEYS:
        own = open_db(_machine_db)
        try:
            with own:
                _write_setting(own, key, value)
        finally:
            own.close()
        return
    _write_setting(db, key, value)


def _write_setting(db: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
