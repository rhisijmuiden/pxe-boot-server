"""SQLite storage. Small, synchronous and good enough for an internal appliance."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from . import config

_local = threading.local()

SCHEMA = [
    # version 1
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'admin',
        lang TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        last_login REAL
    );
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE isos (
        id INTEGER PRIMARY KEY,
        relpath TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        mtime REAL NOT NULL DEFAULT 0,
        method TEXT NOT NULL DEFAULT 'sanboot',
        method_override TEXT NOT NULL DEFAULT '',
        info TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'new',
        error TEXT NOT NULL DEFAULT '',
        added REAL NOT NULL
    );
    CREATE TABLE answers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        iso_id INTEGER NOT NULL REFERENCES isos(id) ON DELETE CASCADE,
        format TEXT NOT NULL,
        data TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'ready',
        created REAL NOT NULL,
        updated REAL NOT NULL
    );
    CREATE TABLE machines (
        id INTEGER PRIMARY KEY,
        mac TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        iso_id INTEGER REFERENCES isos(id) ON DELETE CASCADE,
        answer_id INTEGER REFERENCES answers(id) ON DELETE SET NULL,
        mode TEXT NOT NULL DEFAULT 'once',
        used INTEGER NOT NULL DEFAULT 0,
        last_seen REAL,
        created REAL NOT NULL
    );
    CREATE TABLE boot_events (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        mac TEXT NOT NULL DEFAULT '',
        ip TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT '',
        hostname TEXT NOT NULL DEFAULT '',
        iso_id INTEGER,
        iso_name TEXT NOT NULL DEFAULT '',
        answer_id INTEGER,
        event TEXT NOT NULL
    );
    CREATE INDEX boot_events_ts ON boot_events(ts);
    CREATE TABLE uploads (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        folder TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL,
        received INTEGER NOT NULL DEFAULT 0,
        user_id INTEGER,
        created REAL NOT NULL,
        updated REAL NOT NULL
    );
    CREATE TABLE password_resets (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires REAL NOT NULL
    );
    """,
    # version 2: UEFI clients use snponly.efi. ipxe.efi takes over the hardware with its own
    # drivers, and on many PCs the USB keyboard then stops working in the boot menu.
    """
    UPDATE settings SET value='"snponly.efi"' WHERE key='uefi_loader' AND value='"ipxe.efi"';
    """,
    # version 3: ISO catalog downloads
    """
    CREATE TABLE downloads (
        id INTEGER PRIMARY KEY,
        catalog_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        filename TEXT NOT NULL,
        folder TEXT NOT NULL DEFAULT '',
        archive TEXT NOT NULL DEFAULT '',
        expected TEXT NOT NULL DEFAULT '',
        actual TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0,
        received INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued',
        error TEXT NOT NULL DEFAULT '',
        iso_id INTEGER,
        dismissed INTEGER NOT NULL DEFAULT 0,
        user_id INTEGER,
        created REAL NOT NULL,
        updated REAL NOT NULL
    );
    """,
    # version 4: machine groups. A machine without its own ISO follows its group; a group can also
    # catch unknown machines by MAC prefix (for example 52:54:00 for Proxmox/QEMU VMs).
    """
    CREATE TABLE machine_groups (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        prefixes TEXT NOT NULL DEFAULT '',
        iso_id INTEGER REFERENCES isos(id) ON DELETE SET NULL,
        answer_id INTEGER REFERENCES answers(id) ON DELETE SET NULL,
        mode TEXT NOT NULL DEFAULT 'default',
        created REAL NOT NULL
    );
    ALTER TABLE machines ADD COLUMN group_id INTEGER REFERENCES machine_groups(id) ON DELETE SET NULL;
    ALTER TABLE machines ADD COLUMN group_used INTEGER NOT NULL DEFAULT 0;
    """,
]


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def migrate() -> None:
    conn = connect()
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(SCHEMA[current:], start=current + 1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version={i};COMMIT;")


def q(sql: str, *args: Any) -> list[sqlite3.Row]:
    return connect().execute(sql, args).fetchall()


def q1(sql: str, *args: Any) -> sqlite3.Row | None:
    return connect().execute(sql, args).fetchone()


def run(sql: str, *args: Any) -> int:
    cur = connect().execute(sql, args)
    return cur.lastrowid or cur.rowcount


# ---------------------------------------------------------------- settings

DEFAULTS: dict[str, Any] = {
    "server_name": "pxe-boot-server",
    "default_lang": "en",
    "interfaces": [],          # list of interface names dnsmasq listens on
    "server_ip": "",           # override; empty = address of first interface
    "dhcp_mode": "proxy",      # proxy | full
    "dhcp_range_start": "",
    "dhcp_range_end": "",
    "dhcp_gateway": "",
    "dhcp_dns": "",
    "dhcp_lease": "12h",
    "uefi_loader": "snponly.efi",  # snponly.efi (firmware drivers, keyboard keeps working) | ipxe.efi
    "log_dhcp": False,         # dnsmasq log-dhcp: every DHCP/PXE exchange in the journal (Logs page)
    "menu_timeout": 30,
    "menu_default": "local",   # "local" or iso id
    "menu_title": "",
    "smtp": {
        "host": "", "port": 587, "security": "starttls", "verify": True,
        "username": "", "password": "", "from_addr": "", "from_name": "PXE Boot Server",
        "reply_to": "", "notify_to": "", "notify_upload": True, "notify_install": True,
    },
    "proxmox": {"host": "", "token_id": "", "secret": "", "verify": False,
                "node": "", "storage": "", "bridge": "vmbr0"},
    "update_channel": "main",
    "smb_password": "",
    "traffic_daily": {},
}


def get_setting(key: str) -> Any:
    row = q1("SELECT value FROM settings WHERE key=?", key)
    if row is None:
        val = DEFAULTS.get(key)
        return json.loads(json.dumps(val)) if isinstance(val, (dict, list)) else val
    value = json.loads(row["value"])
    if isinstance(DEFAULTS.get(key), dict):
        merged = dict(DEFAULTS[key])
        merged.update(value)
        return merged
    return value


def set_setting(key: str, value: Any) -> None:
    run(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        key, json.dumps(value),
    )


def log_event(event: str, *, mac: str = "", ip: str = "", platform: str = "", hostname: str = "",
              iso_id: int | None = None, iso_name: str = "", answer_id: int | None = None) -> None:
    run(
        "INSERT INTO boot_events(ts,mac,ip,platform,hostname,iso_id,iso_name,answer_id,event) VALUES(?,?,?,?,?,?,?,?,?)",
        time.time(), mac, ip, platform, hostname, iso_id, iso_name, answer_id, event,
    )
