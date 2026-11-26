"""An event's data as one file, for moving it between the hub and a site node.

The file is a zip holding ``rdr.db`` (a consistent copy made with SQLite's backup API), the event logo
if there is one, and ``manifest.json``. Machine settings (the admin password, away notices, the hub
link itself) are never part of it.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from .. import __version__
from ..db import LOCAL_SETTING_KEYS, LOCAL_SETTING_PREFIXES, changes, init_db, is_local_setting, open_db, using_db

MAX_BYTES = 256 * 1024 * 1024
_LOGO = re.compile(r"^event-logo\.(png|jpe?g|svg|webp|gif)$")


def copy_db(src_path: Path, dst_path: Path) -> None:
    """Consistent copy of a live database into a new file."""
    src, dst = sqlite3.connect(src_path, timeout=30), sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close(); dst.close()


def _strip_local(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        with con:
            for k in LOCAL_SETTING_KEYS:
                con.execute("DELETE FROM settings WHERE key = ?", (k,))
            for p in LOCAL_SETTING_PREFIXES:
                con.execute("DELETE FROM settings WHERE key LIKE ?", (p + "%",))
            con.execute("DELETE FROM kiosk_state")
    finally:
        con.close()


def pack(db_path: Path, upload_dir: Path, extra: Optional[dict] = None) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "rdr.db"
        copy_db(db_path, snap)
        _strip_local(snap)
        con = open_db(snap)
        try:
            ev = con.execute("SELECT name, logo_path FROM event WHERE id = 1").fetchone()
            n = changes(con)
        finally:
            con.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, "rdr.db")
            logo = (ev["logo_path"] or "").split("?")[0].rsplit("/", 1)[-1] if ev else ""
            if logo and _LOGO.match(logo) and (upload_dir / logo).exists():
                zf.write(upload_dir / logo, f"uploads/{logo}")
            zf.writestr("manifest.json", json.dumps({"version": __version__, "event": ev["name"] if ev else "", "changes": n, **(extra or {})}))
        return buf.getvalue()


def unpack(blob: bytes, dest: Path) -> dict:
    """Check a received file and write its database to ``dest`` (a new file). Returns the manifest plus the logo."""
    if len(blob) > MAX_BYTES:
        raise ValueError("That event file is too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise ValueError("Not an RDR event file") from e
    names = set(zf.namelist())
    if "rdr.db" not in names:
        raise ValueError("Not an RDR event file")
    manifest = json.loads(zf.read("manifest.json")) if "manifest.json" in names else {}
    dest.write_bytes(zf.read("rdr.db"))
    con = sqlite3.connect(dest)
    try:
        ok = con.execute("PRAGMA quick_check").fetchone()[0]
        has = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()
    if ok != "ok" or not {"event", "devices", "people", "transactions"} <= has:
        dest.unlink(missing_ok=True)
        raise ValueError("The event file is damaged")
    with using_db(dest):
        init_db()   # bring an older copy up to this version's schema
    logo = None
    for n in names:
        if n.startswith("uploads/") and _LOGO.match(n[8:]):
            logo = (n[8:], zf.read(n))
    manifest["logo"] = logo
    return manifest


def replace_live(live_path: Path, new_path: Path, upload_dir: Optional[Path] = None, logo=None) -> None:
    """Make the live database a copy of ``new_path`` in place, keeping its machine settings and kiosk screens.
    Uses the backup API, so connections that are open elsewhere see either the old or the new copy."""
    keep_settings, keep_kiosk = [], []
    if live_path.exists():
        con = open_db(live_path)
        try:
            keep_settings = [(r["key"], r["value"]) for r in con.execute("SELECT key, value FROM settings") if is_local_setting(r["key"])]
            try:
                keep_kiosk = [tuple(r) for r in con.execute("SELECT site_id, state_json, updated_at FROM kiosk_state")]
            except sqlite3.Error:
                pass
        finally:
            con.close()
    src, dst = sqlite3.connect(new_path, timeout=30), sqlite3.connect(live_path, timeout=30)
    try:
        src.backup(dst)
    finally:
        src.close(); dst.close()
    con = open_db(live_path)
    try:
        with con:
            for k, v in keep_settings:
                con.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
            con.execute("DELETE FROM kiosk_state")
            con.executemany("INSERT OR REPLACE INTO kiosk_state (site_id, state_json, updated_at) VALUES (?,?,?)", keep_kiosk)
    finally:
        con.close()
    if upload_dir is not None and logo:
        upload_dir.mkdir(parents=True, exist_ok=True)
        for old in upload_dir.glob("event-logo.*"):
            old.unlink(missing_ok=True)
        (upload_dir / logo[0]).write_bytes(logo[1])
