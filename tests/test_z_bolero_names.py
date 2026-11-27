"""Names set on Bolero packs compared with imported people, and search by pack name."""
import pytest

from app.services import bolero, bolero_names

PACKS = {"801": "Dana Whitfield", "802": "R Okonkwo", "803": "Sam P", "804": "Bolero 804", "805": "Quinn Lavelle", "806": "Teodor Brask"}


def test_same_name_rules():
    s = bolero_names.same_name
    assert s("Dana Whitfield", "dana  whitfield")
    assert s("Rafael Okonkwo", "R Okonkwo") and s("Rafael Okonkwo", "R. Okonkwo")
    assert s("Samantha Price", "Sam") and s("Samantha Price", "Sam P") and s("Samantha Price", "Samantha Pr")
    assert not s("Sam Price", "Samantha Price")
    assert not s("Dana Whitfield", "Morgan Hale")
    assert not s("Rafael Okonkwo", "T Okonkwo")
    # Bolero's 8 characters: first 6 of the first name, then the last initial, with or without the space
    assert s("Jonathan Smith", "Jonath S") and s("Jonathan Smith", "JonathS") and s("Hannah Weiss", "HannahW")
    assert s("Mary Jane Watson", "Mary W") and s("Bartholomew de Vries", "Bartho V") and s("Al Kerr", "Al K")
    assert not s("Hannah Weiss", "Hannah P") and not s("Hannah Weiss", "HannahP") and not s("Jonathan Smith", "Jonath T")
    assert not s("Jonathan Smith", "Joanna S")
    p = bolero_names.pack_style
    assert p("Jonathan Smith") == "Jonath S" and p("Al Kerr") == "Al K" and p("Mary Jane watson") == "Mary W" and p("Cher") == "Cher"
    assert not bolero_names.real_name("Bolero 61") and not bolero_names.real_name("BP 7") and not bolero_names.real_name("")
    assert bolero_names.real_name("Alex") and bolero_names.id_of("Bolero 061") == "61"


@pytest.fixture()
def names_on(monkeypatch, client):
    monkeypatch.setattr(bolero_names, "enabled", lambda: True)
    monkeypatch.setattr(bolero, "pack_names", lambda: dict(PACKS))
    monkeypatch.setitem(PACKS, "805", PACKS["805"])   # restored after the test
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    ids = {}
    for name, bid in (("Dana Whitfield", "801"), ("Rafael Okonkwo", "Bolero 802"), ("Samantha Price", "803"), ("Morgan Hale", "805")):
        ids[name] = client.post("/api/people", json={"name": name, "bolero_id": bid}).json()["person"]["id"]
    return ids


def test_prompt_search_and_choices(names_on, client):
    ids = names_on
    d = client.get(f"/api/people/{ids['Dana Whitfield']}").json()
    assert d["name_check"] is None
    assert client.get(f"/api/people/{ids['Rafael Okonkwo']}").json()["name_check"] is None   # initial counts as the same
    c = client.get(f"/api/people/{ids['Morgan Hale']}").json()["name_check"]
    assert c == {"number": "805", "pack_name": "Quinn Lavelle", "expected": "Morgan H"}

    # search by the pack name finds the person holding that ID, flagged as different
    r = client.get("/api/people/search?q=quinn").json()
    hit = [p for p in r["results"] if p["id"] == ids["Morgan Hale"]]
    assert hit and hit[0]["bolero_name"] == "Quinn Lavelle" and hit[0]["bolero_differs"]
    # a named pack nobody in RDR has
    r = client.get("/api/people/search?q=teodor").json()
    assert r["packs"] == [{"number": "806", "pack_name": "Teodor Brask"}]
    # default pack names are never searched
    assert client.get("/api/people/search?q=bolero 804").json()["packs"] == []

    # keep the RDR name: no more prompt for that pack name
    assert client.post(f"/api/people/{ids['Morgan Hale']}/bolero-name", json={"use": False, "pack_name": "Quinn Lavelle"}).status_code == 200
    assert client.get(f"/api/people/{ids['Morgan Hale']}").json()["name_check"] is None
    # a stale answer is refused
    assert client.post(f"/api/people/{ids['Morgan Hale']}/bolero-name", json={"use": True, "pack_name": "Quinn Lavelle"}).status_code == 409
    # the pack is renamed again: it asks again, and using it renames the person
    PACKS["805"] = "Morgan Hale-Byrne"
    c = client.get(f"/api/people/{ids['Morgan Hale']}").json()["name_check"]
    assert c and c["pack_name"] == "Morgan Hale-Byrne"
    r = client.post(f"/api/people/{ids['Morgan Hale']}/bolero-name", json={"use": True, "pack_name": "Morgan Hale-Byrne"})
    assert r.json()["person"]["name"] == "Morgan Hale-Byrne"
    assert client.get(f"/api/people/{ids['Morgan Hale']}").json()["name_check"] is None


def test_off_by_default(client):
    assert bolero.settings()["names"] is False
    r = client.get("/api/people/search?q=a").json()
    assert r["packs"] == [] and all("bolero_name" not in p for p in r["results"])


def test_flag_only_never_renames(monkeypatch, client):
    """Flag only: a name that differs is flagged, never asked about and never changed. People renamed by 0.9.17 can be restored."""
    from app.db import get_db
    monkeypatch.setattr(bolero_names, "enabled", lambda: True)
    monkeypatch.setattr(bolero_names, "flag_only", lambda: True)
    monkeypatch.setattr(bolero, "pack_names", lambda: {"821": "Imogen Strand", "822": "P Achterberg"})
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    a = client.post("/api/people", json={"name": "Wendell Frost", "bolero_id": "821"}).json()["person"]["id"]
    b = client.post("/api/people", json={"name": "Pieter Achterberg", "bolero_id": "822"}).json()["person"]["id"]

    d = client.get(f"/api/people/{a}").json()
    assert d["person"]["name"] == "Wendell Frost" and d["name_check"] is None
    assert d["name_flag"]["pack_name"] == "Imogen Strand" and d["name_flag"]["expected"] == "Wendel F"
    assert client.get(f"/api/people/{b}").json()["name_flag"] is None   # an initial on the pack is the same person
    hit = [p for p in client.get("/api/people/search?q=wendell").json()["results"] if p["id"] == a]
    assert hit and hit[0]["name"] == "Wendell Frost" and hit[0]["bolero_differs"]
    with get_db() as db:
        assert db.execute("SELECT name, import_name FROM people WHERE id = ?", (a,)).fetchone()[:] == ("Wendell Frost", "")
        # someone renamed by 0.9.17 before this change
        db.execute("UPDATE people SET import_name = name, name = 'Imogen Strand' WHERE id = ?", (a,))

    if "Set the admin password" in client.get("/admin/login").text:
        client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"})
    else:
        client.post("/admin/login", data={"password": "test1234"})
    monkeypatch.setattr(bolero, "settings", lambda: {"enabled": True, "names": True, "names_mode": "always", "hosts": [], "host": "", "mode": "standalone", "has_password": False, "charger_resource": ""})
    page = client.get("/admin/bolero").text
    assert "Names from Bolero · 1" in page and "Flag only, never ask" in page and "Names that differ" in page
    client.post("/admin/bolero/names/restore", data={"person_id": a})
    p = client.get(f"/api/people/{a}").json()
    assert p["person"]["name"] == "Wendell Frost" and p["name_flag"] is None   # restored on purpose: not flagged again


def test_names_mode_setting(monkeypatch):
    saved = {}
    monkeypatch.setattr(bolero, "load_config", lambda: dict(saved))
    monkeypatch.setattr(bolero, "save_config", lambda c: (saved.clear(), saved.update(c)))
    monkeypatch.setattr(bolero, "restart", lambda: None)
    for mode in ("always", "ask", "off"):
        bolero.configure("192.168.1.20", True, None, names=mode)
        assert bolero.settings()["names_mode"] == mode and bolero.settings()["names"] == (mode != "off")
    assert bolero.names_mode({"bolero_names": True}) == "ask"   # saved by the earlier tick box


def test_search_finds_cut_pack_names(monkeypatch):
    monkeypatch.setattr(bolero_names, "enabled", lambda: True)
    monkeypatch.setattr(bolero, "pack_names", lambda: {"831": "Jonath S", "832": "Hannah P"})
    from app.db import get_db
    with get_db() as db:
        _, loose = bolero_names.search(db, "jonathan", set())
        assert [x["number"] for x in loose] == ["831"]
        _, loose = bolero_names.search(db, "hannah p", set())
        assert [x["number"] for x in loose] == ["832"]
