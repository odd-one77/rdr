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
    """Barcodes come as 'SI-BOL-1001', 'si99000014' or '@si99000014' (easyjob labels); store and match without the '@'."""
    return (si or "").strip().lstrip("@").upper()


def norm_phone(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+")


# -- lookups ------------------------------------------------------------------

def device_by_si(db: sqlite3.Connection, si: str) -> Optional[sqlite3.Row]:
    return db.execute(DEVICE_SELECT + " WHERE d.si = ? COLLATE NOCASE", (norm_si(si),)).fetchone()


def search_devices(db: sqlite3.Connection, q: str, limit: int = 12) -> list[sqlite3.Row]:
    """Partial SI / label search for the kiosk: 'SI ends with q' ranks first, then contains."""
    q = norm_si(q)
    if not q:
        return []
    like = f"%{q}%"
    return db.execute(
        DEVICE_SELECT + """ WHERE d.si LIKE ? OR d.label LIKE ? COLLATE NOCASE
        ORDER BY CASE WHEN d.si = ? THEN 0 WHEN d.si LIKE ? THEN 1 WHEN d.si LIKE ? THEN 2 ELSE 3 END, t.sort_order, d.label, d.si
        LIMIT ?""",
        (like, like, q, f"%{q}", f"{q}%", limit),
    ).fetchall()


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
               (SELECT COALESCE(NULLIF(d.label,''), t.name) FROM devices d JOIN device_types t ON t.id = d.type_id
                 WHERE d.holder_id = p.id AND d.status = 'out' AND t.name LIKE '%radio%' ORDER BY d.label LIMIT 1) AS radio_label,
               (SELECT COALESCE(NULLIF(d.linked_id,''), NULLIF(d.label,''), 'Bolero') FROM devices d JOIN device_types t ON t.id = d.type_id
                 WHERE d.holder_id = p.id AND d.status = 'out' AND t.name LIKE '%bolero%' AND t.name NOT LIKE '%antenna%' LIMIT 1) AS held_bolero,
               (SELECT COUNT(*) FROM assignments a JOIN devices d ON d.id = a.device_id
                 WHERE a.person_id = p.id AND (d.holder_id IS NULL OR d.holder_id <> p.id)) AS assigned_count
        FROM people p
        WHERE p.name LIKE ? COLLATE NOCASE
           OR p.external_id LIKE ? COLLATE NOCASE
           OR p.dept LIKE ? COLLATE NOCASE
           OR p.role LIKE ? COLLATE NOCASE
           OR p.bolero_id LIKE ? COLLATE NOCASE
           OR (? IS NOT NULL AND REPLACE(REPLACE(p.phone,' ',''),'-','') LIKE ?)
        ORDER BY CASE WHEN p.name LIKE ? COLLATE NOCASE THEN 0 WHEN p.bolero_id = ? COLLATE NOCASE THEN 0 WHEN p.role LIKE ? COLLATE NOCASE THEN 1 ELSE 2 END, p.name
        LIMIT ?
        """,
        (like, like, like, like, like, phone_like, phone_like, f"{q}%", q, f"{q}%", limit),
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


def is_bolero_pack(type_name: str) -> bool:
    t = (type_name or "").lower()
    return "bolero" in t and "antenna" not in t


def id_from_label(label: str) -> str:
    """'Bolero 61' -> '61': a pack labelled with a number is configured as that ID until told otherwise."""
    import re
    m = re.search(r"(\d+)", label or "")
    return m.group(1) if m else ""


def default_linked_id(db: sqlite3.Connection, type_id: int, label: str, linked_id: str) -> str:
    if linked_id:
        return linked_id.strip()
    t = db.execute("SELECT name FROM device_types WHERE id = ?", (type_id,)).fetchone()
    return id_from_label(label) if t and is_bolero_pack(t["name"]) else ""


def link_reserved_packs(db: sqlite3.Connection, person_id: int) -> None:
    """A Bolero pack reserved for a person with an ID is soft-linked to that ID straight away."""
    person = person_by_id(db, person_id)
    if not person or not person["bolero_id"]:
        return
    rows = db.execute(
        "SELECT d.id, d.linked_id, t.name AS type_name FROM assignments a JOIN devices d ON d.id = a.device_id JOIN device_types t ON t.id = d.type_id WHERE a.person_id = ?",
        (person_id,),
    ).fetchall()
    for r in rows:
        if is_bolero_pack(r["type_name"]) and not r["linked_id"]:
            db.execute("UPDATE devices SET linked_id = ? WHERE id = ?", (person["bolero_id"], r["id"]))


def _set_device(db: sqlite3.Connection, device_id: int, status: str, holder_id: Optional[int]) -> None:
    db.execute(
        "UPDATE devices SET status = ?, holder_id = ?, updated_at = ? WHERE id = ?",
        (status, holder_id, now_iso(), device_id),
    )


def _link_bolero(db: sqlite3.Connection, device_id: int, person_id: int) -> None:
    """Soft-link a Bolero pack and a person on sign out.

    * person has an ID  -> the pack is now configured as that ID (linked_id)
    * person has no ID but the pack has one (imported / previous) -> the person adopts the pack's ID
    The ID stays on the pack when it comes back; a later scan to a different person re-links it.
    """
    row = db.execute(
        "SELECT d.linked_id, t.name AS type_name, p.bolero_id FROM devices d JOIN device_types t ON t.id = d.type_id, people p "
        "WHERE d.id = ? AND p.id = ?",
        (device_id, person_id),
    ).fetchone()
    if not row or not is_bolero_pack(row["type_name"]):
        return
    if row["bolero_id"]:
        db.execute("UPDATE devices SET linked_id = ? WHERE id = ?", (row["bolero_id"], device_id))
    elif row["linked_id"]:
        db.execute("UPDATE people SET bolero_id = ?, updated_at = ? WHERE id = ?", (row["linked_id"], now_iso(), person_id))


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
        "linked_id": d["linked_id"],
    }


# -- operations ---------------------------------------------------------------

def sign_out(
    db: sqlite3.Connection,
    device_ids: list[int],
    person_id: int,
    tech: Optional[dict],
    force_ids: Optional[set[int]] = None,
    collector_id: Optional[int] = None,
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
    via = ""
    if collector_id and collector_id != person_id:
        c = person_by_id(db, collector_id)
        if c is None:
            raise ValueError("Unknown collector")
        via = f"Collected by {c['name']}"
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
            _link_bolero(db, did, person_id)
            r.update(outcome="issued", message=f"Signed out to {person['name']}", ts=log(db, "out", did, person_id, tech, note=via))
        elif d["status"] == "out" and d["holder_id"] == person_id:
            r.update(outcome="already_held", message="Already signed out to this person", ts=log(db, "out_noop", did, person_id, tech))
        elif d["status"] == "out":
            if did in force_ids:
                old = d["holder_id"]
                log(db, "in", did, old, tech, note=f"Reassigned to {person['name']}")
                _set_device(db, did, "out", person_id)
                _link_bolero(db, did, person_id)
                r.update(outcome="reassigned", message=f"Reassigned from {d['holder_name']}", ts=log(db, "out_reassign", did, person_id, tech, note=(f"Was out to {d['holder_name']}" + (f". {via}" if via else ""))))
            else:
                r.update(outcome="conflict", message=f"Already OUT to {d['holder_name']}")
        else:  # lost / damaged
            if did in force_ids:
                log(db, "restored", did, None, tech, note="Restored on sign out")
                _set_device(db, did, "out", person_id)
                _link_bolero(db, did, person_id)
                r.update(outcome="reissued", message=f"Was {d['status']}, restored and signed out", ts=log(db, "out", did, person_id, tech, note=via))
            else:
                r.update(outcome="conflict", message=f"Device is marked {d['status'].upper()}")
        if r.get("outcome") in ("issued", "reassigned", "reissued"):
            r["linked_id"] = device_by_id(db, did)["linked_id"]
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
        raise ValueError("The device being swapped out is not signed out to anyone")
    if new["status"] == "out":
        raise ValueError(f"Replacement {new['si']} is already out to {new['holder_name']}")
    if old_status not in ("damaged", "lost", "in"):
        raise ValueError("Bad status for the returned device")
    person_id = old["holder_id"]
    _set_device(db, old_device_id, old_status, None)
    log(db, "swap_out", old_device_id, person_id, tech, reason, note, related_device_id=new_device_id)
    if old_status != "in":
        log(db, old_status, old_device_id, person_id, tech, reason, note)
    if new["status"] in ("lost", "damaged"):
        log(db, "restored", new_device_id, None, tech, note="Restored on swap")
    _set_device(db, new_device_id, "out", person_id)
    if is_bolero_pack(old["type_name"]) and is_bolero_pack(new["type_name"]):
        # The IDs trade places: the spare becomes the person's ID, the returned pack takes the spare's old ID.
        person = person_by_id(db, person_id)
        keep = (person["bolero_id"] if person and person["bolero_id"] else old["linked_id"]) or ""
        db.execute("UPDATE devices SET linked_id = ? WHERE id = ?", (new["linked_id"] or "", old_device_id))
        db.execute("UPDATE devices SET linked_id = ? WHERE id = ?", (keep, new_device_id))
        if keep:
            note = (note + " " if note else "") + f"Bolero ID {keep} moved to {new['label'] or new['si']}"
    else:
        _link_bolero(db, new_device_id, person_id)
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
    linked_id: str = "",
) -> sqlite3.Row:
    ts = now_iso()
    cur = db.execute(
        """INSERT INTO devices (si, type_id, label, site_id, status, notes, linked_id, created_at, updated_at)
           VALUES (?,?,?,?, 'in', ?, ?, ?, ?)""",
        (norm_si(si), type_id, (label or "").strip(), site_id, notes, default_linked_id(db, type_id, label, linked_id or ""), ts, ts),
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
    faceplate: str = "",
    allocation: str = "",
) -> sqlite3.Row:
    ts = now_iso()
    cur = db.execute(
        """INSERT INTO people (external_id, name, dept, role, phone, bolero_id, faceplate, allocation, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ((external_id or "").strip() or None, name.strip(), dept.strip(), role.strip(), phone.strip(), (bolero_id or "").strip(),
         (faceplate or "").strip().lstrip("#").upper(), (allocation or "").strip(), notes, ts, ts),
    )
    return person_by_id(db, cur.lastrowid)


def update_person(db: sqlite3.Connection, person_id: int, **fields) -> sqlite3.Row:
    allowed = {"name", "dept", "role", "phone", "external_id", "bolero_id", "notes", "faceplate", "allocation"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            v = v.strip()
            if k == "external_id":
                v = v or None
            sets.append(f"{k} = ?"); vals.append(v)
    if sets:
        sets.append("updated_at = ?"); vals.append(now_iso())
        db.execute(f"UPDATE people SET {', '.join(sets)} WHERE id = ?", vals + [person_id])
    if fields.get("bolero_id"):
        link_reserved_packs(db, person_id)
    return person_by_id(db, person_id)


def set_assignment(db: sqlite3.Connection, device_id: int, person_id: int, tech: Optional[dict], note: str = "") -> None:
    db.execute(
        """INSERT INTO assignments (device_id, person_id, note, created_at) VALUES (?,?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET person_id = excluded.person_id, note = excluded.note, created_at = excluded.created_at""",
        (device_id, person_id, note, now_iso()),
    )
    log(db, "assigned", device_id, person_id, tech, note=note)
    link_reserved_packs(db, person_id)


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
