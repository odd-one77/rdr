"""The hub: many events, the offsite copy of every site node, and the overview of them all.

Layout under ``data/hub``:
  hub.db                      the hub's own database: events, nodes, log, the admin password
  events/<id>/rdr.db          each event's database, the same schema a site node uses
  events/<id>/uploads/        its logo
  events/<id>/versions/       earlier copies, kept whenever a node's copy replaces the hub's

A site node *takes* an event: from then on the node is where devices are signed in and out, and the
hub's copy of that event cannot be scanned (it can still be managed: people, devices, sites). The node
sends its copy whenever it changes; the hub keeps the previous one in versions. Each event has a
revision number that goes up whenever the hub's copy changes, so a node can tell whether the hub
changed since they last agreed.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import HUB_DIR
from ..db import changes, init_db, now_iso, open_db, set_machine_db, using_db
from . import eventfile

HUB_DB = HUB_DIR / "hub.db"
EVENTS_DIR = HUB_DIR / "events"
ONLINE_SECONDS = 90          # a node that sent nothing for longer is shown offline
KEEP_VERSIONS = 60           # recent copies per event, plus one per day
KEEP_DAYS = 60
_lock = threading.RLock()

HUB_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    archived       INTEGER NOT NULL DEFAULT 0,
    rev            INTEGER NOT NULL DEFAULT 1,
    counter_at_rev INTEGER NOT NULL DEFAULT 0,
    node_id        INTEGER REFERENCES hub_nodes(id) ON DELETE SET NULL,
    taken_at       TEXT,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    token_hash  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_seen   REAL,
    last_ip     TEXT NOT NULL DEFAULT '',
    version     TEXT NOT NULL DEFAULT '',
    status_json TEXT NOT NULL DEFAULT '{}',
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hub_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    event_id INTEGER,
    node_id  INTEGER,
    kind     TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS hub_log_event ON hub_log(event_id, id);
"""


# -- setup --------------------------------------------------------------------

def setup() -> None:
    """Create the hub database (it also carries the RDR schema, for pages opened with no event picked)."""
    HUB_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    with using_db(HUB_DB):
        init_db()
    con = hub_db()
    try:
        with con:
            con.executescript(HUB_SCHEMA)
            con.execute("UPDATE event SET name = 'RDR Hub' WHERE id = 1 AND name = 'New Event'")
    finally:
        con.close()
    set_machine_db(HUB_DB)


def hub_db() -> sqlite3.Connection:
    return open_db(HUB_DB)


def event_dir(event_id: int) -> Path:
    return EVENTS_DIR / str(int(event_id))


def event_db_path(event_id: int) -> Path:
    return event_dir(event_id) / "rdr.db"


def log(con: sqlite3.Connection, kind: str, detail: str = "", event_id: Optional[int] = None, node_id: Optional[int] = None) -> None:
    con.execute("INSERT INTO hub_log (ts, event_id, node_id, kind, detail) VALUES (?,?,?,?,?)", (now_iso(), event_id, node_id, kind, detail))


# -- events -------------------------------------------------------------------

COPY_TABLES = ("sites", "device_types", "techs", "people", "devices")


def create_event(name: str, show_name: str = "", starts_on: str = "", ends_on: str = "",
                 copy_from: Optional[int] = None, copy_what: tuple[str, ...] = ()) -> int:
    """A new event with its own database. ``copy_what`` picks what to bring over from another event:
    "setup" (sites, device types, techs), "devices" (every device, back in stock), "people"."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the event a name")
    con = hub_db()
    try:
        with con:
            cur = con.execute("INSERT INTO hub_events (name, created_at, updated_at) VALUES (?,?,?)", (name, now_iso(), now_iso()))
            eid = cur.lastrowid
            log(con, "created", name, eid)
    finally:
        con.close()
    path = event_db_path(eid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with using_db(path):
        init_db()
    ev = open_db(path)
    try:
        with ev:
            ev.execute("UPDATE event SET name = ?, show_name = ?, starts_on = ?, ends_on = ?, updated_at = ? WHERE id = 1",
                       (name, show_name.strip(), starts_on or None, ends_on or None, now_iso()))
            if copy_from and copy_what:
                _copy_into(ev, int(copy_from), copy_what)
    finally:
        ev.close()
    _sync_rev(eid)
    return eid


def _copy_into(ev: sqlite3.Connection, src_id: int, what: tuple[str, ...]) -> None:
    src = open_db(event_db_path(src_id))
    try:
        tables = []
        if "setup" in what or "devices" in what:
            tables += ["sites", "device_types"]
        if "setup" in what:
            tables.append("techs")
        if "people" in what:
            tables.append("people")
        if "devices" in what:
            tables.append("devices")
        ev.execute("PRAGMA foreign_keys = OFF")
        for t in tables:
            if t in ("sites", "device_types", "techs"):   # the seeded defaults make way for the copied ones
                ev.execute(f"DELETE FROM {t}")
            cols = [c[1] for c in src.execute(f"PRAGMA table_info({t})")]
            mine = {c[1] for c in ev.execute(f"PRAGMA table_info({t})")}
            cols = [c for c in cols if c in mine]
            for r in src.execute(f"SELECT {', '.join(cols)} FROM {t}"):
                vals = dict(zip(cols, tuple(r)))
                if t == "devices":   # a new event starts with everything back in stock
                    vals.update(status="in", holder_id=None, linked_id="", last_event_at=None)
                ev.execute(f"INSERT INTO {t} ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))
        ev.execute("PRAGMA foreign_keys = ON")
    finally:
        src.close()


def _event_info(eid: int) -> dict:
    """Name, sites and counts from the event's own database."""
    path = event_db_path(eid)
    if not path.exists():
        return {"name": "", "sites": [], "counts": {}, "people": 0}
    con = open_db(path)
    try:
        ev = dict(con.execute("SELECT name, show_name, starts_on, ends_on FROM event WHERE id = 1").fetchone() or {})
        sites = [dict(r) for r in con.execute("SELECT id, name, stock_mode FROM sites WHERE active = 1 ORDER BY sort_order, name")]
        counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM devices GROUP BY status")}
        for s in sites:
            s["counts"] = {r["status"]: r["n"] for r in con.execute(
                "SELECT d.status, COUNT(*) AS n FROM devices d WHERE d.site_id = ? GROUP BY d.status", (s["id"],))}
            last = con.execute("SELECT MAX(ts) FROM transactions WHERE site_id = ?", (s["id"],)).fetchone()[0]
            s["last_scan"] = last or ""
        people = con.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        last = con.execute("SELECT MAX(ts) FROM transactions").fetchone()[0] or ""
        return {**ev, "sites": sites, "counts": counts, "people": people, "last_scan": last}
    finally:
        con.close()


def _sync_rev(eid: int) -> int:
    """The event's revision, raised first if its database changed on the hub since the last look."""
    with _lock:
        path = event_db_path(eid)
        ev = open_db(path)
        try:
            n = changes(ev)
            name = (ev.execute("SELECT name FROM event WHERE id = 1").fetchone() or [""])[0]
        finally:
            ev.close()
        con = hub_db()
        try:
            with con:
                row = con.execute("SELECT rev, counter_at_rev FROM hub_events WHERE id = ?", (eid,)).fetchone()
                if row is None:
                    raise KeyError(eid)
                rev = row["rev"]
                if row["counter_at_rev"] != n:
                    rev += 1
                    con.execute("UPDATE hub_events SET rev = ?, counter_at_rev = ?, name = ?, updated_at = ? WHERE id = ?", (rev, n, name or "Event", now_iso(), eid))
                return rev
        finally:
            con.close()


def rev(eid: int) -> int:
    return _sync_rev(eid)


def event(eid: int) -> Optional[dict]:
    con = hub_db()
    try:
        r = con.execute("SELECT e.*, n.name AS node_name, n.last_seen AS node_seen, n.status_json AS node_status FROM hub_events e "
                        "LEFT JOIN hub_nodes n ON n.id = e.node_id WHERE e.id = ?", (eid,)).fetchone()
        return _event_row(r) if r else None
    finally:
        con.close()


def _event_row(r) -> dict:
    d = dict(r)
    d["node_online"] = bool(d.get("node_seen") and time.time() - d["node_seen"] < ONLINE_SECONDS)
    try:
        d["node_state"] = json.loads(d.pop("node_status") or "{}")
    except ValueError:
        d["node_state"] = {}
    d["node_seen_text"] = _ago(d.get("node_seen"))
    return d


def events(include_archived: bool = False) -> list[dict]:
    con = hub_db()
    try:
        rows = con.execute("SELECT e.*, n.name AS node_name, n.last_seen AS node_seen, n.status_json AS node_status FROM hub_events e "
                           "LEFT JOIN hub_nodes n ON n.id = e.node_id " + ("" if include_archived else "WHERE e.archived = 0 ") +
                           "ORDER BY e.archived, e.id DESC").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = _event_row(r)
        try:
            d["rev"] = _sync_rev(d["id"])
        except Exception:  # noqa: BLE001
            pass
        d.update(info=_event_info(d["id"]))
        d["versions"] = len(list_versions(d["id"]))
        out.append(d)
    return out


def set_archived(eid: int, archived: bool) -> None:
    con = hub_db()
    try:
        with con:
            row = con.execute("SELECT node_id FROM hub_events WHERE id = ?", (eid,)).fetchone()
            if archived and row and row["node_id"]:
                raise ValueError("A node is running this event. Release it first.")
            con.execute("UPDATE hub_events SET archived = ?, updated_at = ? WHERE id = ?", (1 if archived else 0, now_iso(), eid))
            log(con, "archived" if archived else "unarchived", "", eid)
    finally:
        con.close()


def delete_event(eid: int) -> None:
    """Only an archived event can be deleted; its folder is moved aside, not erased."""
    con = hub_db()
    try:
        row = con.execute("SELECT archived, name FROM hub_events WHERE id = ?", (eid,)).fetchone()
        if not row or not row["archived"]:
            raise ValueError("Archive the event before deleting it")
        with con:
            con.execute("DELETE FROM hub_events WHERE id = ?", (eid,))
            log(con, "deleted", row["name"], eid)
    finally:
        con.close()
    src = event_dir(eid)
    if src.exists():
        trash = HUB_DIR / "deleted"
        trash.mkdir(parents=True, exist_ok=True)
        src.rename(trash / f"event-{eid}-{datetime.now():%Y%m%d-%H%M%S}")


def release(eid: int, by: str = "hub") -> None:
    con = hub_db()
    try:
        with con:
            row = con.execute("SELECT node_id FROM hub_events WHERE id = ?", (eid,)).fetchone()
            con.execute("UPDATE hub_events SET node_id = NULL, taken_at = NULL, updated_at = ? WHERE id = ?", (now_iso(), eid))
            log(con, "released", f"by the {by}", eid, row["node_id"] if row else None)
    finally:
        con.close()


def leased_node(eid: int) -> Optional[dict]:
    e = event(eid)
    return {"id": e["node_id"], "name": e["node_name"], "online": e["node_online"]} if e and e["node_id"] else None


# -- versions (the offsite backup) ---------------------------------------------

_VER = re.compile(r"^v(\d+)-(\d{8})-(\d{6})-([a-z]+)\.db$")


def save_version(eid: int, reason: str) -> Optional[Path]:
    path = event_db_path(eid)
    if not path.exists():
        return None
    vdir = event_dir(eid) / "versions"
    vdir.mkdir(parents=True, exist_ok=True)
    r = rev(eid)
    dest = vdir / f"v{r}-{datetime.now():%Y%m%d-%H%M%S}-{re.sub('[^a-z]', '', reason) or 'copy'}.db"
    tmp = dest.with_suffix(".part")
    eventfile.copy_db(path, tmp)
    tmp.replace(dest)
    _prune(vdir)
    return dest


def list_versions(eid: int) -> list[dict]:
    vdir = event_dir(eid) / "versions"
    out = []
    if vdir.exists():
        for p in sorted(vdir.glob("v*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            m = _VER.match(p.name)
            if m:
                rv, d, t, why = m.groups()
                out.append({"name": p.name, "rev": int(rv), "when": f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}", "reason": why, "size": p.stat().st_size})
    return out


def _prune(vdir: Path) -> None:
    files = sorted(vdir.glob("v*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    keep = set(files[:KEEP_VERSIONS])
    days: set[str] = set()
    cutoff = time.time() - KEEP_DAYS * 86400
    for p in files:
        day = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d")
        if day not in days and p.stat().st_mtime >= cutoff:
            days.add(day); keep.add(p)
    for p in files:
        if p not in keep:
            p.unlink(missing_ok=True)


def restore_version(eid: int, name: str) -> None:
    if not _VER.match(name):
        raise ValueError("Bad version name")
    if leased_node(eid):
        raise ValueError("A node is running this event. Release it first, or the node would overwrite this.")
    src = event_dir(eid) / "versions" / name
    if not src.exists():
        raise FileNotFoundError(name)
    save_version(eid, "beforerestore")
    eventfile.replace_live(event_db_path(eid), src)
    _sync_rev(eid)


# -- nodes ----------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def add_node(name: str) -> str:
    """Register a site node. Returns its pairing key, shown once."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the node a name")
    token = "rdrn_" + secrets.token_urlsafe(24)
    con = hub_db()
    try:
        with con:
            try:
                cur = con.execute("INSERT INTO hub_nodes (name, token_hash, created_at) VALUES (?,?,?)", (name, _hash(token), now_iso()))
            except sqlite3.IntegrityError as e:
                raise ValueError("A node with that name already exists") from e
            log(con, "node added", name, None, cur.lastrowid)
    finally:
        con.close()
    return token


def new_key(node_id: int) -> str:
    token = "rdrn_" + secrets.token_urlsafe(24)
    con = hub_db()
    try:
        with con:
            con.execute("UPDATE hub_nodes SET token_hash = ?, revoked = 0 WHERE id = ?", (_hash(token), node_id))
            log(con, "node key replaced", "", None, node_id)
    finally:
        con.close()
    return token


def revoke_node(node_id: int) -> None:
    con = hub_db()
    try:
        with con:
            con.execute("UPDATE hub_nodes SET revoked = 1 WHERE id = ?", (node_id,))
            con.execute("UPDATE hub_events SET node_id = NULL, taken_at = NULL WHERE node_id = ?", (node_id,))
            log(con, "node revoked", "", None, node_id)
    finally:
        con.close()


def node_for_token(token: str) -> Optional[dict]:
    if not token or not token.startswith("rdrn_"):
        return None
    con = hub_db()
    try:
        r = con.execute("SELECT * FROM hub_nodes WHERE token_hash = ? AND revoked = 0", (_hash(token),)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def nodes() -> list[dict]:
    con = hub_db()
    try:
        rows = con.execute("SELECT n.*, (SELECT GROUP_CONCAT(e.name, ', ') FROM hub_events e WHERE e.node_id = n.id) AS running "
                           "FROM hub_nodes n ORDER BY n.revoked, n.name").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["online"] = bool(d["last_seen"] and time.time() - d["last_seen"] < ONLINE_SECONDS) and not d["revoked"]
        d["seen_text"] = _ago(d["last_seen"])
        try:
            d["state"] = json.loads(d.pop("status_json") or "{}")
        except ValueError:
            d["state"] = {}
        out.append(d)
    return out


def heartbeat(node: dict, ip: str, status: dict) -> None:
    con = hub_db()
    try:
        with con:
            con.execute("UPDATE hub_nodes SET last_seen = ?, last_ip = ?, version = ?, status_json = ? WHERE id = ?",
                        (time.time(), ip[:64], str(status.get("version", ""))[:32], json.dumps(status)[:20000], node["id"]))
    finally:
        con.close()


def take(eid: int, node: dict) -> None:
    """The node starts running this event. Refused while another node has it."""
    con = hub_db()
    try:
        with con:
            row = con.execute("SELECT node_id, archived FROM hub_events WHERE id = ?", (eid,)).fetchone()
            if row is None:
                raise KeyError(eid)
            if row["archived"]:
                raise ValueError("That event is archived on the hub")
            if row["node_id"] and row["node_id"] != node["id"]:
                other = con.execute("SELECT name FROM hub_nodes WHERE id = ?", (row["node_id"],)).fetchone()
                raise PermissionError(f"{other['name'] if other else 'Another node'} is running this event. Release it on the hub first.")
            con.execute("UPDATE hub_events SET node_id = ?, taken_at = ?, updated_at = ? WHERE id = ?", (node["id"], now_iso(), now_iso(), eid))
            log(con, "taken", node["name"], eid, node["id"])
    finally:
        con.close()


def download(eid: int) -> tuple[int, bytes]:
    with _lock:
        r = _sync_rev(eid)
        return r, eventfile.pack(event_db_path(eid), event_dir(eid) / "uploads", {"rev": r, "event_id": eid})


def upload(eid: int, node: dict, blob: bytes, base_rev: int, force: bool = False, reason: str = "node") -> int:
    """A node's copy replaces the hub's. Refused when the hub changed since the node's base, unless forced
    (the node's admin chose Overwrite). The hub's copy is kept in versions first."""
    with _lock:
        e = event(eid)
        if e is None:
            raise KeyError(eid)
        if e["node_id"] != node["id"]:
            raise PermissionError("This node is not running that event on the hub")
        current = _sync_rev(eid)
        if current != base_rev and not force:
            raise RuntimeError(f"The hub's copy changed (revision {current}, the node's base is {base_rev})")
        tmp = event_dir(eid) / "incoming.db"
        try:
            manifest = eventfile.unpack(blob, tmp)
            save_version(eid, "beforenode" if not force else "beforeoverwrite")
            eventfile.replace_live(event_db_path(eid), tmp, event_dir(eid) / "uploads", manifest.get("logo"))
        finally:
            tmp.unlink(missing_ok=True)
            for s in ("-wal", "-shm"):
                Path(str(tmp) + s).unlink(missing_ok=True)
        con = hub_db()
        try:
            with con:
                ev = open_db(event_db_path(eid))
                try:
                    n = changes(ev)
                finally:
                    ev.close()
                new = current + 1
                con.execute("UPDATE hub_events SET rev = ?, counter_at_rev = ?, updated_at = ? WHERE id = ?", (new, n, now_iso(), eid))
                if force or reason != "node":
                    log(con, reason if not force else "overwritten", f"by {node['name']}", eid, node["id"])
        finally:
            con.close()
        return new


def recent_log(limit: int = 30, event_id: Optional[int] = None) -> list[dict]:
    con = hub_db()
    try:
        q = "SELECT l.*, e.name AS event_name, n.name AS node_name FROM hub_log l LEFT JOIN hub_events e ON e.id = l.event_id LEFT JOIN hub_nodes n ON n.id = l.node_id"
        args: tuple = ()
        if event_id:
            q += " WHERE l.event_id = ?"; args = (event_id,)
        return [dict(r) for r in con.execute(q + " ORDER BY l.id DESC LIMIT ?", (*args, limit))]
    finally:
        con.close()


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M")


# -- the hub's own copies ---------------------------------------------------------

_stop = threading.Event()
_thread: Optional[threading.Thread] = None
HOURLY = 3600


def _loop() -> None:
    """Every hour, keep a copy of any event whose data changed on the hub itself."""
    last: dict[int, int] = {}
    while not _stop.wait(HOURLY):
        for e in events(include_archived=False):
            try:
                if last.get(e["id"]) != e["rev"]:
                    if last.get(e["id"]) is not None:
                        save_version(e["id"], "hourly")
                    last[e["id"]] = e["rev"]
            except Exception:  # noqa: BLE001
                pass


def start() -> None:
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="rdr-hub")
        _thread.start()


def stop() -> None:
    _stop.set()


def copy_file(eid: int, name: str) -> Path:
    if not _VER.match(name):
        raise ValueError("Bad version name")
    p = event_dir(eid) / "versions" / name
    if not p.exists():
        raise FileNotFoundError(name)
    return p

