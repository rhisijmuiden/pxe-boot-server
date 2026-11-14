import json

from app import answers

PW = "$6$salt$hash"


def _row(fmt, iso_id=1, **data):
    d = dict(answers.DEFAULT_DATA, root_password_hash=PW, user_password_hash=PW, hostname="web01", **data)
    return {"id": 7, "iso_id": iso_id, "format": fmt, "data": json.dumps(d), "name": "t", "status": "ready", "updated": 1700000000.0, "created": 1700000000.0}


def test_autoinstall():
    files = answers.render(_row("autoinstall"), "http://10.0.0.5", "52:54:00:12:34:56")
    assert files["user-data"].startswith("#cloud-config")
    assert "web01" in files["user-data"]
    assert "meta-data" in files


def test_preseed():
    s = answers.render(_row("preseed"), "http://10.0.0.5")["preseed.cfg"]
    assert "d-i netcfg/get_hostname string web01" in s
    assert PW in s


def test_kickstart():
    s = answers.render(_row("kickstart"), "http://10.0.0.5")["ks.cfg"]
    assert "rootpw --iscrypted" in s
    assert "web01" in s


def test_proxmox():
    files = answers.render(_row("proxmox"), "http://10.0.0.5")
    assert "[global]" in files["answer.toml"]
    assert "web01" in files["answer.toml"]


def test_hash_password_is_sha512():
    assert answers.hash_password("secret123").startswith("$6$")


def test_clean_hostname():
    assert answers.clean_hostname("Web Server_01!") == "web-server-01" or \
        answers.clean_hostname("Web Server_01!").replace("-", "").isalnum()
