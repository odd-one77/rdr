"""FastAPI application factory and shared template helpers."""
from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .auth import current_tech, is_admin
from .config import APP_DIR, UPLOAD_DIR, ensure_dirs, load_config
from .db import ACTION_LABELS, get_db, init_db

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["version"] = __version__
templates.env.globals["ACTION_LABELS"] = ACTION_LABELS


def lan_addresses() -> list[str]:
    """Best-effort list of LAN IPs so techs know where to point phones / mirror screens."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except socket.gaierror:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


MOBILE_MARKERS = ("iphone", "ipod", "android", "mobile", "windows phone", "blackberry")


def is_mobile(request: Request) -> bool:
    """Phones get the phone view, everything else the desktop kiosk. Android tablets without 'Mobile' count as desktop."""
    ua = request.headers.get("user-agent", "").lower()
    if "ipad" in ua or ("android" in ua and "mobile" not in ua):
        return False
    return any(m in ua for m in MOBILE_MARKERS)


def base_context(request: Request) -> dict:
    """Context every page needs: event, tech session, sites, flash messages."""
    with get_db() as db:
        event = db.execute("SELECT * FROM event WHERE id = 1").fetchone()
        sites = db.execute("SELECT * FROM sites WHERE active = 1 ORDER BY sort_order, name").fetchall()
        tech = None
        sess = current_tech(request)
        if sess:
            t = db.execute("SELECT * FROM techs WHERE id = ? AND active = 1", (sess["tech_id"],)).fetchone()
            s = db.execute("SELECT * FROM sites WHERE id = ?", (sess["site_id"],)).fetchone()
            if t and s:
                tech = {"id": t["id"], "username": t["username"], "display_name": t["display_name"], "role": t["role"],
                        "site_id": s["id"], "site_name": s["name"], "site": dict(s)}
    return {
        "request": request,
        "event": dict(event) if event else {},
        "sites": [dict(s) for s in sites],
        "tech": tech,
        "is_admin": is_admin(request),
        "msg": request.query_params.get("msg"),
        "err": request.query_params.get("err"),
        "config": load_config(),
    }


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    full = base_context(request)
    full.update(ctx)
    return templates.TemplateResponse(request, name, full)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from .services import snapshots
    ok, detail = snapshots.integrity_check()
    if not ok:
        print(f"!! Database integrity check failed: {detail}. Restore a snapshot from data/backups via Admin > Backup.", flush=True)
    snapshots.start()
    yield
    from .services.streams import stop_all
    stop_all()
    snapshots.stop()


def create_app() -> FastAPI:
    ensure_dirs()
    init_db()
    app = FastAPI(title="RDR", version=__version__, docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
    app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
    from .services.streams import STREAM_DIR
    STREAM_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/streams", StaticFiles(directory=str(STREAM_DIR)), name="streams")

    from .routers import admin, api, kiosk  # noqa: WPS433 (local import keeps startup order simple)

    app.include_router(kiosk.router)
    app.include_router(api.router)
    app.include_router(admin.router)

    @app.middleware("http")
    async def refresh_admin_cookie(request: Request, call_next):
        """Admin sessions are idle-limited; every admin page visit extends them.
        Static files are revalidated on every load so an update never leaves a kiosk on old CSS/JS."""
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif not request.url.path.startswith(("/uploads/", "/streams/")):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.path.startswith("/admin") and request.url.path != "/admin/logout" and is_admin(request):
            from .auth import ADMIN_COOKIE, admin_token
            response.set_cookie(ADMIN_COOKIE, admin_token(), max_age=60 * 60 * 12, samesite="lax", httponly=True)
        return response

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # pragma: no cover - simple UX nicety
        if request.url.path.startswith("/api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "Not found"}, status_code=404)
        return render(request, "error.html", title="Not found", detail=str(request.url.path)), 404

    return app


app = create_app()
