import hashlib
import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import catalog, config, db

DATA = b"ISO" * 100_000


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("inner/memtest.iso", DATA)
    return buf.getvalue()


FILES = {"/a.iso": DATA, "/m.iso.zip": _zip_bytes()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = FILES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(body):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        self.wfile.write(body[start:])


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_parse_sums_gnu_and_bsd():
    text = ("abc" * 0 + "a" * 64 + " *ubuntu-24.04.3-live-server-amd64.iso\n"
            + "b" * 64 + "  ubuntu-24.04.4-live-server-amd64.iso\n"
            "SHA256 (Rocky-10.1-x86_64-minimal.iso) = " + "c" * 64 + "\n"
            + "d" * 32 + "  clonezilla-live-3.2.2-15-amd64.iso\n"
            + "e" * 64 + "  clonezilla-live-3.2.2-15-amd64.iso\n"
            "# comment\n")
    sums = catalog.parse_sums(text)
    assert sums["ubuntu-24.04.4-live-server-amd64.iso"] == "b" * 64
    assert sums["Rocky-10.1-x86_64-minimal.iso"] == "c" * 64
    assert sums["clonezilla-live-3.2.2-15-amd64.iso"] == "e" * 64  # strongest hash wins


def test_pick_latest_compares_versions_numerically():
    sums = {"ubuntu-24.04-live-server-amd64.iso": "1" * 64,
            "ubuntu-24.04.10-live-server-amd64.iso": "2" * 64,
            "ubuntu-24.04.9-live-server-amd64.iso": "3" * 64,
            "ubuntu-24.04.10-desktop-amd64.iso": "4" * 64}
    name, v, h = catalog.pick_latest(sums, catalog.BY_ID["ubuntu-2404-server"]["pattern"])
    assert (name, v, h) == ("ubuntu-24.04.10-live-server-amd64.iso", "24.04.10", "2" * 64)


def test_resolve_builds_template_url(monkeypatch):
    monkeypatch.setattr(catalog, "fetch_text", lambda url: "f" * 64 + "  clonezilla-live-3.3.0-33-amd64.iso\n")
    r = catalog.resolve("clonezilla")
    assert r["url"].endswith("/clonezilla_live_stable/3.3.0-33/clonezilla-live-3.3.0-33-amd64.iso")
    assert r["algo"] == "sha256" and not r["pinned"]


def test_resolve_relative_to_sums(monkeypatch):
    monkeypatch.setattr(catalog, "fetch_text", lambda url: "f" * 64 + "  proxmox-ve_9.1-1.iso\n" + "0" * 64 + "  proxmox-ve_9.0-1.iso\n")
    r = catalog.resolve("proxmox-ve")
    assert r["url"] == "https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso"


def test_queue_validation():
    with pytest.raises(ValueError):
        catalog.queue(url="ftp://x/a.iso", filename="a.iso", folder="")
    with pytest.raises(ValueError):
        catalog.queue(url="http://x/a.exe", filename="a.exe", folder="")
    with pytest.raises(ValueError):
        catalog.queue(url="http://x/a.iso", filename="a.iso", folder="../etc")
    with pytest.raises(ValueError):
        catalog.queue(url="http://x/a.iso", filename="a.iso", folder="", expected="nothex")


def _insert(url, filename, folder="", expected="", archive=""):
    import time
    return db.run("INSERT INTO downloads(name,url,filename,folder,archive,expected,status,created,updated) "
                  "VALUES(?,?,?,?,?,?,'queued',?,?)", filename, url, filename, folder, archive, expected, time.time(), time.time())


def test_download_verifies_and_adds_to_library(server):
    did = _insert(server + "/a.iso", "dl-ok.iso", "Downloads", hashlib.sha256(DATA).hexdigest())
    catalog.run_one(did)
    row = db.q1("SELECT * FROM downloads WHERE id=?", did)
    assert row["status"] == "done", row["error"]
    assert (config.ISO_DIR / "Downloads" / "dl-ok.iso").read_bytes() == DATA
    assert db.q1("SELECT 1 FROM isos WHERE relpath='Downloads/dl-ok.iso'")


def test_download_resumes_partial_file(server):
    did = _insert(server + "/a.iso", "dl-resume.iso", "", hashlib.sha256(DATA).hexdigest())
    catalog.part_path(did).write_bytes(DATA[:12345])
    catalog.run_one(did)
    assert db.q1("SELECT status FROM downloads WHERE id=?", did)["status"] == "done"
    assert (config.ISO_DIR / "dl-resume.iso").read_bytes() == DATA


def test_download_checksum_mismatch_removes_file(server):
    did = _insert(server + "/a.iso", "dl-bad.iso", "", "0" * 64)
    catalog.run_one(did)
    row = db.q1("SELECT * FROM downloads WHERE id=?", did)
    assert row["status"] == "error" and row["error"] == "checksum mismatch"
    assert not (config.ISO_DIR / "dl-bad.iso").exists()
    assert not catalog.part_path(did).exists()


def test_download_zip_is_extracted(server):
    did = _insert(server + "/m.iso.zip", "dl-mem.iso", "Tools", archive="zip")
    catalog.run_one(did)
    assert db.q1("SELECT status FROM downloads WHERE id=?", did)["status"] == "done"
    assert (config.ISO_DIR / "Tools" / "dl-mem.iso").read_bytes() == DATA


def test_catalog_page_renders():
    from fastapi.testclient import TestClient
    from app import auth
    from app.main import app
    c = TestClient(app)
    if not db.q1("SELECT 1 FROM users WHERE username='cat'"):
        auth.create_user("cat", "password123")
    r = c.post("/login", data={"username": "cat", "password": "password123",
                               "csrf": c.get("/login").text.split('name="csrf" value="')[1].split('"')[0]})
    r = c.get("/catalog")
    assert r.status_code == 200 and "Proxmox VE" in r.text
