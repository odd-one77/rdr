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
    with pytest.raises(ValueError, match="Could not read every antenna"):   # one antenna silent: it may hear the pack on air
        bolero.identify("65", seconds=0)


def _reset():
    """Forget what was read, without starting the background poller (it would race the counts)."""
    bolero.stop()
    bolero._fetched.clear()
    bolero._state.update(raw={}, charger_probe_at=0.0, fails=0, charger_resource=None)


def test_read_only_guard():
    """Nothing but Identify (a POST to .../locate) can ever be sent to the system, whatever the caller."""
    for method, res, body in (("POST", "bp/60", {}), ("PUT", "bp/60/locate", {}), ("DELETE", "partyline/1", {}), ("POST", "nodeConfig", {}), ("PATCH", "bp", {}),
                              # Identify that is not addressed to a charger bay could flash a pack on someone's belt
                              ("POST", "locate", {"active": 1, "nodes": [{"id": 60}]}), ("POST", "bp/60/locate", {"active": 1}),
                              ("POST", "chargers/locate", {"active": 1, "nodes": [{"id": 60}]}), ("POST", "audioPorts/1/locate", {"active": 1})):
        with pytest.raises(ValueError, match="only reads"):
            bolero._request("127.0.0.1:1", res, method=method, body=body)
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


def test_never_flash_a_pack_out_of_a_charger(antenna, monkeypatch):
    """A pack that is not clearly docked is never flashed: not from stale data, not when on air, not from an empty bay."""
    import sys; me = sys.modules[__name__]   # the module the fake antenna reads its answers from
    chargers0, status0 = SAMPLE_CHARGERS, SAMPLE_STATUS
    cfg = {"bolero_host": antenna, "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": ""}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(FakeAntenna, "charger_resource", "chargers")
    _reset(); bolero._identify_timers.clear(); bolero._identify_off.clear(); bolero.poll_now()
    assert bolero.pack_info("65")["can_flash"] and not bolero.pack_info("61")["can_flash"]
    n = len(FakeAntenna.posts)

    # pulled from the charger since the last read: the flash reads again first and refuses
    monkeypatch.setattr(me, "SAMPLE_CHARGERS", {"items": [{"id": 7, "name": "Charger A", "bays": [{"bay": 1, "bpNumber": None}]}]})
    assert bolero.pack_info("65")["charger"]          # the cache still says docked
    with pytest.raises(ValueError, match="not in a charger"):
        bolero.identify("65", seconds=0)

    # a charger still lists it but the pack is on air: on someone's belt, so no
    monkeypatch.setattr(me, "SAMPLE_CHARGERS", chargers0)
    monkeypatch.setattr(me, "SAMPLE_STATUS", status0[:2] + [{"bpId": 64, "online": True, "battery": 90}])
    with pytest.raises(ValueError, match="on air"):
        bolero.identify("65", seconds=0)

    # the bay reads empty even though it still carries the number
    monkeypatch.setattr(me, "SAMPLE_STATUS", status0)
    monkeypatch.setattr(me, "SAMPLE_CHARGERS", {"items": [{"id": 7, "name": "Charger A", "bays": [{"bay": 1, "bpNumber": 65, "state": "empty"}]}]})
    with pytest.raises(ValueError, match="empty"):
        bolero.identify("65", seconds=0)

    # the same number in two bays: ambiguous, so no
    monkeypatch.setattr(me, "SAMPLE_CHARGERS", {"items": [{"id": 7, "name": "Charger A", "bays": [{"bay": 1, "bpNumber": 65, "state": "charging"}, {"bay": 2, "bpNumber": 65, "state": "charging"}]}]})
    with pytest.raises(ValueError, match="more than one"):
        bolero.identify("65", seconds=0)

    # the antenna is not answering: nothing is flashed from what was read before
    monkeypatch.setattr(me, "SAMPLE_CHARGERS", chargers0)
    cfg["bolero_host"] = "127.0.0.1:1"
    with pytest.raises(ValueError):
        bolero.identify("65", seconds=0)
    assert len(FakeAntenna.posts) == n                # not one write in all of that

    # docked, off air, one bay: flashes
    cfg["bolero_host"] = antenna
    assert bolero.identify("65", seconds=0)["ok"] and len(FakeAntenna.posts) == n + 1


def test_parse_real_bp_shape():
    """The antenna's /bp keeps the name and number inside bpConfig; the internal id is not the pack number."""
    bp = {"bp": [
        {"registered": True, "id": 0, "ipei": [2, 229, 0, 81, 202], "bpType": 3300, "lastConnectTime": 1795451605,
         "bpConfig": {"profileId": 1, "bpName": "Tobias.R", "bpNumber": 1, "bpDescription": "", "keys": []}},
        {"registered": True, "id": 1, "lastConnectTime": 1795815950, "bpConfig": {"bpName": "Nell  .V", "bpNumber": 2}},
        {"registered": False, "id": 2, "bpConfig": {"bpName": "Spare", "bpNumber": 40}},
    ]}
    packs = bolero.parse_packs(bp, [{"bpId": 2, "online": True}], [])
    assert set(packs) == {"1", "2", "40"}
    assert packs["1"]["name"] == "Tobias.R" and packs["1"]["registered"] is True and packs["1"]["last_connect"] == 1795451605
    assert packs["2"]["name"] == "Nell  .V"
    assert packs["40"]["id"] == 2 and packs["40"]["registered"] is False and packs["40"]["online"] is True   # status found by internal id
    from app.services import bolero_names
    assert bolero_names.same_name("Tobias Reinholt", "Tobias.R") and bolero_names.same_name("Nell Varga", "Nell  .V")
    assert bolero_names.pack_style("Tobias Reinholt") == "Tobias.R" and bolero_names.pack_style("Nell Varga") == "Nell  .V"
    assert not bolero_names.same_name("Tobias Reinholt", "Tobias.K")


def test_flash_tester(antenna, monkeypatch, client):
    """Admin, Bolero link, Flash tester: Check sends nothing; Flash and Stop use the kiosk's rules."""
    cfg = {"bolero_host": antenna, "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": ""}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(FakeAntenna, "charger_resource", "chargers")
    _reset(); bolero._identify_timers.clear(); bolero._identify_off.clear()
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    page = client.get("/admin/login").text
    client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"} if "Set the admin password" in page else {"password": "test1234"})
    bolero.poll_now()
    assert "Flash tester" in client.get("/admin/bolero").text
    n = len(FakeAntenna.posts)

    page = client.post("/admin/bolero/test", data={"number": "65", "action": "check"}).text
    assert "Would flash." in page and "In Charger A, bay 1" in page and "chargers/7/locate" in page
    page = client.post("/admin/bolero/test", data={"number": "61", "action": "check"}).text   # on air, not docked
    assert "Would not flash:" in page and "is on air" in page
    assert len(FakeAntenna.posts) == n   # Check never writes

    page = client.post("/admin/bolero/test", data={"number": "61", "action": "flash"}).text
    assert "Not flashed:" in page and len(FakeAntenna.posts) == n
    page = client.post("/admin/bolero/test", data={"number": "65", "action": "flash", "seconds": "5"}).text
    assert "Flashing for 5 seconds in Charger A, bay 1" in page and FakeAntenna.posts[-1] == ("chargers/7/bays/1/locate", {"active": 1})
    page = client.post("/admin/bolero/test", data={"number": "65", "action": "stop"}).text
    assert "Stopped" in page and FakeAntenna.posts[-1] == ("chargers/7/bays/1/locate", {"active": 0})


def test_tester_test_mode(antenna, monkeypatch, client):
    """Test mode (admin tester only): pack locate without the charger checks; the kiosk path still refuses it."""
    cfg = {"bolero_host": antenna, "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": ""}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(FakeAntenna, "charger_resource", "chargers")
    _reset(); bolero._identify_timers.clear(); bolero._identify_off.clear()
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    page = client.get("/admin/login").text
    client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"} if "Set the admin password" in page else {"password": "test1234"})
    bolero.poll_now()
    n = len(FakeAntenna.posts)
    # not ticked: nothing sent
    r = client.post("/admin/bolero/test", data={"number": "61", "action": "test", "how": "pack"})
    assert r.status_code == 303 and "Tick" in r.headers["location"] and len(FakeAntenna.posts) == n
    # Bolero 61 is on air and in no charger: the kiosk refuses, test mode sends the pack locate (fake accepts bp/<id>/locate)
    with pytest.raises(ValueError):
        bolero.identify("61", seconds=0)
    page = client.post("/admin/bolero/test", data={"number": "61", "action": "test", "how": "pack", "confirm": "1", "seconds": "5"}).text
    assert "accepted locate" in page and "HTTP 404" in page   # the CommsCheck form first, then bp/60/locate
    assert FakeAntenna.posts[-1] == ("bp/60/locate", {"active": 1}) and "61" in bolero._identify_timers
    page = client.post("/admin/bolero/test", data={"number": "61", "action": "test_stop"}).text
    assert "Stopped" in page and FakeAntenna.posts[-1] == ("bp/60/locate", {"active": 0}) and "61" not in bolero._identify_timers
    # outside test mode the pack locate is still refused by the write guard
    with pytest.raises(ValueError, match="only reads"):
        bolero._request(antenna, "bp/60/locate", method="POST", body={"active": 1}, auth=("admin", "secret"))
    with pytest.raises(ValueError, match="only reads"):   # test mode never lets anything else through
        bolero._request(antenna, "bp/60", method="POST", body={"bpName": "x"}, auth=("admin", "secret"), test_mode=True)


def test_tester_choose_locate_id(antenna, monkeypatch, client):
    """The tester lists the IDs locate might expect, flags any that is another pack's slot, and sends the one chosen."""
    cfg = {"bolero_host": antenna, "bolero_enabled": True, "bolero_admin_password": "secret", "bolero_charger_resource": ""}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(cfg))
    _reset(); bolero._identify_timers.clear(); bolero._identify_off.clear()
    bolero.poll_now()
    ids = bolero.locate_ids("65")   # slot 64, number - 1 is also 64, number 65
    assert [c["id"] for c in ids] == [64, 65] and ids[0]["why"] == "slot id in /bp" and not ids[0]["other"]
    with_other = {c["id"]: c["other"] for c in bolero.locate_ids("61")}
    assert with_other[60] == "" and with_other[61] == ""
    with bolero._lock:   # a system whose slots do not follow the numbers
        bolero._state["packs"]["121"] = {"number": "121", "id": 108, "name": "Test pack"}
        bolero._state["packs"]["109"] = {"number": "109", "id": 120, "name": "Other pack"}
    c = {x["id"]: x for x in bolero.locate_ids("121")}
    assert set(c) == {108, 120, 121} and c[120]["other"] == "Bolero 109 (Other pack)" and c[108]["why"] == "slot id in /bp"

    client.post("/start", data={"tech_id": 1, "site_id": 1})
    page = client.get("/admin/login").text
    client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"} if "Set the admin password" in page else {"password": "test1234"})
    page = client.post("/admin/bolero/test", data={"number": "61", "action": "test", "how": "pack", "confirm": "1", "seconds": "5", "send_id": "61"}).text
    assert FakeAntenna.posts[-1] == ("bp/61/locate", {"active": 1}) and "IDs to try for Bolero 61" in page
    client.post("/admin/bolero/test", data={"number": "61", "action": "test_stop"})
    assert FakeAntenna.posts[-1] == ("bp/61/locate", {"active": 0})
    r = client.post("/admin/bolero/test", data={"number": "61", "action": "test", "confirm": "1", "send_id": "6x"})
    assert r.status_code == 303 and "whole%20number" in r.headers["location"]
