"""Away from desk: a per-site notice that locks the kiosk and phone behind the admin password
and replaces the mirror's welcome with "Back in 10 min" / "Back at 14:35" / "Be right back"."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from ..db import get_setting, now_iso, set_setting

PRESETS = [("brb", "Be right back", None), ("5", "Back in 5 min", 5), ("10", "Back in 10 min", 10), ("15", "Back in 15 min", 15), ("30", "Back in 30 min", 30), ("60", "Back in 1 hour", 60)]


def _key(site_id: int) -> str:
    return f"away:{site_id}"


def get(db: sqlite3.Connection, site_id: int) -> Optional[dict]:
    raw = get_setting(db, _key(site_id))
    if not raw:
        return None
    try:
        a = json.loads(raw)
    except ValueError:
        return None
    return describe(a)


def describe(a: dict) -> dict:
    """Add the live wording: minutes left and whether the time has passed."""
    out = dict(a)
    out["remaining"] = None
    out["overdue"] = False
    if a.get("until"):
        try:
            left = (datetime.fromisoformat(a["until"]) - datetime.now()).total_seconds()
        except ValueError:
            left = 0
        out["remaining"] = max(0, int(left // 60) + (1 if left % 60 > 0 and left > 0 else 0))
        out["overdue"] = left <= 0
        out["until_hm"] = a["until"][11:16]
    return out


def start(db: sqlite3.Connection, site_id: int, minutes: Optional[int] = None, until_hm: str = "", note: str = "", tech_name: str = "") -> dict:
    now = datetime.now()
    until: Optional[datetime] = None
    if until_hm:
        try:
            h, m = (int(x) for x in until_hm.strip().split(":")[:2])
        except ValueError as e:
            raise ValueError("Time must be HH:MM") from e
        until = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if until <= now:
            until += timedelta(days=1)
        label = f"Back at {until:%H:%M}"
    elif minutes:
        minutes = max(1, min(int(minutes), 24 * 60))
        until = now + timedelta(minutes=minutes)
        label = f"Back in {minutes} min" if minutes < 60 else (f"Back in {minutes // 60} hour" + ("s" if minutes >= 120 else "") if minutes % 60 == 0 else f"Back at {until:%H:%M}")
    else:
        label = "Be right back"
    a = {"label": label, "until": until.replace(microsecond=0).isoformat() if until else None, "note": (note or "").strip()[:140], "tech_name": tech_name, "since": now_iso()}
    set_setting(db, _key(site_id), json.dumps(a))
    return describe(a)


def clear(db: sqlite3.Connection, site_id: int) -> None:
    set_setting(db, _key(site_id), None)


def all_sites(db: sqlite3.Connection) -> list[dict]:
    out = []
    for r in db.execute("SELECT id, name FROM sites WHERE active = 1 ORDER BY sort_order, name"):
        a = get(db, r["id"])
        if a:
            out.append({"site_id": r["id"], "site_name": r["name"], **a})
    return out
