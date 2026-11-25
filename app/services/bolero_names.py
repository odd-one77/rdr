"""Compare the names people were imported with against the names set on their Bolero packs.

Off unless "Use Bolero pack names" is ticked on the Bolero link page. A pack's name is matched to a
person through their Bolero ID (pack number). Pack screens are short, so a name whose words each start
the person's words ("J Carter", "Sam P", "Jamal Car") counts as the same person. Default pack names
like "Bolero 61" or "BP 61" are ignored. A tech can keep the RDR name, which is remembered until the
pack is renamed again.

With Pack names set to "Always use Bolero names" nobody is asked: after each read of the antenna a
person whose pack carries a different name takes that name. The imported name is kept in
`import_name`, so search and re-imports still find them and it can be restored.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from ..db import get_setting, now_iso, set_setting
from . import bolero

KEPT_KEY = "bolero_names_kept"
_DEFAULT = re.compile(r"^(bolero|bp|beltpack|pack|user|spare)?\s*#?\s*\d*$", re.I)


def enabled() -> bool:
    st = bolero.settings()
    return st["enabled"] and st["names"]


def always() -> bool:
    st = bolero.settings()
    return st["enabled"] and st["names_mode"] == "always"


def id_of(value: str) -> str:
    v = re.sub(r"^\s*bolero\s*", "", str(value or ""), flags=re.I).strip()
    return v.lstrip("0") or v


def _words(name: str) -> list[str]:
    return re.sub(r"[^0-9a-z]+", " ", (name or "").lower()).split()


def real_name(pack_name: str) -> bool:
    return bool((pack_name or "").strip()) and not _DEFAULT.match(pack_name.strip())


def same_name(rdr_name: str, pack_name: str) -> bool:
    a, b = _words(rdr_name), _words(pack_name)
    if not a or not b:
        return True
    # each word on the pack starts the matching word in RDR: "Sam P", "J Carter", or cut off on the pack screen
    return len(b) <= len(a) and all(x.startswith(y) for x, y in zip(a, b))


def names() -> dict[str, str]:
    """Pack number to its real (non-default) name."""
    if not enabled():
        return {}
    return {n: nm for n, nm in bolero.pack_names().items() if real_name(nm)}


def _kept(db: sqlite3.Connection) -> dict[str, str]:
    try:
        return json.loads(get_setting(db, KEPT_KEY) or "{}")
    except ValueError:
        return {}


def keep(db: sqlite3.Connection, person_id: int, pack_name: str) -> None:
    kept = _kept(db)
    kept[str(person_id)] = pack_name
    set_setting(db, KEPT_KEY, json.dumps(kept))


def check(db: sqlite3.Connection, person, packs: Optional[dict[str, str]] = None) -> Optional[dict]:
    """The pack name when it differs from the person's name and the tech has not kept the RDR name."""
    if not person or not person["bolero_id"]:
        return None
    packs = names() if packs is None else packs
    number = id_of(person["bolero_id"])
    pack_name = packs.get(number)
    if not pack_name or same_name(person["name"], pack_name):
        return None
    if not always() and _kept(db).get(str(person["id"])) == pack_name:
        return None
    return {"number": number, "pack_name": pack_name}


def label(person, packs: dict[str, str]) -> str:
    """The pack name to show beside a person in search results ('' when there is none)."""
    if not person["bolero_id"]:
        return ""
    return packs.get(id_of(person["bolero_id"]), "")


def search(db: sqlite3.Connection, q: str, exclude: set[int], limit: int = 8) -> tuple[list[dict], list[dict]]:
    """People found by the name on their pack, and packs with a matching name that nobody in RDR has."""
    packs = names()
    words = _words(q)
    if not packs or not words:
        return [], []
    hits = {n: nm for n, nm in packs.items() if all(any(w.startswith(x) for w in _words(nm)) for x in words)}
    if not hits:
        return [], []
    people, claimed = [], set()
    for row in db.execute("SELECT id, bolero_id FROM people WHERE bolero_id <> ''"):
        n = id_of(row["bolero_id"])
        if n in hits:
            claimed.add(n)
            if row["id"] not in exclude:
                people.append({"id": row["id"], "number": n, "pack_name": hits[n]})
    loose = [{"number": n, "pack_name": nm} for n, nm in sorted(hits.items(), key=lambda x: (len(x[0]), x[0])) if n not in claimed]
    return people[:limit], loose[:limit]


def differences(db: sqlite3.Connection) -> list[dict]:
    """Everyone whose pack carries a different name, for the Bolero link page."""
    packs = names()
    if not packs:
        return []
    out = []
    for p in db.execute("SELECT * FROM people WHERE bolero_id <> '' ORDER BY name"):
        c = check(db, p, packs)
        if c:
            out.append({"id": p["id"], "name": p["name"], "dept": p["dept"], **c})
    return sorted(out, key=lambda x: (len(x["number"]), x["number"]))


def apply_all(db: sqlite3.Connection) -> list[dict]:
    """Always use Bolero names: give everyone whose pack name differs that name, keeping the imported one."""
    if not always():
        return []
    packs = names()
    if not packs:
        return []
    changed = []
    for p in db.execute("SELECT * FROM people WHERE bolero_id <> ''").fetchall():
        c = check(db, p, packs)
        if c:
            db.execute("UPDATE people SET import_name = CASE WHEN import_name = '' THEN name ELSE import_name END, name = ?, updated_at = ? WHERE id = ?",
                       (c["pack_name"], now_iso(), p["id"]))
            changed.append({"id": p["id"], "from": p["name"], "to": c["pack_name"]})
    return changed


def renamed(db: sqlite3.Connection) -> list[dict]:
    """People whose name came from their pack, with the imported name that can be restored."""
    rows = db.execute("SELECT id, name, import_name, dept, bolero_id FROM people WHERE import_name <> '' AND import_name <> name ORDER BY name")
    return [dict(r) | {"number": id_of(r["bolero_id"])} for r in rows]


def restore(db: sqlite3.Connection, person_id: Optional[int] = None) -> int:
    """Put the imported name back (one person, or everyone renamed from Bolero)."""
    where, args = ("AND id = ?", (person_id,)) if person_id else ("", ())
    rows = db.execute(f"SELECT id, bolero_id FROM people WHERE import_name <> '' {where}", args).fetchall()
    db.execute(f"UPDATE people SET name = import_name, import_name = '', updated_at = ? WHERE import_name <> '' {where}", (now_iso(), *args))
    # restored on purpose: in Ask mode, do not ask about these names again until the pack is renamed
    packs, kept = names(), _kept(db)
    for r in rows:
        if packs.get(id_of(r["bolero_id"])):
            kept[str(r["id"])] = packs[id_of(r["bolero_id"])]
    set_setting(db, KEPT_KEY, json.dumps(kept))
    return len(rows)


def _sync_after_poll() -> None:
    if not always():
        return
    from ..db import get_db
    with get_db() as db:
        apply_all(db)


bolero._after_poll.append(_sync_after_poll)
