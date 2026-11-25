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
POLL_SECONDS = 3
IDENTIFY_SECONDS = 10
CHARGER_RESOURCES = ("chargers", "charger", "chargerStatus", "chargerList")   # tried in turn until one answers
_lock = threading.Lock()
_state: dict[str, Any] = {"ok": False, "error": "not configured", "packs": {}, "chargers": [], "raw": {}, "updated": 0.0, "charger_resource": None, "locate_form": {}}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_identify_timers: dict[str, threading.Timer] = {}


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
        "names": bool(cfg.get("bolero_names")),
    }


def configure(host: str, enabled: bool, admin_password: Optional[str], clear_password: bool = False, charger_resource: str = "", mode: str = "standalone", names: bool = False) -> None:
    cfg = load_config()
    for h in hosts(host):
        if not _is_private(h):
            raise ValueError(f"{h}: addresses must be on the show network (10.x, 172.16-31.x, 192.168.x)")
    cfg["bolero_host"] = ", ".join(hosts(host))
    cfg["bolero_mode"] = "artist" if mode == "artist" else "standalone"
    cfg["bolero_enabled"] = bool(enabled and hosts(host))
    cfg["bolero_charger_resource"] = charger_resource.strip().strip("/")
    cfg["bolero_names"] = bool(names)
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

def _request(host: str, resource: str, method: str = "GET", body: Optional[dict] = None, auth: tuple[str, str] = READ_AUTH, timeout: float = 4.0) -> Any:
    if not _is_private(host):
        raise ValueError("Antenna address must be on the show network (10.x, 172.16-31.x, 192.168.x)")
    url = f"http://{host}/rest/{resource.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json", "User-Agent": "RDR"})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
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
        internal = _get(rec, "id", "bpId", "index", default=i)
        number = _get(rec, "number", "bpNumber", "no", "beltpackNumber", default=None)
        if number is None:
            number = int(internal) + 1 if isinstance(internal, int) else internal
        packs[str(number)] = {"number": str(number), "id": internal, "name": str(_get(rec, "name", "label", default="") or ""),
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

def _poll_host(host: str, charger_resource: str, raw: dict) -> tuple[dict[str, dict], list[dict], Optional[str]]:
    bp = _request(host, "bp")
    node_status = _request(host, "nodeStatus")
    try:
        audio_ports = _request(host, "audioPorts")
    except Exception:  # noqa: BLE001 - optional
        audio_ports = None
    raw[host] = {"bp": bp, "nodeStatus": node_status, "audioPorts": audio_ports}
    packs = parse_packs(bp, node_status, audio_ports)
    chargers: list[dict] = []
    found: Optional[str] = None
    for res in ([charger_resource] if charger_resource else list(CHARGER_RESOURCES)):
        try:
            payload = _request(host, res)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        raw[host]["chargers"] = payload
        chargers = parse_chargers(payload)
        found = res
        break
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


def _poll_once() -> None:
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
            p, c, f = _poll_host(host, resource, raw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{host}: {type(e).__name__}: {e}")
            continue
        _merge_packs(packs, p)
        seen = {x["name"] for x in chargers}
        chargers += [x for x in c if x["name"] not in seen]
        found = found or f
    with _lock:
        if found:
            _state["charger_resource"] = found
        if packs or not errors:
            _state.update(ok=True, error="; ".join(errors), packs=packs, chargers=chargers, raw=raw, updated=time.time())
        else:
            _state.update(ok=False, error="; ".join(errors), updated=time.time(), raw=raw)


def _loop() -> None:
    while not _stop.wait(POLL_SECONDS):
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
        threading.Thread(target=_poll_once, daemon=True).start()


def stop() -> None:
    _stop.set()


def restart() -> None:
    stop()
    time.sleep(0.05)
    _stop.clear()
    global _thread
    _thread = None
    start()


def poll_now() -> dict:
    _poll_once()
    return status()


def status() -> dict:
    with _lock:
        return {k: (dict(v) if isinstance(v, dict) and k == "packs" else v) for k, v in _state.items() if k != "raw"} | {"settings": settings()}


def pack_names() -> dict[str, str]:
    """Pack number to the name set on the pack, from the last good poll (empty when the link is off or down)."""
    if not settings()["enabled"]:
        return {}
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
    with _lock:
        ok, err = _state["ok"], _state["error"]
        p = dict(_state["packs"].get(number) or {})
        chargers = list(_state["chargers"])
        has_charger_data = bool(_state.get("charger_resource"))
    info = {"configured": settings()["enabled"], "ok": ok, "error": err, "number": number, "known": bool(p), "can_identify": settings()["has_password"]}
    info.update({k: p.get(k) for k in ("name", "online", "battery", "antenna", "charging")})
    info["charger"] = None
    for c in chargers:
        for b in c["bays"]:
            if b["number"] and b["number"].lstrip("0") == number:
                info["charger"] = {"id": c.get("id"), "name": c["name"], "bay": b["bay"], "state": b["state"]}
    info["charger_data"] = has_charger_data
    return info


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
        (f"{res}/{cid}/bays/{bay}", {"locate": act}),
    ]


def identify(number: str, on: bool = True, seconds: int = IDENTIFY_SECONDS) -> dict:
    """Flash a pack docked in a networked charger, through its bay. A pack that is not in a charger
    is never flashed. Identify is a toggle, so RDR turns it off again after `seconds`."""
    cfg = load_config()
    host_list, pw = hosts(cfg.get("bolero_host") or ""), cfg.get("bolero_admin_password") or ""
    if not host_list or not pw:
        raise ValueError("Set the antenna address and admin password on the Bolero link page first")
    info = pack_info(number)
    if not info["known"]:
        raise ValueError(f"No Bolero {number} on the system")
    if not info.get("charger"):
        if not info.get("charger_data"):
            raise ValueError(f"Bolero {number} is not seen in a networked charger (no charger data from the system), so it is not flashed")
        raise ValueError(f"Bolero {number} is not in a charger, so it is not flashed")
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
    for host in host_list:
        for resource, payload in forms:
            try:
                _request(host, resource, method="POST", body=payload, auth=auth)
                used = resource
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
    try:
        identify(number, on=False)
    except Exception:  # noqa: BLE001
        pass
