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


def test_bolero_id_soft_link(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    client.post("/admin/login", data={"password": "test1234"})
    p = client.post("/api/people", json={"name": "Link Person", "bolero_id": "961"}).json()["person"]
    assert p["bolero_id"] == "961"
    assert client.get("/api/people/search?q=961").json()["results"][0]["id"] == p["id"]
    types = {t["name"]: t["id"] for t in client.get("/api/types").json()["types"]}
    pack = client.post("/api/devices/quick", json={"si": "SI-LINK-1", "type_id": types["Bolero Beltpack"], "label": "Bolero 99"}).json()["device"]
    hs = client.post("/api/devices/quick", json={"si": "SI-LINK-2", "type_id": types["Pro-D2 Headset"]}).json()["device"]
    r = client.post("/api/signout", json={"person_id": p["id"], "device_ids": [pack["id"], hs["id"]]}).json()["results"]
    assert {x["outcome"] for x in r} == {"issued"}
    held = client.get(f"/api/people/{p['id']}").json()["holdings"]
    by_si = {h["si"]: h for h in held}
    assert by_si["SI-LINK-1"]["linked_id"] == "961"      # the pack now carries the person's Bolero ID
    assert by_si["SI-LINK-2"]["linked_id"] == ""         # headsets do not
    client.post("/api/signin", json={"device_ids": [pack["id"]]})
    assert client.get("/api/devices/lookup?si=SI-LINK-1").json()["device"]["linked_id"] == "961"   # the pack keeps its configured ID
    # people import carries the Bolero ID column
    csv = "Name,Phone,Bolero\nImported Link,+44 7700 900998,63\n"
    loc = client.post("/admin/import/people", files={"file": ("crew.csv", csv.encode(), "text/csv")}).headers["location"]
    token = loc.rsplit("/", 1)[1]
    client.post(loc, data={"map_name": "0", "map_phone": "1", "map_bolero_id": "2"})
    client.post(f"/admin/import/s/{token}/commit", data={"action": "commit"})
    who = client.get("/api/people/search?q=Imported+Link").json()["results"][0]
    assert who["bolero_id"] == "63"


def test_capture_feed_commands():
    from pathlib import Path
    from app.services import streams
    assert streams.classify_feed("dshow:Blackmagic WDM Capture") == "dshow"
    assert streams.classify_feed("decklink:DeckLink Mini Recorder") == "decklink"
    cmd = streams._command("ffmpeg", "dshow:Blackmagic WDM Capture?video_size=1920x1080&framerate=25", Path("/tmp/x"), transcode=False)
    assert cmd[cmd.index("-f") + 1] == "dshow" and "video=Blackmagic WDM Capture" in cmd and "libx264" in cmd and "-video_size" in cmd
    cmd = streams._command("ffmpeg", "decklink:DeckLink Mini Recorder", Path("/tmp/x"), transcode=False)
    assert "decklink" in cmd and "DeckLink Mini Recorder" in cmd


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


def test_pgm_size_setting(client):
    client.post("/admin/login", data={"password": "test1234"})
    r = client.post("/admin/settings/mirror", data={"mirror_title": "", "mirror_prompt": "", "mirror_sayings": "", "pgm_size": "large"})
    assert r.status_code == 303
    client.post("/admin/sites/1", data={"action": "save", "name": "RDR North", "stock_mode": "shared", "rtsp_url": "https://youtu.be/dQw4w9WgXcQ", "active": "1"})
    page = client.get("/mirror?site=1").text
    assert "pgm-large" in page and "youtube.com/embed/dQw4w9WgXcQ" in page
    client.post("/admin/sites/1", data={"action": "save", "name": "RDR North", "stock_mode": "shared", "rtsp_url": "", "active": "1"})


def test_bolero_rules_adopt_keep_and_swap(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    client.post("/admin/login", data={"password": "test1234"})
    types = {t["name"]: t["id"] for t in client.get("/api/types").json()["types"]}
    bol = types["Bolero Beltpack"]
    # rule 2b: a pack imported/created with an ID gives it to a person who has none
    nobody = client.post("/api/people", json={"name": "No Id Yet"}).json()["person"]
    r = client.post("/admin/devices/new", data={"si": "SI-RULE-1", "type_id": bol, "label": "Pack A", "linked_id": "71"})
    pack_a = client.get("/api/devices/lookup?si=SI-RULE-1").json()["device"]
    assert pack_a["linked_id"] == "71"
    client.post("/api/signout", json={"person_id": nobody["id"], "device_ids": [pack_a["id"]]})
    assert client.get(f"/api/people/{nobody['id']}").json()["person"]["bolero_id"] == "71"
    # the ID stays on the pack when it comes back
    client.post("/api/signin", json={"device_ids": [pack_a["id"]]})
    assert client.get("/api/devices/lookup?si=SI-RULE-1").json()["device"]["linked_id"] == "71"
    # rule 2a: person's ID wins when a pack with another ID is scanned to them
    boss = client.post("/api/people", json={"name": "Has Id", "bolero_id": "80"}).json()["person"]
    client.post("/api/signout", json={"person_id": boss["id"], "device_ids": [pack_a["id"]]})
    assert client.get("/api/devices/lookup?si=SI-RULE-1").json()["device"]["linked_id"] == "80"
    # rule 3: swap trades IDs; the spare (ID 99) becomes 80, the damaged pack becomes 99
    client.post("/admin/devices/new", data={"si": "SI-RULE-2", "type_id": bol, "label": "Spare", "linked_id": "99"})
    r = client.post("/api/swap", json={"old_device_id": pack_a["id"], "new_si": "SI-RULE-2", "reason": "Damaged", "old_status": "damaged"}).json()
    assert r["new"]["linked_id"] == "80" and r["old"]["linked_id"] == "99"
    assert client.get(f"/api/people/{boss['id']}").json()["holdings"][0]["si"] == "SI-RULE-2"
    # quick edit from the kiosk
    r = client.post(f"/api/people/{boss['id']}", json={"role": "Floor", "bolero_id": "81"})
    assert r.json()["person"]["role"] == "Floor" and r.json()["person"]["bolero_id"] == "81"
    assert client.post(f"/api/people/{boss['id']}", json={"name": "  "}).status_code == 400


def test_sign_out_on_behalf_and_chip_data(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    client.post("/admin/login", data={"password": "test1234"})
    types = {t["name"]: t["id"] for t in client.get("/api/types").json()["types"]}
    receiver = client.post("/api/people", json={"name": "Recipient Person"}).json()["person"]
    collector = client.post("/api/people", json={"name": "Collector Person"}).json()["person"]
    radio = client.post("/api/devices/quick", json={"si": "SI-RADIO-9", "type_id": types["Radio"], "label": "Radio 09"}).json()["device"]
    r = client.post("/api/signout", json={"person_id": receiver["id"], "device_ids": [radio["id"]], "collector_id": collector["id"]}).json()["results"]
    assert r[0]["outcome"] == "issued"
    recent = client.get("/api/recent?limit=3").json()["transactions"]
    assert any("Collected by Collector Person" in (t["note"] or "") for t in recent)
    assert client.get(f"/api/people/{receiver['id']}").json()["holdings"][0]["si"] == "SI-RADIO-9"
    hit = client.get("/api/people/search?q=Recipient").json()["results"][0]
    assert hit["radio_label"] == "Radio 09" and hit["held_bolero"] is None
    assert client.post("/api/signout", json={"person_id": receiver["id"], "device_ids": [], "collector_id": 999999}).status_code == 400


def test_demo_data_shows_bolero_rules(client):
    client.post("/admin/login", data={"password": "test1234"})
    client.post("/admin/settings/reset", data={"confirm": "RESET", "keep_password": "1"})
    client.post("/admin/settings/demo")
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    client.post("/admin/login", data={"password": "test1234"})
    emma = client.get("/api/people/search?q=Emma").json()["results"][0]        # 10th person, no planned ID, radio-only
    assert emma["bolero_id"] == "" and emma["radio_label"].startswith("Radio")
    daniel = client.get("/api/people/search?q=Daniel").json()["results"][0]    # 9th person adopted the spare's ID 69
    assert daniel["bolero_id"] == "69"
    jamal = client.get("/api/people/search?q=Jamal").json()["results"][0]      # 5th person: swapped pack, keeps ID 65
    held = client.get(f"/api/people/{jamal['id']}").json()["holdings"]
    pack = next(h for h in held if "Bolero" in h["type_name"])
    assert pack["linked_id"] == "65" and pack["si"] == "SI-BOL-1012"
    damaged = client.get("/api/devices/lookup?si=SI-BOL-1005").json()["device"]
    assert damaged["status"] == "damaged" and damaged["linked_id"] == "70"     # the damaged pack took the spare's old ID
    recent = client.get("/api/recent?limit=60").json()["transactions"]
    assert any("Collected by" in (t["note"] or "") for t in recent)


def test_direct_edit_endpoints_and_site_pin(client):
    client.post("/admin/login", data={"password": "test1234"})
    who = client.get("/api/people/search?q=Emma").json()["results"][0]
    r = client.post(f"/admin/people/{who['id']}/field", json={"field": "role", "value": "Runner 2"})
    assert r.status_code == 200 and client.get(f"/api/people/{who['id']}").json()["person"]["role"] == "Runner 2"
    assert client.post(f"/admin/people/{who['id']}/field", json={"field": "name", "value": " "}).status_code == 400
    assert client.post(f"/admin/people/{who['id']}/field", json={"field": "created_at", "value": "x"}).status_code == 400
    tid = client.get("/api/types").json()["types"][0]["id"]
    assert client.post(f"/admin/types/{tid}/field", json={"field": "sort_order", "value": "abc"}).status_code == 400
    assert client.post(f"/admin/types/{tid}/field", json={"field": "short_code", "value": "bb"}).json()["value"] == "BB"
    # a kiosk tab pinned to site 2 pushes its state to site 2's mirror even though the cookie says site 1
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    client.post("/api/kiosk/state", json={"state": {"person": {"name": "Pinned"}, "basket": []}}, headers={"X-RDR-Site": "2"})
    assert client.get("/api/mirror/2").json()["state"]["person"]["name"] == "Pinned"
    assert (client.get("/api/mirror/1").json()["state"].get("person") or {}).get("name") != "Pinned"
    assert "age" in client.get("/api/mirror/2").json()


def test_live_config_version_changes_on_edits(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    client.post("/admin/login", data={"password": "test1234"})
    c1 = client.get("/api/config").json()
    assert c1["tech"]["display_name"] and c1["site"]["name"] and c1["types"]
    client.post("/admin/techs/1", data={"action": "save", "username": "tech", "display_name": "Renamed Tech", "role": "lead", "site_id": "", "active": "1"})
    c2 = client.get("/api/config").json()
    assert c2["version"] != c1["version"] and c2["tech"]["display_name"] == "Renamed Tech"
    m1 = client.get("/api/mirror/1").json()["config"]
    assert m1["feed"]["kind"] == "unknown"
    client.post("/admin/sites/1", data={"action": "save", "name": "RDR North", "stock_mode": "shared", "rtsp_url": "https://youtu.be/dQw4w9WgXcQ", "active": "1"})
    m2 = client.get("/api/mirror/1").json()["config"]
    assert m2["version"] != m1["version"] and m2["feed"]["kind"] == "youtube" and m2["feed"]["embed"]
    client.post("/admin/settings/mirror", data={"mirror_title": "", "mirror_prompt": "", "mirror_sayings": "", "pgm_size": "small"})
    assert client.get("/api/mirror/1").json()["config"]["feed"]["size"] == "small"
    client.post("/admin/sites/1", data={"action": "save", "name": "RDR North", "stock_mode": "shared", "rtsp_url": "", "active": "1"})


def test_reserved_pack_and_label_link(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    client.post("/admin/login", data={"password": "test1234"})
    types = {t["name"]: t["id"] for t in client.get("/api/types").json()["types"]}
    # a pack labelled with a number is configured as that ID from the start
    client.post("/admin/devices/new", data={"si": "SI-LAB-1", "type_id": types["Bolero Beltpack"], "label": "Bolero 88"})
    assert client.get("/api/devices/lookup?si=SI-LAB-1").json()["device"]["linked_id"] == "88"
    # a headset label never gets an ID
    client.post("/admin/devices/new", data={"si": "SI-LAB-2", "type_id": types["Pro-D2 Headset"], "label": "Pro-D2 12"})
    assert client.get("/api/devices/lookup?si=SI-LAB-2").json()["device"]["linked_id"] == ""
    # reserving an unlabelled pack for someone with an ID links it immediately
    client.post("/admin/devices/new", data={"si": "SI-LAB-3", "type_id": types["Bolero Beltpack"], "label": "Spare pack"})
    who = client.post("/api/people", json={"name": "Reserve Person", "bolero_id": "77"}).json()["person"]
    client.post("/admin/assignments", data={"si": "SI-LAB-3", "person_id": who["id"]})
    assert client.get("/api/devices/lookup?si=SI-LAB-3").json()["device"]["linked_id"] == "77"
    # setting the ID later also links an already-reserved pack
    client.post("/admin/devices/new", data={"si": "SI-LAB-4", "type_id": types["Bolero Beltpack"], "label": "Another spare"})
    late = client.post("/api/people", json={"name": "Late Id"}).json()["person"]
    client.post("/admin/assignments", data={"si": "SI-LAB-4", "person_id": late["id"]})
    assert client.get("/api/devices/lookup?si=SI-LAB-4").json()["device"]["linked_id"] == ""
    client.post(f"/api/people/{late['id']}", json={"bolero_id": "78"})
    assert client.get("/api/devices/lookup?si=SI-LAB-4").json()["device"]["linked_id"] == "78"
