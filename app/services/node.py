"""A site node's link to the hub.

Setup (Admin, Hub link): the hub's address and this node's pairing key, then pick an event. Taking an
event downloads the hub's copy into this RDR (a snapshot is taken first) and tells the hub that this
node now runs it.

From then on the node works on its own and never waits for the hub. Every SYNC_SECONDS it reports in;
when its data changed it sends its copy, which becomes the hub's copy and the offsite backup. The copy
both sides last agreed on is kept in data/node/base.db, so each side can tell what the other changed:

* only this node changed: its copy is sent;
* only the hub changed (people or devices edited there): the hub's copy is merged in;
* both changed while the link stayed up: merged, and the result sent;
* after the hub could not be reached, and the hub changed: nothing is sent until the admin chooses
  Merge, Overwrite the hub (this node's copy wins), or Pull from the hub (the hub's copy replaces this
  node's; a snapshot is kept). Only this node changed: its copy is sent without asking.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .. import __version__
from ..config import DB_PATH, NODE_DIR, UPLOAD_DIR, load_config, save_config
from ..db import changes, get_db, open_db
from . import eventfile, merge

SYNC_SECONDS = 30
TIMEOUT = 20
STATE_PATH = NODE_DIR / "state.json"
BASE_PATH = NODE_DIR / "base.db"
_lock = threading.RLock()     # one sync at a time
_stop = threading.Event()
_wake = threading.Event()
_thread: Optional[threading.Thread] = None


# -- settings and state ------------------------------------------------------------

def settings() -> dict:
    cfg = load_config()
    return {"url": (cfg.get("hub_url") or "").rstrip("/"), "has_key": bool(cfg.get("hub_key")), "pin": cfg.get("hub_pin") or "",
            "event_id": cfg.get("hub_event_id"), "event_name": cfg.get("hub_event_name") or "", "node_name": cfg.get("hub_node_name") or ""}


def linked() -> bool:
    s = settings()
    return bool(s["url"] and s["has_key"])


def running() -> bool:
    s = settings()
    return linked() and isinstance(s["event_id"], int)


def state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


def _update(**kw) -> dict:
    with _lock:
        st = state()
        st.update(kw)
        _save_state(st)
        return st


def configure(url: str, key: Optional[str]) -> None:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url) if url else None
    if parts and (parts.scheme not in ("http", "https") or not parts.hostname):
        raise ValueError("The hub address looks like https://hub.example.com or https://10.0.0.5:8443")
    cfg = load_config()
    if cfg.get("hub_url") != url:
        cfg.pop("hub_pin", None)
    cfg["hub_url"] = url
    if key:
        key = key.strip()
        if not key.startswith("rdrn_"):
            raise ValueError("That is not a node key. Keys start with rdrn_ and are made on the hub, under Nodes.")
        cfg["hub_key"] = key
    save_config(cfg)


def forget() -> None:
    """Unlink from the hub. The data on this node stays as it is."""
    cfg = load_config()
    for k in ("hub_url", "hub_key", "hub_pin", "hub_event_id", "hub_event_name", "hub_node_name"):
        cfg.pop(k, None)
    save_config(cfg)
    STATE_PATH.unlink(missing_ok=True)
    BASE_PATH.unlink(missing_ok=True)


def trust(pin: str) -> None:
    cfg = load_config()
    cfg["hub_pin"] = pin
    save_config(cfg)


# -- talking to the hub -----------------------------------------------------------------

class HubError(Exception):
    def __init__(self, msg: str, status: int = 0, body: Any = None, fingerprint: str = ""):
        super().__init__(msg)
        self.status, self.body, self.fingerprint = status, body, fingerprint


def _fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def call(method: str, path: str, body: Optional[bytes] = None, ctype: str = "application/json", timeout: float = TIMEOUT) -> tuple[int, dict, bytes]:
    """One request to the hub. HTTPS is checked against the system's trusted certificates; a hub with
    its own certificate (reached over the site to site VPN) is trusted by its pinned fingerprint."""
    cfg = load_config()
    url, key, pin = (cfg.get("hub_url") or "").rstrip("/"), cfg.get("hub_key") or "", cfg.get("hub_pin") or ""
    if not url or not key:
        raise HubError("Not linked to a hub")
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port
    base = parts.path.rstrip("/")
    try:
        if parts.scheme == "https":
            if pin:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE   # checked below against the pinned fingerprint instead
            else:
                ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, port or 443, timeout=timeout, context=ctx)
            try:
                conn.connect()
            except ssl.SSLCertVerificationError as e:
                fp = _peer_fingerprint(host, port or 443)
                raise HubError("The hub's certificate is not trusted by this computer", fingerprint=fp) from e
            der = conn.sock.getpeercert(binary_form=True)
            if pin and _fingerprint(der) != pin:
                conn.close()
                raise HubError("The hub's certificate changed since it was trusted. Check it, then trust it again.", fingerprint=_fingerprint(der))
        else:
            conn = http.client.HTTPConnection(host, port or 80, timeout=timeout)
        headers = {"Authorization": f"Bearer {key}", "User-Agent": f"RDR-node/{__version__}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = ctype
        conn.request(method, base + path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
    except HubError:
        raise
    except (OSError, http.client.HTTPException) as e:
        raise HubError(f"Could not reach the hub: {e}") from e
    if resp.status >= 400:
        try:
            detail = json.loads(data).get("detail") or ""
        except ValueError:
            detail = ""
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        raise HubError(detail or f"The hub answered {resp.status}", status=resp.status, body=parsed)
    return resp.status, hdrs, data


def _peer_fingerprint(host: str, port: int) -> str:
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=8) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            return _fingerprint(t.getpeercert(binary_form=True))
    except OSError:
        return ""


def _json(method: str, path: str, payload: Optional[dict] = None) -> dict:
    _, _, data = call(method, path, json.dumps(payload).encode() if payload is not None else None)
    return json.loads(data or b"{}")


def hello() -> dict:
    d = _json("GET", "/hub/api/hello")
    cfg = load_config()
    if cfg.get("hub_node_name") != d.get("node"):
        cfg["hub_node_name"] = d.get("node")
        save_config(cfg)
    return d


# -- the agreed copy ---------------------------------------------------------------------

def _set_base(rev: int) -> None:
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BASE_PATH.with_suffix(".part")
    eventfile.copy_db(DB_PATH, tmp)
    tmp.replace(BASE_PATH)
    with get_db() as db:
        n = changes(db)
    _update(base_rev=rev, base_changes=n, pending=None, last_agreed=_now())


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _local_changes() -> int:
    with get_db() as db:
        return changes(db)


def _download(eid: int, dest: Path) -> tuple[int, dict]:
    _, hdrs, data = call("GET", f"/hub/api/events/{eid}/copy", timeout=120)
    manifest = eventfile.unpack(data, dest)
    return int(hdrs.get("x-rdr-rev", "0")), manifest


def _push(eid: int, base_rev: int, force: bool = False, reason: str = "node") -> int:
    blob = eventfile.pack(DB_PATH, UPLOAD_DIR)
    _, _, data = call("PUT", f"/hub/api/events/{eid}/copy?base={base_rev}&force={1 if force else 0}&reason={reason}", blob, "application/zip", timeout=180)
    return int(json.loads(data)["rev"])


def take_event(eid: int) -> dict:
    """Start running a hub event on this node: the hub's copy replaces this node's data (snapshot first)."""
    from . import snapshots
    with _lock:
        _json("POST", f"/hub/api/events/{eid}/take")
        NODE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = NODE_DIR / "incoming.db"
        try:
            rev, manifest = _download(eid, tmp)
            snapshots.snapshot("beforehub")
            eventfile.replace_live(DB_PATH, tmp, UPLOAD_DIR, manifest.get("logo"))
        finally:
            _cleanup(tmp)
        cfg = load_config()
        cfg["hub_event_id"] = eid
        cfg["hub_event_name"] = manifest.get("event") or ""
        save_config(cfg)
        _set_base(rev)
        _update(last_ok=time.time(), offline_since=None, last_error="", last_pull=_now(), released=False)
    return {"rev": rev, "event": manifest.get("event")}


def hand_back() -> None:
    """Send the final copy and give the event back to the hub. This node keeps its data."""
    s = settings()
    with _lock:
        st = state()
        eid = s["event_id"]
        if _local_changes() != st.get("base_changes"):
            _push(eid, st.get("base_rev", 0), force=bool(st.get("pending")), reason="handback")
        _json("POST", f"/hub/api/events/{eid}/release")
        cfg = load_config()
        cfg.pop("hub_event_id", None); cfg.pop("hub_event_name", None)
        save_config(cfg)
        _update(pending=None, released=False, last_push=_now())
        BASE_PATH.unlink(missing_ok=True)


def _cleanup(p: Path) -> None:
    for s in ("", "-wal", "-shm"):
        Path(str(p) + s).unlink(missing_ok=True)


# -- sync ------------------------------------------------------------------------------

def _status() -> dict:
    with get_db() as db:
        counts = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) AS n FROM devices GROUP BY status")}
        sites = [r["name"] for r in db.execute("SELECT name FROM sites WHERE active = 1 ORDER BY sort_order, name")]
        last = db.execute("SELECT MAX(ts) FROM transactions").fetchone()[0] or ""
        n = changes(db)
    st = state()
    return {"version": __version__, "event_id": settings()["event_id"], "counts": counts, "sites": sites, "last_scan": last,
            "unsent": n != st.get("base_changes"), "pending": bool(st.get("pending")), "offline_since": st.get("offline_since")}


def sync_once(manual: bool = False) -> dict:
    """One round with the hub. Never raises: the outcome is written to the state."""
    if not running():
        return state()
    with _lock:
        st = state()
        eid = settings()["event_id"]
        was_offline = bool(st.get("offline_since"))
        try:
            hb = _json("POST", "/hub/api/heartbeat", _status())
        except HubError as e:
            if e.status in (401, 403):
                return _update(last_error=str(e), last_try=_now())
            return _update(last_error=str(e), last_try=_now(), offline_since=st.get("offline_since") or _now())
        now_ok = {"last_ok": time.time(), "last_seen_hub": _now(), "last_error": "", "offline_since": None}
        if hb.get("event") == "gone":
            return _update(**now_ok, released=True, pending={"kind": "gone", "since": _now()})
        if not hb.get("mine"):
            # released on the hub (or given to another node): ask before doing anything
            if not st.get("pending"):
                return _update(**now_ok, released=True, holder=hb.get("holder") or "",
                               pending={"kind": "released", "since": _now(), "outage": st.get("offline_since"), "hub_rev": hb.get("rev")})
            return _update(**now_ok)
        if st.get("pending") and st["pending"].get("kind") != "released":
            return _update(**now_ok, hub_rev=hb.get("rev"))
        hub_rev = int(hb.get("rev") or 0)
        local_changed = _local_changes() != st.get("base_changes")
        hub_changed = hub_rev != st.get("base_rev")
        try:
            if hub_changed and was_offline:
                return _update(**now_ok, hub_rev=hub_rev,
                               pending={"kind": "reconnect", "since": _now(), "outage": st.get("offline_since"), "hub_rev": hub_rev,
                                        "local_changed": local_changed})
            if hub_changed:
                _merge_from_hub(eid, st)
                return _update(**now_ok, last_merge=_now())
            if local_changed:
                new = _push(eid, st.get("base_rev", 0))
                _set_base(new)
                return _update(**now_ok, last_push=_now(), back_online=_now() if was_offline else st.get("back_online"))
            return _update(**now_ok)
        except HubError as e:
            if e.status == 409 and e.body and "rev" in (e.body or {}):   # the hub changed between our look and our send
                return _update(**now_ok, last_error="", hub_rev=e.body["rev"])
            return _update(last_error=str(e), last_try=_now())
        except Exception as e:  # noqa: BLE001 - a sync problem must never stop the node
            return _update(last_error=f"{type(e).__name__}: {e}", last_try=_now())


def _merge_from_hub(eid: int, st: dict) -> dict:
    """Bring the hub's changes in (and send the result back when this node changed too)."""
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NODE_DIR / "hub-copy.db"
    try:
        rev, manifest = _download(eid, tmp)
        local_changed = _local_changes() != st.get("base_changes")   # before the merge writes anything
        p = preview_plan(tmp)
        from . import snapshots
        snapshots.snapshot("beforemerge")
        with get_db() as db:
            merge.apply(db, p["result"])
        if manifest.get("logo"):
            for old in UPLOAD_DIR.glob("event-logo.*"):
                old.unlink(missing_ok=True)
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            (UPLOAD_DIR / manifest["logo"][0]).write_bytes(manifest["logo"][1])
        new = _push(eid, rev, reason="merged") if local_changed else rev
        _set_base(new)
        _update(last_conflicts=[_conflict_text(c) for c in p["conflicts"]][:50])
        return p
    finally:
        _cleanup(tmp)


def preview_plan(hub_copy: Path) -> dict:
    base = open_db(BASE_PATH) if BASE_PATH.exists() else open_db(hub_copy)
    hubc = open_db(hub_copy)
    try:
        with get_db() as db:
            return merge.plan(merge.load(base), merge.load(db), merge.load(hubc))
    finally:
        base.close(); hubc.close()


def _conflict_text(c: dict) -> str:
    if c["kind"] == "both":
        return f"{c['key']}: {c['field']} was {merge.fmt(c['hub'])} on the hub, {merge.fmt(c['node'])} here. Kept {merge.fmt(c['node'])}."
    return f"{c['key']}: {c['detail']}"


def preview() -> dict:
    """What Merge would do, for the prompt."""
    eid = settings()["event_id"]
    tmp = NODE_DIR / "preview.db"
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        rev, _ = _download(eid, tmp)
        p = preview_plan(tmp)
        return {"rev": rev, "lines": merge.summary_lines(p), "conflicts": [_conflict_text(c) for c in p["conflicts"]][:30],
                "n_conflicts": len(p["conflicts"])}
    finally:
        _cleanup(tmp)


def decide(choice: str) -> str:
    """The admin's answer to the prompt: merge, overwrite (this node wins) or pull (the hub wins)."""
    from . import snapshots
    eid = settings()["event_id"]
    with _lock:
        st = state()
        if st.get("released") or (st.get("pending") or {}).get("kind") in ("released", "gone"):
            if (st.get("pending") or {}).get("kind") == "gone":
                raise HubError("The hub no longer has this event. Hand it back or unlink to keep working alone.")
            _json("POST", f"/hub/api/events/{eid}/take")   # take it back first; refused while another node has it
        if choice == "merge":
            _update(pending=None, released=False)
            p = _merge_from_hub(eid, st)
            out = f"Merged. {len(p['conflicts'])} conflicts, this node's values kept." if p["conflicts"] else "Merged."
        elif choice == "overwrite":
            new = _push(eid, st.get("base_rev", 0), force=True)
            _set_base(new)
            out = "The hub now has this node's copy. Its previous copy was kept on the hub."
        elif choice == "pull":
            tmp = NODE_DIR / "incoming.db"
            try:
                rev, manifest = _download(eid, tmp)
                snapshots.snapshot("beforepull")
                eventfile.replace_live(DB_PATH, tmp, UPLOAD_DIR, manifest.get("logo"))
            finally:
                _cleanup(tmp)
            _set_base(rev)
            out = "This node now has the hub's copy. Its previous data was kept as a snapshot under Backup."
        else:
            raise ValueError("Choose merge, overwrite or pull")
        _update(pending=None, released=False, last_error="", last_ok=time.time(), offline_since=None, last_decision=f"{_now()} {choice}")
    return out


# -- the loop ---------------------------------------------------------------------------

def _loop() -> None:
    while not _stop.is_set():
        try:
            sync_once()
        except Exception:  # noqa: BLE001
            pass
        _wake.wait(SYNC_SECONDS)
        _wake.clear()


def start() -> None:
    global _thread
    if running() and (_thread is None or not _thread.is_alive()):
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="rdr-node")
        _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()


def poke() -> None:
    """Sync soon (after Sync now, or when the event was just taken)."""
    if _thread is None or not _thread.is_alive():
        start()
    _wake.set()


def badge() -> Optional[dict]:
    """For the admin header: linked or not, online, waiting for a decision."""
    if not linked():
        return None
    st, s = state(), settings()
    online = bool(st.get("last_ok") and time.time() - st["last_ok"] < SYNC_SECONDS * 3) and not st.get("offline_since")
    return {"event": s["event_name"], "running": running(), "online": online, "pending": bool(st.get("pending")),
            "offline_since": st.get("offline_since"), "error": st.get("last_error") or ""}

