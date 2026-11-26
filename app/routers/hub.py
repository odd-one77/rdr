"""The hub's own pages (overview, events, nodes, reports) and the API site nodes talk to.

Pages need the admin password. The node API under /hub/api/ is used by site nodes with their pairing key
(``Authorization: Bearer rdrn_...``); it sets no cookies and is not reachable with a browser session.
"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from .. import __version__, security
from ..auth import TECH_COOKIE, is_admin, set_cookie
from ..config import is_hub
from ..hubmode import EVENT_COOKIE
from ..main import render
from ..services import hub
from .admin import _redirect

router = APIRouter(prefix="/hub")


def require_hub() -> None:
    if not is_hub():
        raise HTTPException(404, "Not found")


def require_hub_admin(request: Request) -> None:
    require_hub()
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login?next=/hub"})


def _open(eid: int, target: str, request: Request) -> RedirectResponse:
    resp = RedirectResponse(target, status_code=303)
    set_cookie(resp, request, EVENT_COOKIE, str(int(eid)), 60 * 60 * 24 * 90)
    resp.delete_cookie(TECH_COOKIE)   # a tech picked for another event is not a tech here
    return resp


# -- pages ----------------------------------------------------------------------

@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_hub_admin)])
def overview(request: Request):
    evs = hub.events()
    nodes = hub.nodes()
    totals = {"in": 0, "out": 0, "damaged": 0, "lost": 0}
    for e in evs:
        for k in totals:
            totals[k] += e["info"]["counts"].get(k, 0)
    return render(request, "hub/overview.html", events=evs, nodes=nodes, totals=totals, log=hub.recent_log(15), active="hub")


@router.get("/status", dependencies=[Depends(require_hub_admin)])
def status_json():
    """What the overview refreshes: connection state per event and node."""
    return {"events": {e["id"]: {"node": e["node_name"], "online": e["node_online"], "seen": e["node_seen_text"],
                                 "pending": (e["node_state"] or {}).get("pending"), "rev": e["rev"],
                                 "counts": e["info"]["counts"], "last_scan": e["info"].get("last_scan", "")} for e in hub.events()},
            "nodes": {n["id"]: {"online": n["online"], "seen": n["seen_text"]} for n in hub.nodes()}}


@router.get("/events/new", response_class=HTMLResponse, dependencies=[Depends(require_hub_admin)])
def event_new(request: Request):
    return render(request, "hub/event_new.html", events=hub.events(include_archived=True), active="hub-events")


@router.post("/events/new", dependencies=[Depends(require_hub_admin)])
def event_create(request: Request, name: str = Form(""), show_name: str = Form(""), starts_on: str = Form(""), ends_on: str = Form(""),
                 copy_from: str = Form(""), copy_setup: str = Form(""), copy_devices: str = Form(""), copy_people: str = Form("")):
    what = tuple(k for k, v in (("setup", copy_setup), ("devices", copy_devices), ("people", copy_people)) if v)
    try:
        eid = hub.create_event(name, show_name, starts_on, ends_on, int(copy_from) if copy_from.isdigit() else None, what)
    except ValueError as e:
        return _redirect("/hub/events/new", err=str(e))
    return _redirect(f"/hub/events/{eid}", msg="Event created")


@router.get("/events/{eid}", response_class=HTMLResponse, dependencies=[Depends(require_hub_admin)])
def event_page(request: Request, eid: int):
    e = hub.event(eid)
    if e is None:
        raise HTTPException(404, "No such event")
    e["rev"] = hub.rev(eid)
    return render(request, "hub/event.html", e=e, info=hub._event_info(eid), versions=hub.list_versions(eid),
                  log=hub.recent_log(40, eid), active="hub-events")


@router.get("/events/{eid}/open", dependencies=[Depends(require_hub)])
def event_open(request: Request, eid: int, to: str = "admin"):
    e = hub.event(eid)
    if e is None or e["archived"]:
        return _redirect("/hub", err="That event is not available")
    if to == "admin" and not is_admin(request):
        return _redirect("/hub", err="Admin password needed")
    return _open(eid, {"admin": "/admin", "kiosk": "/start", "reports": "/admin/reports"}.get(to, "/admin"), request)


@router.post("/events/{eid}/release", dependencies=[Depends(require_hub_admin)])
def event_release(eid: int):
    hub.release(eid)
    return _redirect(f"/hub/events/{eid}", msg="Released. The hub's copy can be scanned again; the node will be asked what to do when it next connects.")


@router.post("/events/{eid}/archive", dependencies=[Depends(require_hub_admin)])
def event_archive(eid: int, archived: str = Form("1")):
    try:
        hub.set_archived(eid, archived == "1")
    except ValueError as e:
        return _redirect(f"/hub/events/{eid}", err=str(e))
    return _redirect(f"/hub/events/{eid}", msg="Archived" if archived == "1" else "Back in the list")


@router.post("/events/{eid}/delete", dependencies=[Depends(require_hub_admin)])
def event_delete(eid: int):
    try:
        hub.delete_event(eid)
    except ValueError as e:
        return _redirect(f"/hub/events/{eid}", err=str(e))
    return _redirect("/hub", msg="Event deleted. Its folder was moved to hub/deleted on the server.")


@router.get("/events/{eid}/versions/{name}", dependencies=[Depends(require_hub_admin)])
def version_download(eid: int, name: str):
    try:
        p = hub.copy_file(eid, name)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "No such copy")
    return FileResponse(p, filename=f"event-{eid}-{name}", media_type="application/octet-stream")


@router.post("/events/{eid}/versions/{name}/restore", dependencies=[Depends(require_hub_admin)])
def version_restore(eid: int, name: str):
    try:
        hub.restore_version(eid, name)
    except (ValueError, FileNotFoundError) as e:
        return _redirect(f"/hub/events/{eid}", err=str(e))
    return _redirect(f"/hub/events/{eid}", msg=f"Restored {name}. The copy before it was kept.")


@router.post("/events/{eid}/versions/save", dependencies=[Depends(require_hub_admin)])
def version_save(eid: int):
    hub.save_version(eid, "manual")
    return _redirect(f"/hub/events/{eid}#versions", msg="Copy saved")


@router.get("/nodes", response_class=HTMLResponse, dependencies=[Depends(require_hub_admin)])
def nodes_page(request: Request, key: str = ""):
    return render(request, "hub/nodes.html", nodes=hub.nodes(), new_key=key if key.startswith("rdrn_") else "",
                  key_for=request.query_params.get("for", ""), active="hub-nodes")


@router.post("/nodes", dependencies=[Depends(require_hub_admin)])
def node_add(name: str = Form("")):
    try:
        key = hub.add_node(name)
    except ValueError as e:
        return _redirect("/hub/nodes", err=str(e))
    return RedirectResponse(f"/hub/nodes?key={key}&for={name.strip()}", status_code=303)


@router.post("/nodes/{nid}/key", dependencies=[Depends(require_hub_admin)])
def node_key(nid: int):
    key = hub.new_key(nid)
    name = next((n["name"] for n in hub.nodes() if n["id"] == nid), "")
    return RedirectResponse(f"/hub/nodes?key={key}&for={name}", status_code=303)


@router.post("/nodes/{nid}/revoke", dependencies=[Depends(require_hub_admin)])
def node_revoke(nid: int):
    hub.revoke_node(nid)
    return _redirect("/hub/nodes", msg="Node removed. Its key no longer works and its events are released.")


@router.get("/reports", response_class=HTMLResponse, dependencies=[Depends(require_hub_admin)])
def reports_page(request: Request, format: str = ""):
    evs = hub.events(include_archived=True)
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Event", "Show", "Starts", "Ends", "Node", "Sites", "People", "In", "Out", "Damaged", "Lost", "Last scan", "Archived"])
        for e in evs:
            i = e["info"]; c = i["counts"]
            w.writerow([i.get("name") or e["name"], i.get("show_name", ""), i.get("starts_on") or "", i.get("ends_on") or "", e["node_name"] or "",
                        len(i["sites"]), i["people"], c.get("in", 0), c.get("out", 0), c.get("damaged", 0), c.get("lost", 0), i.get("last_scan", ""),
                        "yes" if e["archived"] else ""])
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=rdr-hub-events.csv"})
    return render(request, "hub/reports.html", events=evs, active="hub-reports")


@router.get("/pick", response_class=HTMLResponse, dependencies=[Depends(require_hub)])
def pick(request: Request):
    """Techs and phones on the hub pick which event they are working on."""
    return render(request, "hub/pick.html", events=[e for e in hub.events() if not e["archived"]])


@router.post("/pick", dependencies=[Depends(require_hub)])
def pick_post(request: Request, event_id: int = Form(...)):
    e = hub.event(event_id)
    if e is None or e["archived"]:
        return _redirect("/hub/pick", err="That event is not available")
    return _open(event_id, "/start", request)


# -- node API ---------------------------------------------------------------------

def _node(request: Request) -> dict:
    require_hub()
    origin = security.origin_from(request)
    wait = security.limiter.wait("node key", origin)
    if wait:
        raise HTTPException(429, f"Too many wrong keys. Try again in {security.wait_text(wait)}.")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    node = hub.node_for_token(token)
    if node is None:
        security.limiter.fail("node key", origin)
        raise HTTPException(401, "This node's key is not known to the hub")
    security.limiter.ok("node key", origin)
    return node


def _event_or_404(eid: int) -> dict:
    e = hub.event(eid)
    if e is None:
        raise HTTPException(404, "The hub has no such event")
    return e


@router.get("/api/hello")
def api_hello(node: dict = Depends(_node)):
    return {"hub": True, "version": __version__, "node": node["name"],
            "events": [{"id": e["id"], "name": e["info"].get("name") or e["name"], "show_name": e["info"].get("show_name", ""),
                        "starts_on": e["info"].get("starts_on"), "sites": [s["name"] for s in e["info"]["sites"]],
                        "node": e["node_name"], "mine": e["node_id"] == node["id"], "devices": sum(e["info"]["counts"].values())}
                       for e in hub.events()]}


@router.post("/api/heartbeat")
async def api_heartbeat(request: Request, node: dict = Depends(_node)):
    try:
        status = json.loads(await request.body() or b"{}")
    except ValueError:
        status = {}
    origin = security.origin_from(request)
    hub.heartbeat(node, origin.client, status if isinstance(status, dict) else {})
    eid = status.get("event_id") if isinstance(status, dict) else None
    out = {"ok": True}
    if isinstance(eid, int):
        e = hub.event(eid)
        if e is None:
            out.update(event="gone")
        else:
            out.update(rev=hub.rev(eid), mine=e["node_id"] == node["id"], holder=e["node_name"], archived=bool(e["archived"]))
    return out


@router.post("/api/events/{eid}/take")
def api_take(eid: int, node: dict = Depends(_node)):
    _event_or_404(eid)
    try:
        hub.take(eid, node)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "rev": hub.rev(eid)}


@router.post("/api/events/{eid}/release")
def api_release(eid: int, node: dict = Depends(_node)):
    e = _event_or_404(eid)
    if e["node_id"] == node["id"]:
        hub.release(eid, by=node["name"])
    return {"ok": True}


@router.get("/api/events/{eid}/copy")
def api_download(eid: int, node: dict = Depends(_node)):
    _event_or_404(eid)
    rev, blob = hub.download(eid)
    return Response(blob, media_type="application/zip", headers={"X-RDR-Rev": str(rev)})


@router.put("/api/events/{eid}/copy")
async def api_upload(request: Request, eid: int, base: int, force: int = 0, reason: str = "node", node: dict = Depends(_node)):
    _event_or_404(eid)
    blob = await request.body()
    try:
        new = hub.upload(eid, node, blob, base, force=bool(force), reason=reason if reason in ("node", "merged", "handback") else "node")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except RuntimeError as e:
        return JSONResponse({"detail": str(e), "rev": hub.rev(eid)}, status_code=409)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "rev": new}
