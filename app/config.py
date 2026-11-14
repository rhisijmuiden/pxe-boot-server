"""Paths and runtime configuration.

Everything can be overridden with environment variables so the app can run
in development (PXE_DEV=1) without root, mounts or system services.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent


def _p(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


DEV = os.environ.get("PXE_DEV", "0") == "1"

DATA_DIR = _p("PXE_DATA_DIR", "/var/lib/pxe-boot-server")
SRV_DIR = _p("PXE_SRV_DIR", "/srv/pxe")
ISO_DIR = _p("PXE_ISO_DIR", str(SRV_DIR / "isos"))
MNT_DIR = _p("PXE_MNT_DIR", str(SRV_DIR / "mnt"))
TFTP_DIR = _p("PXE_TFTP_DIR", str(SRV_DIR / "tftp"))
BOOT_DIR = _p("PXE_BOOT_DIR", str(SRV_DIR / "boot"))  # wimboot, memdisk, cached netboot kernels
CACHE_DIR = _p("PXE_CACHE_DIR", str(SRV_DIR / "cache"))
UPLOAD_DIR = _p("PXE_UPLOAD_DIR", str(SRV_DIR / "uploads"))
DERIVED_DIR = _p("PXE_DERIVED_DIR", str(SRV_DIR / "derived"))  # e.g. Proxmox ISOs with auto-installer config
LOG_DIR = _p("PXE_LOG_DIR", "/var/log/pxe-boot-server")
NGINX_ACCESS_LOG = _p("PXE_NGINX_LOG", "/var/log/nginx/pxe-boot-access.log")
ETC_DIR = _p("PXE_ETC_DIR", "/etc/pxe-boot-server")

DB_PATH = DATA_DIR / "app.db"
SECRET_FILE = DATA_DIR / "secret.key"
DNSMASQ_STAGING = DATA_DIR / "dnsmasq.conf"

HELPER = os.environ.get("PXE_HELPER", str(REPO_DIR / "bin" / "pxe-helper"))
UPSTREAM_REPO = os.environ.get("PXE_REPO", "https://github.com/rhisijmuiden/pxe-boot-server")

UPLOAD_CHUNK_MAX = 64 * 1024 * 1024  # must match nginx client_max_body_size

for d in (DATA_DIR, ISO_DIR, UPLOAD_DIR):
    try:
        d.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass


def version() -> str:
    try:
        return (REPO_DIR / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"
