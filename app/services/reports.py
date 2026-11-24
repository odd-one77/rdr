"""Read-only report queries for the admin Reports page and CSV exports."""
from __future__ import annotations

import sqlite3

from .inventory import DEVICE_SELECT


def stock_by_site(db: sqlite3.Connection) -> list[dict]:
    """One row per (site, type): own stock + shared stock counts, in/out/lost/damaged."""
    sites = db.execute("SELECT * FROM sites WHERE active = 1 ORDER BY sort_order, name").fetchall()
    out = []
    for s in sites:
        rows = db.execute(
            """
            SELECT t.name AS type_name,
                   SUM(CASE WHEN d.site_id = ? THEN 1 ELSE 0 END) AS own_total,
                   SUM(CASE WHEN d.site_id = ? AND d.status = 'in' THEN 1 ELSE 0 END) AS own_in,
                   SUM(CASE WHEN d.site_id IS NULL THEN 1 ELSE 0 END) AS shared_total,
                   SUM(CASE WHEN d.site_id IS NULL AND d.status = 'in' THEN 1 ELSE 0 END) AS shared_in,
                   SUM(CASE WHEN (d.site_id = ? OR d.site_id IS NULL) AND d.status = 'out' THEN 1 ELSE 0 END) AS n_out,
                   SUM(CASE WHEN (d.site_id = ? OR d.site_id IS NULL) AND d.status IN ('lost','damaged') THEN 1 ELSE 0 END) AS n_bad,
                   SUM(CASE WHEN x.site_id = ? THEN 1 ELSE 0 END) AS issued_here
            FROM device_types t
            JOIN devices d ON d.type_id = t.id
            LEFT JOIN (SELECT device_id, site_id FROM transactions WHERE action IN ('out','out_reassign','swap_in')
                         AND id IN (SELECT MAX(id) FROM transactions WHERE action IN ('out','out_reassign','swap_in') GROUP BY device_id)) x
                   ON x.device_id = d.id AND d.status = 'out'
            GROUP BY t.id HAVING own_total + shared_total > 0 ORDER BY t.sort_order, t.name
            """,
            (s["id"], s["id"], s["id"], s["id"], s["id"]),
        ).fetchall()
        out.append({"site": dict(s), "types": [dict(r) for r in rows]})
    return out


def outstanding(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every device currently out, lost or damaged with its holder, grouped by dept then person."""
    return db.execute(
        DEVICE_SELECT + " WHERE d.status <> 'in' ORDER BY COALESCE(p.dept,''), COALESCE(p.name,''), t.sort_order, d.label, d.si"
    ).fetchall()


def issued_by_site(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Where each outstanding device was last issued (which site / tech)."""
    return db.execute(
        """
        SELECT d.id, d.si, d.label, t.name AS type_name, d.status, p.name AS holder_name, p.dept AS holder_dept,
               x.ts AS issued_at, s.name AS issued_site, tc.display_name AS issued_by
        FROM devices d
        JOIN device_types t ON t.id = d.type_id
        LEFT JOIN people p ON p.id = d.holder_id
        LEFT JOIN transactions x ON x.id = (SELECT MAX(id) FROM transactions WHERE device_id = d.id AND action IN ('out','out_reassign','swap_in'))
        LEFT JOIN sites s ON s.id = x.site_id
        LEFT JOIN techs tc ON tc.id = x.tech_id
        WHERE d.status = 'out'
        ORDER BY s.name, x.ts
        """
    ).fetchall()


def dept_summary(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT COALESCE(NULLIF(p.dept,''), '(no dept)') AS dept, COUNT(DISTINCT p.id) AS people, COUNT(d.id) AS items,
               GROUP_CONCAT(DISTINCT t.name) AS types
        FROM devices d JOIN people p ON p.id = d.holder_id JOIN device_types t ON t.id = d.type_id
        WHERE d.status = 'out'
        GROUP BY dept ORDER BY items DESC, dept
        """
    ).fetchall()


def activity_by_site(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT s.name AS site, tc.display_name AS tech,
               SUM(CASE WHEN x.action IN ('out','out_reassign','swap_in') THEN 1 ELSE 0 END) AS outs,
               SUM(CASE WHEN x.action IN ('in','swap_out','restored') THEN 1 ELSE 0 END) AS ins,
               SUM(CASE WHEN x.action IN ('lost','damaged') THEN 1 ELSE 0 END) AS flagged,
               MIN(x.ts) AS first_ts, MAX(x.ts) AS last_ts
        FROM transactions x LEFT JOIN sites s ON s.id = x.site_id LEFT JOIN techs tc ON tc.id = x.tech_id
        WHERE x.action IN ('out','out_reassign','swap_in','in','swap_out','restored','lost','damaged')
        GROUP BY x.site_id, x.tech_id ORDER BY s.name, outs DESC
        """
    ).fetchall()


def end_of_event(db: sqlite3.Connection, statuses: tuple[str, ...] = ("out", "lost", "damaged")) -> list[dict]:
    """Who still has what: one entry per person holding devices, with contact details,
    when they were last seen at a counter, and when/where each device was issued."""
    rows = db.execute(
        """
        SELECT p.id AS person_id, p.name, p.dept, p.role, p.phone, p.bolero_id,
               (SELECT MAX(ts) FROM transactions x WHERE x.person_id = p.id) AS last_seen,
               d.id AS device_id, d.si, d.label, d.status, t.name AS type_name,
               x.ts AS issued_at, s.name AS issued_site, tc.display_name AS issued_by
        FROM devices d
        JOIN people p ON p.id = d.holder_id
        JOIN device_types t ON t.id = d.type_id
        LEFT JOIN transactions x ON x.id = (SELECT MAX(id) FROM transactions WHERE device_id = d.id AND action IN ('out','out_reassign','swap_in'))
        LEFT JOIN sites s ON s.id = x.site_id
        LEFT JOIN techs tc ON tc.id = x.tech_id
        WHERE d.status IN ({})
        ORDER BY COALESCE(NULLIF(p.dept,''), 'zzz'), p.name, t.sort_order, d.label
        """.format(",".join("?" * len(statuses))),
        statuses,
    ).fetchall()
    people: dict[int, dict] = {}
    for r in rows:
        entry = people.setdefault(r["person_id"], {
            "person_id": r["person_id"], "name": r["name"], "dept": r["dept"], "role": r["role"], "phone": r["phone"],
            "bolero_id": r["bolero_id"], "last_seen": r["last_seen"], "items": [],
        })
        entry["items"].append({k: r[k] for k in ("device_id", "si", "label", "status", "type_name", "issued_at", "issued_site", "issued_by")})
    return list(people.values())


def flagged(db: sqlite3.Connection, status: str) -> list[dict]:
    """Devices currently damaged or lost, with the reason and note from when they were flagged,
    who last had them and which tech and site reported it."""
    rows = db.execute(
        """
        SELECT d.id AS device_id, d.si, d.label, d.status, d.linked_id, t.name AS type_name, hs.name AS home_site,
               x.ts AS flagged_at, x.reason, x.note, s.name AS flagged_site, tc.display_name AS flagged_by,
               COALESCE(p.id, xp.id) AS person_id, COALESCE(p.name, xp.name) AS person_name,
               COALESCE(p.dept, xp.dept) AS person_dept, COALESCE(p.phone, xp.phone) AS person_phone,
               (d.holder_id IS NOT NULL) AS still_held
        FROM devices d
        JOIN device_types t ON t.id = d.type_id
        LEFT JOIN sites hs ON hs.id = d.site_id
        LEFT JOIN people p ON p.id = d.holder_id
        LEFT JOIN transactions x ON x.id = (SELECT MAX(id) FROM transactions WHERE device_id = d.id AND action = d.status)
        LEFT JOIN people xp ON xp.id = x.person_id
        LEFT JOIN sites s ON s.id = x.site_id
        LEFT JOIN techs tc ON tc.id = x.tech_id
        WHERE d.status = ?
        ORDER BY t.sort_order, t.name, d.label, d.si
        """,
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


def by_type(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Counts of devices not in stock, per type, for the end of show summary."""
    return db.execute(
        """
        SELECT t.name AS type_name, COUNT(d.id) AS total,
               SUM(CASE WHEN d.status = 'out' THEN 1 ELSE 0 END) AS n_out,
               SUM(CASE WHEN d.status = 'damaged' THEN 1 ELSE 0 END) AS n_damaged,
               SUM(CASE WHEN d.status = 'lost' THEN 1 ELSE 0 END) AS n_lost
        FROM device_types t JOIN devices d ON d.type_id = t.id
        GROUP BY t.id HAVING n_out + n_damaged + n_lost > 0 ORDER BY t.sort_order, t.name
        """
    ).fetchall()


def end_of_show(db: sqlite3.Connection) -> dict:
    """Everything the end of show handover needs: who still has what, and what came back damaged or went missing."""
    still_out = end_of_event(db, statuses=("out",))
    return {
        "still_out": still_out,
        "n_out": sum(len(p["items"]) for p in still_out),
        "damaged": flagged(db, "damaged"),
        "lost": flagged(db, "lost"),
        "by_type": by_type(db),
    }
