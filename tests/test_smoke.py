"""End-to-end smoke test: admin setup, people, devices, kiosk sign out/in, swap, imports."""
import io

import openpyxl


def test_first_run_admin_password(client):
    r = client.get("/admin/login")
    assert r.status_code == 200 and "Set the admin password" in r.text
    r = client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200


def test_event_setup(client):
    r = client.post("/admin/event", data={"name": "Test Event", "show_name": "Night 1"})
    assert r.status_code == 303
    assert client.post("/admin/sites", data={"name": "RDR South", "stock_mode": "specific"}).status_code == 303
    assert client.post("/admin/techs", data={"username": "sam", "display_name": "Sam", "role": "tech"}).status_code == 303
    assert "RDR South" in client.get("/admin/event").text


def test_kiosk_requires_tech(client):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, follow_redirects=False) as anon:
        r = anon.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/start"
        assert anon.get("/api/people/search?q=a").status_code == 401


def test_start_sets_tech_cookie(client):
    r = client.post("/start", data={"tech_id": 1, "site_id": 1})
    assert r.status_code == 303 and "rdr_tech" in r.cookies
    assert client.get("/").status_code == 200
    # picking a tech deliberately drops any admin login; log the admin back in for the rest of the tests
    assert client.get("/admin").status_code == 303
    client.post("/admin/login", data={"password": "test1234"})


def test_people_and_devices_via_api(client):
    r = client.post("/api/people", json={"name": "Alex Morgan", "dept": "Broadcast", "role": "Director", "phone": "+44 7700 900101"})
    assert r.status_code == 200
    pid = r.json()["person"]["id"]
    r = client.get("/api/people/search?q=alex")
    assert r.json()["results"][0]["id"] == pid
    r = client.get("/api/people/search?q=7700900101")
    assert r.json()["results"][0]["id"] == pid

    r = client.get("/api/devices/lookup?si=si-bol-1001")
    assert r.json() == {"found": False, "si": "SI-BOL-1001"}
    types = client.get("/api/types").json()["types"]
    bol = next(t for t in types if t["name"] == "Bolero Beltpack")["id"]
    hs = next(t for t in types if t["name"] == "Pro-D2 Headset")["id"]
    for si, tid, label in (("SI-BOL-1001", bol, "Bolero 61"), ("SI-BOL-1002", bol, "Bolero 62"), ("SI-PD2-4001", hs, "Pro-D2 01"), ("SI-PD2-4002", hs, "Pro-D2 02")):
        assert client.post("/api/devices/quick", json={"si": si, "type_id": tid, "label": label}).status_code == 200
    assert client.post("/api/devices/quick", json={"si": "SI-BOL-1001", "type_id": bol}).status_code == 409
    d = client.get("/api/devices/lookup?si=SI-BOL-1001").json()["device"]
    assert d["status"] == "in" and d["label"] == "Bolero 61"


def _ids(client, *sis):
    return [client.get(f"/api/devices/lookup?si={s}").json()["device"]["id"] for s in sis]


def test_sign_out_and_in_flow(client):
    pid = client.get("/api/people/search?q=alex").json()["results"][0]["id"]
    bol, hs = _ids(client, "SI-BOL-1001", "SI-PD2-4001")
    r = client.post("/api/signout", json={"person_id": pid, "device_ids": [bol, hs]})
    assert [x["outcome"] for x in r.json()["results"]] == ["issued", "issued"]
    assert r.json()["summary"]["totals"]["n_out"] == 2

    # same again: already held, logged but unchanged
    r = client.post("/api/signout", json={"person_id": pid, "device_ids": [bol]})
    assert r.json()["results"][0]["outcome"] == "already_held"

    # someone else wants the same Bolero -> conflict, then forced reassign
    other = client.post("/api/people", json={"name": "Priya Nair", "phone": "+44 7700 900102"}).json()["person"]["id"]
    r = client.post("/api/signout", json={"person_id": other, "device_ids": [bol]})
    assert r.json()["results"][0]["outcome"] == "conflict"
    r = client.post("/api/signout", json={"person_id": other, "device_ids": [bol], "force_ids": [bol]})
    assert r.json()["results"][0]["outcome"] == "reassigned"
    holdings = client.get(f"/api/people/{other}").json()["holdings"]
    assert [h["si"] for h in holdings] == ["SI-BOL-1001"]
    assert [h["si"] for h in client.get(f"/api/people/{pid}").json()["holdings"]] == ["SI-PD2-4001"]

    # sign in: one out, one already in
    bol2 = _ids(client, "SI-BOL-1002")[0]
    r = client.post("/api/signin", json={"device_ids": [bol, bol2]})
    assert [x["outcome"] for x in r.json()["results"]] == ["returned", "already_in"]
    assert client.get("/api/devices/lookup?si=SI-BOL-1001").json()["device"]["status"] == "in"

    # history has the in_noop timestamp for the already-in scan
    recent = client.get("/api/recent?limit=5").json()["transactions"]
    assert {t["action"] for t in recent} >= {"in", "in_noop"}


def test_swap_and_flag(client):
    pid = client.get("/api/people/search?q=alex").json()["results"][0]["id"]
    old = _ids(client, "SI-PD2-4001")[0]  # alex still holds this headset
    r = client.post("/api/swap", json={"old_device_id": old, "new_si": "SI-PD2-4002", "reason": "Damaged", "old_status": "damaged"})
    assert r.status_code == 200, r.text
    assert r.json()["old"]["status"] == "damaged" and r.json()["new"]["status"] == "out"
    holdings = client.get(f"/api/people/{pid}").json()["holdings"]
    assert [h["si"] for h in holdings] == ["SI-PD2-4002"]
    # swap to a device that is out fails
    bol = _ids(client, "SI-BOL-1001")[0]
    client.post("/api/signout", json={"person_id": pid, "device_ids": [bol]})
    new_id = _ids(client, "SI-PD2-4002")[0]
    r = client.post("/api/swap", json={"old_device_id": new_id, "new_si": "SI-BOL-1001", "reason": "x"})
    assert r.status_code == 400
    # flag the bolero lost
    r = client.post(f"/api/devices/{bol}/status", json={"status": "lost", "reason": "left in taxi"})
    assert r.json()["status"] == "lost"
    assert all(h["si"] != "SI-BOL-1001" for h in client.get(f"/api/people/{pid}").json()["holdings"])
    # scanning a lost item back in restores it
    r = client.post("/api/signin", json={"device_ids": [bol]})
    assert r.json()["results"][0]["outcome"] == "restored"
    # damaged headset restored via admin status form
    r = client.post(f"/admin/devices/{old}", data={"action": "status", "status": "in", "reason": "repaired"})
    assert r.status_code == 303


def test_assignments(client):
    pid = client.get("/api/people/search?q=priya").json()["results"][0]["id"]
    r = client.post("/admin/assignments", data={"si": "SI-BOL-1002", "person_id": pid, "note": "plan"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    pre = client.get(f"/api/people/{pid}").json()["assignments"]
    assert [a["si"] for a in pre] == ["SI-BOL-1002"]
    assert "SI-BOL-1002" in client.get("/admin/assignments").text


def _xlsx(headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _follow(client, r):
    assert r.status_code == 303, r.text
    return r.headers["location"]


def test_import_people_with_conflict(client):
    content = _xlsx(["Staff ID", "Full Name", "Department", "Job Title", "Mobile"], [
        ["C001", "Alex Morgan", "Broadcast", "Director", "+44 7700 900101"],   # exact -> update
        ["C002", "Priya Nair", "Broadcast", "Floor Manager", "+44 7700 999999"],  # name match, phone differs -> conflict
        ["C003", "Tom Becker", "Lighting", "LD", "+44 7700 900103"],          # create
        ["", "", "x", "", ""],                                                # error: no name
    ])
    loc = _follow(client, client.post("/admin/import/people", files={"file": ("crew.xlsx", content, "application/octet-stream")}))
    token = loc.rsplit("/", 1)[1]
    page = client.get(loc).text
    assert "Map columns" in page and 'selected>Full Name' in page  # auto-guessed
    loc2 = _follow(client, client.post(loc, data={"map_external_id": "0", "map_name": "1", "map_dept": "2", "map_role": "3", "map_phone": "4"}))
    review = client.get(loc2).text
    assert "Possible duplicates" in review and "Tom Becker" in review
    # decide: update Priya
    pid = client.get("/api/people/search?q=priya").json()["results"][0]["id"]
    rows = client.get(loc2).text
    import re
    m = re.search(r'name="dec_(\d+)"', rows)
    loc3 = _follow(client, client.post(f"/admin/import/s/{token}/commit", data={"action": "commit", f"dec_{m.group(1)}": f"update:{pid}"}))
    assert "Import+done" in loc3 or "Import%20done" in loc3
    assert len(client.get("/api/people/search?q=priya").json()["results"]) == 1
    p = client.get(f"/api/people/{pid}").json()["person"]
    assert p["phone"] == "+44 7700 999999" and p["external_id"] == "C002"
    assert client.get("/api/people/search?q=tom").json()["results"][0]["dept"] == "Lighting"


def test_import_devices_new_type(client):
    content = _xlsx(["Inventory No", "Article", "Name"], [
        ["SI-BOL-1001", "Bolero Beltpack", "Bolero 61 (renamed)"],   # update label
        ["SI-RAD-2001", "Radio", "Radio 01"],                        # create
        ["SI-NEW-9001", "Bolero NSA-002A Antenna", "Antenna A"],     # new type
        ["SI-RAD-2001", "Radio", "dup"],                             # dup in file
    ])
    loc = _follow(client, client.post("/admin/import/devices", files={"file": ("easyjob.xlsx", content, "application/octet-stream")}))
    token = loc.rsplit("/", 1)[1]
    loc2 = _follow(client, client.post(loc, data={"map_si": "0", "map_type": "1", "map_label": "2"}))
    assert "New device type" in client.get(loc2).text
    _follow(client, client.post(f"/admin/import/s/{token}/commit", data={"action": "commit"}))
    d = client.get("/api/devices/lookup?si=SI-NEW-9001").json()["device"]
    assert d["type_name"] == "Bolero NSA-002A Antenna"
    assert client.get("/api/devices/lookup?si=SI-BOL-1001").json()["device"]["label"] == "Bolero 61 (renamed)"
    # status untouched by import
    assert client.get("/api/devices/lookup?si=SI-BOL-1001").json()["device"]["status"] == "in"


def test_admin_pages_render(client):
    for path in ("/admin", "/admin/event", "/admin/types", "/admin/people", "/admin/devices", "/admin/assignments",
                 "/admin/transactions", "/admin/import", "/admin/settings", "/admin/people/1", "/admin/devices/1",
                 "/admin/people/1/report", "/mirror", "/start", "/"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
    r = client.get("/admin/transactions?format=csv")
    assert r.headers["content-type"].startswith("text/csv") and "Signed out" in r.text
    assert client.get("/api/mirror/1").json()["summary"]["totals"]["total"] > 0


def test_summary_per_site(client):
    s = client.get("/api/summary").json()
    assert s["all"]["totals"]["total"] >= 6
    assert s["site"]["totals"]["total"] == s["all"]["totals"]["total"]  # all shared stock


def test_reset_database(client):
    r = client.post("/admin/settings/reset", data={"confirm": "nope"})
    assert "err=" in r.headers["location"]
    r = client.post("/admin/settings/reset", data={"confirm": "RESET", "keep_password": "1"})
    assert r.status_code == 303
    assert client.get("/api/summary").json()["all"]["totals"]["total"] == 0
    assert client.get("/admin").status_code == 200  # password kept, still logged in
    # demo data loads
    r = client.post("/admin/settings/demo")
    assert r.status_code == 303 and "Demo" in r.headers["location"]
    assert client.get("/api/summary").json()["all"]["totals"]["total"] > 50


def test_mirror_greeting_and_tech_name(client):
    r = client.post("/admin/settings/mirror", data={"mirror_title": "", "mirror_prompt": "", "mirror_sayings": "One\n\n Two \n"})
    assert r.status_code == 303
    client.post("/api/kiosk/state", json={"state": {"person": None, "basket": []}})
    d = client.get("/api/mirror/1").json()
    assert [x["text"] for x in d["greeting"]["sayings"]] == ["One", "Two"]
    assert d["greeting"]["title"] == "Welcome to RDR"
    assert "{tech}" in d["greeting"]["prompt"]
    assert d["state"]["tech_name"] == "RDR Tech"
    assert "Save mirror screen" in client.get("/admin/settings").text
