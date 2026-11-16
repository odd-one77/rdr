"""The access code page: what a browser sees first when RDR asks for the shared code (see security.py)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import security
from ..auth import ACCESS_COOKIE, ACCESS_DAYS, access_token, check_access_code, has_access, set_cookie
from ..config import load_config
from ..main import templates

router = APIRouter()


def _target(next_: str) -> str:
    """Only a page on this server, and never the code page itself."""
    ok = next_.startswith("/") and not next_.startswith(("//", "/\\", "/access")) and "\\" not in next_
    return next_ if ok else "/"


def _page(request: Request, next_: str, err: str | None = None, status: int = 200) -> HTMLResponse:
    # deliberately standalone: no event name, logo or anything else before the code is in
    resp = templates.TemplateResponse(request, "access.html", {"request": request, "next": next_, "err": err})
    resp.status_code = status
    return resp


@router.get("/access", response_class=HTMLResponse)
def access_page(request: Request, next: str = "/"):
    target = _target(next)
    cfg = load_config()
    if not security.code_needed(security.origin_from(request), cfg) or has_access(request.cookies, cfg):
        return RedirectResponse(target, status_code=303)
    return _page(request, target)


@router.post("/access")
def access_post(request: Request, code: str = Form(""), next: str = Form("/")):
    target = _target(next)
    origin = security.origin_from(request)
    wait = security.limiter.wait("access code", origin)
    if wait:
        return _page(request, target, security.wait_text(wait), 429)
    if not check_access_code(code):
        security.limiter.fail("access code", origin)
        return _page(request, target, "Wrong code", 403)
    security.limiter.ok("access code", origin)
    resp = RedirectResponse(target, status_code=303)
    set_cookie(resp, request, ACCESS_COOKIE, access_token(), ACCESS_DAYS * 86400)
    return resp


@router.get("/access/forget")
def access_forget():
    """Drop the code on this browser, for a shared screen."""
    resp = RedirectResponse("/access?next=" + quote("/", safe=""), status_code=303)
    resp.delete_cookie(ACCESS_COOKIE, path="/")
    return resp
