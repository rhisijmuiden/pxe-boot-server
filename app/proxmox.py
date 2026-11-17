"""Proxmox VE integration: create a VM that network boots a chosen ISO from this server.

Talks to the Proxmox API with an API token (Datacenter > Permissions > API Tokens). The VM gets
network first in its boot order and a machine rule (by its MAC address) so it boots the chosen ISO,
optionally with an answer file, on its first start.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db, isos

TAG = "pxe-boot"
NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$")  # Proxmox wants a DNS name


class ProxmoxError(Exception):
    pass


def settings() -> dict:
    return db.get_setting("proxmox")


def configured() -> bool:
    s = settings()
    return bool(s.get("host") and s.get("token_id") and s.get("secret"))


def normalize_host(host: str) -> str:
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if not re.match(r"^https?://", host):
        host = "https://" + host
    p = urllib.parse.urlparse(host)
    if not p.hostname:
        raise ValueError("host")
    port = p.port or 8006
    return f"{p.scheme}://{p.hostname}:{port}"


class Client:
    def __init__(self, s: dict | None = None):
        s = s or settings()
        if not (s.get("host") and s.get("token_id") and s.get("secret")):
            raise ProxmoxError("not configured")
        self.base = s["host"].rstrip("/") + "/api2/json"
        self.auth = f"PVEAPIToken={s['token_id']}={s['secret']}"
        self.ctx = ssl.create_default_context()
        if not s.get("verify"):
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, method: str, path: str, params: dict | None = None, timeout: int = 30):
        url = self.base + path
        data = None
        if params and method == "GET":
            url += "?" + urllib.parse.urlencode(params)
        elif params:
            data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": self.auth})
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:  # noqa: S310 - https/http only
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                j = json.loads(exc.read() or b"{}")
                detail = j.get("message") or json.dumps(j.get("errors") or "")
            except ValueError:
                pass
            raise ProxmoxError(f"{exc.code} {exc.reason} {detail}".strip()[:300]) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProxmoxError(str(getattr(exc, "reason", exc))[:300]) from exc
        return body.get("data")

    def wait_task(self, node: str, upid: str, timeout: int = 120) -> None:
        end = time.time() + timeout
        while time.time() < end:
            st = self.call("GET", f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/status")
            if st and st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise ProxmoxError(f"task failed: {st.get('exitstatus')}")
                return
            time.sleep(1)
        raise ProxmoxError("task timed out")


# ---------------------------------------------------------------- queries

def overview(c: Client | None = None) -> dict:
    c = c or Client()
    ver = c.call("GET", "/version") or {}
    nodes = sorted(n["node"] for n in (c.call("GET", "/nodes") or []) if n.get("status", "online") == "online")
    return {"version": ver.get("version", ""), "nodes": nodes}


def node_options(node: str, c: Client | None = None) -> dict:
    c = c or Client()
    storages = [s["storage"] for s in (c.call("GET", f"/nodes/{node}/storage", {"content": "images", "enabled": 1}) or [])]
    bridges = sorted(b["iface"] for b in (c.call("GET", f"/nodes/{node}/network", {"type": "any_bridge"}) or []))
    return {"storages": sorted(storages), "bridges": bridges}


def list_vms(c: Client | None = None) -> list[dict]:
    c = c or Client()
    out = []
    for r in c.call("GET", "/cluster/resources", {"type": "vm"}) or []:
        tags = (r.get("tags") or "").replace(",", ";").split(";")
        if TAG in tags and r.get("type") == "qemu":
            out.append({"vmid": r["vmid"], "name": r.get("name", ""), "node": r.get("node", ""),
                        "status": r.get("status", ""), "uptime": r.get("uptime", 0)})
    return sorted(out, key=lambda v: v["vmid"])


def mac_from_config(cfg: dict) -> str:
    m = re.search(r"=([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", cfg.get("net0", ""))
    return m.group(1).lower() if m else ""


# ---------------------------------------------------------------- create

def vm_params(spec: dict, iso_row) -> dict:
    """Proxmox create parameters for spec. Windows gets SATA + e1000 because WinPE has no virtio drivers."""
    windows = isos.method_of(iso_row) == "windows"
    uefi = spec["firmware"] == "uefi"
    st = spec["storage"]
    net = f"{'e1000' if windows else 'virtio'},bridge={spec['bridge']}"
    if spec.get("vlan"):
        net += f",tag={int(spec['vlan'])}"
    disk_bus = "sata0" if windows else "scsi0"
    p = {
        "vmid": spec["vmid"], "name": spec["name"], "tags": TAG,
        "cores": spec["cores"], "sockets": 1, "cpu": "host", "memory": spec["memory_mb"],
        "ostype": "win11" if windows else "l26", "machine": "q35", "scsihw": "virtio-scsi-single",
        disk_bus: f"{st}:{spec['disk_gb']}" + ("" if windows else ",discard=on,iothread=1"),
        "net0": net, "boot": f"order=net0;{disk_bus}", "agent": 1,
        "bios": "ovmf" if uefi else "seabios",
    }
    if uefi:
        # Small (2 MB) OVMF vars without pre-enrolled keys: Proxmox' default 4 MB secure boot vars
        # do not PXE boot iPXE.
        p["efidisk0"] = f"{st}:1,efitype=2m,pre-enrolled-keys=0"
        if windows:
            p["tpmstate0"] = f"{st}:1,version=v2.0"
    return p


def validate(spec: dict) -> dict:
    out = dict(spec)
    if not NAME_RE.match(out.get("name", "")):
        raise ValueError("name")
    for key, lo, hi in (("cores", 1, 128), ("memory_mb", 512, 1024 * 1024), ("disk_gb", 4, 65536)):
        out[key] = int(out.get(key) or 0)
        if not lo <= out[key] <= hi:
            raise ValueError(key)
    if out.get("vlan") not in (None, "", 0):
        v = int(out["vlan"])
        if not 1 <= v <= 4094:
            raise ValueError("vlan")
        out["vlan"] = v
    else:
        out["vlan"] = None
    for key in ("node", "storage", "bridge"):
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", str(out.get(key, ""))):
            raise ValueError(key)
    out["firmware"] = "bios" if out.get("firmware") == "bios" else "uefi"
    return out


def create_vm(spec: dict, iso_row, c: Client | None = None) -> dict:
    """Create the VM and return {vmid, node, mac}. Does not start it."""
    c = c or Client()
    spec = dict(spec)
    spec["vmid"] = int(c.call("GET", "/cluster/nextid"))
    upid = c.call("POST", f"/nodes/{spec['node']}/qemu", vm_params(spec, iso_row))
    if isinstance(upid, str) and upid.startswith("UPID:"):
        c.wait_task(spec["node"], upid)
    cfg = c.call("GET", f"/nodes/{spec['node']}/qemu/{spec['vmid']}/config") or {}
    mac = mac_from_config(cfg)
    if not mac:
        raise ProxmoxError("VM created but no MAC address found in net0")
    return {"vmid": spec["vmid"], "node": spec["node"], "mac": mac}


def start_vm(node: str, vmid: int, c: Client | None = None) -> None:
    c = c or Client()
    upid = c.call("POST", f"/nodes/{node}/qemu/{int(vmid)}/status/start", {})
    if isinstance(upid, str) and upid.startswith("UPID:"):
        c.wait_task(node, upid, timeout=60)


def console_url(node: str, vmid: int) -> str:
    s = settings()
    return f"{s.get('host', '').rstrip('/')}/#v1:0:=qemu%2F{int(vmid)}:4:::::::"
