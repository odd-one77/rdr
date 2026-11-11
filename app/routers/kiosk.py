"""Kiosk pages: tech start screen, the main sign in/out screen, and the mirror."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..auth import ADMIN_COOKIE, TECH_COOKIE, live_tech, tech_token
from ..db import get_db
from ..main import render
from ..config import load_config
from ..main import is_mobile, lan_addresses
from ..services import certs, inventory

router = APIRouter()


def _to_start(target: str = "/start") -> RedirectResponse:
    """Back to the start screen, dropping a session that no longer matches a tech and site."""
    resp = RedirectResponse(target, status_code=303)
    resp.delete_cookie(TECH_COOKIE)
    return resp


@router.get("/", response_class=HTMLResponse)
def kiosk_home(request: Request):
    if is_mobile(request):
        return RedirectResponse("/m", status_code=303)
    with get_db() as db:
        if live_tech(db, request) is None:
            return _to_start()
        types = db.execute("SELECT * FROM device_types WHERE active = 1 ORDER BY sort_order, name").fetchall()
    return render(request, "kiosk/kiosk.html", types=[dict(t) for t in types])


@router.get("/start", response_class=HTMLResponse)
def start(request: Request):
    with get_db() as db:
        techs = db.execute("SELECT * FROM techs WHERE active = 1 ORDER BY display_name").fetchall()
    return render(request, "kiosk/start.html", techs=[dict(t) for t in techs])


@router.post("/start")
def start_post(request: Request, tech_id: int = Form(...), site_id: int = Form(...), next: str = Form("/")):
    with get_db() as db:
        t = db.execute("SELECT id FROM techs WHERE id = ? AND active = 1", (tech_id,)).fetchone()
        s = db.execute("SELECT id FROM sites WHERE id = ? AND active = 1", (site_id,)).fetchone()
    if not t or not s:
        return RedirectResponse("/start?err=Please+pick+a+tech+and+a+site", status_code=303)
    target = "/m" if is_mobile(request) else (next if next.startswith("/") and not next.startswith("//") else "/")
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(TECH_COOKIE, tech_token(tech_id, site_id), max_age=60 * 60 * 24 * 30, samesite="lax")
    resp.delete_cookie(ADMIN_COOKIE)  # a tech session never carries an admin login along
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/start", status_code=303)
    resp.delete_cookie(TECH_COOKIE)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@router.get("/mirror", response_class=HTMLResponse)
def mirror(request: Request, site: int | None = None):
    """Customer-facing mirror of the kiosk. Pick a site with ?site=ID, else the first site."""
    with get_db() as db:
        if site is None:
            row = db.execute("SELECT id FROM sites WHERE active = 1 ORDER BY sort_order, name LIMIT 1").fetchone()
            site = row["id"] if row else 1
        s = db.execute("SELECT * FROM sites WHERE id = ?", (site,)).fetchone()
    from ..db import get_setting
    from ..services import streams
    with get_db() as db:
        pgm_size = get_setting(db, "pgm_size") or "half"
    site_d = dict(s) if s else {"id": site, "name": "RDR", "rtsp_url": None}
    kind = streams.classify_feed(site_d.get("rtsp_url") or "")
    feed = {"kind": kind, "relay": kind in streams.RELAY_KINDS, "url": site_d.get("rtsp_url"),
            "embed": streams.youtube_embed(site_d["rtsp_url"]) if kind == "youtube" else None}
    return render(request, "kiosk/mirror.html", mirror_site=site_d, feed=feed, pgm_size=pgm_size if pgm_size in ("small", "half", "large") else "half")


def _https_url() -> str | None:
    cfg = load_config()
    if not cfg.get("https"):
        return None
    ips = lan_addresses()
    host = ips[0] if ips else "localhost"
    return f"https://{host}:{cfg.get('https_port', 8443)}/m"


@router.get("/m", response_class=HTMLResponse)
def phone(request: Request):
    """Phone-sized kiosk with camera scanning (camera needs the https address)."""
    if not is_mobile(request):
        return RedirectResponse("/", status_code=303)
    with get_db() as db:
        if live_tech(db, request) is None:
            return _to_start("/start?next=/m")
        types = db.execute("SELECT * FROM device_types WHERE active = 1 ORDER BY sort_order, name").fetchall()
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" or request.url.hostname in ("localhost", "127.0.0.1")
    return render(request, "kiosk/phone.html", types=[dict(t) for t in types], secure=secure,
                  https_on=bool(load_config().get("https")), https_url=_https_url())


@router.get("/trust", response_class=HTMLResponse)
def trust(request: Request):
    return render(request, "kiosk/trust.html", https_on=bool(load_config().get("https")), https_url=_https_url(), cert=certs.status())


@router.get("/trust/rdr-ca.crt")
def trust_cert():
    if not certs.CA_CRT.exists():
        return RedirectResponse("/trust", status_code=303)
    return FileResponse(str(certs.CA_CRT), media_type="application/x-x509-ca-cert", filename="rdr-ca.crt")
