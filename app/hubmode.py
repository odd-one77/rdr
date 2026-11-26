"""On the hub, every request works on one event's database: the one picked in the ``rdr_event`` cookie.

Runs inside the security middleware. Pages that need an event when none is picked go to the hub's
overview (admin) or the event picker (kiosk and phones). An event that a site node is running cannot be
scanned on the hub: requests that would change it through the kiosk API are refused.
"""
from __future__ import annotations

import json
from http.cookies import SimpleCookie

from .config import is_hub
from .db import set_db_path

EVENT_COOKIE = "rdr_event"
# work without an event: the hub's own pages, the machine settings, signing in and out of admin
NO_EVENT = ("/hub", "/static/", "/access", "/admin/login", "/admin/logout", "/admin/settings", "/favicon.ico", "/api/ping", "/trust")
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")
HUB_ELSEWHERE = ("/admin/backup", "/admin/settings/reset")
NEEDS_EVENT = ("/admin/settings/demo",)   # loads demo data into an event, never into the hub's own database


def picked_event(scope) -> int | None:
    raw = ""
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            raw = v.decode("latin-1")
            break
    if not raw:
        return None
    try:
        c = SimpleCookie(raw)
    except Exception:  # noqa: BLE001
        return None
    m = c.get(EVENT_COOKIE)
    return int(m.value) if m and m.value.isdigit() else None


class HubMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_hub():
            await self.app(scope, receive, send)
            return
        from .services import hub
        path = scope.get("path", "/")
        if path.startswith(HUB_ELSEWHERE):
            # one RDR's backup and reset work on its single database; on the hub each event keeps its
            # copies on its own page, and an event is deleted there rather than reset
            eid = picked_event(scope)
            target = f"/hub/events/{eid}#versions".encode() if eid else b"/hub"
            await send({"type": "http.response.start", "status": 303, "headers": [(b"location", target), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        eid = picked_event(scope)
        e = hub.event(eid) if eid else None
        if e and not e["archived"] and hub.event_db_path(eid).exists():
            set_db_path(hub.event_db_path(eid))
            scope.setdefault("state", {})["hub_event"] = e
            if e["node_id"] and scope.get("method", "GET").upper() in UNSAFE and path.startswith("/api/") and path != "/api/ping":
                await _json(send, 409, f"{e['node_name']} is running this event on site. Scan there; the hub's copy is view only until the node hands it back.")
                return
        else:
            set_db_path(hub.HUB_DB)
            scope.setdefault("state", {})["hub_event"] = None
            if not path.startswith(NO_EVENT) or path.startswith(NEEDS_EVENT):
                if path.startswith("/api/"):
                    await _json(send, 409, "Pick an event first")
                    return
                target = b"/hub" if path.startswith("/admin") else b"/hub/pick"
                await send({"type": "http.response.start", "status": 303, "headers": [(b"location", target), (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


async def _json(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
