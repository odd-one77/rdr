"""Live configuration: a fingerprint of everything the kiosk, phone and mirror
display from settings (event, sites, techs, device types, mirror options).

Pages poll a tiny endpoint and, when the fingerprint changes, pull the new
values and update themselves in place, so nobody has to press refresh after
an admin edit.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from ..db import get_setting
from . import streams

MIRROR_KEYS = ("mirror_title", "mirror_prompt", "mirror_sayings", "stream_transcode", "pgm_size", "movie_quotes")


def snapshot(db: sqlite3.Connection) -> dict:
    event = db.execute("SELECT name, show_name, logo_path FROM event WHERE id = 1").fetchone()
    sites = [dict(r) for r in db.execute("SELECT id, name, stock_mode, rtsp_url, active FROM sites ORDER BY sort_order, name")]
    techs = [dict(r) for r in db.execute("SELECT id, username, display_name, role, site_id, active FROM techs ORDER BY id")]
    types = [dict(r) for r in db.execute("SELECT id, name, short_code, sort_order, active FROM device_types ORDER BY sort_order, name")]
    settings = {k: get_setting(db, k) for k in MIRROR_KEYS}
    return {"event": dict(event) if event else {}, "sites": sites, "techs": techs, "types": types, "settings": settings}


def version(snap: dict) -> str:
    return hashlib.md5(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()[:12]


def for_kiosk(db: sqlite3.Connection, tech_id: int, site_id: int) -> dict:
    snap = snapshot(db)
    tech = next((t for t in snap["techs"] if t["id"] == tech_id), None)
    site = next((s for s in snap["sites"] if s["id"] == site_id), None)
    return {
        "version": version(snap),
        "event": snap["event"],
        "tech": {"id": tech_id, "display_name": tech["display_name"] if tech else None, "active": bool(tech and tech["active"])},
        "site": {"id": site_id, "name": site["name"] if site else None, "active": bool(site and site["active"])},
        "sites": [{"id": s["id"], "name": s["name"]} for s in snap["sites"] if s["active"]],
        "types": [t for t in snap["types"] if t["active"]],
    }


def for_mirror(db: sqlite3.Connection, site_id: int) -> dict:
    snap = snapshot(db)
    site = next((s for s in snap["sites"] if s["id"] == site_id), None)
    url = (site or {}).get("rtsp_url") or ""
    size = snap["settings"].get("pgm_size") or "half"
    return {
        "version": version(snap),
        "event": snap["event"],
        "site": {"id": site_id, "name": site["name"] if site else "RDR"},
        "feed": dict(streams.browser_feed(url), size=size if size in ("small", "half", "large") else "half"),
    }
