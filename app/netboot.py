"""Downloads network installer kernels that are not on the ISO itself.

The Debian CD installer looks for its packages on a CD drive, which a network
booted machine does not have. The matching Debian *netboot* kernel and initrd
install from a Debian mirror instead, so they are fetched once per release.
"""
from __future__ import annotations

import logging
import re
import threading
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger("pxe.netboot")
_lock = threading.Lock()
DEBIAN_URL = ("https://deb.debian.org/debian/dists/{codename}/main/installer-amd64/current/images/"
              "netboot/debian-installer/amd64/{file}")


def debian_dir(codename: str) -> Path:
    return config.BOOT_DIR / "debian" / codename


def debian_ready(codename: str) -> bool:
    d = debian_dir(codename)
    return bool(codename) and (d / "linux").is_file() and (d / "initrd.gz").is_file()


def ensure_debian(codename: str) -> bool:
    if not re.fullmatch(r"[a-z]+", codename or ""):
        return False
    with _lock:
        if debian_ready(codename):
            return True
        d = debian_dir(codename)
        try:
            d.mkdir(parents=True, exist_ok=True)
            for name in ("linux", "initrd.gz"):
                url = DEBIAN_URL.format(codename=codename, file=name)
                tmp = d / (name + ".part")
                with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
                    while chunk := resp.read(1 << 20):
                        out.write(chunk)
                tmp.rename(d / name)
            log.info("Debian %s netboot files downloaded", codename)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Debian %s netboot download failed: %s", codename, exc)
            return False


def ensure_debian_async(codename: str) -> None:
    if codename and not debian_ready(codename):
        threading.Thread(target=ensure_debian, args=(codename,), daemon=True).start()
