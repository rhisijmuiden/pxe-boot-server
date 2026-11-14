"""Talks to the operating system: the privileged helper, network interfaces and services."""
from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import subprocess
from pathlib import Path

from . import config, db

log = logging.getLogger("pxe.system")


class HelperError(RuntimeError):
    pass


def helper(*args: str, stdin: str | None = None, timeout: int = 120) -> str:
    """Run the root helper through sudo. In dev mode most actions are simulated."""
    if config.DEV:
        log.info("DEV helper call skipped: %s", " ".join(args))
        return ""
    cmd = ["sudo", "-n", config.HELPER, *args]
    try:
        res = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError(str(exc)) from exc
    if res.returncode != 0:
        raise HelperError((res.stderr or res.stdout).strip() or f"helper {args[0]} failed")
    return res.stdout


# ---------------------------------------------------------------- network

def interfaces() -> list[dict]:
    """All non-loopback interfaces with their IPv4 addresses (VLAN sub-interfaces included)."""
    try:
        out = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=5).stdout
        data = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        data = []
    result = []
    for itf in data:
        name = itf.get("ifname", "")
        if name == "lo" or itf.get("link_type") == "loopback":
            continue
        addrs = []
        for a in itf.get("addr_info", []):
            if a.get("family") == "inet":
                addrs.append(f"{a['local']}/{a['prefixlen']}")
        result.append({
            "name": name,
            "addrs": addrs,
            "up": "UP" in itf.get("flags", []),
            "mac": itf.get("address", ""),
            "vlan": (itf.get("linkinfo", {}) or {}).get("info_kind") == "vlan",
        })
    return result


def server_ip() -> str:
    override = (db.get_setting("server_ip") or "").strip()
    if override:
        return override
    wanted = db.get_setting("interfaces") or []
    itfs = interfaces()
    for itf in itfs:
        if (not wanted or itf["name"] in wanted) and itf["addrs"]:
            return itf["addrs"][0].split("/")[0]
    for itf in itfs:
        if itf["addrs"]:
            return itf["addrs"][0].split("/")[0]
    return "127.0.0.1"


def interface_networks() -> list[tuple[str, ipaddress.IPv4Interface]]:
    wanted = db.get_setting("interfaces") or []
    res = []
    for itf in interfaces():
        if wanted and itf["name"] not in wanted:
            continue
        for a in itf["addrs"]:
            res.append((itf["name"], ipaddress.IPv4Interface(a)))
    return res


def net_counters() -> dict[str, tuple[int, int]]:
    """rx/tx byte counters per interface from /sys."""
    res = {}
    base = Path("/sys/class/net")
    if not base.exists():
        return res
    for d in base.iterdir():
        if d.name == "lo":
            continue
        try:
            rx = int((d / "statistics/rx_bytes").read_text())
            tx = int((d / "statistics/tx_bytes").read_text())
            res[d.name] = (rx, tx)
        except (OSError, ValueError):
            continue
    return res


def link_speed(name: str) -> int:
    try:
        v = int(Path(f"/sys/class/net/{name}/speed").read_text())
        return v if v > 0 else 1000
    except (OSError, ValueError):
        return 1000


# ---------------------------------------------------------------- services

SERVICES = [
    ("dnsmasq", "svc_dnsmasq", "UDP 67/4011/69"),
    ("nginx", "svc_nginx", "TCP 80"),
    ("smbd", "svc_smb", "TCP 445"),
    ("pxe-boot-server", "svc_app", "127.0.0.1:8080"),
]


def service_status() -> list[dict]:
    res = []
    for unit, key, ports in SERVICES:
        state = "unknown"
        if shutil.which("systemctl"):
            try:
                state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True,
                                       timeout=5).stdout.strip() or "unknown"
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif config.DEV:
            state = "active"
        res.append({"unit": unit, "key": key, "ports": ports, "state": state})
    return res


def disk_usage() -> tuple[int, int]:
    try:
        u = shutil.disk_usage(config.ISO_DIR)
        return u.used, u.total
    except OSError:
        return 0, 0


def os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return "Linux"


# ---------------------------------------------------------------- git / version

def git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(config.REPO_DIR), *args], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def version_info() -> dict:
    return {
        "version": config.version(),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD") or db.get_setting("update_channel"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "commit_date": git("log", "-1", "--format=%cI"),
        "os": os_name(),
        "repo": config.UPSTREAM_REPO,
    }
