"""Server logs for the Logs page: HTTP (nginx access log) and TFTP/DHCP (dnsmasq, from the journal).

The app user is in the `adm` group, which on Debian may read both /var/log/nginx and the journal.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import config

IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
MAC_RE = re.compile(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", re.I)
HTTP_RE = re.compile(r'^(\d+(?:\.\d+)?) (\S+) (\d{3}) (\d+) "(.*)"$')

# Files clients fetch all the time; hidden unless the filter asks for them.
NOISE = ("/api/", "/static/", "/healthz")


def _tail(path, max_bytes: int = 1024 * 1024) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > max_bytes else lines


def _match(entry: dict, q: str) -> bool:
    if not q:
        return True
    q = q.lower()
    return any(q in str(v).lower() for v in entry.values())


def http(limit: int = 300, q: str = "", all_requests: bool = False) -> list[dict]:
    out = []
    for line in reversed(_tail(config.NGINX_ACCESS_LOG)):
        m = HTTP_RE.match(line.strip())
        if not m:
            continue
        e = {"ts": float(m.group(1)), "ip": m.group(2), "status": int(m.group(3)), "bytes": int(m.group(4)),
             "path": m.group(5)}
        if not all_requests and not q and e["path"].startswith(NOISE):
            continue
        if _match(e, q):
            out.append(e)
            if len(out) >= limit:
                break
    return out


def parse_journal(text: str) -> list[dict]:
    """journalctl -o json lines -> [{ts, kind, message, ip, mac}], oldest first."""
    out = []
    for line in text.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        msg = j.get("MESSAGE")
        if isinstance(msg, list):  # journald stores non-UTF-8 messages as byte arrays
            msg = bytes(msg).decode("utf-8", "replace")
        if not isinstance(msg, str):
            continue
        ident = j.get("SYSLOG_IDENTIFIER", "")
        kind = "tftp" if ident.endswith("-tftp") else "dhcp" if ident.endswith("-dhcp") else "dnsmasq"
        ip = IP_RE.search(msg)
        mac = MAC_RE.search(msg)
        try:
            ts = int(j.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        except (TypeError, ValueError):
            ts = 0
        e = {"ts": ts, "kind": kind, "message": msg, "ip": ip.group(1) if ip else "", "mac": mac.group(1).lower() if mac else ""}
        if kind == "tftp":
            m = re.match(r"^(sent|file|error)\b", msg)
            e["ok"] = bool(m and m.group(1) == "sent")
            f = re.search(r"(?:sent|file) (\S+)", msg)
            e["file"] = os.path.basename(f.group(1)) if f else ""
        out.append(e)
    return out


def journal_text(lines: int = 2000) -> tuple[str, str]:
    """(json text, error). In development a file stands in for the journal."""
    if config.DEV:
        p = config.LOG_DIR / "dnsmasq.journal.json"
        try:
            return p.read_text(errors="replace"), ""
        except OSError:
            return "", ""
    try:
        res = subprocess.run(["journalctl", "-u", "dnsmasq", "-o", "json", "-n", str(lines), "--no-pager", "-q"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    if res.returncode != 0 and not res.stdout:
        return "", (res.stderr or "journalctl failed").strip()[:300]
    return res.stdout, ""


def dnsmasq(kind: str, limit: int = 300, q: str = "") -> tuple[list[dict], str]:
    text, err = journal_text()
    entries = [e for e in parse_journal(text) if (kind == "dhcp" and e["kind"] in ("dhcp", "dnsmasq")) or e["kind"] == kind]
    out = [e for e in reversed(entries) if _match(e, q)][:limit]
    return out, err
