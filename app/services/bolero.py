"""Riedel Bolero link: standalone (AES67) and Artist-connected systems.

The antennas serve the same undocumented REST interface their own web page uses,
at http://<antenna>/rest/<resource>. Only the core (DECT master) antenna is needed, in
standalone and Artist-connected mode alike: it reports every pack and charger. The
code tolerates a comma-separated list of addresses (answers merged) but the page asks
for one. Reads use the built-in read-only account
the Riedel page itself uses; writes (Identify) need the admin password. This is
not a Riedel-supported interface and a firmware update may change it, so every
read is tolerant of unknown field names and the raw answers can be inspected
from Admin > Settings > Bolero.

Read only: every request is a GET with the read-only account, except Identify (a POST to a
.../locate resource). ``_request`` refuses anything else, so no code path can change the system.

What RDR uses it for:
  * status of the pack carrying a person's Bolero ID: online / offline, battery, antenna
  * where a docked pack is: charger and bay (reported only with the Charger Monitoring
    licence on networked chargers)
  * Identify: flash a pack docked in a networked, licensed charger (through the charger
    and bay) for a few seconds so the tech can find it. A pack that is not in a charger is
    never flashed: it may be on someone's belt during the show.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from ..config import load_config, save_config

READ_AUTH = ("user", "userPass")          # the read-only account the Bolero web page uses
IDENTIFY_SECONDS = 10
CHARGER_RESOURCES = ("chargers", "charger", "chargerStatus", "chargerList")   # tried in turn until one answers

# Load on the antenna. RDR is a guest on the show network, so it reads gently: one request at a time,
# each resource only as often as it is useful, much slower when no kiosk is looking at pack data,
# and backing off when the antenna is slow or not answering.
POLL_SECONDS = 5                          # base tick while a kiosk is using pack data
IDLE_AFTER = 120                          # seconds without a kiosk asking before slowing down
IDLE_POLL_SECONDS = 30
MAX_BACKOFF = 120                         # longest wait after repeated failures
INTERVALS = {"nodeStatus": 5, "chargers": 10, "audioPorts": 15, "bp": 60}   # pack names and numbers rarely change
CHARGER_PROBE_EVERY = 600                 # without the charger licence, look for charger data only this often
MAX_BYTES = 4_000_000                     # an answer bigger than this is cut off rather than held in memory
TIMEOUT = 4.0

_lock = threading.Lock()
_state: dict[str, Any] = {"ok": False, "error": "not configured", "packs": {}, "chargers": [], "raw": {}, "updated": 0.0,
                          "charger_resource": None, "charger_probe_at": 0.0, "locate_form": {}, "fails": 0, "requests": 0}
_fetched: dict[tuple[str, str], float] = {}   # (host, resource) -> when it was last read
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_wake = threading.Event()
_last_demand = 0.0
_identify_timers: dict[str, threading.Timer] = {}
_after_poll: list = []   # callbacks run after a good poll (Always use Bolero names)
_identify_off: dict[str, tuple] = {}   # pack -> (host, resource, body[, test_mode]) that turns its flashing off


# -- config -------------------------------------------------------------------

def hosts(raw: str) -> list[str]:
    return [h.strip() for h in re.split(r"[,\s;]+", raw or "") if h.strip()]


def settings() -> dict:
    cfg = load_config()
    return {
        "enabled": bool(cfg.get("bolero_enabled")),
        "host": (cfg.get("bolero_host") or "").strip(),
        "hosts": hosts(cfg.get("bolero_host") or ""),
        "mode": cfg.get("bolero_mode") or "standalone",
        "has_password": bool(cfg.get("bolero_admin_password")),
        "charger_resource": cfg.get("bolero_charger_resource") or "",
        "names": names_mode(cfg) != "off",
        "names_mode": names_mode(cfg),
    }


def names_mode(cfg: dict) -> str:
    """Pack names: off, ask (prompt when they differ) or always (use the Bolero name without asking)."""
    v = cfg.get("bolero_names")
    if v is True:
        return "ask"
    return v if v in ("ask", "always") else "off"


def configure(host: str, enabled: bool, admin_password: Optional[str], clear_password: bool = False, charger_resource: str = "", mode: str = "standalone", names: str = "off") -> None:
    cfg = load_config()
    for h in hosts(host):
        if not _is_private(h):
            raise ValueError(f"{h}: addresses must be on the show network (10.x, 172.16-31.x, 192.168.x)")
    cfg["bolero_host"] = ", ".join(hosts(host))
    cfg["bolero_mode"] = "artist" if mode == "artist" else "standalone"
    cfg["bolero_enabled"] = bool(enabled and hosts(host))
    cfg["bolero_charger_resource"] = charger_resource.strip().strip("/")
    cfg["bolero_names"] = names if names in ("ask", "always") else ("ask" if names is True else "off")
    if clear_password:
        cfg.pop("bolero_admin_password", None)
    elif admin_password:
        cfg["bolero_admin_password"] = admin_password
    save_config(cfg)
    with _lock:
        _state["charger_resource"] = None
    restart()


def _is_private(host: str) -> bool:
    """Antennas live on the show LAN; refuse anything else so a typo cannot hit the internet."""
    h = host.split(":")[0]
    if h in ("localhost",) or h.startswith("127."):
        return True
    m = re.match(r"^(\d+)\.(\d+)\.\d+\.\d+$", h)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 169 and b == 254)


# -- http -----------------------------------------------------------------------

def _charger_locate(method: str, resource: str, body: Optional[dict]) -> bool:
    """The one write RDR may send: Identify addressed to a charger resource. The antenna's own
    `locate` and `bp/<id>/locate` flash a pack wherever it is (it may be on someone's belt), so
    they are refused here, as is any body that names packs (`nodes`) rather than charger bays."""
    if method != "POST":
        return False
    parts = resource.strip("/").split("/")
    with _lock:
        known = set(CHARGER_RESOURCES) | {_state.get("charger_resource") or ""}
    known |= {(load_config().get("bolero_charger_resource") or "").strip("/")}
    known.discard("")
    if len(parts) < 2 or parts[-1] != "locate" or parts[0] not in known:
        return False
    return not (isinstance(body, dict) and any(str(k).lower() in ("nodes", "node", "bp", "bpid", "bps") for k in body))


def _pack_locate(method: str, resource: str, body: Optional[dict]) -> bool:
    """The pack's own locate (CommsCheck: POST locate {active, nodes: [{id}]}, or bp/<id>/locate). It flashes
    and buzzes a pack wherever it is, so only the admin tester's test mode may send it."""
    if method != "POST":
        return False
    r = resource.strip("/")
    if r == "locate":
        return isinstance(body, dict) and set(body) <= {"active", "nodes"}
    return bool(re.fullmatch(r"bp/\d+/locate", r)) and isinstance(body, dict) and set(body) <= {"active"}


def _request(host: str, resource: str, method: str = "GET", body: Optional[dict] = None, auth: tuple[str, str] = READ_AUTH, timeout: float = TIMEOUT,
             test_mode: bool = False) -> Any:
    if not _is_private(host):
        raise ValueError("Antenna address must be on the show network (10.x, 172.16-31.x, 192.168.x)")
    # Read only, with one exception: Identify through a charger bay. Nothing else may ever be written.
    # The admin tester's test mode may also send the pack's own locate (never the kiosk).
    if method != "GET" and not _charger_locate(method, resource, body) and not (test_mode and _pack_locate(method, resource, body)):
        raise ValueError(f"RDR only reads from the Bolero system; refusing {method} {resource}")
    with _lock:
        _state["requests"] = _state.get("requests", 0) + 1
    url = f"http://{host}/rest/{resource.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json", "User-Agent": "RDR"})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def read(resource: str, host: Optional[str] = None) -> Any:
    """GET any /rest resource with the read-only account (used by the explorer and the poller)."""
    host = host or (settings()["hosts"] or [""])[0]
    if not host:
        raise ValueError("No antenna address configured")
    return _request(host, resource)


# -- tolerant parsing ---------------------------------------------------------------

def _get(d: dict, *names, default=None):
    """Case-insensitive lookup of the first present key among several spellings."""
    if not isinstance(d, dict):
        return default
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in lower and lower[n.lower()] not in (None, ""):
            return lower[n.lower()]
    return default


def _items(payload: Any) -> list[dict]:
    """A list of records from whatever shape the antenna returns (list, {"items": [...]}, {"0": {...}})."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "list", "nodes", "beltpacks", "bp", "data", "chargers"):
            v = payload.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        vals = [v for v in payload.values() if isinstance(v, dict)]
        if vals and len(vals) >= max(1, len(payload) // 2):
            return vals
    return []


def _truthy(v) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("online", "connected", "true", "yes", "on", "1", "ok", "registered"):
            return True
        if s in ("offline", "disconnected", "false", "no", "off", "0", "not connected", "charging"):
            return False
    return None


def parse_packs(bp: Any, node_status: Any, audio_ports: Any) -> dict[str, dict]:
    """Packs keyed by their number (the Bolero ID people are given): name, online, battery, antenna, internal id."""
    packs: dict[str, dict] = {}
    for i, rec in enumerate(_items(bp)):
        # the antenna's /bp: {"bp": [{"id": 0, "registered": true, "lastConnectTime": ..., "bpConfig": {"bpName": "Jonath.S", "bpNumber": 1, ...}}]}
        cfg = rec.get("bpConfig") if isinstance(rec.get("bpConfig"), dict) else {}
        internal = _get(rec, "id", "bpId", "index", default=i)
        number = _get(cfg, "bpNumber", "number", default=None)
        if number is None:
            number = _get(rec, "number", "bpNumber", "no", "beltpackNumber", default=None)
        if number is None:
            number = int(internal) + 1 if isinstance(internal, int) else internal
        name = _get(cfg, "bpName", "name", default=None) or _get(rec, "bpName", "name", "label", default="")
        packs[str(number)] = {"number": str(number), "id": internal, "name": str(name or "").strip(),
                              "registered": _truthy(_get(rec, "registered", default=None)),
                              "last_connect": _get(rec, "lastConnectTime", default=None),
                              "online": None, "battery": None, "antenna": None, "charging": None}
    by_id = {str(p["id"]): p for p in packs.values()}
    for rec in _items(node_status):
        ref = _get(rec, "bpId", "beltpackId", "bp", "id", "nodeId", default=None)
        p = by_id.get(str(ref))
        if p is None:
            num = _get(rec, "number", "bpNumber", default=None)
            p = packs.get(str(num)) if num is not None else None
        if p is None:
            continue
        online = _truthy(_get(rec, "online", "connected", "status", "state", default=None))
        if online is not None:
            p["online"] = online
        bat = _get(rec, "battery", "batteryLevel", "batteryPercent", "bat", "charge", default=None)
        if isinstance(bat, dict):
            bat = _get(bat, "level", "percent", "capacity", default=None)
        if isinstance(bat, (int, float)):
            p["battery"] = int(bat)
        chg = _truthy(_get(rec, "charging", "usbPower", "onCharger", default=None))
        if chg is not None:
            p["charging"] = chg
    for rec in _items(audio_ports):
        if _get(rec, "type", default=None) not in (1, "1", None):
            continue
        ref = _get(rec, "bpId", "beltpackId", "bp", default=None)
        p = by_id.get(str(ref)) if ref is not None else None
        if p is not None:
            p["antenna"] = _get(rec, "nodeName", "antenna", "nodeId", default=None)
    return packs


def parse_chargers(payload: Any) -> list[dict]:
    """[{name, bays: [{bay, number, state}]}] from whatever the charger resource returns (needs the PRO 1 licence)."""
    out = []
    for i, rec in enumerate(_items(payload)):
        name = str(_get(rec, "name", "label", "id", "serial", default=f"Charger {i + 1}"))
        cid = _get(rec, "id", "index", "chargerId", default=i)
        bays_raw = _get(rec, "bays", "slots", "ports", default=None)
        bays = []
        for j, b in enumerate(_items(bays_raw) if bays_raw is not None else []):
            number = _get(b, "bpNumber", "number", "beltpackNumber", "bp", "beltpack", default=None)
            if isinstance(number, dict):
                number = _get(number, "number", "id", default=None)
            state = _get(b, "state", "status", "charging", default="")
            bays.append({"bay": _get(b, "bay", "slot", "index", "id", default=j + 1), "number": (str(number) if number not in (None, "", 0) else ""), "state": str(state)})
        out.append({"id": cid, "name": name, "bays": bays})
    return out


# -- poller -------------------------------------------------------------------------

def _due(host: str, resource: str, now: float, force: bool) -> bool:
    return force or now - _fetched.get((host, resource), 0.0) >= INTERVALS.get(resource, INTERVALS["chargers"])


def _poll_host(host: str, charger_resource: Optional[str], raw: dict, force: bool = False) -> tuple[dict[str, dict], list[dict], Optional[str]]:
    """Read what is due from one antenna; anything not due is taken from the last answer."""
    now = time.time()
    with _lock:
        last = dict((_state.get("raw") or {}).get(host) or {})
    got = dict(last)

    def fetch(resource: str, key: str, optional: bool = False) -> None:
        if key in last and not _due(host, resource, now, force):
            return
        try:
            got[key] = _request(host, resource)
        except Exception:  # noqa: BLE001
            if not optional:
                raise
            got[key] = None
        _fetched[(host, resource)] = now

    fetch("nodeStatus", "nodeStatus")
    fetch("bp", "bp")
    fetch("audioPorts", "audioPorts", optional=True)
    found: Optional[str] = None
    if charger_resource:
        if "chargers" not in last or _due(host, "chargers", now, force):
            try:
                got["chargers"] = _request(host, charger_resource)
                found = charger_resource
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                got["chargers"] = None   # the resource went away (firmware change): look again later
            _fetched[(host, "chargers")] = now
        else:
            found = charger_resource
    else:
        with _lock:
            probe = force or now - _state.get("charger_probe_at", 0.0) >= CHARGER_PROBE_EVERY
            if probe:
                _state["charger_probe_at"] = now
        if probe:   # no charger licence is normal: look for a charger resource rarely, not on every poll
            for res in CHARGER_RESOURCES:
                try:
                    got["chargers"] = _request(host, res)
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        continue
                    raise
                found = res
                _fetched[(host, "chargers")] = now
                break
    raw[host] = got
    packs = parse_packs(got.get("bp"), got.get("nodeStatus"), got.get("audioPorts"))
    chargers = parse_chargers(got.get("chargers")) if found else []
    return packs, chargers, found


def _merge_packs(into: dict[str, dict], more: dict[str, dict]) -> None:
    for n, p in more.items():
        cur = into.get(n)
        if cur is None:
            into[n] = p
            continue
        for k, v in p.items():
            if cur.get(k) in (None, "", False) and v not in (None, ""):
                cur[k] = v
        if p.get("online"):
            cur["online"] = True


def _poll_once(force: bool = False) -> None:
    st = settings()
    if not st["enabled"] or not st["hosts"]:
        with _lock:
            _state.update(ok=False, error="not configured", packs={}, chargers=[])
        return
    raw: dict[str, Any] = {}
    packs: dict[str, dict] = {}
    chargers: list[dict] = []
    errors: list[str] = []
    with _lock:
        resource = st["charger_resource"] or _state.get("charger_resource")
    found: Optional[str] = None
    for host in st["hosts"]:
        try:
            p, c, f = _poll_host(host, resource, raw, force)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{host}: {type(e).__name__}: {e}")
            continue
        _merge_packs(packs, p)
        seen = {x["name"] for x in chargers}
        chargers += [x for x in c if x["name"] not in seen]
        found = found or f
    with _lock:
        _state["charger_resource"] = found if (found or not st["charger_resource"]) else _state.get("charger_resource")
        if packs or not errors:
            _state.update(ok=True, error="; ".join(errors), packs=packs, chargers=chargers, raw=raw, updated=time.time(), fails=0)
        else:
            _state.update(ok=False, error="; ".join(errors), updated=time.time(), fails=_state.get("fails", 0) + 1)
            return
    for fn in list(_after_poll):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


def _touch() -> None:
    """A kiosk wants pack data: poll at the normal pace again (and right away after a quiet spell)."""
    global _last_demand
    idle = time.time() - _last_demand > IDLE_AFTER
    _last_demand = time.time()
    if idle:
        _wake.set()


def _next_wait() -> float:
    with _lock:
        fails = _state.get("fails", 0)
    if fails:
        return min(MAX_BACKOFF, POLL_SECONDS * 2 ** min(fails, 6))
    return IDLE_POLL_SECONDS if time.time() - _last_demand > IDLE_AFTER else POLL_SECONDS


def _loop() -> None:
    while not _stop.is_set():
        _wake.wait(_next_wait())
        _wake.clear()
        if _stop.is_set():
            break
        try:
            _poll_once()
        except Exception:  # noqa: BLE001
            pass


def start() -> None:
    global _thread
    if settings()["enabled"] and (_thread is None or not _thread.is_alive()):
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="rdr-bolero")
        _thread.start()
        threading.Thread(target=_poll_once, kwargs={"force": True}, daemon=True).start()


def stop() -> None:
    _stop.set()
    _wake.set()


def restart() -> None:
    stop()
    time.sleep(0.05)
    _stop.clear()
    _wake.clear()
    _fetched.clear()
    with _lock:
        _state.update(raw={}, charger_probe_at=0.0, fails=0)
    global _thread
    _thread = None
    start()


def poll_now() -> dict:
    """An admin pressed Poll now or saved the settings: read everything once."""
    _poll_once(force=True)
    return status()


def status() -> dict:
    with _lock:
        return {k: (dict(v) if isinstance(v, dict) and k == "packs" else v) for k, v in _state.items() if k != "raw"} | {"settings": settings()}


def pack_names() -> dict[str, str]:
    """Pack number to the name set on the pack, from the last good poll (empty when the link is off or down)."""
    if not settings()["enabled"]:
        return {}
    _touch()
    with _lock:
        if not _state["ok"]:
            return {}
        return {n: (p.get("name") or "").strip() for n, p in _state["packs"].items()}


def raw() -> dict:
    with _lock:
        return dict(_state.get("raw") or {})


# -- per pack ---------------------------------------------------------------------------

def pack_info(number: str) -> dict:
    """Everything the kiosk shows beside a Bolero ID: online, battery, antenna, charger bay."""
    number = str(number or "").strip().lstrip("0") or str(number)
    _touch()
    with _lock:
        ok, err = _state["ok"], _state["error"]
        p = dict(_state["packs"].get(number) or {})
        chargers = list(_state["chargers"])
        has_charger_data = bool(_state.get("charger_resource"))
    info = {"configured": settings()["enabled"], "ok": ok, "error": err, "number": number, "known": bool(p), "can_identify": settings()["has_password"]}
    info.update({k: p.get(k) for k in ("name", "online", "battery", "antenna", "charging")})
    info["charger"] = None
    bays = [(c, b) for c in chargers for b in c["bays"] if b["number"] and b["number"].lstrip("0") == number]
    if bays:
        c, b = bays[0]
        info["charger"] = {"id": c.get("id"), "name": c["name"], "bay": b["bay"], "state": b["state"]}
    info["charger_data"] = has_charger_data
    info["flash_block"] = _flash_block(info, len(bays))
    info["can_flash"] = info["can_identify"] and not info["flash_block"]
    return info


EMPTY_BAY = re.compile(r"^(empty|free|none|removed|vacant|no ?pack|absent|unplugged|out|0)$", re.I)


def _flash_block(info: dict, n_bays: int) -> str:
    """Why this pack must not be flashed, or "" when it is safely docked. A pack out of a charger
    may be on someone's belt mid-show, so anything short of a clear, single, occupied bay with the
    pack off air is a no."""
    n = info["number"]
    if not info.get("known"):
        return f"No Bolero {n} on the system"
    if not info.get("charger"):
        if not info.get("charger_data"):
            return f"Bolero {n} is not seen in a networked charger (no charger data from the system), so it is not flashed"
        return f"Bolero {n} is not in a charger, so it is not flashed"
    if n_bays > 1:
        return f"Bolero {n} shows in more than one charger bay, so it is not flashed"
    if EMPTY_BAY.match(str(info["charger"].get("state") or "").strip()):
        return f"The charger bay for Bolero {n} reads as empty, so it is not flashed"
    if info.get("online") or info.get("antenna"):
        return f"Bolero {n} is on air, so it is not flashed even though a charger lists it"
    return ""


def _locate_forms(info: dict, bp_id, on: bool) -> list[tuple[str, dict]]:
    """Candidate (resource, body) pairs for Identify on a docked pack, most likely first. The exact
    shapes are not published; the first that the system accepts is remembered."""
    act = 1 if on else 0
    ch = info["charger"]
    with _lock:
        res = _state.get("charger_resource") or "chargers"
    cid, bay = ch.get("id"), ch.get("bay")
    return [
        (f"{res}/{cid}/locate", {"active": act, "bays": [{"id": bay}]}),
        (f"{res}/{cid}/bays/{bay}/locate", {"active": act}),
        (f"{res}/{cid}/bay/{bay}/locate", {"active": act}),
        (f"{res}/locate", {"active": act, "chargers": [{"id": cid, "bays": [{"id": bay}]}]}),
    ]


def flash_check(number: str) -> dict:
    """For the tester: read the antennas again, run every check a flash would, and say what would be sent.
    Sends nothing to the system."""
    cfg = load_config()
    steps: list[dict] = []
    if not hosts(cfg.get("bolero_host") or ""):
        return {"number": str(number), "ok": False, "steps": [{"ok": False, "text": "No antenna address set"}], "would_send": []}
    steps.append({"ok": bool(cfg.get("bolero_admin_password")), "text": "Admin password saved" if cfg.get("bolero_admin_password") else "No admin password saved: flashing needs it"})
    _poll_once(force=True)
    with _lock:
        fresh = bool(_state.get("ok")) and not _state.get("error")
        err = _state.get("error")
    steps.append({"ok": fresh, "text": "Every antenna answered just now" if fresh else f"Not every antenna answered: {err}"})
    info = pack_info(number)
    n = info["number"]
    steps.append({"ok": info["known"], "text": f"Bolero {n} is on the system" + (f", named {info['name']}" if info.get("name") else "") if info["known"] else f"No Bolero {n} on the system"})
    if info["known"]:
        on_air = bool(info.get("online") or info.get("antenna"))
        steps.append({"ok": not on_air, "text": f"Bolero {n} is on air" + (f" via {info['antenna']}" if info.get("antenna") else "") if on_air else f"Bolero {n} is off air"})
    ch = info.get("charger")
    if ch:
        steps.append({"ok": True, "text": f"In {ch['name']}, bay {ch['bay']}" + (f" ({ch['state']})" if ch.get("state") else "")})
    elif info["known"]:
        steps.append({"ok": False, "text": "No charger data from the system" if not info.get("charger_data") else f"Bolero {n} is not in a charger"})
    block = info["flash_block"] or ("" if fresh else f"Could not read every antenna just now, so Bolero {n} is not flashed")
    would = []
    if not block:
        with _lock:
            bp_id = _state["packs"].get(n, {}).get("id")
            remembered = (_state.get("locate_form") or {}).get("docked")
        forms = _locate_forms(info, bp_id, True)
        if remembered in [f[0] for f in forms]:
            forms.sort(key=lambda f: f[0] != remembered)
        would = [{"resource": r, "body": b, "known_good": r == remembered} for r, b in forms]
    return {"number": n, "ok": not block and bool(cfg.get("bolero_admin_password")), "block": block, "steps": steps, "would_send": would,
            "flashing": n in _identify_timers}


def identify(number: str, on: bool = True, seconds: int = IDENTIFY_SECONDS) -> dict:
    """Flash a pack docked in a networked charger, through its bay. A pack that is not in a charger
    is never flashed. Identify is a toggle, so RDR turns it off again after `seconds`."""
    cfg = load_config()
    host_list, pw = hosts(cfg.get("bolero_host") or ""), cfg.get("bolero_admin_password") or ""
    if not host_list or not pw:
        raise ValueError("Set the antenna address and admin password on the Bolero link page first")
    info = pack_info(number)
    if on and info["number"] in _identify_timers:   # already flashing (another kiosk, or picked again): no second write
        return {"ok": True, "number": info["number"], "on": True, "seconds": seconds, "via": "charger", "charger": info["charger"], "resource": None, "already": True}
    if on:
        # Never flash from cached data: the pack may have left the charger since the last read.
        _poll_once(force=True)
        info = pack_info(number)
        with _lock:
            fresh = _state.get("ok") and not _state.get("error")
        if not fresh:
            raise ValueError(f"Could not read every antenna just now, so Bolero {number} is not flashed")
    if not on and info["number"] in _identify_off:   # stop flashing with the request that started it
        t = _identify_timers.pop(info["number"], None)
        if t:
            t.cancel()
        _safe_off(info["number"])
        return {"ok": True, "number": info["number"], "on": False, "seconds": 0, "via": "charger", "charger": info["charger"], "resource": None}
    if info["flash_block"]:
        raise ValueError(info["flash_block"])
    with _lock:
        bp_id = _state["packs"].get(info["number"], {}).get("id")
        remembered = dict(_state.get("locate_form") or {})
    kind = "docked"
    forms = _locate_forms(info, bp_id, on)
    if kind in remembered and remembered[kind] in [f[0] for f in forms]:
        forms.sort(key=lambda f: f[0] != remembered[kind])
    auth = ("admin", pw)
    last_err: Optional[Exception] = None
    used = None
    used_host = None
    for host in host_list:
        for resource, payload in forms:
            try:
                _request(host, resource, method="POST", body=payload, auth=auth)
                used, used_host = resource, host
                last_err = None
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (401, 403):
                    raise ValueError("The antenna refused the admin password") from e
                continue
            except Exception as e:  # noqa: BLE001
                last_err = e
                break
        if used:
            break
    if last_err is not None:
        raise ValueError(f"Identify failed for Bolero {number} (in {info['charger']['name']}, bay {info['charger']['bay']}): {last_err}")
    with _lock:
        _state.setdefault("locate_form", {})[kind] = used
    key = info["number"]
    if on:
        off_body = next(b for r, b in _locate_forms(info, bp_id, False) if r == used)
        _identify_off[key] = (used_host, used, off_body)
        t = _identify_timers.pop(key, None)
        if t:
            t.cancel()
        if seconds > 0:
            timer = threading.Timer(seconds, lambda: _safe_off(key))
            timer.daemon = True
            timer.start()
            _identify_timers[key] = timer
    return {"ok": True, "number": key, "on": on, "seconds": seconds if on else 0, "via": "charger", "charger": info["charger"], "resource": used}


def _safe_off(number: str) -> None:
    """Turn flashing off with the same request that turned it on, even if the pack has left the charger since."""
    _identify_timers.pop(number, None)
    target = _identify_off.pop(number, None)
    pw = load_config().get("bolero_admin_password") or ""
    if not target or not pw:
        return
    host, resource, body = target[:3]
    test_mode = len(target) > 3 and target[3]
    for attempt in range(2):
        try:
            _request(host, resource, method="POST", body=body, auth=("admin", pw), test_mode=test_mode)
            return
        except Exception:  # noqa: BLE001
            time.sleep(1)


def test_locate(number: str, how: str = "pack", seconds: int = IDENTIFY_SECONDS) -> dict:
    """Admin tester, test mode: locate a pack WITHOUT the charger checks, to see whether locate works on this
    system at all. how="pack" sends the pack's own locate (it flashes and buzzes wherever the pack is);
    how="charger" sends Identify to its charger bay even if the pack looks on air. Turned off after `seconds`.
    Every request tried, and what the system answered, is returned."""
    cfg = load_config()
    host_list, pw = hosts(cfg.get("bolero_host") or ""), cfg.get("bolero_admin_password") or ""
    if not host_list or not pw:
        raise ValueError("Set the antenna address and admin password first")
    _poll_once(force=True)
    info = pack_info(number)
    n = info["number"]
    with _lock:
        bp_id = (_state["packs"].get(n) or {}).get("id")
    if not info["known"] or bp_id is None:
        raise ValueError(f"No Bolero {n} on the system")
    if how == "charger":
        if not info.get("charger"):
            raise ValueError(f"Bolero {n} is not listed in any charger bay, so there is no bay to send Identify to")
        forms = _locate_forms(info, bp_id, True)
        off = {r: b for r, b in _locate_forms(info, bp_id, False)}
    else:
        forms = [("locate", {"active": 1, "nodes": [{"id": bp_id}]}), (f"bp/{bp_id}/locate", {"active": 1})]
        off = {"locate": {"active": 0, "nodes": [{"id": bp_id}]}, f"bp/{bp_id}/locate": {"active": 0}}
    old = _identify_timers.pop(n, None)
    if old:
        old.cancel()
        _safe_off(n)
    attempts, used = [], None
    for host in host_list:
        for resource, body in forms:
            try:
                answer = _request(host, resource, method="POST", body=body, auth=("admin", pw), test_mode=True)
                attempts.append({"host": host, "resource": resource, "body": body, "ok": True, "answer": str(answer)[:300] if answer else "accepted"})
                used = (host, resource)
                break
            except urllib.error.HTTPError as e:
                attempts.append({"host": host, "resource": resource, "body": body, "ok": False, "answer": f"HTTP {e.code} {e.reason}"})
                if e.code in (401, 403):
                    raise ValueError("The antenna refused the admin password") from e
            except Exception as e:  # noqa: BLE001
                attempts.append({"host": host, "resource": resource, "body": body, "ok": False, "answer": f"{type(e).__name__}: {e}"})
                break
        if used:
            break
    if used:
        _identify_off[n] = (used[0], used[1], off[used[1]], True)
        timer = threading.Timer(seconds, lambda: _safe_off(n))
        timer.daemon = True
        timer.start()
        _identify_timers[n] = timer
    return {"number": n, "name": info.get("name"), "how": how, "ok": bool(used), "seconds": seconds, "attempts": attempts,
            "charger": info.get("charger"), "online": info.get("online"), "antenna": info.get("antenna")}


def test_stop(number: str) -> bool:
    """Stop whatever the tester started for this pack, with the request that started it."""
    n = str(number or "").strip().lstrip("0") or str(number)
    t = _identify_timers.pop(n, None)
    if t:
        t.cancel()
    if n in _identify_off:
        _safe_off(n)
        return True
    return False
