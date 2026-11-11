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
    assert r.status_code == 200 and "Outstanding" in r.text and "Night 3" in r.text
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


def test_partial_si_search(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    r = client.get("/api/devices/search?q=1001").json()["results"]
    assert r and r[0]["si"].endswith("1001") and "type_name" in r[0]
    r = client.get("/api/devices/search?q=bol-10").json()["results"]
    assert all("BOL-10" in x["si"] for x in r) and len(r) <= 12
    assert client.get("/api/devices/search?q=").json()["results"] == []


def test_movie_quotes_toggle(client):
    from app.services import quotes
    assert len(quotes.MOVIE_QUOTES) >= 40 and all(q["credit"] for q in quotes.MOVIE_QUOTES)
    assert len({q["id"] for q in quotes.MOVIE_QUOTES}) == len(quotes.MOVIE_QUOTES)
    client.post("/admin/login", data={"password": "test1234"})
    r = client.post("/admin/settings/mirror", data={"mirror_title": "", "mirror_prompt": "", "mirror_sayings": "Own line", "pgm_size": "half", "movie_quotes": "1"})
    assert r.status_code == 303
    g = client.get("/api/mirror/1").json()["greeting"]
    assert len(g["sayings"]) == 1 + len(quotes.MOVIE_QUOTES)
    assert g["sayings"][0] == {"text": "Own line", "credit": ""}
    assert any(x["text"].startswith("Ah, Mr. Bond") and "Bond" in x["credit"] for x in g["sayings"])
    assert 'name="movie_quotes" value="1" checked' in client.get("/admin/settings").text
    client.post("/admin/settings/mirror", data={"mirror_title": "", "mirror_prompt": "", "mirror_sayings": "", "pgm_size": "half"})
    assert client.get("/api/mirror/1").json()["greeting"]["sayings"] == []


def _allocation_workbook():
    """A sheet in the real allocation layout: a title row, the header on row 2, a blank colour column (B),
    'xx' IDs, TBA names, pack and headset columns."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill
    wb = Workbook(); ws = wb.active; ws.title = "2026 Device Allocation"
    ws.append(["BOLERO UNITS"])
    ws.append(["Bolero #", "Column2", "Position", "Role Name", "Person", "Team Leader", "Pack", "Headset"])
    rows = [
        (1, "E9516A", "Audio Crew 1", "AUDIO 1", "Sam Porter", "", "Bolero", "Pro-D1"),
        (2, "00B050", "Lx Crew 1", "LX 1", "Alex Reed", "", "Bolero", ""),
        (3, "00B050", "Followspot 1", "SPOT 1", "TBA", "", "Bolero", "Pro-D2"),
        (4, "000000", "Floor Manager", "STAGE MANAGER", "Dana Hill", "", "Bolero", "Pro-D1"),
        ("xx", "FF0000", "Riedel 1", "COMMS 1", "Chris Lane", "", "Bolero", ""),
        (5, "", "Choir Manager", "CHOIR MGR", "", "", "", ""),
        (6, "00B050", "Pyro Crew 1", "PYRO 1", "Flame Crew", "", "Bolero", "Max-E1"),
        (7, "00B050", "Pyro Crew 2", "PYRO 2", "Flame Crew", "", "Bolero", ""),
        (8, "00B0F0", "Guest listen", "GUEST 1", "TBA", "", "Bolero", ""),
        (9, "00B0F0", "Guest listen", "GUEST 2", "TBA", "", "Bolero", ""),
        (10, "FFFF00", "", "CAST 9", "", "", "Bolero", ""),          # no name, no position: the role name carries it
    ]
    for i, (bid, colour, pos, role, name, tl, pack, hs) in enumerate(rows, start=3):
        ws.append([bid, None, pos, role, name, tl, pack, hs])
        if colour:
            ws.cell(i, 2).fill = PatternFill("solid", fgColor="FF" + colour)
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def test_allocation_sheet_import(client):
    client.post("/admin/login", data={"password": "test1234"})
    loc = client.post("/admin/import/people", files={"file": ("Comms_allocation.xlsx", _allocation_workbook(), "application/octet-stream")}).headers["location"]
    token = loc.rsplit("/", 1)[1]
    from app.services import importer
    staged = importer.load_staged(token)
    assert staged["header_row"] == 1 and staged["headers"][0] == "Bolero #"
    m = staged["mapping"]
    assert m["bolero_id"] == 0 and m["faceplate"] == 1 and m["dept"] == 2 and m["role"] == 3 and m["name"] == 4 and m["pack"] == 6 and m["headset"] == 7
    client.post(loc, data={f"map_{k}": ("" if v is None else str(v)) for k, v in m.items()})
    plan = importer.load_staged(token)["plan"]
    by_name = {it["data"]["name"]: it for it in plan["items"]}
    assert by_name["Sam Porter"]["data"]["faceplate"] == "E9516A" and by_name["Sam Porter"]["data"]["allocation"] == "Bolero · Pro-D1 headset"
    assert by_name["Followspot 1 (TBA)"]["data"]["bolero_id"] == "3"          # placeholder keeps its position as the name
    assert by_name["Dana Hill"]["data"]["faceplate"] == "000000"
    assert by_name["Chris Lane"]["data"]["bolero_id"] == "" and by_name["Chris Lane"]["data"]["faceplate"] == "FF0000"   # 'xx' is not an ID
    assert by_name["Choir Manager (TBA)"]["data"]["bolero_id"] == "5"
    assert by_name["Flame Crew"]["data"]["bolero_id"] == "6" and by_name["Flame Crew (Pyro Crew 2)"]["data"]["bolero_id"] == "7"   # one crew, two packs
    assert by_name["Guest listen (TBA)"]["data"]["bolero_id"] == "8" and by_name["Guest listen (TBA #9)"]["data"]["bolero_id"] == "9"
    assert by_name["CAST 9 (TBA)"]["data"]["bolero_id"] == "10" and by_name["CAST 9 (TBA)"]["data"]["role"] == "CAST 9"
    client.post(f"/admin/import/s/{token}/commit", data={"action": "commit"})
    sam = client.get("/api/people/search?q=Sam+Porter").json()["results"][0]
    assert sam["faceplate"] == "E9516A" and sam["bolero_id"] == "1" and sam["dept"] == "Audio Crew 1" and sam["role"] == "AUDIO 1"
    client.post("/start", data={"tech_id": 1, "site_id": 1}); client.post("/admin/login", data={"password": "test1234"})
    assert client.get("/api/people/search?q=SPOT+1").json()["results"][0]["name"] == "Followspot 1 (TBA)"   # searchable by role name
    assert client.get("/api/people/search?q=CAST+9").json()["results"][0]["bolero_id"] == "10"
    # colour text also works, and the header-row override re-guesses the mapping
    csv = "Name,Bolero,Colour\nText Colour,90,green\n"
    loc = client.post("/admin/import/people", files={"file": ("c.csv", csv.encode(), "text/csv")}).headers["location"]
    token = loc.rsplit("/", 1)[1]
    client.post(loc, data={"map_name": "0", "map_bolero_id": "1", "map_faceplate": "2"})
    client.post(f"/admin/import/s/{token}/commit", data={"action": "commit"})
    assert client.get("/api/people/search?q=Text+Colour").json()["results"][0]["faceplate"] == "00B050"


def test_delivery_note_parsing_and_rules():
    from app.services import importer
    text = """Pos. Art.No. Description Qty.
17 1000118 Riedel PRO-D1 (XLR4F) Intercom Headset, medium weight, single ear, dynamic xxx 3
Barcode:
@si99000002, @si99000004, @si99000010
18 1008477 Riedel BL-ANT-1010-19G-EU Bolero Wireless Antenna, DECT, 1880-1900MHz, G2, Standalone 2
Barcode:
@si99000011, @si99000008
20 1008090 Riedel BL-BPK-1006-19 Bolero Beltpack, DECT 10
Barcode:
@si99000014, @si99000013, @si99000006, @si99000015, @si99000016, @si99000017, @si99000012, @si99000005,
@si99000007, @si99000009
21 1008128 Riedel BL-BAT-1015-ST Bolero Li-Ion Battery, 17,3Wh, incl. Beltclip 16
26 1006654 Riedel HC Headset Cord, for Hytera PD6XX 1
Barcode:
@si99000003
16 1000065 Riedel MIC-30 Panel Microphone 6
Barcode:
123456, @si99000001, 654321
26-0000.00 | Page 2 of 3
"""
    rows = importer.parse_delivery_text(text)
    by_type = {}
    for r in rows:
        by_type.setdefault(r[1], []).append(r[0])
    assert len(by_type["Bolero Beltpack"]) == 10 and by_type["Bolero Beltpack"][0] == "SI99000014"
    assert by_type["Pro-D1 Headset"] == ["SI99000002", "SI99000004", "SI99000010"]
    assert by_type["Radio Headset Adapter"] == ["SI99000003"]           # the cord is for radio headsets, not a radio
    assert "" in by_type and len(by_type[""]) == 2 + 1 + 3               # antenna x2, battery (no barcode), mic x3 ignored
    assert importer.classify_description("Riedel PRO-D2 (XLR4F) Intercom Headset") == "Pro-D2 Headset"
    assert importer.classify_description("Hytera PD685 Radio") == "Radio"
    assert importer.classify_description("Riedel RSP-2318 Smartpanel") == ""
    assert importer.classify_description("Riedel BL-CHG-1005-R Bolero 5-way Charger, for Riedel BL-BPK-1006-19 Bolero") == ""
    assert importer.classify_description("Riedel Bolero BL-BPK-Cover Black") == ""
    assert importer.classify_description("Punqtum Q110 Beltpack") == ""


def test_delivery_note_pdf_import(client):
    """Build a small PDF with pypdfium2-free text via pdfplumber's sibling? No writer available; so exercise the
    staged path with the parsed rows through the normal devices flow instead."""
    from app.services import importer
    client.post("/admin/login", data={"password": "test1234"})
    rows = importer.parse_delivery_text("20 1008090 Riedel BL-BPK-1006-19 Bolero Beltpack, DECT 2\nBarcode:\n@si90000001, @si90000002\n15 1006644 Riedel SPX-AES Expansion Card 3\nBarcode:\n@si90000003, @si90000004, @si90000005\n")
    raw = [["SI", "Type", "Label", "Art.No.", "Description", "Qty", "Pos"]] + rows
    csv = "\n".join(",".join(f'"{c}"' for c in r) for r in raw) + "\n"
    loc = client.post("/admin/import/devices", files={"file": ("delivery.csv", csv.encode(), "text/csv")}).headers["location"]
    token = loc.rsplit("/", 1)[1]
    staged = importer.load_staged(token)
    m = staged["mapping"]
    assert m["si"] == 0 and m["type"] == 1 and m["notes"] == 4
    client.post(loc, data={f"map_{k}": ("" if v is None else str(v)) for k, v in m.items()})
    plan = importer.load_staged(token)["plan"]
    assert plan["counts"]["create"] == 2 and plan["counts"]["error"] == 3
    assert all("Ignored" in it["message"] for it in plan["items"] if it["action"] == "error")
    client.post(f"/admin/import/s/{token}/commit", data={"action": "commit"})
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    d = client.get("/api/devices/lookup?si=@si90000001").json()           # a scanner that sends the '@' still matches
    assert d["found"] and d["device"]["type_name"] == "Bolero Beltpack"
    assert not client.get("/api/devices/lookup?si=SI90000003").json()["found"]


def test_easyjob_export_descriptions_become_types(client):
    """An easyjob export lists the article description, not a type: beltpacks and headsets must still land
    in the right types and panels must be ignored, exactly like the PDF."""
    from app.services import importer
    client.post("/admin/login", data={"password": "test1234"})
    csv = ("Inventory No,Article,Qty\n"
           "@si77000001,\"Riedel BL-BPK-1006-19 Bolero Beltpack, DECT\",1\n"
           "@si77000002,\"Riedel PRO-D1 (XLR4F) Intercom Headset, medium weight, single ear, dynamic\",1\n"
           "@si77000003,\"Riedel RSP-2318 Smartpanel, 2 headset connectors\",1\n"
           "@si77000004,\"Riedel 1200 Series Panel\",1\n"
           "@si77000005,Hytera PD685 Radio,1\n")
    loc = client.post("/admin/import/devices", files={"file": ("export.csv", csv.encode(), "text/csv")}).headers["location"]
    token = loc.rsplit("/", 1)[1]
    m = importer.load_staged(token)["mapping"]
    assert m["si"] == 0 and m["type"] == 1
    client.post(loc, data={f"map_{k}": ("" if v is None else str(v)) for k, v in m.items()})
    plan = importer.load_staged(token)["plan"]
    assert plan["counts"] == {"create": 3, "update": 0, "conflict": 0, "skip": 0, "error": 2}
    assert {it["data"]["type"] for it in plan["items"] if it["action"] == "create"} == {"Bolero Beltpack", "Pro-D1 Headset", "Radio"}
    assert all("Ignored" in it["message"] for it in plan["items"] if it["action"] == "error")
    client.post(f"/admin/import/s/{token}/commit", data={"action": "commit"})
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    d = client.get("/api/devices/lookup?si=si77000001").json()["device"]
    assert d["type_name"] == "Bolero Beltpack" and "BL-BPK" in d["notes"]
    assert not client.get("/api/devices/lookup?si=si77000003").json()["found"]


def test_delivery_note_wrapped_lines():
    from app.services import importer
    text = """20 1008090 Riedel BL-BPK-1006-19 Bolero Beltpack, DECT, with very long description that
wraps to the next line 4
Barcode:
@si99000014, @si99000013
@si99000006, @si99000015
17 1000118 Riedel PRO-D1 (XLR4F) Intercom Headset 1
Barcode: @si99000002
"""
    rows = importer.parse_delivery_text(text)
    packs = [r for r in rows if r[1] == "Bolero Beltpack"]
    assert [r[0] for r in packs] == ["SI99000014", "SI99000013", "SI99000006", "SI99000015"] and packs[0][5] == "4"
    assert [r[0] for r in rows if r[1] == "Pro-D1 Headset"] == ["SI99000002"]


def test_away_from_desk(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    h = {"X-RDR-Site": "1"}
    assert client.get("/api/away", headers=h).json()["away"] is None
    r = client.post("/api/away", json={"minutes": 10, "note": "Gone to stage"}, headers=h)
    a = r.json()["away"]
    assert r.status_code == 200 and a["label"] == "Back in 10 min" and a["remaining"] in (9, 10) and a["until_hm"] and not a["overdue"]
    assert client.get("/api/config", headers=h).json()["away"]["label"] == "Back in 10 min"
    assert client.get("/api/mirror/1").json()["away"]["note"] == "Gone to stage"
    assert client.get("/api/mirror/2").json()["away"] is None      # per site
    assert client.post("/api/away/unlock", json={"password": "wrong"}, headers=h).status_code == 403
    assert client.get("/api/away", headers=h).json()["away"] is not None
    assert client.post("/api/away/unlock", json={"password": "test1234"}, headers=h).status_code == 200
    assert client.get("/api/away", headers=h).json()["away"] is None
    # a clock time, and the plain "be right back"
    assert client.post("/api/away", json={"until": "23:59"}, headers=h).json()["away"]["label"] == "Back at 23:59"
    assert client.post("/api/away", json={}, headers=h).json()["away"]["label"] == "Be right back"
    client.post("/admin/login", data={"password": "test1234"})      # picking a tech dropped the admin login
    assert "Be right back" in client.get("/admin").text
    client.post("/admin/away/clear/1")
    assert client.get("/api/away", headers=h).json()["away"] is None
