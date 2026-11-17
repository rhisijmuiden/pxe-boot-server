import time

import pytest

from app import db, rules


def test_normalize_prefixes():
    assert rules.normalize_prefixes("52-54-00, BC:24:11\naa") == "52:54:00,bc:24:11,aa"
    assert rules.normalize_prefixes("") == ""
    for bad in ("5", "zz:00", "52:54:0", "52:54:00:11:22:33:44"):
        with pytest.raises(ValueError):
            rules.normalize_prefixes(bad)


@pytest.fixture
def iso(iso_row):
    return iso_row({"casper/vmlinuz": "k", "casper/initrd": "i"})


def _group(iso_id, mode="once", prefixes=""):
    return db.run("INSERT INTO machine_groups(name,prefixes,iso_id,mode,created) VALUES(?,?,?,?,?)",
                  f"g{time.time()}", prefixes, iso_id, mode, time.time())


def test_prefix_group_once_registers_machine(iso):
    gid = _group(iso["id"], "once", "02:aa:01")
    mac = "02:aa:01:00:00:01"
    rule = rules.effective(mac)
    assert rule["source"] == "group" and rules.auto_boot(rule)
    rules.mark_used(mac, rule)
    m = db.q1("SELECT * FROM machines WHERE mac=?", mac)
    assert m["group_id"] == gid and m["group_used"] == 1
    assert not rules.auto_boot(rules.effective(mac))
    rules.rearm_group(gid)
    assert rules.auto_boot(rules.effective(mac))


def test_longest_prefix_wins(iso):
    _group(iso["id"], "always", "02:bb")
    g2 = _group(iso["id"], "default", "02:bb:01")
    assert rules.effective("02:bb:01:00:00:02")["group"]["id"] == g2


def test_machine_rule_beats_group(iso):
    gid = _group(iso["id"], "always")
    mac = "02:cc:00:00:00:01"
    db.run("INSERT INTO machines(mac,name,iso_id,mode,created,group_id) VALUES(?,?,?,?,?,?)",
           mac, "m", iso["id"], "default", time.time(), gid)
    rule = rules.effective(mac)
    assert rule["source"] == "machine" and rule["mode"] == "default" and not rules.auto_boot(rule)
    db.run("UPDATE machines SET iso_id=NULL WHERE mac=?", mac)
    rule = rules.effective(mac)
    assert rule["source"] == "group" and rules.auto_boot(rule)


def test_group_without_iso_shows_menu(iso):
    _group(None, "always", "02:dd")
    assert rules.effective("02:dd:00:00:00:01") is None


def test_menu_endpoint_boots_group_iso(iso):
    from fastapi.testclient import TestClient
    from app.main import app
    _group(iso["id"], "once", "02:ee")
    c = TestClient(app)
    mac = "02:ee:00:00:00:09"
    r1 = c.get(f"/ipxe/menu.ipxe?mac={mac}&platform=efi", headers={"host": "10.0.0.5"})
    assert "kernel" in r1.text and "choose" not in r1.text
    r2 = c.get(f"/ipxe/menu.ipxe?mac={mac}&platform=efi", headers={"host": "10.0.0.5"})
    assert "choose" in r2.text
