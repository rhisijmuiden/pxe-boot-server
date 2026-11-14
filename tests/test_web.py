from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"]


def test_ipxe_entry_without_login():
    r = client.get("/ipxe/boot.ipxe", headers={"host": "10.0.0.5"})
    assert r.status_code == 200 and r.text.startswith("#!ipxe")


def test_pages_require_login():
    r = client.get("/library", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_translations_complete():
    import json
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "app" / "locales"
    en = json.loads((base / "en.json").read_text())
    nl = json.loads((base / "nl.json").read_text())
    assert set(en) == set(nl)
    assert all(v.strip() for v in nl.values())
