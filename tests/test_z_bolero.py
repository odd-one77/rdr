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
SAMPLE_CHARGERS = {"items": [{"name": "Charger A", "bays": [{"bay": 1, "bpNumber": 65, "state": "charging"}, {"bay": 2, "bpNumber": None}]}]}


class FakeAntenna(BaseHTTPRequestHandler):
    posts: list = []
    charger_resource = "chargers"

    def log_message(self, *a):  # quiet
        pass

    def _auth(self):
        return self.headers.get("Authorization", "")

    def do_GET(self):
        path = self.path.split("/rest/", 1)[-1]
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
        if not path.startswith("bp/") or not path.endswith("/locate"):
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
    assert jamal["online"] is False and jamal["charger"] == {"name": "Charger A", "bay": 1, "state": "charging"}
    # a docked (off) pack cannot be flashed; the message says where it is
    with pytest.raises(ValueError, match="Charger A, bay 1"):
        bolero.identify("65")
    # a powered-on pack flashes, and the auto-off is scheduled
    r = bolero.identify("61", seconds=0)
    assert r["ok"] and FakeAntenna.posts[-1] == ("bp/60/locate", {"active": 1})
    bolero.identify("61", on=False)
    assert FakeAntenna.posts[-1] == ("bp/60/locate", {"active": 0})
    # wrong password
    cfg["bolero_admin_password"] = "nope"
    with pytest.raises(ValueError, match="refused the admin password"):
        bolero.identify("61", seconds=0)
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
