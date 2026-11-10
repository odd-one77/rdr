"""Demo data so the UI can be explored before any real sheets are imported."""
from __future__ import annotations

import random
import sqlite3

from ..db import now_iso
from .inventory import create_person, log, quick_create_device, set_assignment, sign_out

PEOPLE = [
    ("Alex Morgan", "Broadcast", "Director", "+44 7700 900101"),
    ("Priya Nair", "Broadcast", "Floor Manager", "+44 7700 900102"),
    ("Tom Becker", "Lighting", "Lighting Director", "+44 7700 900103"),
    ("Sofia Reyes", "Audio", "A1", "+44 7700 900104"),
    ("Jamal Carter", "Stage", "Stage Manager", "+44 7700 900105"),
    ("Hannah Weiss", "Production", "Producer", "+44 7700 900106"),
    ("Luca Moretti", "Cameras", "Camera Op", "+44 7700 900107"),
    ("Mei Tanaka", "Cameras", "Camera Op", "+44 7700 900108"),
    ("Daniel Okafor", "Security", "Head of Security", "+44 7700 900109"),
    ("Emma Lindqvist", "Production", "Runner", "+44 7700 900110"),
    ("Chris Dawson", "Rigging", "Head Rigger", "+44 7700 900111"),
    ("Nadia Haddad", "Broadcast", "Script Supervisor", "+44 7700 900112"),
]


def load_demo(db: sqlite3.Connection) -> int:
    ts = now_iso()
    db.execute("UPDATE event SET name = 'Summer Arena Tour 2026', show_name = 'Night 3 - London', updated_at = ? WHERE id = 1", (ts,))
    db.execute("UPDATE sites SET name = 'RDR North' WHERE id = (SELECT MIN(id) FROM sites)")
    if db.execute("SELECT COUNT(*) FROM sites").fetchone()[0] < 2:
        db.execute("INSERT INTO sites (name, stock_mode, sort_order) VALUES ('RDR South', 'shared', 1)")
    for u, d in (("sam", "Sam Hughes"), ("kate", "Kate Nowak")):
        db.execute("INSERT OR IGNORE INTO techs (username, display_name, role) VALUES (?, ?, 'tech')", (u, d))

    types = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM device_types")}
    people = []
    for i, (name, dept, role, phone) in enumerate(PEOPLE, start=1):
        existing = db.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()
        people.append(existing or create_person(db, name, dept, role, phone, external_id=f"CREW{i:03d}"))

    tech = {"tech_id": 1, "site_id": db.execute("SELECT MIN(id) FROM sites").fetchone()[0]}
    devices = []

    def mk(si, tname, label, action="imported"):
        if db.execute("SELECT 1 FROM devices WHERE si = ?", (si,)).fetchone():
            return
        devices.append(quick_create_device(db, si, types[tname], label, tech, action=action))

    for n in range(1, 25):
        mk(f"SI-BOL-{1000 + n}", "Bolero Beltpack", f"Bolero {60 + n}")
    for n in range(1, 17):
        mk(f"SI-RAD-{2000 + n}", "Radio", f"Radio {n:02d}")
    for n in range(1, 9):
        mk(f"SI-RSM-{3000 + n}", "RSM", f"RSM {n:02d}")
    for n in range(1, 21):
        mk(f"SI-PD2-{4000 + n}", "Pro-D2 Headset", f"Pro-D2 {n:02d}")
    for n in range(1, 13):
        mk(f"SI-EAR-{5000 + n}", "Earpiece", f"Earpiece {n:02d}")
    for n in range(1, 5):
        mk(f"SI-ANT-{6000 + n}", "Bolero Antenna", f"Antenna {n}")

    if not devices:
        return 0
    rnd = random.Random(42)
    by_type: dict[str, list] = {}
    for d in devices:
        by_type.setdefault(d["type_name"], []).append(d)

    # pre-assign a Bolero + headset to the first six people, issue kit to some others
    for p, bol, hs in zip(people[:6], by_type["Bolero Beltpack"][:6], by_type["Pro-D2 Headset"][:6]):
        set_assignment(db, bol["id"], p["id"], tech, note="Show plan")
        set_assignment(db, hs["id"], p["id"], tech, note="Show plan")
    for p, bol, hs in zip(people[6:10], by_type["Bolero Beltpack"][6:10], by_type["Pro-D2 Headset"][6:10]):
        sign_out(db, [bol["id"], hs["id"]], p["id"], tech)
    for p, rad, ear in zip(people[9:12], by_type["Radio"][:3], by_type["Earpiece"][:3]):
        sign_out(db, [rad["id"], ear["id"]], p["id"], tech)
    dmg = by_type["Pro-D2 Headset"][-1]
    db.execute("UPDATE devices SET status = 'damaged' WHERE id = ?", (dmg["id"],))
    log(db, "damaged", dmg["id"], None, tech, reason="Broken boom arm")
    return len(devices)
