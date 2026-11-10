"""Spreadsheet import with column mapping.

Flow (all driven from the admin UI):
  1. ``stage_upload``  - parse an .xlsx / .csv into headers + rows and store it
                         as JSON under data/imports/<token>.json
  2. ``guess_mapping`` - propose which column feeds which field
  3. ``analyze``       - with a mapping, work out per row whether it is a
                         create, an update, or a duplicate that needs a decision
  4. ``commit``        - apply the plan plus the user's decisions
"""
from __future__ import annotations

import csv
import io
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook

from ..config import IMPORT_DIR, ensure_dirs
from ..db import now_iso
from .inventory import log, norm_phone, norm_si

KINDS = {
    "people": {
        "title": "People",
        "fields": [
            ("external_id", "Unique ID / badge (optional)"),
            ("name", "Full name"),
            ("first_name", "First name (if split)"),
            ("last_name", "Last name (if split)"),
            ("dept", "Department"),
            ("role", "Role"),
            ("phone", "Phone"),
            ("bolero_id", "Bolero / comms ID"),
            ("kit", "Kit plan (types, comma separated, e.g. Bolero, Pro-D2)"),
            ("notes", "Notes"),
        ],
        "required": ["name|first_name"],
    },
    "plans": {
        "title": "Kit plans (type per person)",
        "fields": [
            ("person_external_id", "Person unique ID"),
            ("person_name", "Person name"),
            ("person_phone", "Person phone"),
            ("type", "Device type"),
            ("qty", "Quantity (default 1)"),
            ("note", "Note"),
        ],
        "required": ["type", "person_external_id|person_name|person_phone"],
    },
    "devices": {
        "title": "Devices (easyjob export)",
        "fields": [
            ("si", "SI number / barcode"),
            ("type", "Device type"),
            ("label", "Label / display name (e.g. Bolero 61)"),
            ("site", "Site (optional)"),
            ("notes", "Notes"),
        ],
        "required": ["si", "type"],
    },
    "assignments": {
        "title": "Specific device pre-assignments (by SI)",
        "fields": [
            ("si", "Device SI number"),
            ("person_external_id", "Person unique ID"),
            ("person_name", "Person name"),
            ("person_phone", "Person phone"),
            ("note", "Note"),
        ],
        "required": ["si", "person_external_id|person_name|person_phone"],
    },
}

# header keywords -> field, checked in order (lowercased, punctuation stripped)
GUESSES = {
    "people": [
        (("badge", "staff id", "staffid", "employee", "crew id", "id number", "unique"), "external_id"),
        (("full name", "fullname", "name"), "name"),
        (("first", "given", "forename"), "first_name"),
        (("last", "surname", "family"), "last_name"),
        (("dept", "department", "team", "company"), "dept"),
        (("role", "position", "job", "title", "function"), "role"),
        (("phone", "mobile", "cell", "tel", "contact"), "phone"),
        (("bolero", "beltpack", "comms id", "pack"), "bolero_id"),
        (("kit", "headset", "equipment", "needs", "requires", "allocation"), "kit"),
        (("note", "comment", "remark"), "notes"),
    ],
    "plans": [
        (("badge", "staff id", "employee", "unique", "person id"), "person_external_id"),
        (("name", "person", "user"), "person_name"),
        (("phone", "mobile", "tel"), "person_phone"),
        (("type", "item", "device", "kit", "equipment"), "type"),
        (("qty", "quantity", "count", "number"), "qty"),
        (("note", "comment", "remark"), "note"),
    ],
    "devices": [
        (("si", "serial", "barcode", "asset", "inventory no", "inventar"), "si"),
        (("type", "category", "group", "article", "item", "description", "product"), "type"),
        (("label", "display", "name", "number", "no.", "id"), "label"),
        (("site", "zone", "location", "rdr"), "site"),
        (("note", "comment", "remark"), "notes"),
    ],
    "assignments": [
        (("si", "serial", "barcode", "asset"), "si"),
        (("badge", "staff id", "employee", "unique", "person id"), "person_external_id"),
        (("name", "person", "user", "assigned to"), "person_name"),
        (("phone", "mobile", "tel"), "person_phone"),
        (("note", "comment", "remark"), "note"),
    ],
}


def _clean(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def parse_file(filename: str, content: bytes) -> tuple[list[str], list[list[str]], list[str]]:
    """Return (headers, rows, sheet_names). Uses the first non-empty row as the header."""
    name = filename.lower()
    sheets: list[str] = []
    if name.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheets = wb.sheetnames
        ws = wb[sheets[0]]
        raw = [[_clean(c) for c in row] for row in ws.iter_rows(values_only=True)]
    elif name.endswith((".csv", ".txt", ".tsv")):
        text = content.decode("utf-8-sig", errors="replace")
        dialect = csv.excel_tab if name.endswith(".tsv") else csv.excel
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            pass
        raw = [[_clean(c) for c in row] for row in csv.reader(io.StringIO(text), dialect)]
    else:
        raise ValueError("Unsupported file type. Please upload .xlsx or .csv")

    raw = [r for r in raw if any(c for c in r)]
    if not raw:
        raise ValueError("The file has no data rows")
    headers = raw[0]
    width = max(len(r) for r in raw)
    headers = [h or f"Column {i + 1}" for i, h in enumerate(headers + [""] * (width - len(headers)))]
    rows = [r + [""] * (width - len(r)) for r in raw[1:]]
    return headers, rows, sheets


def stage_upload(kind: str, filename: str, content: bytes) -> dict:
    if kind not in KINDS:
        raise ValueError("Unknown import kind")
    headers, rows, sheets = parse_file(filename, content)
    ensure_dirs()
    token = secrets.token_urlsafe(10)
    staged = {
        "token": token,
        "kind": kind,
        "filename": filename,
        "headers": headers,
        "rows": rows,
        "sheets": sheets,
        "created_at": now_iso(),
        "mapping": guess_mapping(kind, headers),
        "plan": None,
    }
    _path(token).write_text(json.dumps(staged), "utf-8")
    return staged


def _path(token: str) -> Path:
    if not token.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Bad token")
    return IMPORT_DIR / f"{token}.json"


def load_staged(token: str) -> dict:
    p = _path(token)
    if not p.exists():
        raise FileNotFoundError("Import session expired or not found")
    return json.loads(p.read_text("utf-8"))


def save_staged(staged: dict) -> None:
    _path(staged["token"]).write_text(json.dumps(staged), "utf-8")


def discard(token: str) -> None:
    p = _path(token)
    if p.exists():
        p.unlink()


def guess_mapping(kind: str, headers: list[str]) -> dict[str, Optional[int]]:
    mapping: dict[str, Optional[int]] = {f: None for f, _ in KINDS[kind]["fields"]}
    for idx, h in enumerate(headers):
        hl = h.lower().replace("_", " ").replace("-", " ")
        for keywords, field in GUESSES[kind]:
            if mapping[field] is None and any(k in hl for k in keywords):
                mapping[field] = idx
                break
    return mapping


def _val(row: list[str], mapping: dict, field: str) -> str:
    idx = mapping.get(field)
    if idx is None or idx == "" or idx >= len(row):
        return ""
    return row[int(idx)].strip()


def check_required(kind: str, mapping: dict) -> list[str]:
    missing = []
    for req in KINDS[kind]["required"]:
        options = req.split("|")
        if not any(mapping.get(o) not in (None, "") for o in options):
            missing.append(" or ".join(dict(KINDS[kind]["fields"])[o] for o in options))
    return missing


# -- analysis -----------------------------------------------------------------

def analyze(db: sqlite3.Connection, staged: dict) -> dict:
    kind = staged["kind"]
    fn = {"people": _analyze_people, "devices": _analyze_devices, "assignments": _analyze_assignments, "plans": _analyze_plans}[kind]
    plan = fn(db, staged["rows"], staged["mapping"])
    counts = {"create": 0, "update": 0, "conflict": 0, "skip": 0, "error": 0}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    staged["plan"] = {"items": plan, "counts": counts}
    save_staged(staged)
    return staged["plan"]


def _person_row(row, mapping) -> dict:
    name = _val(row, mapping, "name")
    if not name:
        name = " ".join(x for x in (_val(row, mapping, "first_name"), _val(row, mapping, "last_name")) if x)
    return {
        "external_id": _val(row, mapping, "external_id"),
        "name": name,
        "dept": _val(row, mapping, "dept"),
        "role": _val(row, mapping, "role"),
        "phone": _val(row, mapping, "phone"),
        "bolero_id": _val(row, mapping, "bolero_id"),
        "kit": _val(row, mapping, "kit"),
        "notes": _val(row, mapping, "notes"),
    }


def _analyze_people(db, rows, mapping) -> list[dict]:
    people = db.execute("SELECT id, external_id, name, dept, phone FROM people").fetchall()
    by_ext = {p["external_id"]: p for p in people if p["external_id"]}
    by_name: dict[str, list] = {}
    by_phone: dict[str, list] = {}
    for p in people:
        by_name.setdefault(p["name"].lower(), []).append(p)
        ph = norm_phone(p["phone"])
        if ph:
            by_phone.setdefault(ph, []).append(p)
    plan = []
    seen_in_file: set[tuple[str, str]] = set()
    for i, row in enumerate(rows):
        data = _person_row(row, mapping)
        item = {"row": i, "data": data, "action": "create", "existing_id": None, "candidates": [], "message": ""}
        if not data["name"]:
            item.update(action="error", message="No name")
            plan.append(item)
            continue
        key = (data["name"].lower(), norm_phone(data["phone"]))
        if key in seen_in_file:
            item.update(action="skip", message="Duplicate row within the file")
            plan.append(item)
            continue
        seen_in_file.add(key)

        ext = data["external_id"]
        if ext and ext in by_ext:
            item.update(action="update", existing_id=by_ext[ext]["id"], message="Matched on unique ID")
            plan.append(item)
            continue
        name_matches = by_name.get(data["name"].lower(), [])
        phone = norm_phone(data["phone"])
        phone_matches = by_phone.get(phone, []) if phone else []
        exact = [p for p in name_matches if norm_phone(p["phone"]) == phone]
        if exact and (phone or not name_matches[0]["phone"]):
            item.update(action="update", existing_id=exact[0]["id"], message="Matched on name and phone")
        elif name_matches or phone_matches:
            cands = {p["id"]: p for p in name_matches + phone_matches}
            item.update(
                action="conflict",
                message="Similar person already exists",
                candidates=[
                    {"id": p["id"], "name": p["name"], "dept": p["dept"], "phone": p["phone"], "external_id": p["external_id"]}
                    for p in cands.values()
                ],
            )
        plan.append(item)
    return plan


def _analyze_devices(db, rows, mapping) -> list[dict]:
    devices = {r["si"].upper(): r for r in db.execute("SELECT id, si, type_id, label FROM devices").fetchall()}
    types = {r["name"].lower(): r["id"] for r in db.execute("SELECT id, name FROM device_types").fetchall()}
    plan, seen = [], set()
    for i, row in enumerate(rows):
        data = {
            "si": norm_si(_val(row, mapping, "si")),
            "type": _val(row, mapping, "type"),
            "label": _val(row, mapping, "label"),
            "site": _val(row, mapping, "site"),
            "notes": _val(row, mapping, "notes"),
        }
        item = {"row": i, "data": data, "action": "create", "existing_id": None, "candidates": [], "message": ""}
        if not data["si"]:
            item.update(action="error", message="No SI number")
        elif not data["type"]:
            item.update(action="error", message="No device type")
        elif data["si"] in seen:
            item.update(action="skip", message="Duplicate SI within the file")
        else:
            seen.add(data["si"])
            if data["type"].lower() not in types:
                item["message"] = f"New device type '{data['type']}' will be created"
            if data["si"] in devices:
                item.update(action="update", existing_id=devices[data["si"]]["id"], message=(item["message"] + " Matched on SI").strip())
        plan.append(item)
    return plan


def _analyze_assignments(db, rows, mapping) -> list[dict]:
    devices = {r["si"].upper(): r for r in db.execute("SELECT id, si FROM devices").fetchall()}
    people = db.execute("SELECT id, external_id, name, dept, phone FROM people").fetchall()
    by_ext = {p["external_id"]: p for p in people if p["external_id"]}
    plan, seen = [], set()
    for i, row in enumerate(rows):
        data = {
            "si": norm_si(_val(row, mapping, "si")),
            "person_external_id": _val(row, mapping, "person_external_id"),
            "person_name": _val(row, mapping, "person_name"),
            "person_phone": _val(row, mapping, "person_phone"),
            "note": _val(row, mapping, "note"),
        }
        item = {"row": i, "data": data, "action": "create", "existing_id": None, "candidates": [], "message": ""}
        if not data["si"]:
            item.update(action="error", message="No SI number"); plan.append(item); continue
        dev = devices.get(data["si"])
        if dev is None:
            item.update(action="error", message=f"Unknown device {data['si']}"); plan.append(item); continue
        if data["si"] in seen:
            item.update(action="skip", message="Device listed twice in file"); plan.append(item); continue
        seen.add(data["si"])
        item["device_id"] = dev["id"]
        match = None
        if data["person_external_id"] and data["person_external_id"] in by_ext:
            match = by_ext[data["person_external_id"]]
        else:
            phone = norm_phone(data["person_phone"])
            name = data["person_name"].lower()
            cands = [p for p in people if (name and p["name"].lower() == name) or (phone and norm_phone(p["phone"]) == phone)]
            exact = [p for p in cands if (not name or p["name"].lower() == name) and (not phone or norm_phone(p["phone"]) == phone)]
            if len(exact) == 1:
                match = exact[0]
            elif cands:
                item.update(
                    action="conflict",
                    message="Several people match",
                    candidates=[{"id": p["id"], "name": p["name"], "dept": p["dept"], "phone": p["phone"], "external_id": p["external_id"]} for p in cands],
                )
                plan.append(item); continue
        if match is None:
            item.update(action="error", message="Person not found")
        else:
            item["person_id"] = match["id"]
            item["message"] = f"{data['si']} -> {match['name']}"
        plan.append(item)
    return plan


def _find_person(people, by_ext, data) -> tuple:
    """Shared person matching for plan/assignment sheets -> (match, candidates)."""
    if data.get("person_external_id") and data["person_external_id"] in by_ext:
        return by_ext[data["person_external_id"]], []
    phone = norm_phone(data.get("person_phone", ""))
    name = data.get("person_name", "").lower()
    cands = [p for p in people if (name and p["name"].lower() == name) or (phone and norm_phone(p["phone"]) == phone)]
    exact = [p for p in cands if (not name or p["name"].lower() == name) and (not phone or norm_phone(p["phone"]) == phone)]
    if len(exact) == 1:
        return exact[0], []
    return None, cands


def _analyze_plans(db, rows, mapping) -> list[dict]:
    from .inventory import match_type
    people = db.execute("SELECT id, external_id, name, dept, phone FROM people").fetchall()
    by_ext = {p["external_id"]: p for p in people if p["external_id"]}
    plan = []
    for i, row in enumerate(rows):
        data = {
            "person_external_id": _val(row, mapping, "person_external_id"),
            "person_name": _val(row, mapping, "person_name"),
            "person_phone": _val(row, mapping, "person_phone"),
            "type": _val(row, mapping, "type"),
            "qty": _val(row, mapping, "qty") or "1",
            "note": _val(row, mapping, "note"),
        }
        item = {"row": i, "data": data, "action": "create", "existing_id": None, "candidates": [], "message": ""}
        if not data["type"]:
            item.update(action="error", message="No device type"); plan.append(item); continue
        t = match_type(db, data["type"])
        if t is None:
            item["message"] = f"New device type '{data['type']}' will be created"
        else:
            item["type_id"] = t["id"]
        if not data["qty"].isdigit():
            item.update(action="error", message=f"Quantity '{data['qty']}' is not a number"); plan.append(item); continue
        match, cands = _find_person(people, by_ext, data)
        if match is None and cands:
            item.update(action="conflict", message="Several people match",
                        candidates=[{"id": p["id"], "name": p["name"], "dept": p["dept"], "phone": p["phone"], "external_id": p["external_id"]} for p in cands])
        elif match is None:
            item.update(action="error", message="Person not found")
        else:
            item["person_id"] = match["id"]
            item["message"] = (item["message"] + f" {data['qty']} x {t['name'] if t else data['type']} -> {match['name']}").strip()
        plan.append(item)
    return plan


def _commit_plans(db, items, decisions, tech) -> dict:
    from .inventory import set_plan
    created = skipped = new_types = 0
    types = {r["name"].lower(): r["id"] for r in db.execute("SELECT id, name FROM device_types").fetchall()}
    for it in items:
        person_id = it.get("person_id")
        if it["action"] == "conflict":
            dec = decisions.get(str(it["row"]), "skip")
            if dec.startswith("update:"):
                person_id = int(dec.split(":", 1)[1])
        if it["action"] == "error" or not person_id:
            skipped += 1
            continue
        type_id = it.get("type_id")
        if type_id is None:
            key = it["data"]["type"].lower()
            if key not in types:
                cur = db.execute("INSERT INTO device_types (name, short_code, sort_order) VALUES (?, ?, 500)", (it["data"]["type"], it["data"]["type"][:3].upper()))
                types[key] = cur.lastrowid; new_types += 1
            type_id = types[key]
        set_plan(db, person_id, type_id, int(it["data"]["qty"]), it["data"]["note"], tech)
        created += 1
    return {"created": created, "skipped": skipped, "new_types": new_types}


# -- commit -------------------------------------------------------------------

def commit(db: sqlite3.Connection, staged: dict, decisions: dict[str, str], tech: Optional[dict] = None) -> dict:
    """``decisions`` maps row index (as str) -> 'update:<id>' | 'create' | 'skip' for conflict rows."""
    kind = staged["kind"]
    plan = staged.get("plan")
    if not plan:
        raise ValueError("Import has not been analysed")
    fn = {"people": _commit_people, "devices": _commit_devices, "assignments": _commit_assignments, "plans": _commit_plans}[kind]
    result = fn(db, plan["items"], decisions, tech)
    discard(staged["token"])
    return result


def _apply_kit(db, person_id: int, kit: str, tech) -> int:
    """'Bolero, Pro-D2 x2' -> plan rows. Returns the number of types that matched."""
    from .inventory import match_type, set_plan
    n = 0
    for part in [x.strip() for x in kit.replace(";", ",").replace("+", ",").split(",") if x.strip()]:
        qty = 1
        low = part.lower()
        for sep in (" x", "x", "×", " *"):
            if sep in low and low.rsplit(sep, 1)[-1].strip().isdigit():
                qty = int(low.rsplit(sep, 1)[-1].strip()); part = part[: low.rfind(sep)].strip(); break
        t = match_type(db, part)
        if t:
            set_plan(db, person_id, t["id"], qty, tech=tech); n += 1
    return n


def _commit_people(db, items, decisions, tech) -> dict:
    created = updated = skipped = 0
    ts = now_iso()
    for it in items:
        action, existing = it["action"], it["existing_id"]
        if action == "conflict":
            dec = decisions.get(str(it["row"]), "skip")
            if dec.startswith("update:"):
                action, existing = "update", int(dec.split(":", 1)[1])
            elif dec == "create":
                action = "create"
            else:
                action = "skip"
        d = it["data"]
        if action == "create":
            cur = db.execute(
                "INSERT INTO people (external_id, name, dept, role, phone, bolero_id, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (d["external_id"] or None, d["name"], d["dept"], d["role"], d["phone"], d["bolero_id"], d["notes"], ts, ts),
            )
            if d.get("kit"):
                _apply_kit(db, cur.lastrowid, d["kit"], tech)
            created += 1
        elif action == "update":
            db.execute(
                """UPDATE people SET
                     external_id = COALESCE(NULLIF(?, ''), external_id),
                     name = ?, dept = CASE WHEN ? <> '' THEN ? ELSE dept END,
                     role = CASE WHEN ? <> '' THEN ? ELSE role END,
                     phone = CASE WHEN ? <> '' THEN ? ELSE phone END,
                     bolero_id = CASE WHEN ? <> '' THEN ? ELSE bolero_id END,
                     notes = CASE WHEN ? <> '' THEN ? ELSE notes END,
                     updated_at = ?
                   WHERE id = ?""",
                (d["external_id"], d["name"], d["dept"], d["dept"], d["role"], d["role"], d["phone"], d["phone"], d["bolero_id"], d["bolero_id"], d["notes"], d["notes"], ts, existing),
            )
            if d.get("kit"):
                _apply_kit(db, existing, d["kit"], tech)
            updated += 1
        else:
            skipped += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _commit_devices(db, items, decisions, tech) -> dict:
    created = updated = skipped = 0
    new_types = 0
    ts = now_iso()
    types = {r["name"].lower(): r["id"] for r in db.execute("SELECT id, name FROM device_types").fetchall()}
    sites = {r["name"].lower(): r["id"] for r in db.execute("SELECT id, name FROM sites").fetchall()}
    for it in items:
        if it["action"] not in ("create", "update"):
            skipped += 1
            continue
        d = it["data"]
        tkey = d["type"].lower()
        if tkey not in types:
            cur = db.execute("INSERT INTO device_types (name, short_code, sort_order) VALUES (?, ?, 500)", (d["type"], d["type"][:3].upper()))
            types[tkey] = cur.lastrowid
            new_types += 1
        site_id = sites.get(d["site"].lower()) if d["site"] else None
        if it["action"] == "create":
            cur = db.execute(
                "INSERT INTO devices (si, type_id, label, site_id, status, notes, created_at, updated_at) VALUES (?,?,?,?,'in',?,?,?)",
                (d["si"], types[tkey], d["label"], site_id, d["notes"], ts, ts),
            )
            log(db, "imported", cur.lastrowid, None, tech)
            created += 1
        else:
            db.execute(
                """UPDATE devices SET type_id = ?,
                     label = CASE WHEN ? <> '' THEN ? ELSE label END,
                     site_id = CASE WHEN ? <> '' THEN ? ELSE site_id END,
                     notes = CASE WHEN ? <> '' THEN ? ELSE notes END,
                     updated_at = ?
                   WHERE id = ?""",
                (types[tkey], d["label"], d["label"], d["site"], site_id, d["notes"], d["notes"], ts, it["existing_id"]),
            )
            updated += 1
    return {"created": created, "updated": updated, "skipped": skipped, "new_types": new_types}


def _commit_assignments(db, items, decisions, tech) -> dict:
    created = skipped = 0
    ts = now_iso()
    for it in items:
        person_id = it.get("person_id")
        if it["action"] == "conflict":
            dec = decisions.get(str(it["row"]), "skip")
            if dec.startswith("update:"):
                person_id = int(dec.split(":", 1)[1])
        if it["action"] in ("error", "skip") or not person_id:
            skipped += 1
            continue
        db.execute(
            """INSERT INTO assignments (device_id, person_id, note, created_at) VALUES (?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET person_id = excluded.person_id, note = excluded.note, created_at = excluded.created_at""",
            (it["device_id"], person_id, it["data"]["note"], ts),
        )
        log(db, "assigned", it["device_id"], person_id, tech, note="Imported")
        created += 1
    return {"created": created, "skipped": skipped}
