"""JSON API used by the kiosk front end (and later by the phone view)."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import current_tech, is_admin
from ..db import get_db, get_setting, now_iso
from ..services import inventory, streams

router = APIRouter(prefix="/api")


def require_tech(request: Request) -> dict:
    tech = current_tech(request)
    if tech is None:
        raise HTTPException(status_code=401, detail="Pick a tech and site first")
    return tech


def _row(r) -> Optional[dict]:
    return dict(r) if r is not None else None


# -- people -------------------------------------------------------------------

@router.get("/people/search")
def people_search(q: str = "", tech: dict = Depends(require_tech)):
    with get_db() as db:
        return {"results": [dict(r) for r in inventory.search_people(db, q)]}


@router.get("/people/{person_id}")
def person_detail(person_id: int, tech: dict = Depends(require_tech)):
    with get_db() as db:
        p = inventory.person_by_id(db, person_id)
        if p is None:
            raise HTTPException(404, "Unknown person")
        return {
            "person": dict(p),
            "holdings": [dict(r) for r in inventory.person_holdings(db, person_id)],
            "assignments": [dict(r) for r in inventory.person_assignments(db, person_id)],
            "recent": [dict(r) for r in inventory.transactions(db, limit=15, person_id=person_id)],
        }


class PersonIn(BaseModel):
    name: str = Field(min_length=1)
    dept: str = ""
    role: str = ""
    phone: str = ""
    external_id: str = ""
    bolero_id: str = ""


@router.post("/people")
def person_create(body: PersonIn, tech: dict = Depends(require_tech)):
    with get_db() as db:
        if body.external_id.strip():
            dup = db.execute("SELECT id FROM people WHERE external_id = ?", (body.external_id.strip(),)).fetchone()
            if dup:
                raise HTTPException(409, "That unique ID is already used")
        p = inventory.create_person(db, body.name, body.dept, body.role, body.phone, body.external_id, bolero_id=body.bolero_id)
        return {"person": dict(p)}


class PersonUpdate(BaseModel):
    name: str | None = None
    dept: str | None = None
    role: str | None = None
    phone: str | None = None
    external_id: str | None = None
    bolero_id: str | None = None


@router.post("/people/{person_id}")
def person_update(person_id: int, body: PersonUpdate, tech: dict = Depends(require_tech)):
    """Quick edit from the kiosk: name, department, role, phone, IDs."""
    with get_db() as db:
        if inventory.person_by_id(db, person_id) is None:
            raise HTTPException(404, "Unknown person")
        if body.name is not None and not body.name.strip():
            raise HTTPException(400, "Name cannot be empty")
        if body.external_id and db.execute("SELECT 1 FROM people WHERE external_id = ? AND id <> ?", (body.external_id.strip(), person_id)).fetchone():
            raise HTTPException(409, "That unique ID is already used")
        p = inventory.update_person(db, person_id, **body.model_dump())
        return {"person": dict(p)}


# -- devices ------------------------------------------------------------------

@router.get("/devices/lookup")
def device_lookup(si: str, tech: dict = Depends(require_tech)):
    with get_db() as db:
        d = inventory.device_by_si(db, si)
        if d is None:
            return {"found": False, "si": inventory.norm_si(si)}
        return {"found": True, "device": dict(d)}


@router.get("/types")
def types(tech: dict = Depends(require_tech)):
    with get_db() as db:
        return {"types": [dict(r) for r in db.execute("SELECT * FROM device_types WHERE active = 1 ORDER BY sort_order, name")]}


class QuickDevice(BaseModel):
    si: str = Field(min_length=1)
    type_id: int
    label: str = ""
    site_specific: bool = False


@router.post("/devices/quick")
def device_quick(body: QuickDevice, tech: dict = Depends(require_tech)):
    with get_db() as db:
        if inventory.device_by_si(db, body.si):
            raise HTTPException(409, "Device already exists")
        d = inventory.quick_create_device(
            db, body.si, body.type_id, body.label, tech, site_id=tech["site_id"] if body.site_specific else None
        )
        return {"device": dict(d)}


class StatusIn(BaseModel):
    status: str
    reason: str = ""
    note: str = ""
    keep_holder: bool = False


@router.post("/devices/{device_id}/status")
def device_status(device_id: int, body: StatusIn, tech: dict = Depends(require_tech)):
    with get_db() as db:
        try:
            return inventory.mark_status(db, device_id, body.status, tech, body.reason, body.note, body.keep_holder)
        except ValueError as e:
            raise HTTPException(400, str(e))


# -- transactions -------------------------------------------------------------

class SignOut(BaseModel):
    person_id: int
    device_ids: list[int]
    force_ids: list[int] = []
    collector_id: int | None = None   # someone picking up on the person's behalf


@router.post("/signout")
def signout(body: SignOut, tech: dict = Depends(require_tech)):
    with get_db() as db:
        try:
            results = inventory.sign_out(db, body.device_ids, body.person_id, tech, set(body.force_ids), collector_id=body.collector_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"results": results, "summary": inventory.summary(db, tech["site_id"])}


class SignIn(BaseModel):
    device_ids: list[int]


@router.post("/signin")
def signin(body: SignIn, tech: dict = Depends(require_tech)):
    with get_db() as db:
        results = inventory.sign_in(db, body.device_ids, tech)
        return {"results": results, "summary": inventory.summary(db, tech["site_id"])}


class SwapIn(BaseModel):
    old_device_id: int
    new_si: str
    reason: str = "damaged"
    note: str = ""
    old_status: str = "damaged"


@router.post("/swap")
def swap(body: SwapIn, tech: dict = Depends(require_tech)):
    with get_db() as db:
        new = inventory.device_by_si(db, body.new_si)
        if new is None:
            raise HTTPException(404, f"Replacement {inventory.norm_si(body.new_si)} is not in the database")
        try:
            return inventory.swap(db, body.old_device_id, new["id"], tech, body.reason, body.note, body.old_status)
        except ValueError as e:
            raise HTTPException(400, str(e))


@router.get("/summary")
def summary(request: Request, scope: str = "site"):
    tech = current_tech(request)
    with get_db() as db:
        site_id = tech["site_id"] if (tech and scope == "site") else None
        return {"site": inventory.summary(db, site_id), "all": inventory.summary(db, None)}


@router.get("/recent")
def recent(request: Request, limit: int = 25, scope: str = "site"):
    tech = current_tech(request)
    with get_db() as db:
        site_id = tech["site_id"] if (tech and scope == "site") else None
        rows = inventory.transactions(db, limit=min(limit, 200), site_id=site_id)
        return {"transactions": [dict(r) for r in rows]}


# -- mirror -------------------------------------------------------------------

class KioskState(BaseModel):
    state: dict


@router.post("/kiosk/state")
def kiosk_state(body: KioskState, tech: dict = Depends(require_tech)):
    with get_db() as db:
        t = db.execute("SELECT display_name FROM techs WHERE id = ?", (tech["tech_id"],)).fetchone()
        body.state["tech_name"] = t["display_name"] if t else None
        db.execute(
            "INSERT INTO kiosk_state (site_id, state_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(site_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at",
            (tech["site_id"], json.dumps(body.state), now_iso()),
        )
    return {"ok": True}


@router.get("/mirror/{site_id}")
def mirror_state(site_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM kiosk_state WHERE site_id = ?", (site_id,)).fetchone()
        event = db.execute("SELECT * FROM event WHERE id = 1").fetchone()
        sayings = [ln.strip() for ln in (get_setting(db, "mirror_sayings") or "").splitlines() if ln.strip()]
        return {
            "state": json.loads(row["state_json"]) if row else {},
            "updated_at": row["updated_at"] if row else None,
            "event": dict(event) if event else {},
            "summary": inventory.summary(db, site_id),
            "greeting": {
                "title": get_setting(db, "mirror_title") or "Welcome to RDR",
                "sayings": sayings,
                "prompt": get_setting(db, "mirror_prompt") or "Please give your name and role to {tech}",
            },
        }


@router.get("/stream/{site_id}/ping")
def stream_ping(site_id: int):
    """The mirror calls this every few seconds while it shows the PGM feed."""
    with get_db() as db:
        site = db.execute("SELECT rtsp_url FROM sites WHERE id = ?", (site_id,)).fetchone()
        transcode = get_setting(db, "stream_transcode") == "1"
    if not site or not site["rtsp_url"]:
        return {"running": False, "error": "No feed address set for this site"}
    kind = streams.classify_feed(site["rtsp_url"])
    if kind not in streams.RELAY_KINDS:
        return {"running": False, "kind": kind, "error": "This feed does not need the relay"}
    return streams.ensure_running(site_id, site["rtsp_url"], transcode or kind != "rtsp")
