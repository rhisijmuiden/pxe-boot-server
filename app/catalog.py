"""ISO catalog: one-click downloads of well known images, verified against the vendor's checksums.

Most entries are resolved live: the vendor's checksum file is fetched, the newest file that
matches the entry's pattern is picked and its published hash is used to verify the download.
Entries without a checksum file are pinned to one version and marked as such in the UI.

Downloads run one at a time in a background thread. They resume after a dropped connection
or a restart (HTTP Range), are hashed while they stream in and land in the ISO library when
the hash matches.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import config, db, isos

log = logging.getLogger("pxe.catalog")

HASH_LEN = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}

# category: linux | infra | tools
CATALOG: list[dict] = [
    {"id": "ubuntu-2604-server", "name": "Ubuntu Server 26.04 LTS", "tag": "UBU", "category": "linux", "folder": "Linux",
     "sums": "https://releases.ubuntu.com/26.04/SHA256SUMS",
     "pattern": r"ubuntu-(?P<v>26\.04(?:\.\d+)*)-live-server-amd64\.iso"},
    {"id": "ubuntu-2404-server", "name": "Ubuntu Server 24.04 LTS", "tag": "UBU", "category": "linux", "folder": "Linux",
     "sums": "https://releases.ubuntu.com/24.04/SHA256SUMS",
     "pattern": r"ubuntu-(?P<v>24\.04(?:\.\d+)*)-live-server-amd64\.iso"},
    {"id": "ubuntu-2604-desktop", "name": "Ubuntu Desktop 26.04 LTS", "tag": "UBU", "category": "linux", "folder": "Linux",
     "sums": "https://releases.ubuntu.com/26.04/SHA256SUMS",
     "pattern": r"ubuntu-(?P<v>26\.04(?:\.\d+)*)-desktop-amd64\.iso"},
    {"id": "debian-netinst", "name": "Debian stable (netinst)", "tag": "DEB", "category": "linux", "folder": "Linux",
     "sums": "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/SHA256SUMS",
     "pattern": r"debian-(?P<v>\d+(?:\.\d+)+)-amd64-netinst\.iso"},
    {"id": "rocky-10-minimal", "name": "Rocky Linux 10 (minimal)", "tag": "RKY", "category": "linux", "folder": "Linux",
     "sums": "https://download.rockylinux.org/pub/rocky/10/isos/x86_64/CHECKSUM",
     "pattern": r"Rocky-(?P<v>10\.\d+)-x86_64-minimal\.iso"},
    {"id": "proxmox-ve", "name": "Proxmox VE", "tag": "PVE", "category": "infra", "folder": "Proxmox",
     "sums": "https://enterprise.proxmox.com/iso/SHA256SUMS",
     "pattern": r"proxmox-ve_(?P<v>\d+\.\d+-\d+)\.iso"},
    {"id": "proxmox-backup", "name": "Proxmox Backup Server", "tag": "PBS", "category": "infra", "folder": "Proxmox",
     "sums": "https://enterprise.proxmox.com/iso/SHA256SUMS",
     "pattern": r"proxmox-backup-server_(?P<v>\d+\.\d+-\d+)\.iso"},
    {"id": "truenas", "name": "TrueNAS Community Edition", "tag": "TN", "category": "infra", "folder": "Storage",
     "version": "25.04.2.4",
     "url": "https://download.sys.truenas.net/TrueNAS-SCALE-Fangtooth/25.04.2.4/TrueNAS-SCALE-25.04.2.4.iso",
     "hash_url": "https://download.sys.truenas.net/TrueNAS-SCALE-Fangtooth/25.04.2.4/TrueNAS-SCALE-25.04.2.4.iso.sha256"},
    {"id": "clonezilla", "name": "Clonezilla Live (stable)", "tag": "CZ", "category": "tools", "folder": "Tools",
     "sums": "https://clonezilla.org/downloads/stable/data/CHECKSUMS.TXT",
     "pattern": r"clonezilla-live-(?P<v>\d+\.\d+\.\d+-\d+)-amd64\.iso",
     "url_template": "https://downloads.sourceforge.net/project/clonezilla/clonezilla_live_stable/{v}/{file}"},
    {"id": "memtest86plus", "name": "Memtest86+", "tag": "MEM", "category": "tools", "folder": "Tools",
     "version": "7.20", "url": "https://www.memtest.org/download/v7.20/mt86plus_7.20_64.iso.zip",
     "archive": "zip", "filename": "mt86plus_7.20_64.iso"},
]
BY_ID = {e["id"]: e for e in CATALOG}

USER_AGENT = "pxe-boot-server/{v} (+https://github.com/rhisijmuiden/pxe-boot-server)"
_cache: dict[str, tuple[float, object]] = {}
CACHE_TTL = 3600


class ResolveError(Exception):
    pass


# ---------------------------------------------------------------- resolving

def _open(url: str, headers: dict | None = None, timeout: int = 30):
    if not url.startswith(("https://", "http://")):
        raise ValueError("url")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT.format(v=config.version())} | (headers or {}))
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - scheme checked above


def fetch_text(url: str) -> str:
    hit = _cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]  # type: ignore[return-value]
    with _open(url) as resp:
        text = resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
    _cache[url] = (time.time() + CACHE_TTL, text)
    return text


def parse_sums(text: str) -> dict[str, str]:
    """Parse GNU (`hash  *file`) and BSD (`SHA256 (file) = hash`) checksum lists.
    Returns {filename: hash}. When a file is listed with several algorithms the strongest wins."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^([0-9a-fA-F]{32,128})\s+\*?(\S+)$", line)
        if m:
            h, name = m.group(1).lower(), m.group(2)
        else:
            m = re.match(r"^(?:SHA\d+|MD5)\s*\((.+)\)\s*=\s*([0-9a-fA-F]{32,128})$", line)
            if not m:
                continue
            name, h = m.group(1), m.group(2).lower()
        if len(h) not in HASH_LEN:
            continue
        name = name.rsplit("/", 1)[-1]
        if len(h) >= len(out.get(name, "")):
            out[name] = h
    return out


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def pick_latest(sums: dict[str, str], pattern: str) -> tuple[str, str, str] | None:
    """(filename, version, hash) of the newest file matching pattern."""
    rx = re.compile(pattern)
    best = None
    for name, h in sums.items():
        m = rx.fullmatch(name)
        if not m:
            continue
        v = m.groupdict().get("v") or ""
        if best is None or _vkey(v) > _vkey(best[1]):
            best = (name, v, h)
    return best


def resolve(entry_id: str) -> dict:
    """Where to download an entry from and which hash to expect. Raises ResolveError."""
    e = BY_ID.get(entry_id)
    if e is None:
        raise ResolveError("unknown")
    try:
        if "sums" in e:
            hit = pick_latest(parse_sums(fetch_text(e["sums"])), e["pattern"])
            if hit is None:
                raise ResolveError("no matching file in the vendor's checksum list")
            name, v, h = hit
            if "url_template" in e:
                url = e["url_template"].format(v=v, file=name)
            else:
                url = urllib.parse.urljoin(e["sums"], name)
            return {"id": e["id"], "url": url, "filename": name, "version": v, "hash": h,
                    "algo": HASH_LEN[len(h)], "pinned": False}
        h = ""
        if e.get("hash_url"):
            m = re.search(r"\b([0-9a-fA-F]{64})\b", fetch_text(e["hash_url"]))
            h = m.group(1).lower() if m else ""
        filename = e.get("filename") or e["url"].rsplit("/", 1)[-1]
        return {"id": e["id"], "url": e["url"], "filename": filename, "version": e.get("version", ""), "hash": h,
                "algo": HASH_LEN.get(len(h), ""), "pinned": True}
    except ResolveError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ResolveError(str(getattr(exc, "reason", exc))[:200]) from exc


def in_library(filename: str) -> bool:
    return db.q1("SELECT 1 FROM isos WHERE relpath=? OR relpath LIKE ?", filename, "%/" + filename) is not None


# ---------------------------------------------------------------- downloads

_wake = threading.Event()
_started = False
_start_lock = threading.Lock()
_cancel: set[int] = set()

NAME_RE = re.compile(r"^[\w .()+,@\-]+\.iso$", re.I)


def part_path(did: int) -> Path:
    return config.UPLOAD_DIR / f"dl-{did}.part"


def list_downloads(limit: int = 20) -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM downloads WHERE dismissed=0 ORDER BY id DESC LIMIT ?", limit)]


def queue(*, url: str, filename: str, folder: str, expected: str = "", name: str = "", catalog_id: str = "",
          archive: str = "", user_id: int | None = None) -> int:
    """Add a download to the queue. Raises ValueError for bad input, FileExistsError when the ISO exists."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("url")
    filename = os.path.basename(filename.strip())
    if not NAME_RE.match(filename):
        raise ValueError("name")
    folder = isos.safe_relpath(folder)
    expected = (expected or "").strip().lower()
    if expected and (len(expected) not in HASH_LEN or not re.fullmatch(r"[0-9a-f]+", expected)):
        raise ValueError("checksum")
    rel = f"{folder}/{filename}" if folder else filename
    if (config.ISO_DIR / rel).exists():
        raise FileExistsError(rel)
    busy = db.q1("SELECT id FROM downloads WHERE filename=? AND folder=? AND status IN ('queued','downloading','verifying')",
                 filename, folder)
    if busy:
        return busy["id"]
    now = time.time()
    did = db.run("INSERT INTO downloads(catalog_id,name,url,filename,folder,archive,expected,status,user_id,created,updated) "
                 "VALUES(?,?,?,?,?,?,?,'queued',?,?,?)",
                 catalog_id, name or isos.pretty_name(filename), url, filename, folder, archive, expected, user_id, now, now)
    start()
    _wake.set()
    return did


def cancel(did: int) -> None:
    row = db.q1("SELECT status FROM downloads WHERE id=?", did)
    if row is None:
        return
    if row["status"] in ("queued", "downloading", "verifying"):
        _cancel.add(did)
        db.run("UPDATE downloads SET status='cancelled', updated=? WHERE id=?", time.time(), did)
        if row["status"] == "queued":
            part_path(did).unlink(missing_ok=True)
    else:
        db.run("UPDATE downloads SET dismissed=1 WHERE id=?", did)


def retry(did: int) -> None:
    db.run("UPDATE downloads SET status='queued', error='', updated=? WHERE id=? AND status IN ('error','cancelled')",
           time.time(), did)
    _cancel.discard(did)
    start()
    _wake.set()


def start() -> None:
    """Start the worker once; downloads that were running when the app stopped are resumed."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    db.run("UPDATE downloads SET status='queued' WHERE status IN ('downloading','verifying')")
    threading.Thread(target=_loop, daemon=True, name="catalog-downloads").start()


def _loop() -> None:
    while True:
        row = db.q1("SELECT * FROM downloads WHERE status='queued' ORDER BY id LIMIT 1")
        if row is None:
            _wake.wait(30)
            _wake.clear()
            continue
        try:
            run_one(row["id"])
        except Exception:  # noqa: BLE001
            log.exception("download %s", row["id"])
            db.run("UPDATE downloads SET status='error', error='internal error' WHERE id=?", row["id"])


class Cancelled(Exception):
    pass


def _free_space() -> int:
    try:
        st = os.statvfs(config.UPLOAD_DIR)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def _set(did: int, **kw) -> None:
    kw["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in kw)
    db.run(f"UPDATE downloads SET {cols} WHERE id=?", *kw.values(), did)  # noqa: S608 - keys are ours


def _fetch(did: int, url: str, part: Path, hasher) -> int:
    """Stream url into part (resuming), feeding hasher. Returns the final size."""
    have = part.stat().st_size if part.exists() else 0
    if have:
        with part.open("rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                hasher.update(chunk)
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        resp = _open(url, headers, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:  # already complete
            return have
        raise
    with resp:
        if have and resp.status != 206:  # server ignored the range: start over
            raise _Restart()
        length = int(resp.headers.get("Content-Length") or 0)
        total = have + length if length else 0
        if total:
            free = _free_space()
            if free and total - have > free:
                raise OSError("not enough disk space")
        _set(did, size=total, received=have, status="downloading")
        last = time.time()
        with part.open("ab") as f:
            while True:
                if did in _cancel:
                    raise Cancelled()
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                hasher.update(chunk)
                have += len(chunk)
                if time.time() - last > 1:
                    _set(did, received=have)
                    last = time.time()
    _set(did, received=have)
    return have


class _Restart(Exception):
    pass


def run_one(did: int) -> None:
    row = db.q1("SELECT * FROM downloads WHERE id=?", did)
    if row is None or row["status"] != "queued":
        return
    _cancel.discard(did)
    part = part_path(did)
    algo = HASH_LEN.get(len(row["expected"]), "sha256")
    _set(did, status="downloading", error="")
    attempt = 0
    while True:
        hasher = hashlib.new(algo)
        try:
            _fetch(did, row["url"], part, hasher)
            break
        except Cancelled:
            part.unlink(missing_ok=True)
            _cancel.discard(did)
            return
        except _Restart:
            part.unlink(missing_ok=True)
            continue
        except (OSError, urllib.error.URLError) as exc:
            attempt += 1
            msg = str(getattr(exc, "reason", exc))[:200]
            if attempt >= 5 or "disk space" in msg:
                _set(did, status="error", error=msg)
                return
            log.warning("download %s: %s, retrying", did, msg)
            time.sleep(min(60, 5 * attempt))
            if did in _cancel:
                part.unlink(missing_ok=True)
                return
    _set(did, status="verifying")
    digest = hasher.hexdigest()
    if row["expected"] and digest != row["expected"]:
        part.unlink(missing_ok=True)
        _set(did, status="error", error="checksum mismatch", actual=digest)
        return
    rel = f"{row['folder']}/{row['filename']}" if row["folder"] else row["filename"]
    target = config.ISO_DIR / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(rel)
        if row["archive"] == "zip":
            _extract_zip(part, target)
            part.unlink(missing_ok=True)
        else:
            shutil.move(str(part), str(target))
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        _set(did, status="error", error=str(exc)[:200], actual=digest)
        return
    iso_id = isos.add_file(rel)
    isos.process(iso_id)
    _set(did, status="done", iso_id=iso_id, actual=digest)
    try:
        from . import mailer
        iso = isos.get(iso_id)
        mailer.notify("upload", f"ISO downloaded: {iso['name']}",
                      f"{rel} was downloaded from {row['url']}.\n"
                      f"{algo.upper()}: {digest}" + (" (verified)" if row["expected"] else " (no published checksum)"))
    except Exception:  # noqa: BLE001
        log.exception("download notification")


def _extract_zip(src: Path, target: Path) -> None:
    with zipfile.ZipFile(src) as z:
        members = [m for m in z.infolist() if m.filename.lower().endswith(".iso") and not m.is_dir()]
        if not members:
            raise ValueError("no ISO inside the archive")
        tmp = target.with_name("." + target.name + ".tmp")
        with z.open(members[0]) as fin, tmp.open("wb") as fout:
            shutil.copyfileobj(fin, fout, 4 * 1024 * 1024)
        tmp.replace(target)
