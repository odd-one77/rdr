"""Phase 2: backup/restore, reports, phone view, trust page, stream ping, certificates."""
import io
import json
import zipfile

import pytest


@pytest.fixture(scope="module", autouse=True)
def prepared(client):
    """Admin logged in, demo data present, tech session set (independent of test order)."""
    client.post("/start", data={"tech_id": 1, "site_id": 1})   # tech first: picking a tech clears any admin login
    page = client.get("/admin/login").text
    if "Set the admin password" in page:
        client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"})
    else:
        client.post("/admin/login", data={"password": "test1234"})
    if client.get("/api/summary").json()["all"]["totals"]["total"] == 0:
        client.post("/admin/settings/demo")


def test_reports_pages_and_csv(client):
    r = client.get("/admin/reports")
    assert r.status_code == 200 and "End of event" in r.text
    assert "Bolero ID" in client.get("/admin/reports?format=csv&report=endofevent").text
    r = client.get("/admin/reports?format=csv&report=stock")
    assert r.headers["content-type"].startswith("text/csv") and "Shared in" in r.text
    r = client.get("/admin/reports?format=csv&report=outstanding")
    assert "Department" in r.text


def test_backup_roundtrip(client):
    before = client.get("/api/summary").json()["all"]["totals"]["total"]
    assert before > 0
    r = client.post("/admin/backup/export", data={})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    blob = r.content
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        assert "rdr.db" in names and "manifest.json" in names and "config.json" in names
        assert "github_token" not in json.loads(zf.read("config.json"))

    # a bogus upload is rejected before anything changes
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("hello.txt", "x")
    r = client.post("/admin/backup/inspect", files={"file": ("x.zip", bad.getvalue(), "application/zip")})
    assert "err=" in r.headers["location"]

    # wipe, then restore from the backup
    client.post("/admin/settings/reset", data={"confirm": "RESET", "keep_password": "1"})
    assert client.get("/api/summary").json()["all"]["totals"]["total"] == 0
    r = client.post("/admin/backup/inspect", files={"file": ("rdr-backup.zip", blob, "application/zip")})
    assert r.status_code == 303 and "token=" in r.headers["location"]
    token = r.headers["location"].split("token=")[1]
    page = client.get(f"/admin/backup?token={token}").text
    assert "Restore now" in page and "Summer Arena Tour" in page
    # wrong confirmation word
    r = client.post("/admin/backup/restore", data={"token": token, "confirm": "nope"})
    assert "err=" in r.headers["location"]
    import app.services.updater as updater
    calls = []
    orig = updater.schedule_restart
    updater.schedule_restart = lambda *a, **k: calls.append(1)
    try:
        r = client.post("/admin/backup/restore", data={"token": token, "confirm": "RESTORE", "restore_config": "1"})
    finally:
        updater.schedule_restart = orig
    assert r.status_code == 303 and calls == [1]
    assert client.get("/api/summary").json()["all"]["totals"]["total"] == before


def test_phone_and_trust_pages(client):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, follow_redirects=False) as phone:
        phone.headers["user-agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Safari/604.1"
        phone.post("/start", data={"tech_id": 1, "site_id": 1})
        r = phone.get("/m")
        assert r.status_code == 200 and "Camera scanning needs HTTPS" in r.text  # test client is http://testserver
        assert "html5-qrcode.min.js" in r.text
    r = client.get("/trust")
    assert r.status_code == 200 and "HTTPS is off" in r.text
    r = client.get("/trust/rdr-ca.crt")
    assert r.status_code == 303  # no CA yet


def test_stream_ping_without_url_or_ffmpeg(client):
    d = client.get("/api/stream/1/ping").json()
    assert d["running"] is False and "feed" in d["error"]


def test_certificates(tmp_path, monkeypatch):
    from app.services import certs
    monkeypatch.setattr(certs, "CERT_DIR", tmp_path)
    monkeypatch.setattr(certs, "CA_KEY", tmp_path / "ca.key"); monkeypatch.setattr(certs, "CA_CRT", tmp_path / "ca.crt")
    monkeypatch.setattr(certs, "SRV_KEY", tmp_path / "s.key"); monkeypatch.setattr(certs, "SRV_CRT", tmp_path / "s.crt")
    assert certs.available()
    info = certs.issue_server_cert(["rdr.local", "10.1.2.3"])
    assert certs.ready() and info["hostname"]
    st = certs.status()
    assert "rdr.local" in st["hosts"] and "10.1.2.3" in st["hosts"] and "localhost" in st["hosts"]
    from cryptography import x509
    ca = x509.load_pem_x509_certificate((tmp_path / "ca.crt").read_bytes())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True


def test_stream_command_shapes():
    from pathlib import Path
    from app.services import streams
    copy = streams._command("ffmpeg", "rtsp://cam/live", Path("/tmp/x"), transcode=False)
    assert "-c:v" in copy and copy[copy.index("-c:v") + 1] == "copy" and "-rtsp_transport" in copy
    enc = streams._command("ffmpeg", "rtsp://cam/live", Path("/tmp/x"), transcode=True)
    assert "libx264" in enc


def test_snapshots_and_restore(client):
    from app.services import snapshots
    before = client.get("/api/summary").json()["all"]["totals"]["total"]
    r = client.post("/admin/backup/snapshot")
    assert r.status_code == 303 and "Snapshot+saved" in r.headers["location"].replace("%20", "+")
    snaps = snapshots.list_snapshots()
    manual = next(x for x in snaps if x["reason"] == "manual")   # start/stop snapshots may share the same second
    name = manual["name"]
    assert client.get(f"/admin/backup/snapshots/{name}").status_code == 200
    # change data, then roll back to the snapshot
    client.post("/api/people", json={"name": "Temp Person"})
    assert client.get("/api/people/search?q=Temp+Person").json()["results"]
    import app.services.updater as updater
    calls = []
    orig = updater.schedule_restart
    updater.schedule_restart = lambda *a, **k: calls.append(1)
    try:
        r = client.post(f"/admin/backup/snapshots/{name}/restore", data={"confirm": "RESTORE"})
    finally:
        updater.schedule_restart = orig
    assert r.status_code == 303 and calls == [1]
    client.post("/admin/login", data={"password": "test1234"})
    assert not client.get("/api/people/search?q=Temp+Person").json()["results"]
    assert client.get("/api/summary").json()["all"]["totals"]["total"] == before
    ok, _ = snapshots.integrity_check()
    assert ok
    # prune keeps the newest
    snapshots.KEEP_RECENT = 1
    snapshots.prune()
    assert len([s for s in snapshots.list_snapshots() if s["when"][:10] == manual["when"][:10]]) >= 1


def test_plans_and_bolero_id(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    client.post("/admin/login", data={"password": "test1234"})
    p = client.post("/api/people", json={"name": "Plan Person", "bolero_id": "961"}).json()["person"]
    assert p["bolero_id"] == "961"
    assert client.get("/api/people/search?q=961").json()["results"][0]["id"] == p["id"]
    types = {t["name"]: t["id"] for t in client.get("/api/types").json()["types"]}
    r = client.post("/admin/plans", data={"person_id": p["id"], "type_id": types["Pro-D2 Headset"], "qty": 1, "bolero_id": "62"})
    assert r.status_code == 303
    r = client.post("/admin/plans", data={"person_id": p["id"], "type_id": types["Bolero Beltpack"], "qty": 1})
    d = client.get(f"/api/people/{p['id']}").json()
    assert d["person"]["bolero_id"] == "62"
    assert sorted(x["type_name"] for x in d["plan"]) == ["Bolero Beltpack", "Pro-D2 Headset"]
    assert all(x["held"] == 0 for x in d["plan"])
    page = client.get("/admin/plans").text
    assert "Plan Person" in page and "Pro-D2 Headset" in page
    # kit column on a people import creates plan rows
    csv = "Name,Phone,Bolero,Kit\nKit Importer,+44 7700 900999,63,\"Bolero, Pro-D2 x2\"\n"
    loc = client.post("/admin/import/people", files={"file": ("crew.csv", csv.encode(), "text/csv")}).headers["location"]
    token = loc.rsplit("/", 1)[1]
    client.post(loc, data={"map_name": "0", "map_phone": "1", "map_bolero_id": "2", "map_kit": "3"})
    client.post(f"/admin/import/s/{token}/commit", data={"action": "commit"})
    who = client.get("/api/people/search?q=Kit+Importer").json()["results"][0]
    d = client.get(f"/api/people/{who['id']}").json()
    assert d["person"]["bolero_id"] == "63"
    assert {(x["type_name"], x["qty"]) for x in d["plan"]} == {("Bolero Beltpack", 1), ("Pro-D2 Headset", 2)}


def test_phone_routing_by_user_agent(client):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, follow_redirects=False) as phone:
        phone.headers["user-agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
        assert phone.get("/").headers["location"] == "/m"
        r = phone.post("/start", data={"tech_id": 1, "site_id": 1})
        assert r.headers["location"] == "/m"
        assert phone.get("/m").status_code == 200
    with TestClient(app, follow_redirects=False) as desk:
        desk.headers["user-agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"
        assert desk.get("/m").headers["location"] == "/"


def test_tech_start_clears_admin_session(client):
    client.post("/admin/login", data={"password": "test1234"})
    assert client.get("/admin").status_code == 200
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    assert client.get("/admin").status_code == 303   # admin login gone after switching tech
    client.post("/admin/login", data={"password": "test1234"})


def test_feed_classification():
    from app.services import streams
    assert streams.classify_feed("rtsp://10.0.0.5/live") == "rtsp"
    assert streams.classify_feed("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube"
    assert streams.youtube_embed("https://youtu.be/dQw4w9WgXcQ?t=3").startswith("https://www.youtube.com/embed/dQw4w9WgXcQ?")
    assert streams.classify_feed("https://cdn/x/master.m3u8") == "hls"
    assert streams.classify_feed("http://x/clip.mp4") == "file"
