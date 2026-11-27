"""Admin area: password protected setup, data management, imports and settings."""
from __future__ import annotations

import csv
import io
from datetime import datetime
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel as _BM

from ..auth import (
    ADMIN_COOKIE, MIN_PASSWORD, access_code_set, admin_password_set, admin_token, check_admin_password, is_admin,
    live_tech, set_access_code, set_admin_password, set_cookie, upgrade_admin_hash,
)
from .. import security
from ..config import DATA_DIR, IMPORT_DIR, UPLOAD_DIR, load_config, save_config
from ..db import ACTION_LABELS, get_db, get_setting, now_iso, reset_db, set_setting
from ..main import lan_addresses, render
from ..services import away, backup, bolero, bolero_names, certs, importer, inventory, quotes, reports, snapshots, streams, updater

router = APIRouter(prefix="/admin")


def _redirect(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    # a "#section" on the path (the settings tabs) has to stay after the query string
    path, _, frag = path.partition("#")
    sep = "&" if "?" in path else "?"
    if msg:
        path += f"{sep}msg={quote(msg)}"
    elif err:
        path += f"{sep}err={quote(err)}"
    if frag:
        path += f"#{frag}"
    return RedirectResponse(path, status_code=303)


def _upload_dir() -> Path:
    """Where the event logo goes: next to the database this request works on (one per event on the hub)."""
    from ..config import is_hub
    from ..db import current_db_path
    folder = UPLOAD_DIR if not is_hub() else current_db_path().parent / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _tech(db, request: Request) -> dict | None:
    """Who to credit an admin edit to: the tech session, if its tech and site still exist."""
    return live_tech(db, request, active=False)


def _after_login(target: str) -> str:
    """Where to go after the password: only ever a page inside Admin."""
    ok = (target.startswith("/admin/") or target == "/hub" or target.startswith("/hub/")) and "//" not in target and "\\" not in target and not target.startswith("/admin/log")
    return target if ok else "/admin"


def require_admin(request: Request) -> None:
    if not is_admin(request):
        # a page link (the kiosk menu goes straight to Stock transfer) comes back to the same page after the password
        where = "/admin/login"
        if request.method == "GET" and _after_login(request.url.path) != "/admin":
            where += "?next=" + quote(request.url.path + ("?" + request.url.query if request.url.query else ""), safe="")
        raise HTTPException(status_code=303, headers={"Location": where})


# -- login --------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login(request: Request, next: str = ""):
    with get_db() as db:
        first_time = not admin_password_set(db)
    target = _after_login(next)
    return render(request, "admin/login.html", first_time=first_time, next=target if target != "/admin" else "",
                  setup_blocked=first_time and security.is_remote(request), min_password=MIN_PASSWORD)


ADMIN_SESSION = 60 * 60 * 12


@router.post("/login")
def login_post(request: Request, password: str = Form(""), confirm: str = Form(""), next: str = Form("")):
    target = _after_login(next)
    back = "/admin/login" + (f"?next={quote(target, safe='')}" if target != "/admin" else "")
    origin = security.origin_from(request)
    with get_db() as db:
        if not admin_password_set(db):
            # whoever sets the first password owns the server, so that has to happen in the room
            if origin.remote:
                return _redirect(back, err="Set the admin password from a computer on the local network")
            if len(password) < MIN_PASSWORD:
                return _redirect(back, err=f"Password needs at least {MIN_PASSWORD} characters")
            if password != confirm:
                return _redirect(back, err="Passwords do not match")
            set_admin_password(db, password)
        else:
            wait = security.limiter.wait("admin password", origin)
            if wait:
                return _redirect(back, err=security.wait_text(wait))
            if not check_admin_password(db, password):
                security.limiter.fail("admin password", origin)
                return _redirect(back, err="Wrong password")
            security.limiter.ok("admin password", origin)
            upgrade_admin_hash(db, password)
        token = admin_token(db)
    resp = RedirectResponse(target, status_code=303)
    set_cookie(resp, request, ADMIN_COOKIE, token, ADMIN_SESSION)
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


# -- dashboard ----------------------------------------------------------------

@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def dashboard(request: Request):
    with get_db() as db:
        overall = inventory.summary(db)
        sites = db.execute("SELECT * FROM sites WHERE active = 1 ORDER BY sort_order, name").fetchall()
        per_site = [(dict(s), inventory.summary(db, s["id"])) for s in sites]
        recent = inventory.transactions(db, limit=20)
        aways = away.all_sites(db)
        counts = {
            "people": db.execute("SELECT COUNT(*) FROM people").fetchone()[0],
            "devices": db.execute("SELECT COUNT(*) FROM devices").fetchone()[0],
            "bolero": db.execute("SELECT COUNT(*) FROM people WHERE bolero_id <> ''").fetchone()[0],
            "holders": db.execute("SELECT COUNT(DISTINCT holder_id) FROM devices WHERE holder_id IS NOT NULL").fetchone()[0],
        }
    return render(request, "admin/dashboard.html", overall=overall, per_site=per_site, recent=recent, counts=counts, active="dashboard", aways=aways)


# -- event / sites / techs ----------------------------------------------------

@router.get("/event", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def event_page(request: Request):
    with get_db() as db:
        all_sites = db.execute("SELECT * FROM sites ORDER BY active DESC, sort_order, name").fetchall()
        techs = db.execute("SELECT t.*, s.name AS site_name FROM techs t LEFT JOIN sites s ON s.id = t.site_id ORDER BY t.active DESC, t.display_name").fetchall()
    return render(request, "admin/event.html", all_sites=all_sites, techs=techs, active="event")


@router.post("/event", dependencies=[Depends(require_admin)])
async def event_save(
    name: str = Form(...), show_name: str = Form(""), starts_on: str = Form(""), ends_on: str = Form(""),
    logo: UploadFile | None = File(None), remove_logo: str = Form(""),
):
    with get_db() as db:
        ev = db.execute("SELECT logo_path FROM event WHERE id = 1").fetchone()
        logo_path = ev["logo_path"]
        if remove_logo:
            logo_path = None
        if logo is not None and logo.filename:
            ext = Path(logo.filename).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"):
                return _redirect("/admin/event", err="Logo must be PNG, JPG, SVG, WEBP or GIF")
            folder = _upload_dir()
            dest = folder / f"event-logo{ext}"
            for old in folder.glob("event-logo.*"):
                old.unlink(missing_ok=True)
            with dest.open("wb") as fh:
                shutil.copyfileobj(logo.file, fh)
            logo_path = f"/uploads/{dest.name}?v={int(dest.stat().st_mtime)}"
        db.execute(
            "UPDATE event SET name = ?, show_name = ?, starts_on = ?, ends_on = ?, logo_path = ?, updated_at = ? WHERE id = 1",
            (name.strip(), show_name.strip(), starts_on, ends_on, logo_path, now_iso()),
        )
    return _redirect("/admin/event", msg="Event saved")


@router.post("/sites", dependencies=[Depends(require_admin)])
def site_add(name: str = Form(...), stock_mode: str = Form("shared"), rtsp_url: str = Form("")):
    with get_db() as db:
        try:
            db.execute("INSERT INTO sites (name, stock_mode, rtsp_url) VALUES (?,?,?)", (name.strip(), stock_mode, rtsp_url.strip() or None))
        except Exception:
            return _redirect("/admin/event", err="A site with that name already exists")
    return _redirect("/admin/event", msg="Site added")


@router.post("/sites/{site_id}", dependencies=[Depends(require_admin)])
def site_edit(site_id: int, action: str = Form("save"), name: str = Form(""), stock_mode: str = Form("shared"), rtsp_url: str = Form(""), active: str = Form("")):
    with get_db() as db:
        if action == "delete":
            n = db.execute("SELECT COUNT(*) FROM sites WHERE active = 1").fetchone()[0]
            if n <= 1:
                return _redirect("/admin/event", err="You need at least one site")
            db.execute("UPDATE devices SET site_id = NULL WHERE site_id = ?", (site_id,))
            db.execute("UPDATE techs SET site_id = NULL WHERE site_id = ?", (site_id,))
            db.execute("DELETE FROM kiosk_state WHERE site_id = ?", (site_id,))
            db.execute("DELETE FROM sites WHERE id = ?", (site_id,))
            return _redirect("/admin/event", msg="Site deleted")
        db.execute(
            "UPDATE sites SET name = ?, stock_mode = ?, rtsp_url = ?, active = ? WHERE id = ?",
            (name.strip(), stock_mode, rtsp_url.strip() or None, 1 if active else 0, site_id),
        )
    return _redirect("/admin/event", msg="Site saved")


@router.post("/techs", dependencies=[Depends(require_admin)])
def tech_add(username: str = Form(...), display_name: str = Form(""), role: str = Form("tech"), site_id: str = Form("")):
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO techs (username, display_name, role, site_id) VALUES (?,?,?,?)",
                (username.strip().lower(), display_name.strip() or username.strip(), role, int(site_id) if site_id else None),
            )
        except Exception:
            return _redirect("/admin/event", err="That username already exists")
    return _redirect("/admin/event", msg="Tech added")


@router.post("/techs/{tech_id}", dependencies=[Depends(require_admin)])
def tech_edit(tech_id: int, action: str = Form("save"), username: str = Form(""), display_name: str = Form(""), role: str = Form("tech"), site_id: str = Form(""), active: str = Form("")):
    with get_db() as db:
        if action == "delete":
            db.execute("DELETE FROM techs WHERE id = ?", (tech_id,))
            return _redirect("/admin/event", msg="Tech deleted")
        db.execute(
            "UPDATE techs SET username = ?, display_name = ?, role = ?, site_id = ?, active = ? WHERE id = ?",
            (username.strip().lower(), display_name.strip() or username.strip(), role, int(site_id) if site_id else None, 1 if active else 0, tech_id),
        )
    return _redirect("/admin/event", msg="Tech saved")


# -- device types -------------------------------------------------------------

@router.get("/types", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def types_page(request: Request):
    with get_db() as db:
        rows = db.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM devices d WHERE d.type_id = t.id) AS n FROM device_types t ORDER BY t.active DESC, t.sort_order, t.name"
        ).fetchall()
    return render(request, "admin/types.html", types=rows, active="types")


@router.post("/types", dependencies=[Depends(require_admin)])
def type_add(name: str = Form(...), short_code: str = Form(""), sort_order: int = Form(100)):
    with get_db() as db:
        try:
            db.execute("INSERT INTO device_types (name, short_code, sort_order) VALUES (?,?,?)", (name.strip(), short_code.strip().upper(), sort_order))
        except Exception:
            return _redirect("/admin/types", err="That type already exists")
    return _redirect("/admin/types", msg="Type added")


@router.post("/types/{type_id}", dependencies=[Depends(require_admin)])
def type_edit(type_id: int, action: str = Form("save"), name: str = Form(""), short_code: str = Form(""), sort_order: int = Form(100), active: str = Form("")):
    with get_db() as db:
        if action == "delete":
            n = db.execute("SELECT COUNT(*) FROM devices WHERE type_id = ?", (type_id,)).fetchone()[0]
            if n:
                return _redirect("/admin/types", err=f"Cannot delete: {n} devices use this type. Deactivate it instead.")
            db.execute("DELETE FROM device_types WHERE id = ?", (type_id,))
            return _redirect("/admin/types", msg="Type deleted")
        db.execute("UPDATE device_types SET name = ?, short_code = ?, sort_order = ?, active = ? WHERE id = ?",
                   (name.strip(), short_code.strip().upper(), sort_order, 1 if active else 0, type_id))
    return _redirect("/admin/types", msg="Type saved")


# -- people -------------------------------------------------------------------

@router.get("/people", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def people_page(request: Request, q: str = "", holding: str = ""):
    with get_db() as db:
        like = f"%{q}%"
        where = "WHERE (p.name LIKE ? OR p.dept LIKE ? OR p.role LIKE ? OR p.phone LIKE ? OR p.external_id LIKE ? OR p.bolero_id LIKE ?)"
        params = [like] * 6
        if holding:
            where += " AND held > 0"
        rows = db.execute(
            f"""SELECT * FROM (
                  SELECT p.*, (SELECT COUNT(*) FROM devices d WHERE d.holder_id = p.id) AS held,
                         (SELECT COUNT(*) FROM assignments a WHERE a.person_id = p.id) AS assigned
                  FROM people p) p {where} ORDER BY p.name LIMIT 500""",
            params,
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    return render(request, "admin/people.html", people=rows, q=q, holding=holding, total=total, active="people")


@router.get("/people/new", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def person_new(request: Request):
    return render(request, "admin/person_form.html", person=None, active="people")


@router.post("/people/new", dependencies=[Depends(require_admin)])
def person_create(name: str = Form(...), dept: str = Form(""), role: str = Form(""), phone: str = Form(""), external_id: str = Form(""), bolero_id: str = Form(""), notes: str = Form(""), faceplate: str = Form(""), allocation: str = Form("")):
    with get_db() as db:
        if external_id.strip() and db.execute("SELECT 1 FROM people WHERE external_id = ?", (external_id.strip(),)).fetchone():
            return _redirect("/admin/people/new", err="That unique ID is already used")
        p = inventory.create_person(db, name, dept, role, phone, external_id, notes, bolero_id=bolero_id, faceplate=faceplate if faceplate != "none" else "", allocation=allocation)
    return _redirect(f"/admin/people/{p['id']}", msg="Person created")


@router.get("/people/{person_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def person_page(request: Request, person_id: int):
    with get_db() as db:
        p = inventory.person_by_id(db, person_id)
        if p is None:
            raise HTTPException(404)
        holdings = inventory.person_holdings(db, person_id)
        assignments = inventory.person_assignments(db, person_id)
        history = inventory.transactions(db, limit=200, person_id=person_id)
    return render(request, "admin/person_form.html", person=p, holdings=holdings, assignments=assignments, history=history, active="people")


@router.post("/people/{person_id}", dependencies=[Depends(require_admin)])
def person_save(person_id: int, action: str = Form("save"), name: str = Form(""), dept: str = Form(""), role: str = Form(""), phone: str = Form(""), external_id: str = Form(""), bolero_id: str = Form(""), notes: str = Form(""), faceplate: str = Form(""), allocation: str = Form("")):
    with get_db() as db:
        if action == "delete":
            held = db.execute("SELECT COUNT(*) FROM devices WHERE holder_id = ?", (person_id,)).fetchone()[0]
            if held:
                return _redirect(f"/admin/people/{person_id}", err="This person still holds devices. Sign them in first.")
            db.execute("DELETE FROM people WHERE id = ?", (person_id,))
            return _redirect("/admin/people", msg="Person deleted")
        if external_id.strip() and db.execute("SELECT 1 FROM people WHERE external_id = ? AND id <> ?", (external_id.strip(), person_id)).fetchone():
            return _redirect(f"/admin/people/{person_id}", err="That unique ID is already used by someone else")
        db.execute(
            "UPDATE people SET name = ?, dept = ?, role = ?, phone = ?, external_id = ?, bolero_id = ?, faceplate = ?, allocation = ?, notes = ?, updated_at = ? WHERE id = ?",
            (name.strip(), dept.strip(), role.strip(), phone.strip(), external_id.strip() or None, bolero_id.strip(),
             inventory.clean_hex(faceplate), allocation.strip(), notes.strip(), now_iso(), person_id),
        )
        inventory.link_reserved_packs(db, person_id)
    return _redirect(f"/admin/people/{person_id}", msg="Saved")


@router.get("/people/{person_id}/report", dependencies=[Depends(require_admin)])
def person_report(request: Request, person_id: int, format: str = "html"):
    with get_db() as db:
        p = inventory.person_by_id(db, person_id)
        if p is None:
            raise HTTPException(404)
        holdings = inventory.person_holdings(db, person_id)
        history = inventory.transactions(db, limit=5000, person_id=person_id)
        event = db.execute("SELECT * FROM event WHERE id = 1").fetchone()
    if format == "csv":
        return _csv_response(f"report-{p['name']}.csv", history)
    return render(request, "admin/person_report.html", person=p, holdings=holdings, history=history, ev=event)


# -- devices ------------------------------------------------------------------

@router.get("/devices", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def devices_page(request: Request, q: str = "", type_id: str = "", status: str = "", site_id: str = ""):
    clauses, params = [], []
    if q:
        clauses.append("(d.si LIKE ? OR d.label LIKE ? OR p.name LIKE ?)"); params += [f"%{q}%"] * 3
    if type_id:
        clauses.append("d.type_id = ?"); params.append(int(type_id))
    if status:
        clauses.append("d.status = ?"); params.append(status)
    if site_id == "shared":
        clauses.append("d.site_id IS NULL")
    elif site_id:
        clauses.append("d.site_id = ?"); params.append(int(site_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as db:
        rows = db.execute(inventory.DEVICE_SELECT + f" {where} ORDER BY t.sort_order, d.label, d.si LIMIT 1000", params).fetchall()
        types = db.execute("SELECT * FROM device_types ORDER BY sort_order, name").fetchall()
        total = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    return render(request, "admin/devices.html", devices=rows, types=types, q=q, f_type=type_id, f_status=status, f_site=site_id, total=total, active="devices")


@router.get("/devices/new", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def device_new(request: Request):
    with get_db() as db:
        types = db.execute("SELECT * FROM device_types WHERE active = 1 ORDER BY sort_order, name").fetchall()
    return render(request, "admin/device_form.html", device=None, types=types, active="devices")


@router.post("/devices/new", dependencies=[Depends(require_admin)])
def device_create(request: Request, si: str = Form(...), type_id: int = Form(...), label: str = Form(""), site_id: str = Form(""), notes: str = Form(""), linked_id: str = Form("")):
    with get_db() as db:
        if inventory.device_by_si(db, si):
            return _redirect("/admin/devices/new", err=f"SI {inventory.norm_si(si)} already exists")
        d = inventory.quick_create_device(db, si, type_id, label, _tech(db, request), int(site_id) if site_id else None, notes, linked_id=linked_id)
    return _redirect(f"/admin/devices/{d['id']}", msg="Device created")


@router.post("/devices/bulk", dependencies=[Depends(require_admin)])
def device_bulk(request: Request, type_id: int = Form(...), site_id: str = Form(""), lines: str = Form("")):
    """Paste a list: one device per line as 'SI' or 'SI, label'."""
    created = skipped = 0
    with get_db() as db:
        for raw in lines.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.replace("\t", ",").split(",", 1)]
            si, label = parts[0], (parts[1] if len(parts) > 1 else "")
            if inventory.device_by_si(db, si):
                skipped += 1
                continue
            inventory.quick_create_device(db, si, type_id, label, _tech(db, request), int(site_id) if site_id else None)
            created += 1
    return _redirect("/admin/devices", msg=f"Created {created} devices, skipped {skipped} existing")


@router.get("/devices/{device_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def device_page(request: Request, device_id: int):
    with get_db() as db:
        d = inventory.device_by_id(db, device_id)
        if d is None:
            raise HTTPException(404)
        types = db.execute("SELECT * FROM device_types ORDER BY sort_order, name").fetchall()
        history = inventory.transactions(db, limit=300, device_id=device_id)
    return render(request, "admin/device_form.html", device=d, types=types, history=history, active="devices")


@router.post("/devices/{device_id}", dependencies=[Depends(require_admin)])
def device_save(request: Request, device_id: int, action: str = Form("save"), si: str = Form(""), type_id: int = Form(0), label: str = Form(""), site_id: str = Form(""), notes: str = Form(""), status: str = Form(""), reason: str = Form(""), assign_person_id: str = Form(""), linked_id: str = Form("")):
    with get_db() as db:
        tech = _tech(db, request)
        if action == "delete":
            db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            return _redirect("/admin/devices", msg="Device deleted")
        if action == "status":
            try:
                inventory.mark_status(db, device_id, status, tech, reason=reason)
            except ValueError as e:
                return _redirect(f"/admin/devices/{device_id}", err=str(e))
            return _redirect(f"/admin/devices/{device_id}", msg="Status updated")
        if action == "assign":
            if assign_person_id:
                inventory.set_assignment(db, device_id, int(assign_person_id), tech)
                return _redirect(f"/admin/devices/{device_id}", msg="Device reserved")
            inventory.clear_assignment(db, device_id, tech)
            return _redirect(f"/admin/devices/{device_id}", msg="Reservation removed")
        if inventory.device_by_id(db, device_id) is None:
            raise HTTPException(404)
        if not inventory.norm_si(si):
            return _redirect(f"/admin/devices/{device_id}", err="The SI number cannot be empty")
        dup = db.execute("SELECT id FROM devices WHERE si = ? COLLATE NOCASE AND id <> ?", (inventory.norm_si(si), device_id)).fetchone()
        if dup:
            return _redirect(f"/admin/devices/{device_id}", err="Another device already has that SI")
        db.execute(
            "UPDATE devices SET si = ?, type_id = ?, label = ?, notes = ?, linked_id = ?, updated_at = ? WHERE id = ?",
            (inventory.norm_si(si), type_id, label.strip(), notes.strip(), linked_id.strip(), now_iso(), device_id),
        )
        try:
            inventory.transfer(db, device_id, int(site_id) if site_id else None, tech)
        except ValueError as e:
            return _redirect(f"/admin/devices/{device_id}", err=str(e))
    return _redirect(f"/admin/devices/{device_id}", msg="Saved")


# -- stock transfer -----------------------------------------------------------

class TransferScan(_BM):
    si: str
    site_id: str = ""


@router.get("/transfer", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def transfer_page(request: Request, to: str = ""):
    with get_db() as db:
        tech = _tech(db, request)
    # where the scans go by default: the site asked for, else the site this tech is working at
    dest = to if (to.isdigit() or to == "shared") else (str(tech["site_id"]) if tech else "")
    return render(request, "admin/transfer.html", dest=dest, active="transfer")


@router.post("/transfer/scan", dependencies=[Depends(require_admin)])
def transfer_scan(request: Request, body: TransferScan):
    """One scan moves one device. The SI has to match, or be the unmistakable end of exactly one SI."""
    si = inventory.norm_si(body.si)
    if body.site_id and not body.site_id.isdigit():
        raise HTTPException(400, "Unknown site")
    with get_db() as db:
        d = inventory.device_by_si(db, si)
        if d is None and len(si) >= 3:
            hits = db.execute("SELECT id FROM devices WHERE si LIKE ? LIMIT 2", (f"%{si}",)).fetchall()
            if len(hits) == 1:
                d = inventory.device_by_id(db, hits[0]["id"])
        if d is None:
            return {"outcome": "unknown", "si": si}
        try:
            return inventory.transfer(db, d["id"], int(body.site_id) if body.site_id else None, _tech(db, request))
        except ValueError as e:
            raise HTTPException(400, str(e))


# -- assignments --------------------------------------------------------------

@router.get("/assignments", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def assignments_page(request: Request, q: str = ""):
    with get_db() as db:
        like = f"%{q}%"
        rows = db.execute(
            inventory.DEVICE_SELECT + " WHERE a.id IS NOT NULL AND (d.si LIKE ? OR d.label LIKE ? OR ap.name LIKE ?) ORDER BY ap.name, t.sort_order, d.label",
            (like, like, like),
        ).fetchall()
    return render(request, "admin/assignments.html", rows=rows, q=q, active="assignments")


@router.post("/assignments", dependencies=[Depends(require_admin)])
def assignment_add(request: Request, si: str = Form(...), person_id: int = Form(...), note: str = Form("")):
    with get_db() as db:
        d = inventory.device_by_si(db, si)
        if d is None:
            return _redirect("/admin/assignments", err=f"Unknown device {inventory.norm_si(si)}")
        if inventory.person_by_id(db, person_id) is None:
            return _redirect("/admin/assignments", err="Unknown person")
        inventory.set_assignment(db, d["id"], person_id, _tech(db, request), note)
    return _redirect("/admin/assignments", msg=f"{d['si']} reserved")


@router.post("/assignments/{device_id}/delete", dependencies=[Depends(require_admin)])
def assignment_delete(request: Request, device_id: int):
    with get_db() as db:
        inventory.clear_assignment(db, device_id, _tech(db, request))
    return _redirect("/admin/assignments", msg="Removed")


@router.post("/assignments/clear", dependencies=[Depends(require_admin)])
def assignments_clear(request: Request, confirm: str = Form("")):
    if confirm != "CLEAR":
        return _redirect("/admin/assignments", err="Type CLEAR to confirm")
    with get_db() as db:
        db.execute("DELETE FROM assignments")
    return _redirect("/admin/assignments", msg="All reservations removed")


# -- direct edit (inline cells) --------------------------------------------------

class FieldEdit(_BM):
    field: str
    value: str = ""


PEOPLE_FIELDS = {"name", "dept", "role", "phone", "external_id", "bolero_id", "allocation"}
DEVICE_FIELDS = {"label", "si", "type_id", "site_id", "linked_id", "notes"}
TYPE_FIELDS = {"name", "short_code", "sort_order", "active"}


@router.post("/people/{person_id}/field", dependencies=[Depends(require_admin)])
def person_field(person_id: int, body: FieldEdit):
    if body.field not in PEOPLE_FIELDS:
        raise HTTPException(400, "Field not editable")
    v = body.value.strip()
    with get_db() as db:
        if inventory.person_by_id(db, person_id) is None:
            raise HTTPException(404, "Unknown person")
        if body.field == "name" and not v:
            raise HTTPException(400, "Name cannot be empty")
        if body.field == "external_id" and v and db.execute("SELECT 1 FROM people WHERE external_id = ? AND id <> ?", (v, person_id)).fetchone():
            raise HTTPException(409, "That unique ID is already used")
        inventory.update_person(db, person_id, **{body.field: v})
    return {"ok": True, "value": v}


@router.post("/devices/{device_id}/field", dependencies=[Depends(require_admin)])
def device_field(request: Request, device_id: int, body: FieldEdit):
    if body.field not in DEVICE_FIELDS:
        raise HTTPException(400, "Field not editable")
    v = body.value.strip()
    with get_db() as db:
        if inventory.device_by_id(db, device_id) is None:
            raise HTTPException(404, "Unknown device")
        if body.field == "si":
            v = inventory.norm_si(v)
            if not v:
                raise HTTPException(400, "The SI number cannot be empty")
            if db.execute("SELECT 1 FROM devices WHERE si = ? COLLATE NOCASE AND id <> ?", (v, device_id)).fetchone():
                raise HTTPException(409, "Another device already has that SI")
        elif body.field == "type_id":
            if not v.isdigit() or db.execute("SELECT 1 FROM device_types WHERE id = ?", (int(v),)).fetchone() is None:
                raise HTTPException(400, "Unknown type")
        elif body.field == "site_id":
            if v and not v.isdigit():
                raise HTTPException(400, "Unknown site")
            try:
                inventory.transfer(db, device_id, int(v) if v else None, _tech(db, request))
            except ValueError as e:
                raise HTTPException(400, str(e))
            return {"ok": True, "value": v}
        db.execute(f"UPDATE devices SET {body.field} = ?, updated_at = ? WHERE id = ?", (int(v) if body.field == "type_id" else v, now_iso(), device_id))
    return {"ok": True, "value": v}


@router.post("/types/{type_id}/field", dependencies=[Depends(require_admin)])
def type_field(type_id: int, body: FieldEdit):
    if body.field not in TYPE_FIELDS:
        raise HTTPException(400, "Field not editable")
    v = body.value.strip()
    with get_db() as db:
        if db.execute("SELECT 1 FROM device_types WHERE id = ?", (type_id,)).fetchone() is None:
            raise HTTPException(404, "Unknown type")
        if body.field == "name":
            if not v:
                raise HTTPException(400, "Name cannot be empty")
            if db.execute("SELECT 1 FROM device_types WHERE name = ? COLLATE NOCASE AND id <> ?", (v, type_id)).fetchone():
                raise HTTPException(409, "That type already exists")
            db.execute("UPDATE device_types SET name = ? WHERE id = ?", (v, type_id))
        elif body.field == "short_code":
            db.execute("UPDATE device_types SET short_code = ? WHERE id = ?", (v.upper(), type_id)); v = v.upper()
        elif body.field == "sort_order":
            if not v.lstrip("-").isdigit():
                raise HTTPException(400, "Order must be a number")
            db.execute("UPDATE device_types SET sort_order = ? WHERE id = ?", (int(v), type_id))
        elif body.field == "active":
            db.execute("UPDATE device_types SET active = ? WHERE id = ?", (1 if v in ("1", "true", "on") else 0, type_id))
    return {"ok": True, "value": v}


@router.get("/people-search", dependencies=[Depends(require_admin)])
def admin_people_search(q: str = ""):
    with get_db() as db:
        return {"results": [dict(r) for r in inventory.search_people(db, q)]}


# -- transactions -------------------------------------------------------------

def _csv_response(filename: str, rows) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Timestamp", "Action", "SI", "Label", "Type", "Person", "Dept", "Tech", "Site", "Reason", "Note", "Related SI"])
    for r in rows:
        w.writerow([r["ts"], ACTION_LABELS.get(r["action"], r["action"]), r["si"], r["label"], r["type_name"],
                    r["person_name"], r["person_dept"], r["tech_name"], r["site_name"], r["reason"], r["note"], r["related_si"]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/transactions", dependencies=[Depends(require_admin)])
def transactions_page(request: Request, q: str = "", action: str = "", site_id: str = "", date_from: str = "", date_to: str = "", format: str = "html", page: int = 1):
    limit = 100
    with get_db() as db:
        rows = inventory.transactions(
            db, limit=(5000 if format == "csv" else limit), offset=(0 if format == "csv" else (page - 1) * limit),
            q=q or None, action=action or None, site_id=int(site_id) if site_id else None,
            date_from=date_from or None, date_to=date_to or None,
        )
    if format == "csv":
        return _csv_response("rdr-transactions.csv", rows)
    return render(request, "admin/transactions.html", rows=rows, q=q, f_action=action, f_site=site_id, date_from=date_from, date_to=date_to, page=page, limit=limit, active="transactions")


# -- imports ------------------------------------------------------------------

@router.get("/import", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def import_home(request: Request):
    return render(request, "admin/import.html", kinds=importer.KINDS, active="import")


@router.post("/import/{kind}", dependencies=[Depends(require_admin)])
async def import_upload(kind: str, file: UploadFile = File(...)):
    if kind not in importer.KINDS:
        raise HTTPException(404)
    content = await file.read()
    try:
        staged = importer.stage_upload(kind, file.filename or "upload", content)
    except ValueError as e:
        return _redirect("/admin/import", err=str(e))
    return _redirect(f"/admin/import/s/{staged['token']}")


@router.get("/import/s/{token}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def import_mapping(request: Request, token: str):
    try:
        staged = importer.load_staged(token)
    except FileNotFoundError:
        return _redirect("/admin/import", err="That import session has expired")
    kind = importer.KINDS[staged["kind"]]
    return render(request, "admin/import_map.html", staged=staged, kind=kind, preview=staged["rows"][:8], preview_fills=staged.get("row_fills", [])[:8],
                  header_options=[(i, " | ".join(c for c in r if c)[:90] or "(blank)") for i, r in enumerate(staged.get("raw", [])[:6])], active="import")


@router.post("/import/s/{token}", dependencies=[Depends(require_admin)])
async def import_mapping_post(request: Request, token: str):
    try:
        staged = importer.load_staged(token)
    except FileNotFoundError:
        return _redirect("/admin/import", err="That import session has expired")
    form = await request.form()
    if form.get("action") == "header":
        importer.set_header_row(staged, int(form.get("header_row", staged.get("header_row", 0)) or 0))
        return _redirect(f"/admin/import/s/{token}")
    mapping = {}
    for field, _ in importer.KINDS[staged["kind"]]["fields"]:
        v = form.get(f"map_{field}", "")
        mapping[field] = int(v) if str(v).isdigit() else None
    staged["mapping"] = mapping
    missing = importer.check_required(staged["kind"], mapping)
    if missing:
        importer.save_staged(staged)
        return _redirect(f"/admin/import/s/{token}", err="Please map: " + ", ".join(missing))
    with get_db() as db:
        importer.analyze(db, staged)
    return _redirect(f"/admin/import/s/{token}/review")


@router.get("/import/s/{token}/review", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def import_review(request: Request, token: str):
    try:
        staged = importer.load_staged(token)
    except FileNotFoundError:
        return _redirect("/admin/import", err="That import session has expired")
    if not staged.get("plan"):
        return _redirect(f"/admin/import/s/{token}")
    items = staged["plan"]["items"]
    return render(
        request, "admin/import_review.html", staged=staged, kind=importer.KINDS[staged["kind"]],
        counts=staged["plan"]["counts"],
        conflicts=[i for i in items if i["action"] == "conflict"],
        errors=[i for i in items if i["action"] in ("error", "skip")],
        sample=[i for i in items if i["action"] in ("create", "update")][:15],
        active="import",
    )


@router.post("/import/s/{token}/commit", dependencies=[Depends(require_admin)])
async def import_commit(request: Request, token: str):
    try:
        staged = importer.load_staged(token)
    except FileNotFoundError:
        return _redirect("/admin/import", err="That import session has expired")
    form = await request.form()
    if form.get("action") == "discard":
        importer.discard(token)
        return _redirect("/admin/import", msg="Import discarded")
    decisions = {k[4:]: str(v) for k, v in form.multi_items() if k.startswith("dec_")}
    with get_db() as db:
        result = importer.commit(db, staged, decisions, _tech(db, request))
    parts = [f"{v} {k.replace('_', ' ')}" for k, v in result.items()]
    target = {"people": "/admin/people", "devices": "/admin/devices"}[staged["kind"]]
    return _redirect(target, msg="Import done: " + ", ".join(parts))


# -- settings -----------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def settings_page(request: Request, fetch: int = 0):
    info = updater.status(fetch=bool(fetch))
    with get_db() as db:
        mirror = {
            "title": get_setting(db, "mirror_title") or "",
            "prompt": get_setting(db, "mirror_prompt") or "",
            "sayings": get_setting(db, "mirror_sayings") or "",
        }
        mirror["transcode"] = get_setting(db, "stream_transcode") == "1"
        mirror["pgm_size"] = get_setting(db, "pgm_size") or "half"
        mirror["quotes"] = quotes.MOVIE_QUOTES
        mirror["quotes_on"] = quotes.enabled(get_setting(db, "movie_quotes"))
    cfg = load_config()
    https = {"on": bool(cfg.get("https")), "port": cfg.get("https_port", 8443), "cert": certs.status(),
             "listening": _port_open(int(cfg.get("https_port", 8443))) if cfg.get("https") else None, "log": _tail(DATA_DIR / "https.log")}
    devices_out = streams.list_capture_devices() if request.query_params.get("devices") else None
    origin = security.origin_from(request)
    remote = {"on": bool(cfg.get("remote_access")), "code_set": access_code_set(cfg), "scope": cfg.get("access_scope", "all"),
              "proxies": "\n".join(cfg.get("trusted_proxies") or []), "hosts": "\n".join(cfg.get("public_hosts") or []),
              "local_hosts": "\n".join(cfg.get("local_hosts") or []),
              "admin": bool(cfg.get("remote_admin")), "how": origin.describe(), "here_remote": origin.remote,
              "https": origin.https, "min": MIN_PASSWORD}
    return render(request, "admin/settings.html", info=info, update_log=updater.last_output(), lan=lan_addresses(), mirror=mirror,
                  https=https, streams=streams.status(), capture_devices=devices_out, remote=remote, active="settings")


@router.post("/settings/password", dependencies=[Depends(require_admin)])
def settings_password(request: Request, current: str = Form(""), password: str = Form(""), confirm: str = Form("")):
    origin = security.origin_from(request)
    with get_db() as db:
        wait = security.limiter.wait("admin password", origin)
        if wait:
            return _redirect("/admin/settings#password", err=security.wait_text(wait))
        if not check_admin_password(db, current):
            security.limiter.fail("admin password", origin)
            return _redirect("/admin/settings#password", err="Current password is wrong")
        if len(password) < MIN_PASSWORD or password != confirm:
            return _redirect("/admin/settings#password", err=f"New passwords must match and be at least {MIN_PASSWORD} characters")
        set_admin_password(db, password)
        token = admin_token(db)
    # every other browser's admin session ends with the old password; this one carries on
    resp = _redirect("/admin/settings#password", msg="Password changed. Other browsers have to sign in again.")
    set_cookie(resp, request, ADMIN_COOKIE, token, ADMIN_SESSION)
    return resp


@router.post("/settings/remote", dependencies=[Depends(require_admin)])
def settings_remote(request: Request, remote_access: str = Form(""), access_scope: str = Form("all"),
                    trusted_proxies: str = Form(""), public_hosts: str = Form(""), local_hosts: str = Form(""),
                    remote_admin: str = Form("")):
    proxies, bad = security.clean_proxies(trusted_proxies)
    if bad:
        return _redirect("/admin/settings#remote", err="Not a usable proxy address: " + ", ".join(bad[:3]))
    cfg = load_config()
    if remote_access and not cfg.get("access_code_hash"):
        return _redirect("/admin/settings#remote", err="Set an access code before turning remote access on")
    origin = security.origin_from(request)
    if origin.remote and (not remote_access or not remote_admin):
        # saving this from outside would lock the person saving it out halfway through
        return _redirect("/admin/settings#remote", err="Turn remote access or remote Admin off from the local network")
    cfg["remote_access"] = bool(remote_access)
    cfg["remote_admin"] = bool(remote_admin)
    cfg["access_scope"] = "proxy" if access_scope == "proxy" else "all"
    cfg["trusted_proxies"] = proxies
    cfg["public_hosts"] = security.clean_hosts(public_hosts)
    cfg["local_hosts"] = [h for h in security.clean_hosts(local_hosts) if h not in cfg["public_hosts"]]
    save_config(cfg)
    note = ""
    if cfg["access_scope"] == "proxy" and not proxies:
        note = " Every browser needs the code until a proxy address is listed."
    return _redirect("/admin/settings#remote", msg="Remote access saved." + note)


@router.post("/settings/access-code", dependencies=[Depends(require_admin)])
def settings_access_code(request: Request, code: str = Form(""), confirm: str = Form(""), clear: str = Form("")):
    if clear:
        if security.is_remote(request):
            return _redirect("/admin/settings#remote", err="Remove the access code from the local network")
        set_access_code(None)
        cfg = load_config()
        cfg["remote_access"] = False
        save_config(cfg)
        return _redirect("/admin/settings#remote", msg="Access code removed. Remote access is off.")
    if len(code) < MIN_PASSWORD or code != confirm:
        return _redirect("/admin/settings#remote", err=f"Codes must match and be at least {MIN_PASSWORD} characters")
    set_access_code(code)
    from ..auth import ACCESS_COOKIE, ACCESS_DAYS, access_token
    resp = _redirect("/admin/settings#remote", msg="Access code saved. Other browsers have to enter the new code.")
    set_cookie(resp, request, ACCESS_COOKIE, access_token(), ACCESS_DAYS * 86400)   # this browser keeps going
    return resp


@router.post("/settings/server", dependencies=[Depends(require_admin)])
def settings_server(port: int = Form(8080), open_browser: str = Form("")):
    cfg = load_config()
    cfg["port"] = max(1, min(65535, port))
    cfg["open_browser"] = bool(open_browser)
    save_config(cfg)
    return _redirect("/admin/settings#server", msg="Server settings saved. They apply after a restart.")


@router.post("/settings/update", dependencies=[Depends(require_admin)])
def settings_update():
    report = updater.update(restart=True)
    if report["ok"]:
        return _redirect("/admin/settings?restarting=1#updates", msg="Update installed. Restarting the server now.")
    return _redirect("/admin/settings#updates", err="No update applied. See the log below.")


@router.post("/settings/mirror", dependencies=[Depends(require_admin)])
def settings_mirror(mirror_title: str = Form(""), mirror_prompt: str = Form(""), mirror_sayings: str = Form(""), stream_transcode: str = Form(""), pgm_size: str = Form("half"), movie_quotes: str = Form("")):
    with get_db() as db:
        set_setting(db, "movie_quotes", "1" if movie_quotes else None)
        set_setting(db, "stream_transcode", "1" if stream_transcode else None)
        set_setting(db, "pgm_size", pgm_size if pgm_size in ("small", "half", "large") else None)
        set_setting(db, "mirror_title", mirror_title.strip() or None)
        set_setting(db, "mirror_prompt", mirror_prompt.strip() or None)
        set_setting(db, "mirror_sayings", "\n".join(ln.strip() for ln in mirror_sayings.splitlines() if ln.strip()) or None)
    return _redirect("/admin/settings#mirror", msg="Mirror screen saved")


@router.get("/settings/capture-devices", dependencies=[Depends(require_admin)])
def settings_capture_devices(request: Request):
    """List DirectShow / DeckLink inputs ffmpeg can see, for the PGM feed setting."""
    return _redirect("/admin/settings?devices=1#mirror")


@router.post("/settings/github", dependencies=[Depends(require_admin)])
def settings_github(github_repo: str = Form(""), github_token: str = Form(""), clear_token: str = Form("")):
    cfg = load_config()
    cfg["github_repo"] = github_repo.strip() or updater.DEFAULT_REPO
    if clear_token:
        cfg.pop("github_token", None)
    elif github_token.strip():
        cfg["github_token"] = github_token.strip()
    save_config(cfg)
    return _redirect("/admin/settings?fetch=1#updates", msg="Update source saved")


@router.post("/settings/restart", dependencies=[Depends(require_admin)])
def settings_restart():
    updater.schedule_restart()
    return _redirect("/admin/settings?restarting=1#server", msg="Restarting the server.")


@router.post("/settings/reset", dependencies=[Depends(require_admin)])
def settings_reset(confirm: str = Form(""), keep_password: str = Form("")):
    if confirm != "RESET":
        return _redirect("/admin/settings#data", err="Type RESET to confirm a full database reset")
    with get_db() as db:
        pw = db.execute("SELECT value FROM settings WHERE key = 'admin_password_hash'").fetchone()
    reset_db()
    for f in UPLOAD_DIR.glob("event-logo.*"):
        f.unlink(missing_ok=True)
    if keep_password and pw:
        with get_db() as db:
            db.execute("INSERT INTO settings (key, value) VALUES ('admin_password_hash', ?)", (pw["value"],))
        return _redirect("/admin/event", msg="Database reset. Brand new setup, admin password kept.")
    resp = RedirectResponse("/admin/login?msg=Database+reset.+Set+a+new+admin+password.", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@router.post("/settings/demo", dependencies=[Depends(require_admin)])
def settings_demo():
    from ..services.demo import load_demo

    with get_db() as db:
        n = load_demo(db)
    return _redirect("/admin", msg=f"Demo data loaded ({n} devices)")


@router.post("/away/clear/{site_id}", dependencies=[Depends(require_admin)])
def away_clear(site_id: int):
    with get_db() as db:
        away.clear(db, site_id)
    return _redirect("/admin", msg="Desk reopened")


# -- Bolero link (own page) ----------------------------------------------------

@router.get("/bolero", dependencies=[Depends(require_admin)])
def bolero_page(request: Request, path: str = "", host: str = ""):
    """Settings for the antenna link, live status, and a read-only explorer of the antenna's /rest resources."""
    return _bolero_render(request, path, host)


def _bolero_render(request: Request, path: str = "", host: str = "", test: dict | None = None):
    import json as _json
    path = path.strip().lstrip("/")[:80]
    text, err = "", ""
    if path:
        if not re_ok(path):
            err = "Resource names are letters, digits, / - _ ."
        else:
            try:
                data = bolero.read(path, host.strip() or None)
                text = _json.dumps(data, indent=2)[:200_000] if not isinstance(data, str) else data[:200_000]
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
    st = bolero.status()
    with get_db() as db:
        diffs = bolero_names.differences(db)
        renamed = bolero_names.renamed(db)
    polled = datetime.fromtimestamp(st["updated"]).strftime("%H:%M:%S") if st.get("updated") else ""
    return render(request, "admin/bolero.html", bolero=st, diffs=diffs, renamed=renamed, polled=polled, path=path, host=host, text=text, err=err, test=test, active="bolero")


@router.post("/bolero/test", dependencies=[Depends(require_admin)])
def bolero_test(request: Request, number: str = Form(""), action: str = Form("check"), seconds: int = Form(10)):
    """Flash tester: check a pack (sends nothing), flash it through its charger bay, or stop. Same rules as the kiosk."""
    number = number.strip()
    if not number:
        return _redirect("/admin/bolero#tester", err="Pick a pack to test")
    seconds = max(3, min(60, seconds))
    result: dict = {"number": number, "seconds": seconds}
    try:
        if action == "flash":
            r = bolero.identify(number, on=True, seconds=seconds)
            result["done"] = ("Already flashing" if r.get("already") else f"Flashing for {seconds} seconds") + \
                (f" in {r['charger']['name']}, bay {r['charger']['bay']}" if r.get("charger") else "") + (f" via /rest/{r['resource']}" if r.get("resource") else "")
        elif action == "stop":
            bolero.identify(number, on=False)
            result["done"] = "Stopped"
    except ValueError as e:
        result["error"] = str(e)
    result.update(bolero.flash_check(number) if action == "check" or "error" in result else {"number": number})
    return _bolero_render(request, test=result)


@router.post("/bolero/settings", dependencies=[Depends(require_admin)])
def bolero_save(bolero_host: str = Form(""), bolero_enabled: str = Form(""), bolero_admin_password: str = Form(""), clear_password: str = Form(""), bolero_charger_resource: str = Form(""), bolero_mode: str = Form("standalone"), bolero_names_mode: str = Form("off")):
    try:
        bolero.configure(bolero_host, bool(bolero_enabled), bolero_admin_password.strip() or None, clear_password=bool(clear_password), charger_resource=bolero_charger_resource, mode=bolero_mode, names=bolero_names_mode)
    except ValueError as e:
        return _redirect("/admin/bolero", err=str(e))
    if bolero_enabled and bolero_host.strip():
        st = bolero.poll_now()
        if not st["ok"]:
            return _redirect("/admin/bolero", err=f"Saved, but the antenna did not answer: {st['error']}")
        return _redirect("/admin/bolero", msg=f"Bolero link up: {len(st['packs'])} packs" + (f", charger data from /rest/{st['charger_resource']}" if st.get("charger_resource") else ", no charger data (needs the Charger Monitoring licence)"))
    return _redirect("/admin/bolero", msg="Bolero link saved")


@router.post("/bolero/poll", dependencies=[Depends(require_admin)])
def bolero_poll():
    st = bolero.poll_now()
    return _redirect("/admin/bolero", msg=f"Polled: {len(st['packs'])} packs" if st["ok"] else None, err=None if st["ok"] else st["error"])


@router.post("/bolero/names", dependencies=[Depends(require_admin)])
def bolero_names_apply(request: Request, person_id: int = Form(...), pack_name: str = Form(...), action: str = Form("use")):
    with get_db() as db:
        p = inventory.person_by_id(db, person_id)
        c = bolero_names.check(db, p) if p else None
        if c is None or c["pack_name"] != pack_name:
            return _redirect("/admin/bolero#names", err="Those names no longer differ.")
        if action == "use":
            inventory.update_person(db, person_id, name=pack_name)
            return _redirect("/admin/bolero#names", msg=f"{p['name']} is now {pack_name}")
        bolero_names.keep(db, person_id, pack_name)
    return _redirect("/admin/bolero#names", msg=f"Kept {p['name']}")


@router.post("/bolero/names/restore", dependencies=[Depends(require_admin)])
def bolero_names_restore(person_id: int = Form(0)):
    """Put back the imported names (one person, or everyone with person_id 0)."""
    with get_db() as db:
        n = bolero_names.restore(db, person_id or None)
    return _redirect("/admin/bolero#renamed", msg=f"Restored {n} imported name{'s' if n != 1 else ''}")


@router.get("/bolero/rest", dependencies=[Depends(require_admin)])
def bolero_explore(path: str = "nodeStatus"):
    return RedirectResponse(f"/admin/bolero?path={path}", status_code=303)


def re_ok(path: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_./-]+", path))


# -- HTTPS --------------------------------------------------------------------

def _port_open(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _tail(path, lines: int = 12) -> list[str]:
    try:
        return path.read_text("utf-8", errors="replace").strip().splitlines()[-lines:]
    except OSError:
        return []


@router.post("/settings/https", dependencies=[Depends(require_admin)])
def settings_https(action: str = Form("enable"), https_port: int = Form(8443), extra_hosts: str = Form("")):
    cfg = load_config()
    if action == "disable":
        cfg["https"] = False
        save_config(cfg)
        updater.schedule_restart()
        return _redirect("/admin/settings?restarting=1#phones", msg="HTTPS off. Restarting.")
    if not certs.available():
        return _redirect("/admin/settings#phones", err="The cryptography package is missing. Run Update and restart, then try again.")
    try:
        certs.issue_server_cert([h for h in extra_hosts.replace(",", " ").split() if h])
    except Exception as e:  # noqa: BLE001
        return _redirect("/admin/settings#phones", err=f"Could not create certificate: {e}")
    cfg["https"] = True
    cfg["https_port"] = max(1, min(65535, https_port))
    save_config(cfg)
    updater.schedule_restart()
    return _redirect("/admin/settings?restarting=1#phones", msg="HTTPS on. Restarting. Phones then use the https address and /trust.")


# -- reports ------------------------------------------------------------------

@router.get("/reports", dependencies=[Depends(require_admin)])
def reports_page(request: Request, format: str = "html", report: str = "outstanding", show: str = "both"):
    show = show if show in ("both", "out", "damaged") else "both"
    with get_db() as db:
        if format == "csv":
            buf = io.StringIO(); w = csv.writer(buf)
            if report == "endofshow":
                eos = reports.end_of_show(db)
                w.writerow(["Section", "Person", "Department", "Role", "Phone", "Bolero ID", "Device", "SI", "Type",
                            "Reason", "Note", "When", "Site", "Tech", "Last seen"])
                for person in (eos["still_out"] if show != "damaged" else []):
                    for d in person["items"]:
                        w.writerow(["Still out", person["name"], person["dept"], person["role"], person["phone"], person["bolero_id"],
                                    d["label"], d["si"], d["type_name"], "", "", d["issued_at"], d["issued_site"], d["issued_by"], person["last_seen"]])
                for section, rows in ((("Damaged", eos["damaged"]), ("Lost", eos["lost"])) if show != "out" else ()):
                    for d in rows:
                        w.writerow([section, d["person_name"] or "", d["person_dept"] or "", "", d["person_phone"] or "", d["linked_id"] or "",
                                    d["label"], d["si"], d["type_name"], d["reason"] or "", d["note"] or "", d["flagged_at"] or "",
                                    d["flagged_site"] or "", d["flagged_by"] or "", ""])
                name = {"both": "rdr-end-of-show.csv", "out": "rdr-outstanding.csv", "damaged": "rdr-damaged-lost.csv"}[show]
            elif report == "endofevent":
                w.writerow(["Person", "Department", "Role", "Phone", "Bolero ID", "Last seen", "Device", "SI", "Type", "Status", "Issued at", "Issued at site", "Issued by"])
                for person in reports.end_of_event(db):
                    for d in person["items"]:
                        w.writerow([person["name"], person["dept"], person["role"], person["phone"], person["bolero_id"], person["last_seen"],
                                    d["label"], d["si"], d["type_name"], d["status"], d["issued_at"], d["issued_site"], d["issued_by"]])
                name = "rdr-end-of-event.csv"
            elif report == "stock":
                w.writerow(["Site", "Type", "Own in", "Own total", "Shared in", "Shared total", "Out", "Issued here", "Lost/damaged"])
                for block in reports.stock_by_site(db):
                    for t in block["types"]:
                        w.writerow([block["site"]["name"], t["type_name"], t["own_in"], t["own_total"], t["shared_in"], t["shared_total"], t["n_out"], t["issued_here"], t["n_bad"]])
                name = "rdr-stock.csv"
            else:
                w.writerow(["Status", "Department", "Person", "Phone", "Device", "SI", "Type", "Since"])
                for d in reports.outstanding(db):
                    w.writerow([d["status"], d["holder_dept"] or "", d["holder_name"] or "", d["holder_phone"] or "", d["label"], d["si"], d["type_name"], d["last_event_at"] or ""])
                name = "rdr-outstanding.csv"
            buf.seek(0)
            return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})
        ctx = {"eos": reports.end_of_show(db), "show": show, "now": datetime.now().strftime("%d %b %Y %H:%M")}
    return render(request, "admin/reports.html", active="reports", **ctx)


# -- backup / restore -----------------------------------------------------------

@router.get("/backup", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def backup_page(request: Request, token: str = ""):
    pending = None
    if token:
        staged = IMPORT_DIR / f"backup-{token}.zip"
        if token.replace("-", "").replace("_", "").isalnum() and staged.exists():
            try:
                pending = backup.inspect(staged.read_bytes())
                pending["token"] = token
            except ValueError as e:
                staged.unlink(missing_ok=True)
                return _redirect("/admin/backup", err=str(e))
    return render(request, "admin/backup.html", pending=pending, snaps=snapshots.list_snapshots()[:40], snap_status=snapshots.status(), active="backup")


@router.post("/backup/snapshot", dependencies=[Depends(require_admin)])
def backup_snapshot_now():
    p = snapshots.snapshot("manual")
    return _redirect("/admin/backup", msg=f"Snapshot saved: {p.name}" if p else "No database to snapshot")


@router.get("/backup/snapshots/{name}", dependencies=[Depends(require_admin)])
def backup_snapshot_download(name: str):
    if not snapshots._NAME.match(name) or not (snapshots.SNAP_DIR / name).exists():
        raise HTTPException(404)
    from fastapi.responses import FileResponse
    return FileResponse(str(snapshots.SNAP_DIR / name), media_type="application/x-sqlite3", filename=name)


@router.post("/backup/snapshots/{name}/restore", dependencies=[Depends(require_admin)])
def backup_snapshot_restore(name: str, confirm: str = Form("")):
    if confirm != "RESTORE":
        return _redirect("/admin/backup", err="Type RESTORE next to the snapshot to confirm")
    try:
        snapshots.restore_snapshot(name)
    except (ValueError, FileNotFoundError) as e:
        return _redirect("/admin/backup", err=str(e))
    updater.schedule_restart()
    # the admin cookie is tied to the password: it carries on if the snapshot has the same one
    return RedirectResponse(f"/admin/settings?restarting=1&msg={quote('Snapshot restored. Restarting.')}", status_code=303)


@router.post("/backup/export", dependencies=[Depends(require_admin)])
def backup_export(include_token: str = Form("")):
    name, blob = backup.create(include_token=bool(include_token))
    return StreamingResponse(iter([blob]), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/backup/inspect", dependencies=[Depends(require_admin)])
async def backup_inspect(file: UploadFile = File(...)):
    import secrets
    blob = await file.read()
    try:
        backup.inspect(blob)
    except (ValueError, Exception) as e:  # noqa: BLE001 - bad zip, bad db
        return _redirect("/admin/backup", err=f"Not a usable backup: {e}")
    token = secrets.token_urlsafe(8)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    (IMPORT_DIR / f"backup-{token}.zip").write_bytes(blob)
    return _redirect(f"/admin/backup?token={token}")


@router.post("/backup/restore", dependencies=[Depends(require_admin)])
def backup_restore(token: str = Form(...), confirm: str = Form(""), restore_config: str = Form("")):
    if not token or not token.replace("-", "").replace("_", "").isalnum():
        return _redirect("/admin/backup", err="That upload has expired. Upload the backup again.")
    staged = IMPORT_DIR / f"backup-{token}.zip"
    if not staged.exists():
        return _redirect("/admin/backup", err="That upload has expired. Upload the backup again.")
    if confirm != "RESTORE":
        return _redirect(f"/admin/backup?token={token}", err="Type RESTORE to confirm")
    try:
        info = backup.restore(staged.read_bytes(), restore_config=bool(restore_config))
    except Exception as e:  # noqa: BLE001
        return _redirect("/admin/backup", err=f"Restore failed: {e}")
    staged.unlink(missing_ok=True)
    updater.schedule_restart()
    return RedirectResponse(f"/admin/settings?restarting=1&msg={quote('Restored ' + info['event'] + '. Restarting.')}", status_code=303)


# -- hub link (a site node) ----------------------------------------------------------

@router.get("/hub", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def hub_link_page(request: Request, check: str = ""):
    from ..config import is_hub
    from ..services import node
    if is_hub():
        return RedirectResponse("/hub", status_code=303)
    hub_info, err, fingerprint, preview = None, None, "", None
    s = node.settings()
    st = node.state()
    if node.linked() and (check or not node.running()):
        try:
            hub_info = node.hello()
        except node.HubError as e:
            err, fingerprint = str(e), e.fingerprint
    if st.get("pending") and (st["pending"].get("kind") in ("reconnect", "released")):
        try:
            preview = node.preview()
        except node.HubError as e:
            err, fingerprint = str(e), e.fingerprint
        except Exception as e:  # noqa: BLE001
            err = f"Could not work out the merge: {e}"
    return render(request, "admin/hub_link.html", s=s, st=st, hub_info=hub_info, hub_err=err, fingerprint=fingerprint,
                  preview=preview, badge=node.badge(), active="hub")


@router.post("/hub/link", dependencies=[Depends(require_admin)])
def hub_link_save(url: str = Form(""), key: str = Form("")):
    from ..services import node
    try:
        node.configure(url, key or None)
    except ValueError as e:
        return _redirect("/admin/hub", err=str(e))
    return _redirect("/admin/hub?check=1")


@router.post("/hub/trust", dependencies=[Depends(require_admin)])
def hub_trust(fingerprint: str = Form("")):
    from ..services import node
    if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        return _redirect("/admin/hub", err="That fingerprint is not valid")
    node.trust(fingerprint)
    return _redirect("/admin/hub?check=1", msg="Certificate trusted")


@router.post("/hub/take", dependencies=[Depends(require_admin)])
def hub_take(event_id: int = Form(...), confirm: str = Form("")):
    from ..services import node
    if confirm != "1":
        return _redirect("/admin/hub", err="Tick the box to confirm this node's data is replaced")
    try:
        r = node.take_event(event_id)
    except node.HubError as e:
        return _redirect("/admin/hub", err=str(e))
    except ValueError as e:
        return _redirect("/admin/hub", err=str(e))
    node.poke()
    return _redirect("/admin/hub", msg=f"Running {r['event'] or 'the event'} on this node. It works on its own from now on and sends its copy to the hub.")


@router.post("/hub/sync", dependencies=[Depends(require_admin)])
def hub_sync():
    from ..services import node
    st = node.sync_once(manual=True)
    if st.get("last_error"):
        return _redirect("/admin/hub", err=st["last_error"])
    return _redirect("/admin/hub", msg="Waiting for your choice below" if st.get("pending") else "In step with the hub")


@router.post("/hub/decide", dependencies=[Depends(require_admin)])
def hub_decide(choice: str = Form("")):
    from ..services import node
    try:
        out = node.decide(choice)
    except (node.HubError, ValueError) as e:
        return _redirect("/admin/hub", err=str(e))
    node.poke()
    return _redirect("/admin/hub", msg=out)


@router.post("/hub/handback", dependencies=[Depends(require_admin)])
def hub_handback():
    from ..services import node
    try:
        node.hand_back()
    except node.HubError as e:
        return _redirect("/admin/hub", err=str(e))
    return _redirect("/admin/hub", msg="Handed back. The hub has the final copy and can be scanned again; this node keeps its data.")


@router.post("/hub/forget", dependencies=[Depends(require_admin)])
def hub_forget():
    from ..services import node
    node.stop()
    node.forget()
    return _redirect("/admin/hub", msg="Unlinked from the hub. The data on this node is unchanged.")
