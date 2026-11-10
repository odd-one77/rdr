"""Full backup and restore of everything RDR stores, as one zip.

The zip holds:
  rdr.db          consistent SQLite snapshot (database, settings, history)
  config.json     server config (port, update source; token optional)
  uploads/...     event logo
  manifest.json   version + timestamp

Restoring replaces the database and uploads, so the admin confirms first,
and the server restarts afterwards so every connection reopens the new file.
"""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..config import CONFIG_PATH, DATA_DIR, DB_PATH, UPLOAD_DIR, ensure_dirs, load_config, save_config

SENSITIVE_KEYS = ("github_token",)


def create(include_token: bool = False) -> tuple[str, bytes]:
    """Return (filename, zip bytes)."""
    ensure_dirs()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Consistent DB snapshot via the SQLite backup API (works while the app is live).
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "rdr.db"
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(snap)
            with dst:
                src.backup(dst)
            src.close(); dst.close()
            zf.write(snap, "rdr.db")
        cfg = load_config()
        if not include_token:
            for k in SENSITIVE_KEYS:
                cfg.pop(k, None)
        zf.writestr("config.json", json.dumps(cfg, indent=2))
        if UPLOAD_DIR.exists():
            for f in UPLOAD_DIR.rglob("*"):
                if f.is_file():
                    zf.write(f, f"uploads/{f.relative_to(UPLOAD_DIR).as_posix()}")
        zf.writestr("manifest.json", json.dumps({"app": "RDR", "version": __version__, "created": datetime.now().isoformat(timespec="seconds")}))
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return f"rdr-backup-{stamp}.zip", buf.getvalue()


def inspect(blob: bytes) -> dict:
    """Validate an uploaded backup and describe it without applying it."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        if "rdr.db" not in names:
            raise ValueError("Not an RDR backup: rdr.db is missing")
        manifest = json.loads(zf.read("manifest.json")) if "manifest.json" in names else {}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rdr.db"
            p.write_bytes(zf.read("rdr.db"))
            con = sqlite3.connect(p)
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("The database inside the backup is corrupt")
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"devices", "people", "transactions"} <= tables:
                    raise ValueError("The database inside the backup is not an RDR database")
                ev = con.execute("SELECT name, show_name FROM event WHERE id = 1").fetchone()
                counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("people", "devices", "transactions", "sites", "techs")}
            finally:
                con.close()
        return {
            "version": manifest.get("version", "?"), "created": manifest.get("created", "?"),
            "event": ev[0] if ev else "", "show": ev[1] if ev else "", "counts": counts,
            "has_logo": any(n.startswith("uploads/") for n in names), "has_config": "config.json" in names,
        }


def restore(blob: bytes, restore_config: bool = True) -> dict:
    """Replace the live data with the backup. Caller must restart the server afterwards."""
    info = inspect(blob)
    ensure_dirs()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        tmp_db = DATA_DIR / "rdr.db.restore"
        tmp_db.write_bytes(zf.read("rdr.db"))
        # Drop WAL/SHM leftovers from the old database, then swap files.
        for suffix in ("-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        if DB_PATH.exists():
            shutil.copy2(DB_PATH, DATA_DIR / "rdr.db.before-restore")
        tmp_db.replace(DB_PATH)
        if UPLOAD_DIR.exists():
            shutil.rmtree(UPLOAD_DIR)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for n in zf.namelist():
            if n.startswith("uploads/") and not n.endswith("/") and ".." not in n:
                dest = UPLOAD_DIR / n[len("uploads/"):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(n))
        if restore_config and "config.json" in zf.namelist():
            current = load_config()
            incoming = json.loads(zf.read("config.json"))
            # Keep this machine's token if the backup has none.
            for k in SENSITIVE_KEYS:
                if k not in incoming and k in current:
                    incoming[k] = current[k]
            save_config(incoming)
    return info
