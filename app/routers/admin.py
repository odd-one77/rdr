"""Admin area: password protected setup, data management, imports and settings."""
from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..auth import (
    ADMIN_COOKIE, admin_password_set, admin_token, check_admin_password, current_tech, is_admin, set_admin_password,
)
from ..config import UPLOAD_DIR, load_config, save_config
from ..db import ACTION_LABELS, get_db, now_iso, reset_db
from ..main import lan_addresses, render
from ..services import importer, inventory, updater

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
            "assignments": db.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
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
        where = "WHERE (p.name LIKE ? OR p.dept LIKE ? OR p.role LIKE ? OR p.phone LIKE ? OR p.external_id LIKE ?)"
        params = [like] * 5
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
def person_create(name: str = Form(...), dept: str = Form(""), role: str = Form(""), phone: str = Form(""), external_id: str = Form(""), notes: str = Form("")):
    with get_db() as db:
        if external_id.strip() and db.execute("SELECT 1 FROM people WHERE external_id = ?", (external_id.strip(),)).fetchone():
            return _redirect("/admin/people/new", err="That unique ID is already used")
        p = inventory.create_person(db, name, dept, role, phone, external_id, notes)
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
def person_save(person_id: int, action: str = Form("save"), name: str = Form(""), dept: str = Form(""), role: str = Form(""), phone: str = Form(""), external_id: str = Form(""), notes: str = Form("")):
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
            "UPDATE people SET name = ?, dept = ?, role = ?, phone = ?, external_id = ?, notes = ?, updated_at = ? WHERE id = ?",
            (name.strip(), dept.strip(), role.strip(), phone.strip(), external_id.strip() or None, notes.strip(), now_iso(), person_id),
        )
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
def device_create(request: Request, si: str = Form(...), type_id: int = Form(...), label: str = Form(""), site_id: str = Form(""), notes: str = Form("")):
    with get_db() as db:
        if inventory.device_by_si(db, si):
            return _redirect("/admin/devices/new", err=f"SI {inventory.norm_si(si)} already exists")
        d = inventory.quick_create_device(db, si, type_id, label, current_tech(request), int(site_id) if site_id else None, notes)
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
def device_save(request: Request, device_id: int, action: str = Form("save"), si: str = Form(""), type_id: int = Form(0), label: str = Form(""), site_id: str = Form(""), notes: str = Form(""), status: str = Form(""), reason: str = Form(""), assign_person_id: str = Form("")):
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
            "UPDATE devices SET si = ?, type_id = ?, label = ?, site_id = ?, notes = ?, updated_at = ? WHERE id = ?",
            (inventory.norm_si(si), type_id, label.strip(), int(site_id) if site_id else None, notes.strip(), now_iso(), device_id),
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
    return render(request, "admin/import_map.html", staged=staged, kind=kind, preview=staged["rows"][:8], active="import")


@router.post("/import/s/{token}", dependencies=[Depends(require_admin)])
async def import_mapping_post(request: Request, token: str):
    try:
        staged = importer.load_staged(token)
    except FileNotFoundError:
        return _redirect("/admin/import", err="That import session has expired")
    form = await request.form()
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
    target = {"people": "/admin/people", "devices": "/admin/devices", "assignments": "/admin/assignments"}[staged["kind"]]
    return _redirect(target, msg="Import done: " + ", ".join(parts))


# -- settings -----------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def settings_page(request: Request, fetch: int = 0):
    info = updater.status(fetch=bool(fetch))
    return render(request, "admin/settings.html", info=info, update_log=updater.last_output(), lan=lan_addresses(), active="settings")


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
        return _redirect("/admin/settings?restarting=1", msg="Update pulled. Restarting the server now...")
    return _redirect("/admin/settings", err="Update failed. See the log below.")


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
