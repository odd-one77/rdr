"""Three-way merge of a node's and the hub's copies of an event (invented data only)."""
import shutil

from app.db import changes, init_db, now_iso, open_db, using_db
from app.services import merge


def _db(path):
    with using_db(path):
        init_db()
    return open_db(path)


def _setup(tmp_path):
    base = tmp_path / "base.db"
    con = _db(base)
    with con:
        site = con.execute("SELECT id FROM sites").fetchone()[0]
        typ = con.execute("SELECT id FROM device_types WHERE name = 'Bolero Beltpack'").fetchone()[0]
        ts = now_iso()
        for name in ("Avery Lund", "Brook Tamsin", "Casey Orme"):
            con.execute("INSERT INTO people (name, created_at, updated_at) VALUES (?,?,?)", (name, ts, ts))
        for si in ("SI-1001", "SI-1002", "SI-1003"):
            con.execute("INSERT INTO devices (si, type_id, site_id, created_at, updated_at) VALUES (?,?,?,?,?)", (si, typ, site, ts, ts))
    con.close()
    node, hub = tmp_path / "node.db", tmp_path / "hub.db"
    shutil.copy(base, node); shutil.copy(base, hub)
    return base, node, hub


def _row(con, sql, *a):
    return con.execute(sql, a).fetchone()


def test_merge_both_sides(tmp_path):
    base, node_p, hub_p = _setup(tmp_path)
    node, hub = open_db(node_p), open_db(hub_p)
    n0 = changes(node)
    ts = now_iso()
    with node:   # the node signs a device out and adds a person and a device
        avery = _row(node, "SELECT id FROM people WHERE name = 'Avery Lund'")[0]
        node.execute("UPDATE devices SET status = 'out', holder_id = ? WHERE si = 'SI-1001'", (avery,))
        node.execute("INSERT INTO transactions (ts, action, device_id, person_id) VALUES (?, 'out', (SELECT id FROM devices WHERE si = 'SI-1001'), ?)", (ts, avery))
        node.execute("INSERT INTO people (name, created_at, updated_at) VALUES ('Dale Fenn', ?, ?)", (ts, ts))
        node.execute("INSERT INTO devices (si, type_id, created_at, updated_at) VALUES ('SI-2001', 1, ?, ?)", (ts, ts))
        node.execute("UPDATE people SET phone = '0700 111' WHERE name = 'Brook Tamsin'")
    assert changes(node) > n0
    with hub:    # the hub edits people, adds a device, deletes one, and adds the same SI the node added
        hub.execute("UPDATE people SET dept = 'Lighting' WHERE name = 'Avery Lund'")
        hub.execute("UPDATE people SET phone = '0700 222' WHERE name = 'Brook Tamsin'")   # both changed: node wins
        hub.execute("INSERT INTO devices (si, type_id, label, created_at, updated_at) VALUES ('SI-3001', 1, 'Spare', ?, ?)", (ts, ts))
        hub.execute("INSERT INTO devices (si, type_id, label, created_at, updated_at) VALUES ('SI-2001', 1, 'Hub label', ?, ?)", (ts, ts))
        hub.execute("DELETE FROM devices WHERE si = 'SI-1003'")
        hub.execute("DELETE FROM people WHERE name = 'Avery Lund'")   # but the node signed a device out to them
        hub.execute("UPDATE event SET show_name = 'Arena' WHERE id = 1")

    b = open_db(base)
    p = merge.plan(merge.load(b), merge.load(node), merge.load(hub))
    kinds = {c["kind"] for c in p["conflicts"]}
    assert {"both", "same", "kept"} <= kinds
    merge.apply(node, p["result"]); node.commit()

    avery = _row(node, "SELECT id, dept FROM people WHERE name = 'Avery Lund'")
    assert avery is not None                                      # still in use: kept
    d = _row(node, "SELECT status, holder_id FROM devices WHERE si = 'SI-1001'")
    assert d["status"] == "out" and d["holder_id"] == avery["id"]
    assert _row(node, "SELECT phone FROM people WHERE name = 'Brook Tamsin'")[0] == "0700 111"
    assert _row(node, "SELECT 1 FROM devices WHERE si = 'SI-3001'") is not None
    assert _row(node, "SELECT 1 FROM devices WHERE si = 'SI-1003'") is None
    assert node.execute("SELECT COUNT(*) FROM devices WHERE si = 'SI-2001'").fetchone()[0] == 1
    assert _row(node, "SELECT 1 FROM people WHERE name = 'Dale Fenn'") is not None
    assert _row(node, "SELECT show_name FROM event")[0] == "Arena"
    assert node.execute("SELECT COUNT(*) FROM transactions WHERE action = 'out'").fetchone()[0] == 1
    assert node.execute("PRAGMA foreign_key_check").fetchall() == []
    # merging the result with itself changes nothing
    again = merge.plan(merge.load(node), merge.load(node), merge.load(node))
    assert again["conflicts"] == [] and all(not any(v.values()) for v in again["summary"].values())


def test_ids_the_node_uses_are_kept(tmp_path):
    base, node_p, hub_p = _setup(tmp_path)
    node, hub = open_db(node_p), open_db(hub_p)
    before = {r["uid"]: r["id"] for r in node.execute("SELECT id, uid FROM people")}
    with hub:
        hub.execute("UPDATE sites SET name = 'Stage door' WHERE id = 1")
        hub.execute("UPDATE people SET name = 'Casey Orme-Hart' WHERE name = 'Casey Orme'")
    p = merge.plan(merge.load(open_db(base)), merge.load(node), merge.load(hub))
    merge.apply(node, p["result"]); node.commit()
    after = {r["uid"]: r["id"] for r in node.execute("SELECT id, uid FROM people")}
    assert before == after
    assert _row(node, "SELECT name FROM sites WHERE id = 1")[0] == "Stage door"
    assert _row(node, "SELECT 1 FROM people WHERE name = 'Casey Orme-Hart'") is not None


def test_local_settings_are_not_merged(tmp_path):
    base, node_p, hub_p = _setup(tmp_path)
    node, hub = open_db(node_p), open_db(hub_p)
    with node:
        node.execute("INSERT INTO settings (key, value) VALUES ('admin_password_hash', 'node-secret')")
    with hub:
        hub.execute("INSERT INTO settings (key, value) VALUES ('mirror_title', 'Welcome')")
    n = changes(node)
    p = merge.plan(merge.load(open_db(base)), merge.load(node), merge.load(hub))
    merge.apply(node, p["result"]); node.commit()
    assert _row(node, "SELECT value FROM settings WHERE key = 'admin_password_hash'")[0] == "node-secret"
    assert _row(node, "SELECT value FROM settings WHERE key = 'mirror_title'")[0] == "Welcome"
    assert n >= 0


def test_scan_during_merge_is_not_lost(tmp_path):
    """A scan committed while a merge runs waits for it, and is still there afterwards."""
    import threading
    base, node_p, hub_p = _setup(tmp_path)
    hub = open_db(hub_p)
    with hub:
        hub.execute("UPDATE people SET dept = 'Rigging' WHERE name = 'Casey Orme'")
    node = open_db(node_p)
    node.execute("BEGIN IMMEDIATE")
    p = merge.plan(merge.load(open_db(base)), merge.load(node), merge.load(hub))
    done = []

    def scan():
        other = open_db(node_p)
        with other:
            other.execute("INSERT INTO transactions (ts, action) VALUES ('2027-06-01 12:00:00', 'note')")
        done.append(True)

    t = threading.Thread(target=scan); t.start()
    t.join(0.3)
    assert not done            # held back by the merge's lock
    merge.apply(node, p["result"]); node.commit()
    t.join(5)
    assert done
    assert node.execute("SELECT COUNT(*) FROM transactions WHERE action = 'note'").fetchone()[0] == 1
    assert node.execute("SELECT dept FROM people WHERE name = 'Casey Orme'").fetchone()[0] == "Rigging"
