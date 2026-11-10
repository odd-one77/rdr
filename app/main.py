"""FastAPI application factory and shared template helpers."""
from __future__ import annotations

import socket
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


def create_app() -> FastAPI:
    ensure_dirs()
    init_db()
    app = FastAPI(title="RDR", version=__version__, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
    app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

    from .routers import admin, api, kiosk  # noqa: WPS433 (local import keeps startup order simple)

    app.include_router(kiosk.router)
    app.include_router(api.router)
    app.include_router(admin.router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # pragma: no cover - simple UX nicety
        if request.url.path.startswith("/api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "Not found"}, status_code=404)
        return render(request, "error.html", title="Not found", detail=str(request.url.path)), 404

    return app


app = create_app()
