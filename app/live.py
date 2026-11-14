"""Live view of booting clients and server bandwidth.

What a client is doing is pieced together from what the server can see:

* the iPXE requests it makes to this app (entry script, menu, chosen ISO);
* bytes nginx has sent to that client (completed requests from the access
  log, plus bytes acknowledged on open connections from `ss`);
* for Windows setup, the files it has open on the Samba share;
* answer-file downloads and the "done" callback at the end of an unattended install.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import config, db, isos, system

log = logging.getLogger("pxe.live")

PHASES = ["pxe", "ipxe", "menu", "loading", "installer", "done"]
_lock = threading.RLock()
CLIENTS: dict[str, dict] = {}        # keyed by client IP
BANDWIDTH: deque = deque(maxlen=300)  # (ts, rx_bps, tx_bps)
_log_offset = 0
_log_bytes: dict[str, list[tuple[float, int]]] = {}  # ip -> [(ts, bytes)] for boot file downloads
_smb: dict[str, str] = {}             # ip -> file currently open on the share
_started = False
DAILY: dict[str, int] = {}             # date -> bytes of boot files sent

BOOT_PATH = re.compile(r"^/(mnt|isos|boot|derived)/")


def touch(ip: str, phase: str, **kw) -> dict:
    now = time.time()
    with _lock:
        c = CLIENTS.get(ip)
        if c is None or (phase in ("ipxe",) and c.get("phase") in ("done",)):
            c = {"ip": ip, "mac": "", "platform": "", "arch": "", "manufacturer": "", "product": "", "serial": "",
                 "hostname": "", "phase": phase, "iso_id": None, "iso_name": "", "answer_id": None, "answer_name": "",
                 "started": now, "phase_since": now, "expected": 0, "sent": 0, "speed": 0.0, "file": "",
                 "load_started": None}
            CLIENTS[ip] = c
        for k, v in kw.items():
            if v not in (None, ""):
                c[k] = v
        if PHASES.index(phase) >= PHASES.index(c["phase"]) or phase == "menu":
            if c["phase"] != phase:
                c["phase_since"] = now
            c["phase"] = phase
        if phase == "loading":
            c["load_started"] = now
            c["sent"] = 0
        c["updated"] = now
        return c


def expected_bytes(row, answer=None) -> int:
    """Rough total of what the client will download for this ISO."""
    method = isos.method_of(row)
    info = isos.iso_info(row)
    root = isos.mount_dir(row["id"])

    def size(rel: str | None) -> int:
        if not rel:
            return 0
        try:
            return (root / rel).stat().st_size
        except OSError:
            return 0

    iso_size = row["size"]
    if method == "windows":
        return size(info.get("bcd")) + size(info.get("sdi")) + size(info.get("wim"))
    if method in ("ubuntu", "proxmox"):
        return size(info.get("kernel")) + size(info.get("initrd")) + iso_size
    if method == "debian_installer":
        d = config.BOOT_DIR / "debian" / info.get("codename", "")
        return sum((d / f).stat().st_size for f in ("linux", "initrd.gz") if (d / f).exists())
    if method in ("debian_live", "fedora_live"):
        return size(info.get("kernel")) + size(info.get("initrd")) + size(info.get("squashfs"))
    if method == "rhel":
        return size(info.get("kernel")) + size(info.get("initrd")) + size(isos.ci_find(root, "images/install.img"))
    if method == "arch":
        return size(info.get("kernel")) + size(info.get("initrd")) + size(
            isos.ci_find(root, "arch/x86_64/airootfs.sfs"))
    if method == "memdisk":
        return iso_size
    return 0  # sanboot reads only what it needs


# ---------------------------------------------------------------- sampling

def _read_nginx_log() -> None:
    global _log_offset
    p = config.NGINX_ACCESS_LOG
    try:
        size = p.stat().st_size
    except OSError:
        return
    if size < _log_offset:
        _log_offset = 0
    if size == _log_offset:
        return
    try:
        with open(p, "rb") as f:
            f.seek(_log_offset)
            data = f.read(8 * 1024 * 1024)
            _log_offset += len(data.rsplit(b"\n", 1)[0]) + 1 if b"\n" in data else 0
    except OSError:
        return
    for line in data.decode(errors="ignore").splitlines():
        # format: $msec $remote_addr $status $body_bytes_sent "$uri"
        parts = line.split(" ", 4)
        if len(parts) < 5:
            continue
        try:
            ts, ip, _status, sent, uri = float(parts[0]), parts[1], parts[2], int(parts[3]), parts[4].strip('"')
        except ValueError:
            continue
        if BOOT_PATH.match(uri):
            _log_bytes.setdefault(ip, []).append((ts, sent))
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            DAILY[day] = DAILY.get(day, 0) + sent
    cutoff = time.time() - 7200
    for ip in list(_log_bytes):
        _log_bytes[ip] = [x for x in _log_bytes[ip] if x[0] > cutoff]
        if not _log_bytes[ip]:
            del _log_bytes[ip]


SS_PEER = re.compile(r"\s(\S+):80\s+(?:\[::ffff:)?([0-9.]+)\]?:\d+")
SS_ACK = re.compile(r"bytes_acked:(\d+)")


def _inflight() -> dict[str, int]:
    """Bytes acknowledged on open HTTP connections, per client IP."""
    res: dict[str, int] = {}
    try:
        out = subprocess.run(["ss", "-Htin", "state", "established", "( sport = :80 )"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return res
    peer = None
    for line in out.splitlines():
        m = SS_PEER.search(" " + line)
        if m:
            peer = m.group(2)
            continue
        a = SS_ACK.search(line)
        if a and peer:
            res[peer] = res.get(peer, 0) + int(a.group(1))
            peer = None
    return res


def _read_smb() -> None:
    global _smb
    try:
        out = system.helper("smbstatus", timeout=10)
        data = json.loads(out or "{}")
    except (system.HelperError, ValueError):
        return
    pid_ip = {}
    for s in (data.get("sessions") or {}).values():
        pid = str((s.get("server_id") or {}).get("pid", ""))
        ip = s.get("remote_machine") or s.get("hostname") or ""
        pid_ip[pid] = ip.replace("ipv4:", "").split(":")[0]
    res = {}
    for path, f in (data.get("open_files") or {}).items():
        for o in (f.get("opens") or {}).values():
            pid = str((o.get("server_id") or {}).get("pid", ""))
            ip = pid_ip.get(pid)
            if ip:
                name = f.get("filename") or path
                # prefer the big install image over small files
                if ip not in res or "install." in name.lower():
                    res[ip] = name
    _smb = res


def _sample(prev: dict, prev_t: float) -> tuple[dict, float]:
    now = time.time()
    counters = system.net_counters()
    wanted = db.get_setting("interfaces") or list(counters)
    dt = max(now - prev_t, 0.001)
    rx = tx = 0.0
    for name in wanted:
        if name in counters and name in prev:
            rx += (counters[name][0] - prev[name][0]) * 8 / dt
            tx += (counters[name][1] - prev[name][1]) * 8 / dt
    if prev:
        BANDWIDTH.append((now, max(rx, 0), max(tx, 0)))

    _read_nginx_log()
    inflight = _inflight()
    with _lock:
        for ip, c in list(CLIENTS.items()):
            if c["phase"] in ("loading", "installer") and c.get("load_started"):
                done = sum(b for ts, b in _log_bytes.get(ip, []) if ts >= c["load_started"] - 1)
                total = done + inflight.get(ip, 0)
                delta = total - c["sent"]
                c["speed"] = c["speed"] * 0.6 + max(delta, 0) / dt * 0.4
                if delta > 0:
                    c["updated"] = now
                c["sent"] = max(total, c["sent"])
                if c["phase"] == "loading" and c["expected"] and c["sent"] >= c["expected"] * 0.98 \
                        and ip not in inflight:
                    c["phase"] = "installer"
                    c["phase_since"] = now
            if ip in _smb:
                c["file"] = _smb[ip]
                c["updated"] = now
                if c["phase"] == "loading":
                    c["phase"] = "installer"
                    c["phase_since"] = now
            idle = now - c.get("updated", now)
            limit = {"done": 1800, "installer": 7200}.get(c["phase"], 1200)
            if idle > limit:
                del CLIENTS[ip]
    return counters, now


def _loop() -> None:
    prev, prev_t = {}, time.time()
    n = 0
    while True:
        try:
            prev, prev_t = _sample(prev, prev_t)
            if n % 60 == 0:
                _persist_daily()
            if n % 5 == 0 and any(c["phase"] in ("loading", "installer") for c in CLIENTS.values()):
                _read_smb()
        except Exception:  # noqa: BLE001
            log.exception("live sampler")
        n += 1
        time.sleep(1)


def _persist_daily() -> None:
    keep = sorted(DAILY)[-31:]
    db.set_setting("traffic_daily", {k: DAILY[k] for k in keep})


def start() -> None:
    global _started, _log_offset
    if _started:
        return
    _started = True
    DAILY.update(db.get_setting("traffic_daily") or {})
    try:
        _log_offset = config.NGINX_ACCESS_LOG.stat().st_size
    except OSError:
        _log_offset = 0
    threading.Thread(target=_loop, daemon=True, name="live-sampler").start()


# ---------------------------------------------------------------- views

def snapshot() -> dict:
    now = time.time()
    with _lock:
        clients = []
        for c in sorted(CLIENTS.values(), key=lambda x: x["started"]):
            d = dict(c)
            d["progress"] = round(min(100.0, c["sent"] / c["expected"] * 100), 1) if c["expected"] else None
            d["age"] = int(now - c["started"])
            clients.append(d)
    uploads = db.q("SELECT id, filename, size, received, updated FROM uploads WHERE updated > ?", now - 60)
    bw = list(BANDWIDTH)
    counts = {p: sum(1 for c in clients if c["phase"] == p) for p in PHASES}
    return {
        "clients": clients,
        "counts": counts,
        "uploads": [dict(u) for u in uploads],
        "tasks": sum(1 for c in clients if c["phase"] != "done") + len(uploads),
        "bandwidth": [{"t": round(t, 1), "rx": int(r), "tx": int(x)} for t, r, x in bw],
        "link_mbit": max((system.link_speed(n) for n in (db.get_setting("interfaces") or [])), default=1000),
    }


def sent_today() -> int:
    return DAILY.get(time.strftime("%Y-%m-%d"), 0)
