"""Automatic database snapshots so a crash, bad update or mistaken reset can never
cost the event's data.

* Every write already commits in its own transaction with synchronous=FULL.
* A snapshot (consistent copy via the SQLite backup API) is taken at start-up,
  at shutdown, before update / reset / restore, and every SNAP_INTERVAL while
  the database keeps changing.
* Snapshots live in data/backups/ and are pruned: the last KEEP_RECENT, plus
  one per day for KEEP_DAYS days.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR, DB_PATH

SNAP_DIR = DATA_DIR / "backups"
SNAP_INTERVAL = 10 * 60      # seconds between automatic snapshots while data changes
KEEP_RECENT = 72             # ~12 hours of 10-minute snapshots
KEEP_DAYS = 30
_NAME = re.compile(r"^rdr-(\d{8})-(\d{6})-([a-z]+)\.db$")

_lock = threading.Lock()
_last_auto_mtime: float = 0.0
_thread: threading.Thread | None = None
_stop = threading.Event()
last_error: str | None = None


def _db_mtime() -> float:
    try:
        m = DB_PATH.stat().st_mtime
        wal = Path(str(DB_PATH) + "-wal")
        if wal.exists():
            m = max(m, wal.stat().st_mtime)
        return m
    except OSError:
        return 0.0


def snapshot(reason: str = "manual") -> Path | None:
    """Write a consistent copy of the live database. Returns the path, or None if there is no database yet."""
    global last_error
    if not DB_PATH.exists():
        return None
    reason = re.sub(r"[^a-z]", "", reason.lower()) or "manual"
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"rdr-{datetime.now():%Y%m%d-%H%M%S}-{reason}.db"
    final = SNAP_DIR / name
    tmp = SNAP_DIR / (name + ".part")
    with _lock:
        try:
            src = sqlite3.connect(DB_PATH, timeout=30)
            dst = sqlite3.connect(tmp)
            try:
                with dst:
                    src.backup(dst)
            finally:
                src.close(); dst.close()
            tmp.replace(final)       # atomic: a half-written file is never seen as a snapshot
            last_error = None
        except Exception as e:  # noqa: BLE001
            last_error = f"{datetime.now():%H:%M:%S} snapshot failed: {e}"
            tmp.unlink(missing_ok=True)
            return None
    prune()
    return final


def list_snapshots() -> list[dict]:
    out = []
    if not SNAP_DIR.exists():
        return out
    for p in sorted(SNAP_DIR.glob("rdr-*.db"), reverse=True):
        m = _NAME.match(p.name)
        if not m:
            continue
        d, t, reason = m.groups()
        out.append({"name": p.name, "when": f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}", "reason": reason, "size": p.stat().st_size, "path": p})
    return out


def prune() -> None:
    snaps = list_snapshots()
    keep: set[str] = set(s["name"] for s in snaps[:KEEP_RECENT])
    seen_days: set[str] = set()
    cutoff = time.time() - KEEP_DAYS * 86400
    for s in snaps:
        day = s["when"][:10]
        if day not in seen_days and s["path"].stat().st_mtime >= cutoff:
            seen_days.add(day); keep.add(s["name"])
    for s in snaps:
        if s["name"] not in keep:
            try:
                s["path"].unlink()
            except OSError:
                pass


def integrity_check() -> tuple[bool, str]:
    if not DB_PATH.exists():
        return True, "no database yet"
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            res = con.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            con.close()
        return res == "ok", res
    except sqlite3.Error as e:
        return False, str(e)


def restore_snapshot(name: str) -> None:
    """Replace the live database with a snapshot. The current one is snapshotted first. Caller restarts."""
    if not _NAME.match(name):
        raise ValueError("Bad snapshot name")
    src = SNAP_DIR / name
    if not src.exists():
        raise FileNotFoundError("Snapshot not found")
    snapshot("prerestore")
    for suffix in ("-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    tmp = DATA_DIR / "rdr.db.restoring"
    shutil.copy2(src, tmp)
    tmp.replace(DB_PATH)


def _loop() -> None:
    global _last_auto_mtime
    while not _stop.wait(60):
        try:
            m = _db_mtime()
            latest = list_snapshots()
            age = time.time() - latest[0]["path"].stat().st_mtime if latest else 1e9
            if m > _last_auto_mtime and age >= SNAP_INTERVAL:
                if snapshot("auto"):
                    _last_auto_mtime = m
        except Exception:  # noqa: BLE001 - never let the scheduler die
            pass


def start() -> None:
    global _thread, _last_auto_mtime
    snapshot("start")
    _last_auto_mtime = _db_mtime()
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="rdr-snapshots")
        _thread.start()


def stop() -> None:
    _stop.set()
    snapshot("stop")


def status() -> dict:
    snaps = list_snapshots()
    ok, detail = integrity_check()
    return {"count": len(snaps), "latest": snaps[0] if snaps else None, "integrity_ok": ok, "integrity": detail, "error": last_error, "dir": str(SNAP_DIR)}
