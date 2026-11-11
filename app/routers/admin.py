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

from ..auth import (
    ADMIN_COOKIE, admin_password_set, admin_token, check_admin_password, current_tech, is_admin, set_admin_password,
)
from ..config import DATA_DIR, IMPORT_DIR, UPLOAD_DIR, load_config, save_config
from ..db import ACTION_LABELS, get_db, get_setting, now_iso, reset_db, set_setting
from ..main import lan_addresses, render
from ..services import backup, bolero, certs, importer, inventory, quotes, reports, snapshots, streams, updater

router = APIRouter(prefix="/admin")


def _redirect(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    if msg:
        path += f"{sep}msg={quote(msg)}"
    elif err:
        path += f"{sep}err={quote(err)}"
    return RedirectResponse(path, status_code=303)


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


# -- login --------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    with get_db() as db:
        first_time = not admin_password_set(db)
    return render(request, "admin/login.html", first_time=first_time)


@router.post("/login")
def login_post(request: Request, password: str = Form(""), confirm: str = Form("")):
    with get_db() as db:
        if not admin_password_set(db):
            if len(password) < 4:
                return _redirect("/admin/login", err="Password needs at least 4 characters")
            if password != confirm:
                return _redirect("/admin/login", err="Passwords do not match")
            set_admin_password(db, password)
        elif not check_admin_password(db, password):
            return _redirect("/admin/login", err="Wrong password")
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(ADMIN_COOKIE, admin_token(), max_age=60 * 60 * 12, samesite="lax", httponly=True)
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
        counts = {
            "people": db.execute("SELECT COUNT(*) FROM people").fetchone()[0],
            "devices": db.execute("SELECT COUNT(*) FROM devices").fetchone()[0],
            "bolero": db.execute("SELECT COUNT(*) FROM people WHERE bolero_id <> ''").fetchone()[0],
            "holders": db.execute("SELECT COUNT(DISTINCT holder_id) FROM devices WHERE holder_id IS NOT NULL").fetchone()[0],
        }
    return render(request, "admin/dashboard.html", overall=overall, per_site=per_site, recent=recent, counts=counts, active="dashboard")


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
            dest = UPLOAD_DIR / f"event-logo{ext}"
            for old in UPLOAD_DIR.glob("event-logo.*"):
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
             "" if faceplate in ("", "none") else faceplate.strip().lstrip("#").upper(), allocation.strip(), notes.strip(), now_iso(), person_id),
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
        d = inventory.quick_create_device(db, si, type_id, label, current_tech(request), int(site_id) if site_id else None, notes, linked_id=linked_id)
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
            inventory.quick_create_device(db, si, type_id, label, current_tech(request), int(site_id) if site_id else None)
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
    tech = current_tech(request)
    with get_db() as db:
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
                return _redirect(f"/admin/devices/{device_id}", msg="Pre-assigned")
            inventory.clear_assignment(db, device_id, tech)
            return _redirect(f"/admin/devices/{device_id}", msg="Pre-assignment removed")
        dup = db.execute("SELECT id FROM devices WHERE si = ? COLLATE NOCASE AND id <> ?", (inventory.norm_si(si), device_id)).fetchone()
        if dup:
            return _redirect(f"/admin/devices/{device_id}", err="Another device already has that SI")
        db.execute(
            "UPDATE devices SET si = ?, type_id = ?, label = ?, site_id = ?, notes = ?, linked_id = ?, updated_at = ? WHERE id = ?",
            (inventory.norm_si(si), type_id, label.strip(), int(site_id) if site_id else None, notes.strip(), linked_id.strip(), now_iso(), device_id),
        )
    return _redirect(f"/admin/devices/{device_id}", msg="Saved")


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
        inventory.set_assignment(db, d["id"], person_id, current_tech(request), note)
    return _redirect("/admin/assignments", msg=f"{d['si']} pre-assigned")


@router.post("/assignments/{device_id}/delete", dependencies=[Depends(require_admin)])
def assignment_delete(request: Request, device_id: int):
    with get_db() as db:
        inventory.clear_assignment(db, device_id, current_tech(request))
    return _redirect("/admin/assignments", msg="Removed")


@router.post("/assignments/clear", dependencies=[Depends(require_admin)])
def assignments_clear(request: Request, confirm: str = Form("")):
    if confirm != "CLEAR":
        return _redirect("/admin/assignments", err="Type CLEAR to confirm")
    with get_db() as db:
        db.execute("DELETE FROM assignments")
    return _redirect("/admin/assignments", msg="All pre-assignments removed")


# -- direct edit (inline cells) --------------------------------------------------

from pydantic import BaseModel as _BM


class FieldEdit(_BM):
    field: str
    value: str = ""


PEOPLE_FIELDS = {"name", "dept", "role", "phone", "external_id", "bolero_id", "allocation"}
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
        errors=[i for i in items if i["action"] == "error"],
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
        result = importer.commit(db, staged, decisions, current_tech(request))
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
    bolero_status = bolero.status()
    return render(request, "admin/settings.html", info=info, update_log=updater.last_output(), lan=lan_addresses(), mirror=mirror,
                  https=https, streams=streams.status(), capture_devices=devices_out, bolero=bolero_status, active="settings")


@router.post("/settings/password", dependencies=[Depends(require_admin)])
def settings_password(current: str = Form(""), password: str = Form(""), confirm: str = Form("")):
    with get_db() as db:
        if not check_admin_password(db, current):
            return _redirect("/admin/settings", err="Current password is wrong")
        if len(password) < 4 or password != confirm:
            return _redirect("/admin/settings", err="New passwords must match and be at least 4 characters")
        set_admin_password(db, password)
    return _redirect("/admin/settings", msg="Password changed")


@router.post("/settings/server", dependencies=[Depends(require_admin)])
def settings_server(port: int = Form(8080), open_browser: str = Form("")):
    cfg = load_config()
    cfg["port"] = max(1, min(65535, port))
    cfg["open_browser"] = bool(open_browser)
    save_config(cfg)
    return _redirect("/admin/settings", msg="Server settings saved. They apply after a restart.")


@router.post("/settings/update", dependencies=[Depends(require_admin)])
def settings_update():
    report = updater.update(restart=True)
    if report["ok"]:
        return _redirect("/admin/settings?restarting=1", msg="Update installed. Restarting the server now...")
    return _redirect("/admin/settings", err="No update applied. See the log below.")


@router.post("/settings/mirror", dependencies=[Depends(require_admin)])
def settings_mirror(mirror_title: str = Form(""), mirror_prompt: str = Form(""), mirror_sayings: str = Form(""), stream_transcode: str = Form(""), pgm_size: str = Form("half"), movie_quotes: str = Form("")):
    with get_db() as db:
        set_setting(db, "movie_quotes", "1" if movie_quotes else None)
        set_setting(db, "stream_transcode", "1" if stream_transcode else None)
        set_setting(db, "pgm_size", pgm_size if pgm_size in ("small", "half", "large") else None)
        set_setting(db, "mirror_title", mirror_title.strip() or None)
        set_setting(db, "mirror_prompt", mirror_prompt.strip() or None)
        set_setting(db, "mirror_sayings", "\n".join(ln.strip() for ln in mirror_sayings.splitlines() if ln.strip()) or None)
    return _redirect("/admin/settings", msg="Mirror screen saved")


@router.get("/settings/capture-devices", dependencies=[Depends(require_admin)])
def settings_capture_devices(request: Request):
    """List DirectShow / DeckLink inputs ffmpeg can see, for the PGM feed setting."""
    return _redirect("/admin/settings?devices=1")


@router.post("/settings/github", dependencies=[Depends(require_admin)])
def settings_github(github_repo: str = Form(""), github_token: str = Form(""), clear_token: str = Form("")):
    cfg = load_config()
    cfg["github_repo"] = github_repo.strip() or updater.DEFAULT_REPO
    if clear_token:
        cfg.pop("github_token", None)
    elif github_token.strip():
        cfg["github_token"] = github_token.strip()
    save_config(cfg)
    return _redirect("/admin/settings?fetch=1", msg="Update source saved")


@router.post("/settings/restart", dependencies=[Depends(require_admin)])
def settings_restart():
    updater.schedule_restart()
    return _redirect("/admin/settings?restarting=1", msg="Restarting the server...")


@router.post("/settings/reset", dependencies=[Depends(require_admin)])
def settings_reset(confirm: str = Form(""), keep_password: str = Form("")):
    if confirm != "RESET":
        return _redirect("/admin/settings", err="Type RESET to confirm a full database reset")
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


# -- Bolero antenna link ---------------------------------------------------------

@router.post("/settings/bolero", dependencies=[Depends(require_admin)])
def settings_bolero(bolero_host: str = Form(""), bolero_enabled: str = Form(""), bolero_admin_password: str = Form(""), clear_password: str = Form(""), bolero_charger_resource: str = Form("")):
    try:
        bolero.configure(bolero_host, bool(bolero_enabled), bolero_admin_password.strip() or None, clear_password=bool(clear_password), charger_resource=bolero_charger_resource)
    except ValueError as e:
        return _redirect("/admin/settings", err=str(e))
    if bolero_enabled and bolero_host.strip():
        st = bolero.poll_now()
        if not st["ok"]:
            return _redirect("/admin/settings", err=f"Saved, but the antenna did not answer: {st['error']}")
        return _redirect("/admin/settings", msg=f"Bolero link up: {len(st['packs'])} packs" + (f", charger data from /rest/{st['charger_resource']}" if st.get("charger_resource") else ", no charger data (needs the Charger Monitoring licence)"))
    return _redirect("/admin/settings", msg="Bolero link saved")


@router.get("/bolero/rest", dependencies=[Depends(require_admin)])
def bolero_explore(request: Request, path: str = "nodeStatus"):
    """Read-only explorer for the antenna's /rest resources (to find the charger resource on site)."""
    import json as _json
    path = path.strip().lstrip("/")[:80]
    if not re_ok(path):
        raise HTTPException(400, "Resource names are letters, digits, / - _ .")
    try:
        data = bolero.read(path)
        text = _json.dumps(data, indent=2)[:200_000] if not isinstance(data, str) else data[:200_000]
        err = ""
    except Exception as e:  # noqa: BLE001
        text, err = "", f"{type(e).__name__}: {e}"
    return render(request, "admin/bolero_explore.html", path=path, text=text, err=err, status=bolero.status(), raw=bolero.raw(), active="settings")


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
        return _redirect("/admin/settings?restarting=1", msg="HTTPS off. Restarting...")
    if not certs.available():
        return _redirect("/admin/settings", err="The 'cryptography' package is missing. Run Update & restart to install requirements, then try again.")
    try:
        certs.issue_server_cert([h for h in extra_hosts.replace(",", " ").split() if h])
    except Exception as e:  # noqa: BLE001
        return _redirect("/admin/settings", err=f"Could not create certificate: {e}")
    cfg["https"] = True
    cfg["https_port"] = max(1, min(65535, https_port))
    save_config(cfg)
    updater.schedule_restart()
    return _redirect("/admin/settings?restarting=1", msg="HTTPS on. Restarting... phones then use the https address and /trust.")


# -- reports ------------------------------------------------------------------

@router.get("/reports", dependencies=[Depends(require_admin)])
def reports_page(request: Request, format: str = "html", report: str = "outstanding"):
    with get_db() as db:
        if format == "csv":
            buf = io.StringIO(); w = csv.writer(buf)
            if report == "endofevent":
                w.writerow(["Person", "Department", "Role", "Phone", "Bolero ID", "Last seen", "Item", "SI", "Type", "Status", "Issued at", "Issued at site", "Issued by"])
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
                w.writerow(["Status", "Department", "Person", "Phone", "Item", "SI", "Type", "Since"])
                for d in reports.outstanding(db):
                    w.writerow([d["status"], d["holder_dept"] or "", d["holder_name"] or "", d["holder_phone"] or "", d["label"], d["si"], d["type_name"], d["last_event_at"] or ""])
                name = "rdr-outstanding.csv"
            buf.seek(0)
            return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})
        ctx = {"end_of_event": reports.end_of_event(db), "outstanding": reports.outstanding(db),
               "now": datetime.now().strftime("%d %b %Y %H:%M")}
    return render(request, "admin/reports.html", active="reports", **ctx)


# -- backup / restore -----------------------------------------------------------

@router.get("/backup", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def backup_page(request: Request, token: str = ""):
    pending = None
    if token:
        staged = IMPORT_DIR / f"backup-{token}.zip"
        if staged.exists() and token.replace("-", "").replace("_", "").isalnum():
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
    resp = RedirectResponse(f"/admin/settings?restarting=1&msg={quote('Snapshot restored. Restarting...')}", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


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
    resp = RedirectResponse(f"/admin/settings?restarting=1&msg={quote('Restored ' + info['event'] + '. Restarting...')}", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp
