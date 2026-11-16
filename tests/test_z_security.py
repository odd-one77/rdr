"""Exposure through a reverse proxy: who counts as remote, the host and origin checks, the access code,
wrong password limits, and what the mirror and browser are allowed to see."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import security
from app.config import load_config, save_config
from app.main import app

PUBLIC = ("81.2.69.160", 40000)       # an address on the internet
PROXY_HDR = {"x-forwarded-for": "81.2.69.160", "x-forwarded-proto": "https"}


@pytest.fixture(autouse=True)
def clean_config(client):
    """Each test starts from the machine config it found, logged in as admin, with no lockouts."""
    before = load_config()
    security.limiter.reset()
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    if "Set the admin password" in client.get("/admin/login").text:
        client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"})
    else:
        client.post("/admin/login", data={"password": "test1234"})
    yield
    save_config(before)
    security.limiter.reset()


def other(**kw):
    return TestClient(app, follow_redirects=False, **kw)


def set_code(client, code="room-code-1"):
    r = client.post("/admin/settings/access-code", data={"code": code, "confirm": code})
    assert r.status_code == 303 and "msg=" in r.headers["location"]


def remote_on(client, **extra):
    data = {"remote_access": "1", "access_scope": "all", "trusted_proxies": "", "public_hosts": ""}
    data.update(extra)
    r = client.post("/admin/settings/remote", data=data)
    assert "msg=" in r.headers["location"], r.headers["location"]


# -- who is asking --------------------------------------------------------------

def scope(peer, headers=(), server="192.168.1.10"):
    return {"type": "http", "client": (peer, 1), "server": (server, 8080), "scheme": "http",
            "headers": [(k.encode(), v.encode()) for k, v in [("host", "192.168.1.10:8080"), *headers]]}


def test_origin_classification():
    cfg = {"trusted_proxies": ["10.0.0.5"], "public_hosts": ["rdr.example.org"]}
    assert not security.origin_of(scope("192.168.1.20"), cfg).remote
    assert not security.origin_of(scope("100.101.102.103"), cfg).remote          # Tailscale range
    assert security.origin_of(scope("81.2.69.160"), cfg).remote                  # port forwarded straight in
    o = security.origin_of(scope("10.0.0.5", [("x-forwarded-for", "1.1.1.1, 81.2.69.160")]), cfg)
    assert o.remote and o.proxied and o.proxy_listed and o.client == "81.2.69.160"   # rightmost hop the proxy saw
    o = security.origin_of(scope("81.2.69.160", [("x-forwarded-for", "192.168.1.20")]), cfg)
    assert o.remote and o.client == "81.2.69.160"                                # a public sender cannot pick its address
    assert security.origin_of(scope("192.168.1.20", [("host", "rdr.example.org")]), cfg).remote
    assert not security.origin_of(scope("2001:db8:1:2::77", server="2001:db8:1:2::1"), cfg).remote
    assert security.origin_of(scope("10.0.0.5", [("x-forwarded-proto", "https")]), cfg).https


def test_settings_parsers():
    good, bad = security.clean_proxies("127.0.0.1\n10.0.0.0/24, nonsense 0.0.0.0/0")
    assert good == ["127.0.0.1", "10.0.0.0/24"] and bad == ["nonsense", "0.0.0.0/0"]
    assert security.clean_hosts("https://RDR.example.org/path\nrdr.example.org:443, bad<host") == ["rdr.example.org"]


# -- refusals before anything runs ----------------------------------------------------

def test_remote_refused_until_switched_on(client):
    r = client.get("/start", headers=PROXY_HDR)
    assert r.status_code == 403 and "Remote access is off" in r.text
    r = client.get("/api/mirror/1", headers=PROXY_HDR)
    assert r.status_code == 403 and r.json()["error"] == "Remote access is off"
    with other(client=PUBLIC) as c:
        assert c.get("/start").status_code == 403
    r = client.post("/admin/settings/remote", data={"remote_access": "1"})
    assert "err=" in r.headers["location"]                                       # not without a code


def test_unknown_host_names_refused(client):
    assert client.get("/start", headers={"host": "attacker.example.net"}).status_code == 421
    for name in ("rdr.local", "rdr", "localhost", "10.1.2.3:8080", "[::1]:8080"):
        assert client.get("/api/ping", headers={"host": name}).status_code == 200, name
    cfg = load_config(); cfg["public_hosts"] = ["rdr.example.org"]; save_config(cfg)
    assert client.get("/api/ping", headers={"host": "rdr.example.org"}).status_code != 421
    r = client.post("/admin/settings/remote", data={"local_hosts": "rdr.venue.net", "public_hosts": "rdr.example.org"})
    assert "msg=" in r.headers["location"]
    assert client.get("/start", headers={"host": "rdr.venue.net"}).status_code == 200   # a local name stays local


def test_cross_site_writes_refused(client):
    for headers in ({"origin": "http://evil.example.net"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"}):
        r = client.post("/admin/settings/demo", headers=headers)
        assert r.status_code == 403 and "did not come from an RDR page" in r.text, headers
    r = client.post("/api/kiosk/state", json={"state": {}}, headers={"origin": "http://testserver"})
    assert r.status_code == 200
    # a proxy that rewrites Host but says which name the browser used
    assert security.same_origin({"origin": "https://rdr.example.org", "host": "127.0.0.1:8080", "x-forwarded-host": "rdr.example.org"}, {})
    assert security.same_origin({"origin": "https://rdr.example.org", "host": "127.0.0.1:8080"}, {"public_hosts": ["rdr.example.org"]})
    assert not security.same_origin({"origin": "https://rdr.example.org.evil.net", "host": "127.0.0.1:8080"}, {"public_hosts": ["rdr.example.org"]})


def test_body_limits(client):
    r = client.post("/api/kiosk/state", content=b"{" + b" " * (70 * 1024) + b"}", headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_protective_headers(client):
    r = client.get("/start")
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in r.text and "frame-ancestors 'self'" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "server" not in {k.lower() for k in r.headers} or "uvicorn" not in r.headers.get("server", "")
    assert client.get("/openapi.json").status_code == 404
    assert "https:" in client.get("/mirror").headers["content-security-policy"]   # the feed may be elsewhere
    assert "media-src 'self' blob:;" in csp


# -- access code -------------------------------------------------------------------

def test_access_code_gate(client):
    set_code(client)
    with other() as c:                                                           # another browser on the LAN
        r = c.get("/start")
        assert r.status_code == 303 and r.headers["location"] == "/access?next=%2Fstart"
        assert c.get("/api/people/search?q=a").status_code == 401
        assert c.get("/api/ping").status_code == 200 and c.get("/static/css/rdr.css").status_code == 200
        page = c.get("/access?next=/start")
        assert page.status_code == 200 and "Access code" in page.text
        assert c.post("/access", data={"code": "wrong-code", "next": "/start"}).status_code == 403
        r = c.post("/access", data={"code": "room-code-1", "next": "//evil.example.net"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert c.get("/start").status_code == 200
    assert client.get("/start").status_code == 200                               # the browser that set it carries on
    set_code(client, "room-code-2")                                              # a new code signs other browsers out
    with other() as c:
        c.post("/access", data={"code": "room-code-1"})
        assert c.get("/start").status_code == 303


def test_remote_access_with_code_and_proxy_scope(client):
    set_code(client)
    remote_on(client, access_scope="proxy", trusted_proxies="10.0.0.5")
    with other() as lan:
        assert lan.get("/start").status_code == 200                              # local screens skip the code
    with other(client=("10.0.0.5", 1234), headers=PROXY_HDR, base_url="https://testserver") as c:   # cookies are https-only
        assert c.get("/start").status_code == 303
        c.post("/access", data={"code": "room-code-1"})
        assert c.get("/start").status_code == 200
        r = c.get("/admin/login")
        assert r.status_code == 403 and "local network" in r.text                # Admin stays local by default
    cfg = load_config(); cfg["trusted_proxies"] = []; save_config(cfg)
    with other() as lan:
        assert lan.get("/start").status_code == 303                              # no proxy listed: fail closed


def test_first_password_only_on_the_lan(client):
    from app.db import get_db, get_setting, set_setting
    set_code(client)
    remote_on(client, remote_admin="1")
    with get_db() as db:
        saved = get_setting(db, "admin_password_hash")
        set_setting(db, "admin_password_hash", None)
    try:
        with other(client=PUBLIC) as c:
            c.post("/access", data={"code": "room-code-1"})
            page = c.get("/admin/login").text
            assert "Set it from a computer on the local network" in page
            r = c.post("/admin/login", data={"password": "taken-over", "confirm": "taken-over"})
            assert "err=" in r.headers["location"]
        with get_db() as db:
            assert not get_setting(db, "admin_password_hash")
    finally:
        with get_db() as db:
            set_setting(db, "admin_password_hash", saved)


def test_wrong_tries_lock_out(client):
    set_code(client)
    with other(client=("192.168.1.77", 1)) as c:
        for _ in range(5):
            assert c.post("/access", data={"code": "nope-nope"}).status_code == 403
        r = c.post("/access", data={"code": "room-code-1"})
        assert r.status_code == 429 and "Too many wrong tries" in r.text         # even the right code waits
    with other(client=("192.168.1.78", 1)) as c:
        assert c.post("/access", data={"code": "room-code-1"}).status_code == 303   # others are not affected
    security.limiter.reset()
    with other() as c:
        c.post("/start", data={"tech_id": 1, "site_id": 1})
        for _ in range(5):
            assert c.post("/api/away/unlock", json={"password": "wrong"}).status_code in (401, 403)


def test_password_change_signs_out_other_admins(client):
    with other() as second:
        second.post("/admin/login", data={"password": "test1234"})
        assert second.get("/admin").status_code == 200
        r = client.post("/admin/settings/password", data={"current": "test1234", "password": "test5678", "confirm": "test5678"})
        assert "msg=" in r.headers["location"]
        assert client.get("/admin").status_code == 200                           # this browser keeps its session
        assert second.get("/admin").status_code == 303
        assert "err=" in client.post("/admin/settings/password", data={"current": "test5678", "password": "short", "confirm": "short"}).headers["location"]
        client.post("/admin/settings/password", data={"current": "test5678", "password": "test1234", "confirm": "test1234"})


# -- what leaves the server ------------------------------------------------------------

def test_open_endpoints_and_feed_secrets(client):
    from app.db import get_db
    with other() as c:
        assert c.get("/api/summary").status_code == 401 and c.get("/api/recent").status_code == 401
    with get_db() as db:
        old = db.execute("SELECT rtsp_url FROM sites WHERE id = 1").fetchone()[0]
        db.execute("UPDATE sites SET rtsp_url = 'rtsp://admin:cam-secret@10.9.8.7/live' WHERE id = 1")
    try:
        assert "cam-secret" not in client.get("/mirror?site=1").text
        assert "cam-secret" not in client.get("/api/mirror/1").text
        assert "cam-secret" not in client.get("/").text
    finally:
        with get_db() as db:
            db.execute("UPDATE sites SET rtsp_url = ? WHERE id = 1", (old,))
    from app.services import streams
    assert streams.mask("rtsp://admin:cam-secret@10.9.8.7/live failed") == "rtsp://admin:***@10.9.8.7/live failed"
    args, _ = streams._input_args("dshow:Cam?video_size=1920x1080&y=1&f=null&framerate=-i /etc/passwd")
    assert args == ["-f", "dshow", "-rtbufsize", "256M", "-video_size", "1920x1080", "-i", "video=Cam"]


def test_mirror_state_is_cleaned(client):
    state = {"result": {"kind": '"><img src=x onerror=alert(1)>', "items": [{"type": "Bolero"}]},
             "person": {"name": "Sam Porter", "faceplate": "0f0;background:url(//x)"}}
    assert client.post("/api/kiosk/state", json={"state": state}).status_code == 200
    got = client.get("/api/mirror/1").json()["state"]
    assert got["result"]["kind"] == "in" and got["person"]["faceplate"] == ""


def test_backup_keeps_remote_setup_on_the_machine(client):
    import io
    import zipfile
    set_code(client)
    blob = client.post("/admin/backup/export").content
    cfg = json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("config.json"))
    assert "access_code_hash" not in cfg and "bolero_admin_password" not in cfg
    r = client.post("/admin/backup/restore", data={"token": "../../x", "confirm": "RESTORE"})
    assert "err=" in r.headers["location"]
