"""Core sign in / sign out rules.

Device status is one of: in, out, lost, damaged.
Every change writes a timestamped row to ``transactions`` so the full
history of each device and each person can be viewed later.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

from ..db import now_iso

DEVICE_SELECT = """
SELECT d.*, t.name AS type_name, t.short_code AS type_code,
       p.name AS holder_name, p.dept AS holder_dept, p.phone AS holder_phone,
       s.name AS site_name,
       a.person_id AS assigned_person_id, ap.name AS assigned_person_name
FROM devices d
JOIN device_types t ON t.id = d.type_id
LEFT JOIN people p ON p.id = d.holder_id
LEFT JOIN sites s ON s.id = d.site_id
LEFT JOIN assignments a ON a.device_id = d.id
LEFT JOIN people ap ON ap.id = a.person_id
"""


def norm_si(si: str) -> str:
    return (si or "").strip().upper()


def norm_phone(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+")


# -- lookups ------------------------------------------------------------------

def device_by_si(db: sqlite3.Connection, si: str) -> Optional[sqlite3.Row]:
    return db.execute(DEVICE_SELECT + " WHERE d.si = ? COLLATE NOCASE", (norm_si(si),)).fetchone()


def device_by_id(db: sqlite3.Connection, device_id: int) -> Optional[sqlite3.Row]:
    return db.execute(DEVICE_SELECT + " WHERE d.id = ?", (device_id,)).fetchone()


def devices_by_ids(db: sqlite3.Connection, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
    ids = list(ids)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(DEVICE_SELECT + f" WHERE d.id IN ({marks})", ids).fetchall()
    return {r["id"]: r for r in rows}


def person_by_id(db: sqlite3.Connection, person_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()


def person_holdings(db: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    return db.execute(
        DEVICE_SELECT + " WHERE d.holder_id = ? ORDER BY t.sort_order, d.label, d.si",
        (person_id,),
    ).fetchall()


def person_assignments(db: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    """Pre-assigned devices for a person that they do not currently hold."""
    return db.execute(
        DEVICE_SELECT
        + " WHERE a.person_id = ? AND (d.holder_id IS NULL OR d.holder_id <> ?)"
        " ORDER BY t.sort_order, d.label, d.si",
        (person_id, person_id),
    ).fetchall()


def person_plan(db: sqlite3.Connection, person_id: int) -> list[dict]:
    """Kit plan by device type with how many of each the person currently holds."""
    rows = db.execute(
        """
        SELECT pl.type_id, t.name AS type_name, t.short_code, pl.qty, pl.note,
               (SELECT COUNT(*) FROM devices d WHERE d.holder_id = pl.person_id AND d.type_id = pl.type_id AND d.status = 'out') AS held
        FROM plans pl JOIN device_types t ON t.id = pl.type_id
        WHERE pl.person_id = ? ORDER BY t.sort_order, t.name
        """,
        (person_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_plan(db: sqlite3.Connection, person_id: int, type_id: int, qty: int = 1, note: str = "", tech: Optional[dict] = None) -> None:
    if qty <= 0:
        db.execute("DELETE FROM plans WHERE person_id = ? AND type_id = ?", (person_id, type_id))
    else:
        db.execute(
            """INSERT INTO plans (person_id, type_id, qty, note) VALUES (?,?,?,?)
               ON CONFLICT(person_id, type_id) DO UPDATE SET qty = excluded.qty, note = excluded.note""",
            (person_id, type_id, qty, note),
        )
    t = db.execute("SELECT name FROM device_types WHERE id = ?", (type_id,)).fetchone()
    log(db, "plan", None, person_id, tech, note=f"{qty} x {t['name'] if t else type_id}")


def clear_plan(db: sqlite3.Connection, person_id: int) -> None:
    db.execute("DELETE FROM plans WHERE person_id = ?", (person_id,))


def match_type(db: sqlite3.Connection, text: str) -> Optional[sqlite3.Row]:
    """Find a device type by exact or partial name ('Pro-D2' -> 'Pro-D2 Headset')."""
    text = (text or "").strip()
    if not text:
        return None
    row = db.execute("SELECT * FROM device_types WHERE name = ? COLLATE NOCASE", (text,)).fetchone()
    if row:
        return row
    rows = db.execute("SELECT * FROM device_types WHERE name LIKE ? COLLATE NOCASE OR ? LIKE '%' || name || '%' ORDER BY LENGTH(name) DESC", (f"%{text}%", text)).fetchall()
    return rows[0] if rows else None


def search_people(db: sqlite3.Connection, q: str, limit: int = 12) -> list[sqlite3.Row]:
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    digits = norm_phone(q)
    phone_like = f"%{digits}%" if digits else None
    rows = db.execute(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM devices d WHERE d.holder_id = p.id) AS held_count,
               (SELECT COUNT(*) FROM assignments a JOIN devices d ON d.id = a.device_id
                 WHERE a.person_id = p.id AND (d.holder_id IS NULL OR d.holder_id <> p.id)) AS assigned_count
        FROM people p
        WHERE p.name LIKE ? COLLATE NOCASE
           OR p.external_id LIKE ? COLLATE NOCASE
           OR p.dept LIKE ? COLLATE NOCASE
           OR p.bolero_id LIKE ? COLLATE NOCASE
           OR (? IS NOT NULL AND REPLACE(REPLACE(p.phone,' ',''),'-','') LIKE ?)
        ORDER BY CASE WHEN p.name LIKE ? COLLATE NOCASE THEN 0 WHEN p.bolero_id = ? COLLATE NOCASE THEN 0 ELSE 1 END, p.name
        LIMIT ?
        """,
        (like, like, like, like, phone_like, phone_like, f"{q}%", q, limit),
    ).fetchall()
    return rows


# -- summaries ----------------------------------------------------------------

def summary(db: sqlite3.Connection, site_id: Optional[int] = None) -> dict:
    """Counts per device type. With a site, counts that site's own stock plus shared stock."""
    where = ""
    params: list = []
    if site_id is not None:
        where = "WHERE d.site_id IS NULL OR d.site_id = ?"
        params = [site_id]
    rows = db.execute(
        f"""
        SELECT t.id, t.name, t.short_code,
               COUNT(d.id) AS total,
               SUM(CASE WHEN d.status = 'in' THEN 1 ELSE 0 END) AS n_in,
               SUM(CASE WHEN d.status = 'out' THEN 1 ELSE 0 END) AS n_out,
               SUM(CASE WHEN d.status = 'lost' THEN 1 ELSE 0 END) AS n_lost,
               SUM(CASE WHEN d.status = 'damaged' THEN 1 ELSE 0 END) AS n_damaged
        FROM device_types t
        LEFT JOIN devices d ON d.type_id = t.id {('AND (' + where[6:] + ')') if where else ''}
        WHERE t.active = 1
        GROUP BY t.id
        HAVING COUNT(d.id) > 0
        ORDER BY t.sort_order, t.name
        """,
        params,
    ).fetchall()
    types = [dict(r) for r in rows]
    totals = {
        "total": sum(t["total"] for t in types),
        "n_in": sum(t["n_in"] or 0 for t in types),
        "n_out": sum(t["n_out"] or 0 for t in types),
        "n_lost": sum(t["n_lost"] or 0 for t in types),
        "n_damaged": sum(t["n_damaged"] or 0 for t in types),
    }
    return {"types": types, "totals": totals}


# -- logging ------------------------------------------------------------------

def log(
    db: sqlite3.Connection,
    action: str,
    device_id: Optional[int],
    person_id: Optional[int],
    tech: Optional[dict],
    reason: str = "",
    note: str = "",
    related_device_id: Optional[int] = None,
) -> str:
    ts = now_iso()
    db.execute(
        """INSERT INTO transactions (ts, action, device_id, person_id, tech_id, site_id, reason, note, related_device_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            ts,
            action,
            device_id,
            person_id,
            tech.get("tech_id") if tech else None,
            tech.get("site_id") if tech else None,
            reason,
            note,
            related_device_id,
        ),
    )
    if device_id is not None:
        db.execute("UPDATE devices SET last_event_at = ? WHERE id = ?", (ts, device_id))
    return ts


def _set_device(db: sqlite3.Connection, device_id: int, status: str, holder_id: Optional[int]) -> None:
    db.execute(
        "UPDATE devices SET status = ?, holder_id = ?, updated_at = ? WHERE id = ?",
        (status, holder_id, now_iso(), device_id),
    )


def _brief(d: sqlite3.Row) -> dict:
    return {
        "id": d["id"],
        "si": d["si"],
        "label": d["label"],
        "type": d["type_name"],
        "status": d["status"],
        "holder_id": d["holder_id"],
        "holder_name": d["holder_name"],
        "site_name": d["site_name"],
    }


# -- operations ---------------------------------------------------------------

def sign_out(
    db: sqlite3.Connection,
    device_ids: list[int],
    person_id: int,
    tech: Optional[dict],
    force_ids: Optional[set[int]] = None,
) -> list[dict]:
    """Issue devices to a person.

    Returns one result per device with ``outcome`` of:
      issued        - was in stock, now out to the person
      already_held  - already out to this same person (timestamp logged)
      conflict      - out to someone else / lost / damaged; needs ``force_ids`` to override
      reassigned    - was out to someone else, forced to this person
      reissued      - was lost/damaged, forced back into use and issued
      missing       - unknown device id
    """
    force_ids = force_ids or set()
    person = person_by_id(db, person_id)
    if person is None:
        raise ValueError("Unknown person")
    devices = devices_by_ids(db, device_ids)
    results = []
    for did in device_ids:
        d = devices.get(did)
        if d is None:
            results.append({"id": did, "outcome": "missing", "message": "Unknown device"})
            continue
        r = _brief(d)
        if d["status"] == "in":
            _set_device(db, did, "out", person_id)
            r.update(outcome="issued", message=f"Signed out to {person['name']}", ts=log(db, "out", did, person_id, tech))
        elif d["status"] == "out" and d["holder_id"] == person_id:
            r.update(outcome="already_held", message="Already signed out to this person", ts=log(db, "out_noop", did, person_id, tech))
        elif d["status"] == "out":
            if did in force_ids:
                old = d["holder_id"]
                log(db, "in", did, old, tech, note=f"Reassigned to {person['name']}")
                _set_device(db, did, "out", person_id)
                r.update(outcome="reassigned", message=f"Reassigned from {d['holder_name']}", ts=log(db, "out_reassign", did, person_id, tech, note=f"Was out to {d['holder_name']}"))
            else:
                r.update(outcome="conflict", message=f"Already OUT to {d['holder_name']}")
        else:  # lost / damaged
            if did in force_ids:
                log(db, "restored", did, None, tech, note="Restored on sign out")
                _set_device(db, did, "out", person_id)
                r.update(outcome="reissued", message=f"Was {d['status']}, restored and signed out", ts=log(db, "out", did, person_id, tech))
            else:
                r.update(outcome="conflict", message=f"Device is marked {d['status'].upper()}")
        results.append(r)
    return results


def sign_in(db: sqlite3.Connection, device_ids: list[int], tech: Optional[dict]) -> list[dict]:
    """Return devices to stock. Already-in devices are left alone but still timestamped."""
    devices = devices_by_ids(db, device_ids)
    results = []
    for did in device_ids:
        d = devices.get(did)
        if d is None:
            results.append({"id": did, "outcome": "missing", "message": "Unknown device"})
            continue
        r = _brief(d)
        if d["status"] == "out":
            _set_device(db, did, "in", None)
            r.update(outcome="returned", message=f"Signed in from {d['holder_name']}", ts=log(db, "in", did, d["holder_id"], tech))
        elif d["status"] == "in":
            r.update(outcome="already_in", message="Already in stock (scan recorded)", ts=log(db, "in_noop", did, None, tech))
        else:
            _set_device(db, did, "in", None)
            r.update(outcome="restored", message=f"Was {d['status']}, back in stock", ts=log(db, "restored", did, d["holder_id"], tech))
        results.append(r)
    return results


def mark_status(
    db: sqlite3.Connection,
    device_id: int,
    status: str,
    tech: Optional[dict],
    reason: str = "",
    note: str = "",
    keep_holder: bool = False,
) -> dict:
    """Mark a device lost or damaged (or restore it to 'in')."""
    if status not in ("lost", "damaged", "in"):
        raise ValueError("Bad status")
    d = device_by_id(db, device_id)
    if d is None:
        raise ValueError("Unknown device")
    person_id = d["holder_id"]
    if status == "in":
        _set_device(db, device_id, "in", None)
        ts = log(db, "restored", device_id, person_id, tech, reason, note)
    else:
        _set_device(db, device_id, status, person_id if keep_holder else None)
        ts = log(db, status, device_id, person_id, tech, reason, note)
    r = _brief(device_by_id(db, device_id))
    r.update(outcome=status, ts=ts)
    return r


def swap(
    db: sqlite3.Connection,
    old_device_id: int,
    new_device_id: int,
    tech: Optional[dict],
    reason: str,
    note: str = "",
    old_status: str = "damaged",
) -> dict:
    """Straight swap: the person hands back ``old`` and receives ``new``.

    ``old_status`` is what happens to the returned item: damaged, lost or in.
    """
    old = device_by_id(db, old_device_id)
    new = device_by_id(db, new_device_id)
    if old is None or new is None:
        raise ValueError("Unknown device")
    if old["holder_id"] is None:
        raise ValueError("The item being swapped out is not signed out to anyone")
    if new["status"] == "out":
        raise ValueError(f"Replacement {new['si']} is already out to {new['holder_name']}")
    if old_status not in ("damaged", "lost", "in"):
        raise ValueError("Bad status for returned item")
    person_id = old["holder_id"]
    _set_device(db, old_device_id, old_status, None)
    log(db, "swap_out", old_device_id, person_id, tech, reason, note, related_device_id=new_device_id)
    if old_status != "in":
        log(db, old_status, old_device_id, person_id, tech, reason, note)
    if new["status"] in ("lost", "damaged"):
        log(db, "restored", new_device_id, None, tech, note="Restored on swap")
    _set_device(db, new_device_id, "out", person_id)
    ts = log(db, "swap_in", new_device_id, person_id, tech, reason, note, related_device_id=old_device_id)
    return {"old": _brief(device_by_id(db, old_device_id)), "new": _brief(device_by_id(db, new_device_id)), "ts": ts}


def quick_create_device(
    db: sqlite3.Connection,
    si: str,
    type_id: int,
    label: str,
    tech: Optional[dict],
    site_id: Optional[int] = None,
    notes: str = "",
    action: str = "created",
) -> sqlite3.Row:
    ts = now_iso()
    cur = db.execute(
        """INSERT INTO devices (si, type_id, label, site_id, status, notes, created_at, updated_at)
           VALUES (?,?,?,?, 'in', ?, ?, ?)""",
        (norm_si(si), type_id, (label or "").strip(), site_id, notes, ts, ts),
    )
    log(db, action, cur.lastrowid, None, tech)
    return device_by_id(db, cur.lastrowid)


def create_person(
    db: sqlite3.Connection,
    name: str,
    dept: str = "",
    role: str = "",
    phone: str = "",
    external_id: str = "",
    notes: str = "",
    bolero_id: str = "",
) -> sqlite3.Row:
    ts = now_iso()
    cur = db.execute(
        """INSERT INTO people (external_id, name, dept, role, phone, bolero_id, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ((external_id or "").strip() or None, name.strip(), dept.strip(), role.strip(), phone.strip(), (bolero_id or "").strip(), notes, ts, ts),
    )
    return person_by_id(db, cur.lastrowid)


def set_assignment(db: sqlite3.Connection, device_id: int, person_id: int, tech: Optional[dict], note: str = "") -> None:
    db.execute(
        """INSERT INTO assignments (device_id, person_id, note, created_at) VALUES (?,?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET person_id = excluded.person_id, note = excluded.note, created_at = excluded.created_at""",
        (device_id, person_id, note, now_iso()),
    )
    log(db, "assigned", device_id, person_id, tech, note=note)


def clear_assignment(db: sqlite3.Connection, device_id: int, tech: Optional[dict]) -> None:
    row = db.execute("SELECT person_id FROM assignments WHERE device_id = ?", (device_id,)).fetchone()
    db.execute("DELETE FROM assignments WHERE device_id = ?", (device_id,))
    if row:
        log(db, "unassigned", device_id, row["person_id"], tech)


# -- history ------------------------------------------------------------------

TX_SELECT = """
SELECT x.*, d.si, d.label, dt.name AS type_name,
       p.name AS person_name, p.dept AS person_dept,
       t.display_name AS tech_name, s.name AS site_name,
       rd.si AS related_si, rd.label AS related_label
FROM transactions x
LEFT JOIN devices d ON d.id = x.device_id
LEFT JOIN device_types dt ON dt.id = d.type_id
LEFT JOIN people p ON p.id = x.person_id
LEFT JOIN techs t ON t.id = x.tech_id
LEFT JOIN sites s ON s.id = x.site_id
LEFT JOIN devices rd ON rd.id = x.related_device_id
"""


def transactions(
    db: sqlite3.Connection,
    limit: int = 100,
    offset: int = 0,
    site_id: Optional[int] = None,
    person_id: Optional[int] = None,
    device_id: Optional[int] = None,
    action: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if site_id is not None:
        clauses.append("x.site_id = ?"); params.append(site_id)
    if person_id is not None:
        clauses.append("x.person_id = ?"); params.append(person_id)
    if device_id is not None:
        clauses.append("(x.device_id = ? OR x.related_device_id = ?)"); params += [device_id, device_id]
    if action:
        clauses.append("x.action = ?"); params.append(action)
    if q:
        like = f"%{q}%"
        clauses.append("(d.si LIKE ? OR d.label LIKE ? OR p.name LIKE ? OR x.note LIKE ? OR x.reason LIKE ?)")
        params += [like] * 5
    if date_from:
        clauses.append("x.ts >= ?"); params.append(date_from)
    if date_to:
        clauses.append("x.ts <= ?"); params.append(date_to + " 23:59:59")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return db.execute(
        TX_SELECT + f" {where} ORDER BY x.ts DESC, x.id DESC LIMIT ? OFFSET ?", params + [limit, offset]
    ).fetchall()
