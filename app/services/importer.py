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
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook

from ..config import IMPORT_DIR, ensure_dirs
from ..db import now_iso
from .inventory import log, norm_phone, norm_si

KINDS = {
    "devices": {
        "title": "easyjob import",
        "blurb": "Delivery note PDF, or the easyjob export (.xlsx / .csv). Beltpacks, headsets and radios only.",
        "fields": [
            ("si", "SI number / barcode"),
            ("type", "Device type"),
            ("label", "Label / display name"),
            ("bolero_id", "Configured Bolero ID (packs only)"),
            ("site", "Site (optional)"),
            ("notes", "Notes / description"),
            ("qty", "Quantity (delivery notes)"),
        ],
        "required": ["si", "type"],
    },
    "people": {
        "title": "Allocation",
        "blurb": "The comms allocation sheet (.xlsx / .csv): Bolero #, colour column, Position, Role Name, Person, Pack, Headset.",
        "fields": [
            ("external_id", "Unique ID / badge (optional)"),
            ("name", "Full name"),
            ("first_name", "First name (if split)"),
            ("last_name", "Last name (if split)"),
            ("dept", "Position / department"),
            ("role", "Role name"),
            ("phone", "Phone"),
            ("bolero_id", "Bolero ID"),
            ("faceplate", "Faceplate colour (cell colour or text)"),
            ("pack", "Pack allocation (e.g. Bolero)"),
            ("headset", "Headset allocation (e.g. Pro-D1)"),
            ("notes", "Notes"),
        ],
        "required": ["name|first_name|dept|role"],
    },
}

# header keywords -> field, checked in order (lowercased, punctuation stripped)
GUESSES = {
    "people": [
        (("badge", "staff id", "staffid", "employee", "crew id", "id number", "unique"), "external_id"),
        (("bolero #", "bolero no", "bolero id", "bolero", "beltpack id", "comms id"), "bolero_id"),
        (("full name", "fullname", "person", "name"), "name"),
        (("first", "given", "forename"), "first_name"),
        (("last", "surname", "family"), "last_name"),
        (("position", "dept", "department", "team", "company"), "dept"),
        (("role", "job", "title", "function"), "role"),
        (("phone", "mobile", "cell", "tel", "contact"), "phone"),
        (("faceplate", "colour", "color"), "faceplate"),
        (("headset",), "headset"),
        (("pack", "device"), "pack"),
        (("note", "comment", "remark"), "notes"),
    ],
    "devices": [
        (("si", "serial", "barcode", "asset", "inventory no", "inventar"), "si"),
        (("type", "category", "group", "article", "item", "product"), "type"),
        (("bolero id", "bolero", "beltpack id", "pack id"), "bolero_id"),
        (("label", "display", "name", "number", "no.", "id"), "label"),
        (("site", "zone", "location", "rdr"), "site"),
        (("description", "note", "comment", "remark"), "notes"),
        (("qty", "quantity"), "qty"),
    ],
}


# easyjob descriptions -> RDR device types. First match wins; anything unmatched is ignored
# (panels, antennas, power, batteries, chargers, cables, cases...). Keep the specific before the general.
# Only the product itself is read: "..., for PD605" or "for Beltpacks, Panels" says what it fits, not what it is.
DESCRIPTION_RULES: list[tuple[tuple[str, ...], str]] = [
    (("bl-bpk", "bolero beltpack"), "Bolero Beltpack"),
    (("pro-d1",), "Pro-D1 Headset"),
    (("pro-d2",), "Pro-D2 Headset"),
    (("air-d1",), "Air-D1 Headset"),
    (("max-e1",), "Max-E1 Headset"),
    (("covert",), "Covert Headset"),
    (("hc headset cord", "headset cord", "headset adapter"), "Radio Headset Adapter"),
    (("intercom headset", "headset"), "Headset"),
    (("rsm", "speaker mic", "remote speaker"), "RSM"),
    (("earpiece", "ear piece"), "Earpiece"),
]

# TS2 surveillance kits are headsets, one type per connector family
_TS2 = re.compile(r"\bts2\b|surveillance kit")
# radios are typed by make and model: "Hytera PD682 Radio", "Moto R7 Radio"
_RADIO_BRANDS = (("hytera", "Hytera"), ("motorola", "Moto"), ("moto", "Moto"), ("kenwood", "Kenwood"))
_RADIO_MODEL = re.compile(r"^(?:mototrbo\s+)?([a-z]{1,3}-?\d{1,4}[a-z]{0,2})\b")
_RADIO_FAMILIES = re.compile(r"^(pd|pt|hp|bd|bp|x1|r\d|dp|sl|xpr|nx|tk)")
_RADIO_WORD = re.compile(r"\bradios?\b|\bwalkie")


def _ts2_type(d: str) -> str:
    if "hytera" in d:
        return "Hytera TS2 Headset"
    if re.search(r"\bmoto(rola)?\b|\bmototrbo\b|\br7\b", d):
        return "Moto R7 TS2 Headset"
    return "TS2 Headset"


def _radio_type(product: str) -> str:
    """'Hytera PD682 Handheld Radio, DMR' -> 'Hytera PD682 Radio'. A radio with no readable model is a plain
    'Radio'; a make on its own (some other accessory) is nothing."""
    said_radio = bool(_RADIO_WORD.search(product))
    for word, brand in _RADIO_BRANDS:
        at = re.search(rf"\b{word}\b\s+", product)
        if not at:
            continue
        m = _RADIO_MODEL.match(product[at.end():])
        if m and (said_radio or _RADIO_FAMILIES.match(m.group(1))):
            return f"{brand} {m.group(1).upper()} Radio"
        break
    return "Radio" if said_radio else ""


# accessories and infrastructure that mention a tracked product in their description but are not one
IGNORE_WORDS = ("cover", "charger", "bl-chg", "battery", "bl-bat", "antenna", "bl-ant", "power supply", "bl-eps", "power cable",
                "adapter cable", "expansion card", "panel", "converter", "switch,", "zarges", "clamp", "magic arm",
                "speaker station", "beltclip", "mount", "case,", "flight case", "licence", "license", "software", "rack")


def classify_description(desc: str) -> str:
    full = " ".join((desc or "").lower().split())
    product = re.split(r",?\s+for\s+", full, maxsplit=1)[0]
    d = " " + product + " "
    if _TS2.search(product):
        return _ts2_type(full)
    if any(w in d for w in IGNORE_WORDS):
        return ""
    for keys, tname in DESCRIPTION_RULES:
        if any(k in d for k in keys):
            return tname
    return _radio_type(product)


def new_type_defaults(name: str) -> tuple[str, int]:
    """Short code and list position for a type the import creates, so it sits with its family."""
    n = name.lower()
    if "adapter" not in n and "headset" in n:
        return "HS", 56
    if "adapter" not in n and "radio" in n:
        return "RAD", 31
    return name[:3].upper(), 500


_POS_LINE = re.compile(r"^(\d{1,4})\s+(\d{5,9})\s+(.+?)(?:\s+(\d{1,4}))?\s*$")
_BARCODE_TOKEN = re.compile(r"@?si\s*(\d{5,})|\b(\d{5,9})\b", re.I)


def parse_delivery_text(text: str) -> list[list[str]]:
    """easyjob delivery note text -> one row per barcode: [SI, Type, Label, Art.No., Description, Qty, Pos].
    Items with no barcode are returned once with an empty SI so they show as skipped. Long descriptions
    may wrap onto the next line and the quantity may sit at the end of the last wrapped line."""
    rows: list[list[str]] = []
    pos = art = desc = qty = None
    codes: list[str] = []
    mode = "none"          # none | desc | codes

    def flush():
        if pos is None:
            return
        tname = classify_description(desc)
        if codes:
            for c in codes:
                rows.append([c, tname, "", art, desc, qty or "", pos])
        else:
            rows.append(["", tname, "", art, desc, qty or "", pos])

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("pos.") or re.match(r"^\S+\s*\|\s*Page \d+ of \d+$", line):
            continue
        m = _POS_LINE.match(line)
        if m and not (mode == "codes" and _BARCODE_TOKEN.search(line) and not re.search(r"[A-Za-z]{3,}", line)):
            flush()
            pos, art, desc, qty = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
            codes, mode = [], "desc"
            continue
        if line.lower().startswith("barcode"):
            mode = "codes"
            line = line.split(":", 1)[1] if ":" in line else ""
            if not line.strip():
                continue
        if mode == "codes" and pos is not None:
            hits = _BARCODE_TOKEN.findall(line)
            if hits and not re.search(r"[A-Za-z]{4,}", re.sub(r"@?si", "", line, flags=re.I)):
                for a, b in hits:
                    codes.append(("SI" + a) if a else b)
                continue
            mode = "none"          # text again: the barcode block has ended
        if mode == "desc" and pos is not None:
            # a wrapped description line; a trailing number is the quantity
            mq = re.match(r"^(.*?)(?:\s+(\d{1,4}))?$", line)
            desc = (desc + " " + mq.group(1).strip()).strip()
            if mq.group(2):
                qty = mq.group(2)
            continue
    flush()
    return rows


def _clean(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


THEME_INDEX = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6"]
DEFAULT_THEME = {"lt1": "FFFFFF", "dk1": "000000", "lt2": "E7E6E6", "dk2": "44546A", "accent1": "4472C4", "accent2": "ED7D31",
                 "accent3": "A5A5A5", "accent4": "FFC000", "accent5": "5B9BD5", "accent6": "70AD47"}


def _theme_colours(wb) -> dict:
    import re
    theme = wb.loaded_theme
    if isinstance(theme, bytes):
        theme = theme.decode("utf-8", "ignore")
    out = dict(DEFAULT_THEME)
    if theme:
        for n in THEME_INDEX:
            m = re.search(rf"<a:{n}>(.*?)</a:{n}>", theme, re.S)
            if m:
                c = re.search(r'(?:srgbClr val|lastClr)="([0-9A-Fa-f]{6})"', m.group(1))
                if c:
                    out[n] = c.group(1).upper()
    return out


def _fill_hex(cell, theme: dict) -> str:
    """Solid fill of a cell as RRGGBB, or '' (white and no-fill count as none)."""
    f = cell.fill
    if f is None or f.fill_type != "solid":
        return ""
    fg = f.fgColor
    rgb = ""
    if fg.type == "rgb" and fg.rgb:
        rgb = str(fg.rgb)[-6:].upper()
    elif fg.type == "theme" and fg.theme is not None and fg.theme < len(THEME_INDEX):
        rgb = theme.get(THEME_INDEX[fg.theme], "")
    return "" if rgb in ("", "FFFFFF") else rgb


def parse_file(filename: str, content: bytes) -> tuple[list[list[str]], list[list[str]], list[str]]:
    """Return (raw rows, raw fills, sheet names). The header row is chosen afterwards."""
    name = filename.lower()
    sheets: list[str] = []
    fills: list[list[str]] = []
    if name.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(content), data_only=True)
        sheets = wb.sheetnames
        ws = wb[sheets[0]]
        theme = _theme_colours(wb)
        raw, fills = [], []
        for row in ws.iter_rows():
            raw.append([_clean(c.value) for c in row])
            fills.append([_fill_hex(c, theme) for c in row])
    elif name.endswith(".pdf"):
        try:
            import pdfplumber
        except ImportError as e:  # pragma: no cover
            raise ValueError("PDF import needs the pdfplumber package. Run Update & restart to install requirements.") from e
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        rows = parse_delivery_text(text)
        if not rows:
            raise ValueError("No positions with barcodes found in this PDF. Is it an easyjob delivery note?")
        raw = [["SI", "Type", "Label", "Art.No.", "Description", "Qty", "Pos"]] + rows
        sheets = ["Delivery note"]
        fills = [["" for _ in r] for r in raw]
        return raw, fills, sheets
    elif name.endswith((".csv", ".txt", ".tsv")):
        text = content.decode("utf-8-sig", errors="replace")
        dialect = csv.excel_tab if name.endswith(".tsv") else csv.excel
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            pass
        raw = [[_clean(c) for c in row] for row in csv.reader(io.StringIO(text), dialect)]
        fills = [["" for _ in r] for r in raw]
    else:
        raise ValueError("Unsupported file type. Please upload .xlsx or .csv")
    keep = [i for i, r in enumerate(raw) if any(c for c in r) or any(fills[i])]
    raw = [raw[i] for i in keep]
    fills = [fills[i] for i in keep]
    if not raw:
        raise ValueError("The file has no data rows")
    width = max(len(r) for r in raw)
    raw = [r + [""] * (width - len(r)) for r in raw]
    fills = [f + [""] * (width - len(f)) for f in fills]
    return raw, fills, sheets


def guess_header_row(raw: list[list[str]]) -> int:
    """The header is the row among the first six with the most filled cells (a title row above it loses)."""
    best, best_n = 0, -1
    for i, r in enumerate(raw[:6]):
        n = sum(1 for c in r if c)
        if n > best_n:
            best, best_n = i, n
    return best


def split_header(raw: list[list[str]], fills: list[list[str]], header_row: int) -> tuple[list[str], list[list[str]], list[list[str]]]:
    header_row = max(0, min(header_row, len(raw) - 1))
    headers = [h or f"Column {i + 1}" for i, h in enumerate(raw[header_row])]
    return headers, raw[header_row + 1:], fills[header_row + 1:]


def stage_upload(kind: str, filename: str, content: bytes) -> dict:
    if kind not in KINDS:
        raise ValueError("Unknown import kind")
    raw, fills, sheets = parse_file(filename, content)
    header_row = guess_header_row(raw)
    headers, rows, row_fills = split_header(raw, fills, header_row)
    ensure_dirs()
    token = secrets.token_urlsafe(10)
    mapping = guess_mapping(kind, headers)
    if kind == "people" and mapping.get("faceplate") is None:
        mapping["faceplate"] = guess_colour_column(rows, row_fills, headers)
    staged = {
        "token": token,
        "kind": kind,
        "filename": filename,
        "raw": raw,
        "fills": fills,
        "header_row": header_row,
        "headers": headers,
        "rows": rows,
        "row_fills": row_fills,
        "sheets": sheets,
        "created_at": now_iso(),
        "mapping": mapping,
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


def set_header_row(staged: dict, header_row: int) -> None:
    """Re-split a staged upload on a different header row and re-guess the mapping."""
    staged["header_row"] = header_row
    staged["headers"], staged["rows"], staged["row_fills"] = split_header(staged["raw"], staged["fills"], header_row)
    staged["mapping"] = guess_mapping(staged["kind"], staged["headers"])
    if staged["kind"] == "people" and staged["mapping"].get("faceplate") is None:
        staged["mapping"]["faceplate"] = guess_colour_column(staged["rows"], staged["row_fills"], staged["headers"])
    staged["plan"] = None
    save_staged(staged)


def guess_mapping(kind: str, headers: list[str]) -> dict[str, Optional[int]]:
    """Field by field in priority order; for each field, keywords in order of specificity, and an
    exact header match beats a contains match ("Person" wins the name field over "Role Name")."""
    mapping: dict[str, Optional[int]] = {f: None for f, _ in KINDS[kind]["fields"]}
    used: set[int] = set()
    norm = [h.lower().replace("_", " ").replace("-", " ").strip() for h in headers]
    for keywords, field in GUESSES[kind]:
        if mapping[field] is not None:
            continue
        found = None
        for exact in (True, False):
            for k in keywords:
                for idx, hl in enumerate(norm):
                    if idx in used:
                        continue
                    if (hl == k) if exact else (k in hl):
                        found = idx
                        break
                if found is not None:
                    break
            if found is not None:
                break
        if found is not None:
            mapping[field] = found
            used.add(found)
    return mapping


def guess_colour_column(rows: list[list[str]], row_fills: list[list[str]], headers: list[str]) -> Optional[int]:
    """A column that is mostly empty text but carries several different fill colours is the faceplate column."""
    best, best_score = None, 0
    n = max(1, min(len(rows), 300))
    for idx in range(len(headers)):
        texts = sum(1 for r in rows[:n] if idx < len(r) and r[idx])
        colours = {f[idx] for f in row_fills[:n] if idx < len(f) and f[idx]}
        if texts <= n * 0.05 and len(colours) >= 2:
            score = len(colours) * 10 + sum(1 for f in row_fills[:n] if idx < len(f) and f[idx]) / n
            if score > best_score:
                best, best_score = idx, score
    return best


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
    if kind == "people":
        plan = _analyze_people(db, staged["rows"], staged["mapping"], staged.get("row_fills"))
    else:
        plan = _analyze_devices(db, staged["rows"], staged["mapping"])
    counts = {"create": 0, "update": 0, "conflict": 0, "skip": 0, "error": 0}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    staged["plan"] = {"items": plan, "counts": counts}
    save_staged(staged)
    return staged["plan"]


PLACEHOLDERS = {"tba", "tbc", "tbd", "n/a", "na", "-", "?", "none", "vacant"}
COLOUR_NAMES = {"red": "FF0000", "green": "00B050", "blue": "00B0F0", "yellow": "FFFF00", "black": "000000", "white": "FFFFFF",
                "grey": "A5A5A5", "gray": "A5A5A5", "pink": "E9516A", "orange": "ED7D31", "purple": "7030A0", "silver": "A5A5A5"}


def _faceplate(row, fills, mapping) -> str:
    idx = mapping.get("faceplate")
    if idx is None or idx == "":
        return ""
    idx = int(idx)
    text = (row[idx] if idx < len(row) else "").strip().lstrip("#")
    if text:
        if len(text) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return text.upper()
        return COLOUR_NAMES.get(text.lower(), "")
    return (fills[idx] if fills and idx < len(fills) else "") or ""


def _person_row(row, mapping, fills=None) -> dict:
    name = _val(row, mapping, "name")
    if not name:
        name = " ".join(x for x in (_val(row, mapping, "first_name"), _val(row, mapping, "last_name")) if x)
    dept = _val(row, mapping, "dept")
    role = _val(row, mapping, "role")
    if name.strip().lower() in PLACEHOLDERS or (not name and (dept or role)):
        # no real name yet: keep the position (or the role name) as a placeholder the tech renames on site
        label = dept or role
        name = f"{label} ({name.strip() or 'TBA'})" if label else ""
    bolero_id = _val(row, mapping, "bolero_id")
    if bolero_id and not any(ch.isdigit() for ch in bolero_id):
        bolero_id = ""                                       # 'xx', '-' and the like are not IDs
    pack, headset = _val(row, mapping, "pack"), _val(row, mapping, "headset")
    allocation = " · ".join(x for x in (pack, (headset + " headset") if headset else "") if x)
    return {
        "external_id": _val(row, mapping, "external_id"),
        "name": name,
        "dept": dept,
        "role": role,
        "phone": _val(row, mapping, "phone"),
        "bolero_id": bolero_id,
        "faceplate": _faceplate(row, fills, mapping),
        "allocation": allocation,
        "notes": _val(row, mapping, "notes"),
    }


def _analyze_people(db, rows, mapping, row_fills=None) -> list[dict]:
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
    ids_in_file: dict[tuple[str, str], set[str]] = {}
    for i, row in enumerate(rows):
        data = _person_row(row, mapping, row_fills[i] if row_fills and i < len(row_fills) else None)
        item = {"row": i, "data": data, "action": "create", "existing_id": None, "candidates": [], "message": ""}
        if not data["name"]:
            item.update(action="error", message="No name")
            plan.append(item)
            continue
        key = (data["name"].lower(), norm_phone(data["phone"]))
        if key in seen_in_file:
            # the same name again: a second allocation (another pack / position) becomes its own entry,
            # named by position so both show up at the counter; a true repeat is skipped
            if data["bolero_id"] and data["bolero_id"] not in ids_in_file.get(key, set()) and data["dept"]:
                if data["name"].startswith(data["dept"]):      # already a position placeholder: tell them apart by ID
                    data["name"] = f"{data['dept']} (TBA #{data['bolero_id']})"
                else:
                    data["name"] = f"{data['name']} ({data['dept']})"
                item["message"] = "Same name again with another Bolero ID: kept as a separate entry"
                key = (data["name"].lower(), norm_phone(data["phone"]))
            else:
                item.update(action="skip", message="Duplicate row within the file")
                plan.append(item)
                continue
        seen_in_file.add(key)
        ids_in_file.setdefault(key, set()).add(data["bolero_id"])

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
            "bolero_id": _val(row, mapping, "bolero_id"),
            "site": _val(row, mapping, "site"),
            "notes": _val(row, mapping, "notes"),
        }
        item = {"row": i, "data": data, "action": "create", "existing_id": None, "candidates": [], "message": ""}
        desc = _val(row, mapping, "notes")
        if data["type"] and data["type"].lower() not in types:
            # export sheets carry the article description where a type should be: resolve it the same way as a PDF
            resolved = classify_description(data["type"]) or classify_description(desc)
            if not resolved:
                item.update(action="error", message=f"Ignored: not a beltpack, headset or radio ({data['type'][:60]})")
                plan.append(item); continue
            if not data["notes"]:
                data["notes"] = data["type"]
            data["type"] = resolved
        if not data["type"] and desc and classify_description(desc) == "":
            item.update(action="error", message=f"Ignored: not a beltpack, headset or radio ({desc[:60]})")
        elif not data["type"] and desc:
            data["type"] = classify_description(desc)
        if item["action"] == "error":
            pass
        elif not data["si"]:
            item.update(action="error", message="No SI number" + (f" ({data['type']} x {_val(row, mapping, 'qty') or '?'})" if data["type"] else ""))
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


# -- commit -------------------------------------------------------------------

def commit(db: sqlite3.Connection, staged: dict, decisions: dict[str, str], tech: Optional[dict] = None) -> dict:
    """``decisions`` maps row index (as str) -> 'update:<id>' | 'create' | 'skip' for conflict rows."""
    kind = staged["kind"]
    plan = staged.get("plan")
    if not plan:
        raise ValueError("Import has not been analysed")
    fn = {"people": _commit_people, "devices": _commit_devices}[kind]
    result = fn(db, plan["items"], decisions, tech)
    discard(staged["token"])
    return result


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
            db.execute(
                "INSERT INTO people (external_id, name, dept, role, phone, bolero_id, faceplate, allocation, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (d["external_id"] or None, d["name"], d["dept"], d["role"], d["phone"], d["bolero_id"], d.get("faceplate", ""), d.get("allocation", ""), d["notes"], ts, ts),
            )
            created += 1
        elif action == "update":
            db.execute(
                """UPDATE people SET
                     external_id = COALESCE(NULLIF(?, ''), external_id),
                     name = ?, dept = CASE WHEN ? <> '' THEN ? ELSE dept END,
                     role = CASE WHEN ? <> '' THEN ? ELSE role END,
                     phone = CASE WHEN ? <> '' THEN ? ELSE phone END,
                     bolero_id = CASE WHEN ? <> '' THEN ? ELSE bolero_id END,
                     faceplate = CASE WHEN ? <> '' THEN ? ELSE faceplate END,
                     allocation = CASE WHEN ? <> '' THEN ? ELSE allocation END,
                     notes = CASE WHEN ? <> '' THEN ? ELSE notes END,
                     updated_at = ?
                   WHERE id = ?""",
                (d["external_id"], d["name"], d["dept"], d["dept"], d["role"], d["role"], d["phone"], d["phone"], d["bolero_id"], d["bolero_id"],
                 d.get("faceplate", ""), d.get("faceplate", ""), d.get("allocation", ""), d.get("allocation", ""), d["notes"], d["notes"], ts, existing),
            )
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
            cur = db.execute("INSERT INTO device_types (name, short_code, sort_order) VALUES (?, ?, ?)", (d["type"], *new_type_defaults(d["type"])))
            types[tkey] = cur.lastrowid
            new_types += 1
        site_id = sites.get(d["site"].lower()) if d["site"] else None
        if it["action"] == "create":
            from .inventory import default_linked_id
            cur = db.execute(
                "INSERT INTO devices (si, type_id, label, site_id, status, notes, linked_id, created_at, updated_at) VALUES (?,?,?,?,'in',?,?,?,?)",
                (d["si"], types[tkey], d["label"], site_id, d["notes"], default_linked_id(db, types[tkey], d["label"], d.get("bolero_id", "")), ts, ts),
            )
            log(db, "imported", cur.lastrowid, None, tech)
            created += 1
        else:
            db.execute(
                """UPDATE devices SET type_id = ?,
                     label = CASE WHEN ? <> '' THEN ? ELSE label END,
                     site_id = CASE WHEN ? <> '' THEN ? ELSE site_id END,
                     linked_id = CASE WHEN ? <> '' THEN ? ELSE linked_id END,
                     notes = CASE WHEN ? <> '' THEN ? ELSE notes END,
                     updated_at = ?
                   WHERE id = ?""",
                (types[tkey], d["label"], d["label"], d["site"], site_id, d.get("bolero_id", ""), d.get("bolero_id", ""), d["notes"], d["notes"], ts, it["existing_id"]),
            )
            updated += 1
    return {"created": created, "updated": updated, "skipped": skipped, "new_types": new_types}
