"""Phase 2: backup/restore, reports, phone view, trust page, stream ping, certificates."""
import io
import json
import zipfile

import pytest


@pytest.fixture(scope="module", autouse=True)
def prepared(client):
    """Admin logged in, demo data present, tech session set (independent of test order)."""
    page = client.get("/admin/login").text
    if "Set the admin password" in page:
        client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"})
    else:
        client.post("/admin/login", data={"password": "test1234"})
    if client.get("/api/summary").json()["all"]["totals"]["total"] == 0:
        client.post("/admin/settings/demo")
    client.post("/start", data={"tech_id": 1, "site_id": 1})


def test_reports_pages_and_csv(client):
    r = client.get("/admin/reports")
    assert r.status_code == 200 and "Outstanding items" in r.text
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
    # re-login as admin cookie was cleared by the restore, tech cookie is still there
    r = client.get("/m")
    assert r.status_code == 200 and "Camera scanning needs HTTPS" in r.text  # test client is http://testserver
    assert "html5-qrcode.min.js" in r.text
    r = client.get("/trust")
    assert r.status_code == 200 and "HTTPS is off" in r.text
    r = client.get("/trust/rdr-ca.crt")
    assert r.status_code == 303  # no CA yet


def test_stream_ping_without_url_or_ffmpeg(client):
    d = client.get("/api/stream/1/ping").json()
    assert d["running"] is False and "RTSP" in d["error"]


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
