"""Report sheets: logos, titles, device name with SI and barcode; damaged and lost reasons shown on devices."""
import re

import pytest

from app.services import barcode


@pytest.fixture(scope="module", autouse=True)
def prepared(client):
    client.post("/start", data={"tech_id": 1, "site_id": 1})
    page = client.get("/admin/login").text
    if "Set the admin password" in page:
        client.post("/admin/login", data={"password": "test1234", "confirm": "test1234"})
    else:
        client.post("/admin/login", data={"password": "test1234"})
    if client.get("/api/summary").json()["all"]["totals"]["total"] == 0:
        client.post("/admin/settings/demo")


def decode(svg: str) -> str:
    """Read a barcode back from the SVG bars, the way a scanner does: widths to symbols, check digit, text."""
    total = int(re.search(r'viewBox="0 0 (\d+) 1"', svg).group(1))
    bars = [(int(x), int(w)) for x, w in re.findall(r"M(\d+) 0h(\d+)", svg)]
    widths, pos = [], bars[0][0]
    for k, (x, w) in enumerate(bars):
        if k:
            widths.append(x - pos)   # the space before this bar
        widths.append(w)
        pos = x + w
    assert bars[0][0] >= 10 and total - pos >= 10, "quiet zones"
    table = {p: i for i, p in enumerate(barcode._PATTERNS)}
    syms = ["".join(map(str, widths[i:i + 6])) for i in range(0, len(widths) - 7, 6)]
    syms = [table[s] for s in syms] + [table["".join(map(str, widths[-7:]))]]
    assert syms[-1] == 106
    *data, check, _stop = syms
    assert (data[0] + sum(k * s for k, s in enumerate(data[1:], 1))) % 103 == check
    out, mode = "", {104: "B", 105: "C"}[data[0]]
    for s in data[1:]:
        if mode == "C":
            if s == 100: mode = "B"
            else: out += f"{s:02d}"
        elif s == 99: mode = "C"
        else: out += chr(s + 32)
    return out


@pytest.mark.parametrize("text", ["SI-BOL-1001", "99000014", "si99000014", "A1", "1234567", "X-12345-Y", "000"])
def test_barcode_reads_back(text):
    assert decode(str(barcode.svg(text))) == text


def test_barcode_skips_what_it_cannot_carry():
    assert str(barcode.svg("")) == "" and str(barcode.svg("Pack ü")) == ""


def test_end_of_show_sheet(client):
    page = client.get("/admin/reports").text
    assert "Outstanding and damaged devices" in page and "riedel-dark.png" in page
    codes = re.findall(r'<svg class="barcode".*?</svg>', page, re.S)
    sis = re.findall(r'<div class="ds"><span class="si">([^<]+)</span>', page)
    assert codes and len(codes) == len(sis)
    assert [decode(c) for c in codes] == sis
    assert "Broken boom arm" in page
    assert "<h1>Outstanding devices</h1>" in client.get("/admin/reports?show=out").text
    assert "<h1>Damaged and lost devices</h1>" in client.get("/admin/reports?show=damaged").text


def test_device_name_is_type_unless_labelled(client):
    from app.db import get_db
    with get_db() as db:
        d = db.execute("SELECT d.id, d.si, t.name FROM devices d JOIN device_types t ON t.id = d.type_id WHERE d.status = 'damaged' LIMIT 1").fetchone()
        db.execute("UPDATE devices SET label = '' WHERE id = ?", (d["id"],))
    page = client.get("/admin/reports?show=damaged").text
    assert re.search(rf'<div class="dn"><a href="/admin/devices/{d["id"]}">{re.escape(d["name"])}</a></div>\s*<div class="ds"><span class="si">{re.escape(d["si"])}</span></div>', page)
    with get_db() as db:
        db.execute("UPDATE devices SET label = 'Spare boom' WHERE id = ?", (d["id"],))
    page = client.get("/admin/reports?show=damaged").text
    assert f'<a href="/admin/devices/{d["id"]}">Spare boom</a>' in page and f'{d["si"]}</span> · {d["name"]}' in page


def test_reason_shows_on_the_device(client):
    from app.db import get_db
    with get_db() as db:
        d = db.execute("SELECT d.id, d.si FROM devices d WHERE d.status = 'in' LIMIT 1").fetchone()
    client.post(f"/admin/devices/{d['id']}", data={"action": "status", "status": "lost", "reason": "Left in the van"})
    assert "Lost: Left in the van" in client.get(f"/admin/devices/{d['id']}").text
    assert "Left in the van" in client.get("/admin/devices?status=lost").text
    dev = client.get(f"/api/devices/lookup?si={d['si']}").json()["device"]
    assert dev["flag_reason"] == "Left in the van"
    assert "Left in the van" in client.get("/admin/reports?show=damaged").text
    client.post(f"/admin/devices/{d['id']}", data={"action": "status", "status": "in"})
    assert client.get(f"/api/devices/lookup?si={d['si']}").json()["device"]["flag_reason"] is None   # back in stock: no reason


def test_person_sheet(client):
    from app.db import get_db
    with get_db() as db:
        pid = db.execute("SELECT holder_id FROM devices WHERE status = 'out' AND holder_id IS NOT NULL LIMIT 1").fetchone()[0]
    page = client.get(f"/admin/people/{pid}/report").text
    assert "sheet-riedel" in page and "<h1>Outstanding devices</h1>" in page and '<svg class="barcode"' in page
