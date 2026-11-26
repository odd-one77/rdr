"""Three-way merge of two copies of an event database (a site node's and the hub's).

Both copies started from the same agreed copy, the *base*. Rows are matched by their uid, which is the
same in every copy (integer ids are not). For every row and field:

* changed on one side only: that side's value is taken;
* changed on both sides to the same value: fine;
* changed on both sides differently: a conflict. The node's value wins, because the node is where
  devices are signed in and out, and the conflict is listed so it can be checked;
* added on one side: added;
* deleted on one side and untouched on the other: deleted. Deleted on one side but changed on the
  other: kept, and listed;
* a row that something still points at (a person with a sign-out, a site with devices) is never
  deleted.

Two new rows that are the same thing (a device with the same SI added on both sides, a site with the
same name) become one row; the hub's uid is kept.

The merge never writes the base. ``plan`` works out the result and a summary; ``apply`` writes it into
the node's live database as row by row changes, so the ids the node already uses (a tech's session, the
kiosk's state) stay valid.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..db import SYNCED_TABLES, is_local_setting

# column -> table it points at
FKS: dict[str, dict[str, str]] = {
    "sites": {},
    "device_types": {},
    "techs": {"site_id": "sites"},
    "people": {},
    "devices": {"type_id": "device_types", "site_id": "sites", "holder_id": "people"},
    "assignments": {"device_id": "devices", "person_id": "people"},
    "transactions": {"device_id": "devices", "person_id": "people", "tech_id": "techs", "site_id": "sites", "related_device_id": "devices"},
}
# what makes two rows the same thing even with different uids (UNIQUE in the schema)
NATURAL: dict[str, tuple[str, ...]] = {
    "sites": ("name",), "device_types": ("name",), "techs": ("username",), "devices": ("si",),
    "people": ("external_id",), "assignments": ("device_id",),
}
LABEL = {"sites": "Sites", "device_types": "Device types", "techs": "Techs", "people": "People", "devices": "Devices",
         "assignments": "Reservations", "transactions": "History"}


def _norm(table: str, col: str, v):
    if v is None:
        return None
    if table in ("sites", "device_types", "techs") and col in ("name", "username") and isinstance(v, str):
        return v.strip().lower()
    return v


def load(db: sqlite3.Connection) -> dict[str, dict[str, dict]]:
    """Every synced table as {uid: row}, with references written as the uid of the row they point at."""
    out: dict[str, dict[str, dict]] = {}
    uid_of: dict[str, dict[int, str]] = {}
    for t in SYNCED_TABLES:
        rows = db.execute(f"SELECT * FROM {t}").fetchall()
        uid_of[t] = {r["id"]: r["uid"] for r in rows}
        table = {}
        for r in rows:
            d = {k: r[k] for k in r.keys() if k != "id"}
            for col, ref in FKS[t].items():
                d[col] = uid_of[ref].get(d[col]) if d.get(col) is not None else None
            table[d["uid"]] = d
        out[t] = table
    out["_event"] = {"1": {k: v for k, v in dict(db.execute("SELECT * FROM event WHERE id = 1").fetchone() or {}).items() if k != "id"}}
    out["_settings"] = {r["key"]: {"value": r["value"]} for r in db.execute("SELECT key, value FROM settings") if not is_local_setting(r["key"])}
    return out


def _merge_row(base: Optional[dict], node: Optional[dict], hub: Optional[dict], table: str, key: str, conflicts: list) -> Optional[dict]:
    if base is None:   # new on one side (both at once cannot share a uid)
        return dict(node or hub)
    if node is None and hub is None:
        return None
    if node is None or hub is None:   # deleted on one side
        other, gone_on = (hub, "node") if node is None else (node, "hub")
        if other == base:
            return None
        conflicts.append({"table": table, "key": key, "field": "", "kind": "deleted",
                          "detail": f"deleted on the {gone_on}, changed on the {'hub' if gone_on == 'node' else 'node'}: kept"})
        return dict(other)
    out = {}
    for f in set(base) | set(node) | set(hub):
        b, n, h = base.get(f), node.get(f), hub.get(f)
        if n == b:
            out[f] = h
        elif h == b or n == h:
            out[f] = n
        else:
            out[f] = n
            if f not in ("updated_at", "last_event_at"):
                conflicts.append({"table": table, "key": key, "field": f, "kind": "both", "node": n, "hub": h})
    return out


def _describe(table: str, row: dict) -> str:
    for f in ("name", "si", "username", "display_name", "label", "action", "value"):
        if row.get(f):
            return str(row[f])
    return row.get("uid", "")[:8]


def plan(base: dict, node: dict, hub: dict) -> dict:
    """Work out the merged copy. Returns {"result": tables, "summary": {...}, "conflicts": [...]}."""
    result: dict[str, dict[str, dict]] = {}
    conflicts: list[dict] = []
    alias: dict[str, str] = {}   # node uid -> hub uid for rows that are the same thing
    summary = {t: {"from_node": 0, "from_hub": 0, "deleted": 0} for t in list(SYNCED_TABLES) + ["_event", "_settings"]}

    def remap(row: Optional[dict], t: str) -> Optional[dict]:
        if row is None:
            return None
        r = dict(row)
        for col in FKS.get(t, {}):
            if r.get(col) in alias:
                r[col] = alias[r[col]]
        return r

    for t in list(SYNCED_TABLES) + ["_event", "_settings"]:
        b_t = {k: remap(v, t) for k, v in base.get(t, {}).items()}
        n_t = {alias.get(k, k): remap(v, t) for k, v in node.get(t, {}).items()}
        h_t = {k: remap(v, t) for k, v in hub.get(t, {}).items()}
        out: dict[str, dict] = {}
        for k in set(b_t) | set(n_t) | set(h_t):
            b, n, h = b_t.get(k), n_t.get(k), h_t.get(k)
            label = f"{LABEL.get(t, t)}: {_describe(t, n or h or b)}" if t != "_settings" else f"Setting {k}"
            r = _merge_row(b, n, h, t, label, conflicts)
            if r is not None:
                if "uid" in r:
                    r["uid"] = k
                out[k] = r
                if n != b and r == n and n is not None:
                    summary[t]["from_node"] += 1
                elif h != b and h is not None:
                    summary[t]["from_hub"] += 1
            elif b is not None:
                summary[t]["deleted"] += 1
        # the same thing added under two uids: keep the hub's
        for cols in [NATURAL.get(t)] if NATURAL.get(t) else []:
            seen: dict[tuple, str] = {}
            for k in sorted(out, key=lambda k: (k not in h_t, k)):   # hub rows first
                nk = tuple(_norm(t, c, out[k].get(c)) for c in cols)
                if any(v in (None, "") for v in nk):
                    continue
                if nk in seen:
                    keep = seen[nk]
                    merged = dict(out[keep])
                    for f, v in out[k].items():
                        if f != "uid" and v not in (None, "") and k in n_t and k not in b_t:
                            merged[f] = v
                    out[keep] = merged
                    alias[k] = keep
                    del out[k]
                    conflicts.append({"table": t, "key": f"{LABEL.get(t, t)}: {_describe(t, merged)}", "field": "", "kind": "same",
                                      "detail": "added on both sides: kept as one"})
                else:
                    seen[nk] = k
            if alias:   # references inside this table to a row that was folded
                for r in out.values():
                    for col in FKS.get(t, {}):
                        if r.get(col) in alias:
                            r[col] = alias[r[col]]
        result[t] = out

    # nothing that is still pointed at is deleted
    pools = {"node": node, "hub": hub, "base": base}
    changed = True
    while changed:
        changed = False
        for t in SYNCED_TABLES:
            for r in result[t].values():
                for col, ref in FKS[t].items():
                    u = r.get(col)
                    if u and u not in result[ref]:
                        for src in ("node", "hub", "base"):
                            row = pools[src].get(ref, {}).get(u)
                            if row is not None:
                                result[ref][u] = dict(row)
                                summary[ref]["deleted"] = max(0, summary[ref]["deleted"] - 1)
                                conflicts.append({"table": ref, "key": f"{LABEL[ref]}: {_describe(ref, row)}", "field": "", "kind": "kept",
                                                  "detail": "deleted on one side but still in use: kept"})
                                changed = True
                                break
                        else:
                            r[col] = None
    return {"result": result, "summary": summary, "conflicts": conflicts}


def apply(db: sqlite3.Connection, result: dict) -> dict:
    """Write the merged copy into the node's live database, keeping the ids it already uses."""
    counts = {"inserted": 0, "updated": 0, "deleted": 0}
    db.execute("PRAGMA foreign_keys = OFF")        # takes effect only outside a transaction
    db.execute("PRAGMA defer_foreign_keys = ON")   # inside one: references are checked at commit
    try:
        local = {t: {r["uid"]: r["id"] for r in db.execute(f"SELECT id, uid FROM {t}")} for t in SYNCED_TABLES}
        for t in reversed(SYNCED_TABLES):
            gone = [i for u, i in local[t].items() if u not in result[t]]
            for i in gone:
                db.execute(f"DELETE FROM {t} WHERE id = ?", (i,))
            counts["deleted"] += len(gone)
            local[t] = {u: i for u, i in local[t].items() if u in result[t]}
        # rows whose unique name moves to another row would collide while updating one at a time:
        # park the unique columns first, then write the real values
        for t, cols in NATURAL.items():
            if t == "assignments":
                continue
            for c in cols:
                db.execute(f"UPDATE {t} SET {c} = '~' || uid || '~' || {c} WHERE {c} IS NOT NULL AND {c} <> ''")
        db.execute("DELETE FROM assignments")   # small, and unique per device: written back below
        local["assignments"] = {}
        for t in SYNCED_TABLES:
            cols = [c[1] for c in db.execute(f"PRAGMA table_info({t})") if c[1] != "id"]
            for u, row in result[t].items():
                vals = dict(row)
                for col, ref in FKS[t].items():
                    vals[col] = local[ref].get(vals.get(col)) if vals.get(col) else None
                vals = {c: vals.get(c) for c in cols if c in vals}
                if u in local[t]:
                    db.execute(f"UPDATE {t} SET {', '.join(f'{c} = ?' for c in vals)} WHERE id = ?", (*vals.values(), local[t][u]))
                    counts["updated"] += 1
                else:
                    cur = db.execute(f"INSERT INTO {t} ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))
                    local[t][u] = cur.lastrowid
                    counts["inserted"] += 1
        ev = result["_event"].get("1")
        if ev:
            cols = [c[1] for c in db.execute("PRAGMA table_info(event)") if c[1] != "id"]
            vals = {c: ev.get(c) for c in cols if c in ev}
            db.execute(f"UPDATE event SET {', '.join(f'{c} = ?' for c in vals)} WHERE id = 1", tuple(vals.values()))
        keep = {r["key"] for r in db.execute("SELECT key FROM settings") if not is_local_setting(r["key"])}
        for k in keep - set(result["_settings"]):
            db.execute("DELETE FROM settings WHERE key = ?", (k,))
        for k, v in result["_settings"].items():
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v["value"]))
        bad = db.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise ValueError(f"The merged copy has {len(bad)} broken references; nothing was changed")
    finally:
        db.execute("PRAGMA foreign_keys = ON")
    return counts


def summary_lines(p: dict) -> list[str]:
    """Short lines for the merge prompt."""
    out = []
    for t, s in p["summary"].items():
        name = {"_event": "Event details", "_settings": "Settings"}.get(t, LABEL.get(t, t))
        bits = [f"{s['from_node']} from this node" if s["from_node"] else "", f"{s['from_hub']} from the hub" if s["from_hub"] else "",
                f"{s['deleted']} removed" if s["deleted"] else ""]
        bits = [b for b in bits if b]
        if bits:
            out.append(f"{name}: {', '.join(bits)}")
    return out


def fmt(v: Any) -> str:
    return "empty" if v in (None, "") else str(v)
