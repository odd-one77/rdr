"""A real hub (separate server, own data folder) and this test app as a site node (invented data only)."""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def hub_server():
    data = tempfile.mkdtemp(prefix="rdr-hub-")
    port = _free_port()
    env = dict(os.environ, RDR_DATA_DIR=data, RDR_ROLE="hub")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--no-proxy-headers"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/api/ping", timeout=1).status_code < 500:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    hc = httpx.Client(base_url=url, follow_redirects=False, timeout=20)
    hc.post("/admin/login", data={"password": "hub-pass-1", "confirm": "hub-pass-1"})
    yield url, hc, Path(data)
    proc.terminate(); proc.wait(10)


def _admin(client):
    page = client.get("/admin/login").text
    if "Set the admin password" in page:
        client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"})
    else:
        client.post("/admin/login", data={"password": "test1234"})


def test_hub_pages_and_event(hub_server):
    url, hc, data = hub_server
    assert hc.get("/hub").status_code == 200
    r = hc.post("/hub/events/new", data={"name": "Harbour Festival", "show_name": "Day 1", "starts_on": "2027-06-01"})
    assert r.status_code == 303 and "/hub/events/" in r.headers["location"]
    eid = int(re.search(r"/hub/events/(\d+)", r.headers["location"]).group(1))
    assert "Harbour Festival" in hc.get(f"/hub/events/{eid}").text
    # open the event: the normal admin pages now work on its database
    r = hc.get(f"/hub/events/{eid}/open")
    assert r.status_code == 303
    assert "Harbour Festival" in hc.get("/admin/event").text
    hc.post("/admin/sites", data={"name": "North gate", "stock_mode": "shared"})
    hc.post("/admin/settings/demo")
    hc.post("/admin/event", data={"name": "Harbour Festival", "show_name": "Day 1"})   # the demo renames the event
    assert (data / "hub" / "events" / str(eid) / "rdr.db").exists()
    # a second event starts from the first one's setup and devices, all back in stock
    r = hc.post("/hub/events/new", data={"name": "Harbour Festival Two", "copy_from": str(eid), "copy_setup": "1", "copy_devices": "1"})
    eid2 = int(re.search(r"/hub/events/(\d+)", r.headers["location"]).group(1))
    page = hc.get("/hub").text
    assert "Harbour Festival Two" in page and "North gate" in page
    hc.get(f"/hub/events/{eid2}/open")
    assert "North gate" in hc.get("/admin/event").text
    # without an event picked, a kiosk goes to the picker
    fresh = httpx.Client(base_url=url, follow_redirects=False)
    assert fresh.get("/").headers["location"] == "/hub/pick"
    assert "Harbour Festival" in fresh.get("/hub/pick").text


@pytest.fixture()
def keep_node_data():
    """This test replaces the test app's data with the hub's; put it back for the tests after it."""
    from app.config import DB_PATH
    from app.services import eventfile, node
    saved = Path(tempfile.mkdtemp()) / "saved.db"
    eventfile.copy_db(DB_PATH, saved)
    yield
    node.stop(); node.forget()
    eventfile.replace_live(DB_PATH, saved)


def test_node_runs_event_and_syncs(hub_server, client, keep_node_data):
    url, hc, data = hub_server
    from app.services import node
    from app.db import get_db
    r = hc.post("/hub/nodes", data={"name": "Gate node"})
    key = re.search(r"key=(rdrn_[^&]+)", r.headers["location"]).group(1)
    eid = int(re.search(r'href="/hub/events/(\d+)"><b>Harbour Festival</b>', hc.get("/hub").text).group(1))

    _admin(client)
    assert client.post("/admin/hub/link", data={"url": url, "key": key}).status_code == 303
    page = client.get("/admin/hub?check=1").text
    assert "Harbour Festival" in page and "Gate node" in page
    r = client.post("/admin/hub/take", data={"event_id": eid, "confirm": "1"})
    assert "err=" not in r.headers["location"], r.headers["location"]
    node.stop()   # the test drives each round itself
    with get_db() as db:
        assert db.execute("SELECT name FROM event").fetchone()[0] == "Harbour Festival"
        assert db.execute("SELECT 1 FROM sites WHERE name = 'North gate'").fetchone()
    # the hub copy can no longer be scanned
    hc.get(f"/hub/events/{eid}/open")
    assert hc.post("/api/people", json={"name": "Nobody"}).status_code == 409
    assert "Gate node" in hc.get("/hub").text

    # a change here reaches the hub
    with get_db() as db:
        db.execute("INSERT INTO people (name, created_at, updated_at) VALUES ('Rowan Ashby', '2027-06-01 09:00:00', '2027-06-01 09:00:00')")
    st = node.sync_once()
    assert not st.get("last_error"), st
    assert "Rowan Ashby" in hc.get("/admin/people?q=Rowan").text
    assert node.badge()["online"]

    # the hub edits a person while the link is up: merged here without asking
    hc.post("/admin/people/new", data={"name": "Sloane Whitby", "dept": "Catering"})
    st = node.sync_once()
    assert not st.get("pending") and not st.get("last_error"), st
    with get_db() as db:
        assert db.execute("SELECT 1 FROM people WHERE name = 'Sloane Whitby'").fetchone()

    # the link drops; both sides change; on reconnect the admin is asked
    good = node.load_config()["hub_url"]
    cfg = node.load_config(); cfg["hub_url"] = "http://127.0.0.1:1"; node.save_config(cfg)
    st = node.sync_once()
    assert st.get("offline_since")
    with get_db() as db:
        db.execute("INSERT INTO people (name, created_at, updated_at) VALUES ('Offline Person', '2027-06-01 10:00:00', '2027-06-01 10:00:00')")
    hc.post("/admin/people/new", data={"name": "Hub Person", "dept": "Office"})
    cfg = node.load_config(); cfg["hub_url"] = good; node.save_config(cfg)
    st = node.sync_once()
    assert st.get("pending") and st["pending"]["kind"] == "reconnect", st
    page = client.get("/admin/hub").text
    assert "Back in touch with the hub" in page and "from this node" in page and "from the hub" in page
    assert "Back in touch" in client.get("/admin").text   # the banner on every admin page

    r = client.post("/admin/hub/decide", data={"choice": "merge"})
    assert "msg=Merged" in r.headers["location"], r.headers["location"]
    node.stop()
    with get_db() as db:
        names = {r[0] for r in db.execute("SELECT name FROM people")}
    assert {"Offline Person", "Hub Person", "Rowan Ashby", "Sloane Whitby"} <= names
    people = hc.get("/admin/people").text
    assert "Offline Person" in people and "Hub Person" in people
    # the hub kept a copy before each node copy replaced its own
    assert "Before a node copy" in hc.get(f"/hub/events/{eid}").text

    # pull: the hub's copy replaces this node's
    hc.post("/admin/people/new", data={"name": "Only On Hub"})
    with get_db() as db:
        db.execute("INSERT INTO people (name, created_at, updated_at) VALUES ('Only On Node', '2027-06-01 11:00:00', '2027-06-01 11:00:00')")
    node._update(offline_since="2027-06-01 11:00:00")
    st = node.sync_once()
    assert st["pending"]["kind"] == "reconnect"
    client.post("/admin/hub/decide", data={"choice": "pull"})
    node.stop()
    with get_db() as db:
        names = {r[0] for r in db.execute("SELECT name FROM people")}
        assert db.execute("SELECT value FROM settings WHERE key = 'admin_password_hash'").fetchone()   # the node's password survives
    assert "Only On Hub" in names and "Only On Node" not in names

    # hand back: the hub can scan again
    client.post("/admin/hub/handback")
    assert hc.post("/api/people", json={"name": "Nobody"}).status_code != 409
    client.post("/admin/hub/forget")
    assert node.badge() is None


def test_node_api_needs_a_key(hub_server):
    url, hc, data = hub_server
    anon = httpx.Client(base_url=url)
    assert anon.get("/hub/api/hello").status_code == 401
    assert anon.get("/hub/api/hello", headers={"Authorization": "Bearer rdrn_wrong"}).status_code == 401
    assert anon.get("/hub").status_code in (303, 401)
