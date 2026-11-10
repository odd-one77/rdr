"""Kiosk pages: tech start screen, the main sign in/out screen, and the mirror."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import TECH_COOKIE, tech_token
from ..db import get_db
from ..main import render
from ..services import inventory

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def kiosk_home(request: Request):
    from ..auth import current_tech

    if current_tech(request) is None:
        return RedirectResponse("/start", status_code=303)
    with get_db() as db:
        types = db.execute("SELECT * FROM device_types WHERE active = 1 ORDER BY sort_order, name").fetchall()
    return render(request, "kiosk/kiosk.html", types=[dict(t) for t in types])


@router.get("/start", response_class=HTMLResponse)
def start(request: Request):
    with get_db() as db:
        techs = db.execute("SELECT * FROM techs WHERE active = 1 ORDER BY display_name").fetchall()
    return render(request, "kiosk/start.html", techs=[dict(t) for t in techs])


@router.post("/start")
def start_post(request: Request, tech_id: int = Form(...), site_id: int = Form(...)):
    with get_db() as db:
        t = db.execute("SELECT id FROM techs WHERE id = ? AND active = 1", (tech_id,)).fetchone()
        s = db.execute("SELECT id FROM sites WHERE id = ? AND active = 1", (site_id,)).fetchone()
    if not t or not s:
        return RedirectResponse("/start?err=Please+pick+a+tech+and+a+site", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(TECH_COOKIE, tech_token(tech_id, site_id), max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/start", status_code=303)
    resp.delete_cookie(TECH_COOKIE)
    return resp


@router.get("/mirror", response_class=HTMLResponse)
def mirror(request: Request, site: int | None = None):
    """Customer-facing mirror of the kiosk. Pick a site with ?site=ID, else the first site."""
    with get_db() as db:
        if site is None:
            row = db.execute("SELECT id FROM sites WHERE active = 1 ORDER BY sort_order, name LIMIT 1").fetchone()
            site = row["id"] if row else 1
        s = db.execute("SELECT * FROM sites WHERE id = ?", (site,)).fetchone()
    return render(request, "kiosk/mirror.html", mirror_site=dict(s) if s else {"id": site, "name": "RDR", "rtsp_url": None})
