import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import db, proxmox

CALLS = []


class FakePve(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, data, code=200):
        body = json.dumps({"data": data}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.headers["Authorization"] == "PVEAPIToken=pxe@pve!boot=secret"
        p = urllib.parse.urlparse(self.path).path.replace("/api2/json", "")
        if p == "/version":
            return self._send({"version": "9.0.3"})
        if p == "/nodes":
            return self._send([{"node": "pve2", "status": "online"}, {"node": "pve1", "status": "online"}])
        if p == "/cluster/nextid":
            return self._send("105")
        if p.endswith("/status") and "/tasks/" in p:
            return self._send({"status": "stopped", "exitstatus": "OK"})
        if p == "/nodes/pve1/qemu/105/config":
            return self._send({"net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0"})
        if p == "/nodes/pve1/storage":
            return self._send([{"storage": "local-lvm"}])
        if p == "/nodes/pve1/network":
            return self._send([{"iface": "vmbr0"}])
        if p == "/cluster/resources":
            return self._send([{"vmid": 105, "name": "web02", "node": "pve1", "type": "qemu", "status": "stopped", "tags": "pxe-boot"},
                               {"vmid": 100, "name": "other", "node": "pve1", "type": "qemu", "status": "running"}])
        self._send(None, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path.replace("/api2/json", "")
        n = int(self.headers.get("Content-Length") or 0)
        CALLS.append((p, dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))))
        self._send("UPID:pve1:0000:task:")


@pytest.fixture
def pve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakePve)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    db.set_setting("proxmox", {"host": f"http://127.0.0.1:{srv.server_port}", "token_id": "pxe@pve!boot",
                               "secret": "secret", "verify": False, "node": "", "storage": "", "bridge": "vmbr0"})
    CALLS.clear()
    yield
    srv.shutdown()
    db.set_setting("proxmox", {"host": "", "token_id": "", "secret": ""})


def test_normalize_host():
    assert proxmox.normalize_host("pve1.lan") == "https://pve1.lan:8006"
    assert proxmox.normalize_host("https://10.0.0.2:8006/") == "https://10.0.0.2:8006"


def test_overview_and_options(pve):
    assert proxmox.overview() == {"version": "9.0.3", "nodes": ["pve1", "pve2"]}
    assert proxmox.node_options("pve1") == {"storages": ["local-lvm"], "bridges": ["vmbr0"]}
    assert [v["vmid"] for v in proxmox.list_vms()] == [105]


def test_create_linux_vm(pve, iso_row):
    row = iso_row({"casper/vmlinuz": "k", "casper/initrd": "i"})
    spec = proxmox.validate({"name": "web02", "node": "pve1", "storage": "local-lvm", "bridge": "vmbr0", "vlan": "20",
                             "cores": "2", "memory_mb": 4096, "disk_gb": "32", "firmware": "uefi"})
    vm = proxmox.create_vm(spec, row)
    assert vm == {"vmid": 105, "node": "pve1", "mac": "bc:24:11:aa:bb:cc"}
    path, params = CALLS[0]
    assert path == "/nodes/pve1/qemu"
    assert params["net0"] == "virtio,bridge=vmbr0,tag=20"
    assert params["boot"] == "order=net0;scsi0" and params["bios"] == "ovmf"
    assert "efitype=2m" in params["efidisk0"] and "pre-enrolled-keys=0" in params["efidisk0"]
    assert params["tags"] == "pxe-boot" and "tpmstate0" not in params


def test_windows_vm_params(iso_row):
    row = iso_row({"sources/boot.wim": "w", "boot/bcd": "b", "boot/boot.sdi": "s"})
    p = proxmox.vm_params({"vmid": 1, "name": "pc1", "storage": "s", "bridge": "vmbr0", "vlan": None, "cores": 2,
                           "memory_mb": 8192, "disk_gb": 64, "firmware": "uefi"}, row)
    assert p["net0"].startswith("e1000,") and p["sata0"] == "s:64" and p["boot"] == "order=net0;sata0"
    assert p["ostype"] == "win11" and "tpmstate0" in p


def test_validate_rejects_bad_input():
    base = {"name": "ok", "node": "pve1", "storage": "local", "bridge": "vmbr0", "cores": 2, "memory_mb": 2048, "disk_gb": 16}
    for bad in ({"name": "bad name"}, {"cores": 0}, {"vlan": 5000}, {"node": "pve1;rm"}):
        with pytest.raises(ValueError):
            proxmox.validate(base | bad)


def test_api_error_is_reported(pve):
    proxmox.settings()
    db.set_setting("proxmox", proxmox.settings() | {"secret": "wrong"})
    with pytest.raises(proxmox.ProxmoxError):
        proxmox.overview()
