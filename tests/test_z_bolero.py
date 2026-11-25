"""Bolero antenna link against a small fake antenna served on localhost."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import bolero

SAMPLE_BP = [{"id": 0, "number": 1, "name": "EP"}, {"id": 60, "number": 61, "name": "Alex"}, {"id": 64, "number": 65, "name": "Jamal"}]
SAMPLE_STATUS = [{"bpId": 0, "online": True, "battery": 88}, {"bpId": 60, "status": "online", "battery": {"level": 42}},
                 {"bpId": 64, "connected": False, "battery": 100, "charging": True}]
SAMPLE_PORTS = [{"type": 1, "bpId": 60, "nodeName": "ANT-STAGE"}, {"type": 2, "bpId": 0}]
SAMPLE_CHARGERS = {"items": [{"id": 7, "name": "Charger A", "bays": [{"bay": 1, "bpNumber": 65, "state": "charging"}, {"bay": 2, "bpNumber": None}]}]}


class FakeAntenna(BaseHTTPRequestHandler):
    posts: list = []
    gets: list = []
    charger_resource = "chargers"

    def log_message(self, *a):  # quiet
        pass

    def _auth(self):
        return self.headers.get("Authorization", "")

    def do_GET(self):
        path = self.path.split("/rest/", 1)[-1]
        FakeAntenna.gets.append(path)
        data = {"bp": SAMPLE_BP, "nodeStatus": SAMPLE_STATUS, "audioPorts": SAMPLE_PORTS, self.charger_resource: SAMPLE_CHARGERS}.get(path)
        if data is None:
            self.send_response(404); self.end_headers(); return
        body = json.dumps(data).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        import base64
        creds = base64.b64decode(self._auth().split()[-1]).decode()
        if creds != "admin:secret":
            self.send_response(401); self.end_headers(); return
        path = self.path.split("/rest/", 1)[-1]
        ok = (path.startswith("bp/") and path.endswith("/locate")) or path == "chargers/7/bays/1/locate"
        if not ok:
            self.send_response(404); self.end_headers(); return
        FakeAntenna.posts.append((path, body))
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b"{}")


@pytest.fixture(scope="module")
def antenna():
    srv = HTTPServer(("127.0.0.1", 0), FakeAntenna)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_parsing_is_tolerant():
    packs = bolero.parse_packs(SAMPLE_BP, SAMPLE_STATUS, SAMPLE_PORTS)
    assert packs["61"]["name"] == "Alex" and packs["61"]["online"] is True and packs["61"]["battery"] == 42 and packs["61"]["antenna"] == "ANT-STAGE"
    assert packs["65"]["online"] is False and packs["65"]["charging"] is True
    ch = bolero.parse_chargers(SAMPLE_CHARGERS)
    assert ch[0]["name"] == "Charger A" and ch[0]["bays"][0]["number"] == "65" and ch[0]["bays"][1]["number"] == ""


def test_private_address_rule():
    assert bolero._is_private("192.168.1.20") and bolero._is_private("10.0.0.5:80") and bolero._is_private("127.0.0.1:9999")
    assert not bolero._is_private("8.8.8.8") and not bolero._is_private("riedel.net")


def test_poll_pack_info_and_identify(antenna, monkeypatch):
    cfg = {"bolero_host": antenna, "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": ""}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    st = bolero.poll_now()
    assert st["ok"], st["error"]
    assert st["charger_resource"] == "chargers"
    alex = bolero.pack_info("61")
    assert alex["known"] and alex["online"] is True and alex["battery"] == 42 and alex["antenna"] == "ANT-STAGE" and alex["charger"] is None
    jamal = bolero.pack_info("65")
    assert jamal["online"] is False and jamal["charger"] == {"id": 7, "name": "Charger A", "bay": 1, "state": "charging"}
    # a docked pack flashes through its charger bay (the fake only accepts the bays/<n>/locate form)
    r = bolero.identify("65", seconds=0)
    assert r["via"] == "charger" and FakeAntenna.posts[-1] == ("chargers/7/bays/1/locate", {"active": 1})
    assert bolero.status()["locate_form"]["docked"] == "chargers/7/bays/1/locate"
    bolero.identify("65", on=False)
    assert FakeAntenna.posts[-1] == ("chargers/7/bays/1/locate", {"active": 0})
    # a pack that is not in a charger is never flashed, even when it is on air
    n = len(FakeAntenna.posts)
    with pytest.raises(ValueError, match="not in a charger"):
        bolero.identify("61", seconds=0)
    assert len(FakeAntenna.posts) == n
    # wrong password
    cfg["bolero_admin_password"] = "nope"
    with pytest.raises(ValueError, match="refused the admin password"):
        bolero.identify("65", seconds=0)
    assert bolero.pack_info("99")["known"] is False


def test_admin_bolero_page(antenna, monkeypatch, client):
    cfg = {"bolero_host": antenna, "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": ""}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    c = client
    c.post("/start", data={"tech_id": 1, "site_id": 1})
    page = c.get("/admin/login").text
    c.post("/admin/login", data={"password": "test1234", "confirm": "test1234"} if "Set the admin password" in page else {"password": "test1234"})
    bolero.poll_now()
    r = c.get("/admin/bolero")
    assert r.status_code == 200 and "Packs seen" in r.text and "Charger A" in r.text and "ANT-STAGE" in r.text
    r = c.get("/admin/bolero?path=bp")
    assert 'Alex' in r.text and 'nodeStatus' in r.text
    assert c.get("/admin/bolero?path=../x").status_code == 200
    assert "Bolero antenna link" not in c.get("/admin/settings").text


def test_several_hosts_merge(antenna, monkeypatch):
    cfg = {"bolero_host": f"{antenna}, 127.0.0.1:1", "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": "", "bolero_mode": "artist"}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    st = bolero.poll_now()
    assert st["ok"] and len(st["packs"]) == 3 and "127.0.0.1:1" in st["error"]   # one antenna down, the other carries the system
    assert bolero.settings()["hosts"] == [antenna, "127.0.0.1:1"] and bolero.settings()["mode"] == "artist"
    assert bolero.identify("65", seconds=0)["ok"]


def _reset():
    """Forget what was read, without starting the background poller (it would race the counts)."""
    bolero.stop()
    bolero._fetched.clear()
    bolero._state.update(raw={}, charger_probe_at=0.0, fails=0, charger_resource=None)


def test_read_only_guard():
    """Nothing but Identify (a POST to .../locate) can ever be sent to the system, whatever the caller."""
    for method, res in (("POST", "bp/60"), ("PUT", "bp/60/locate"), ("DELETE", "partyline/1"), ("POST", "nodeConfig"), ("PATCH", "bp")):
        with pytest.raises(ValueError, match="only reads"):
            bolero._request("127.0.0.1:1", res, method=method, body={})
    assert all(r.endswith("/locate") for r, _ in bolero._locate_forms({"charger": {"id": 7, "bay": 1}}, 0, True))


def test_gentle_polling(antenna, monkeypatch):
    cfg = {"bolero_host": antenna, "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": ""}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    _reset()
    clock = [1_000_000.0]
    monkeypatch.setattr(bolero.time, "time", lambda: clock[0])
    FakeAntenna.gets.clear()
    bolero.poll_now()   # forced: everything once
    assert sorted(FakeAntenna.gets) == ["audioPorts", "bp", "chargers", "nodeStatus"]
    FakeAntenna.gets.clear(); bolero._poll_once()
    assert FakeAntenna.gets == []                       # nothing due yet: no requests at all
    clock[0] += 6; bolero._poll_once()
    assert FakeAntenna.gets == ["nodeStatus"]           # live status only
    FakeAntenna.gets.clear(); clock[0] += 61; bolero._poll_once()
    assert sorted(FakeAntenna.gets) == ["audioPorts", "bp", "chargers", "nodeStatus"]
    assert bolero.pack_info("61")["known"]              # answers still come from the cache

    # without a charger licence the charger names are looked for rarely, not every poll
    monkeypatch.setattr(FakeAntenna, "charger_resource", "none-here")
    _reset()
    FakeAntenna.gets.clear(); bolero.poll_now()
    probes = [g for g in FakeAntenna.gets if g in bolero.CHARGER_RESOURCES]
    assert len(probes) == 4
    for _ in range(20):
        clock[0] += 15; bolero._poll_once()
    assert len([g for g in FakeAntenna.gets if g in bolero.CHARGER_RESOURCES]) == 4   # 5 minutes on: no new probes
    clock[0] += bolero.CHARGER_PROBE_EVERY; bolero._poll_once()
    assert len([g for g in FakeAntenna.gets if g in bolero.CHARGER_RESOURCES]) == 8


def test_idle_and_backoff(monkeypatch):
    clock = [2_000_000.0]
    monkeypatch.setattr(bolero.time, "time", lambda: clock[0])
    monkeypatch.setitem(bolero._state, "fails", 0)
    bolero._touch()
    assert bolero._next_wait() == bolero.POLL_SECONDS
    clock[0] += bolero.IDLE_AFTER + 1
    assert bolero._next_wait() == bolero.IDLE_POLL_SECONDS   # nobody looking: slow down
    bolero._state["fails"] = 1
    assert bolero._next_wait() == bolero.POLL_SECONDS * 2
    bolero._state["fails"] = 50
    assert bolero._next_wait() == bolero.MAX_BACKOFF          # antenna down: at most one try every two minutes


def test_identify_once_and_off_after_undock(antenna, monkeypatch):
    cfg = {"bolero_host": antenna, "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": ""}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(FakeAntenna, "charger_resource", "chargers")
    _reset(); bolero.poll_now()
    n = len(FakeAntenna.posts)
    assert bolero.identify("65", seconds=60)["ok"]
    assert bolero.identify("65", seconds=60).get("already")     # a second kiosk picking the same person: no second write
    assert len(FakeAntenna.posts) == n + 1
    # the pack is taken out of the charger before the timer ends: flashing is still turned off at the same bay
    bolero._state["chargers"] = []
    bolero._identify_timers["65"].cancel(); bolero._safe_off("65")
    assert FakeAntenna.posts[-1] == ("chargers/7/bays/1/locate", {"active": 0})
    assert "65" not in bolero._identify_timers
