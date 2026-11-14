"""ISO library: scanning the ISO directory, mounting and detecting how each ISO boots."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, db, system

log = logging.getLogger("pxe.isos")
_lock = threading.RLock()

# Boot methods. "fw" lists the firmware types the method works on.
METHODS: dict[str, dict] = {
    "windows": {"label": "Windows (wimboot)", "fw": ["bios", "efi"], "answers": None},
    "ubuntu": {"label": "Ubuntu / casper (url=)", "fw": ["bios", "efi"], "answers": "autoinstall"},
    "debian_installer": {"label": "Debian installer (netboot)", "fw": ["bios", "efi"], "answers": "preseed"},
    "debian_live": {"label": "Debian live (fetch=)", "fw": ["bios", "efi"], "answers": None},
    "rhel": {"label": "RHEL family (inst.repo)", "fw": ["bios", "efi"], "answers": "kickstart"},
    "fedora_live": {"label": "Fedora live (root=live:)", "fw": ["bios", "efi"], "answers": None},
    "arch": {"label": "Arch Linux (archiso_http_srv)", "fw": ["bios", "efi"], "answers": None},
    "proxmox": {"label": "Proxmox (ISO in initrd)", "fw": ["bios", "efi"], "answers": "proxmox"},
    "sanboot": {"label": "Generic (sanboot)", "fw": ["bios", "efi"], "answers": None},
    "memdisk": {"label": "Generic in RAM (memdisk)", "fw": ["bios"], "answers": None},
}

ISO_RE = re.compile(r"\.iso$", re.I)
SAFE_PART = re.compile(r"^[\w .()+,@\-]+$")


def method_of(row) -> str:
    return row["method_override"] or row["method"]


def safe_relpath(rel: str) -> str:
    """Normalise a user supplied relative path inside ISO_DIR; raises ValueError when unsafe."""
    rel = (rel or "").strip().strip("/")
    if not rel:
        return ""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or p.startswith(".") or not SAFE_PART.match(p):
            raise ValueError(f"invalid name: {p}")
    return "/".join(parts)


# ---------------------------------------------------------------- filesystem helpers

def ci_find(root: Path, rel: str) -> str | None:
    """Case-insensitive lookup of rel below root. Returns the real relative path or None."""
    cur = root
    parts = []
    for part in rel.strip("/").split("/"):
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        match = next((n for n in names if n.lower() == part.lower()), None)
        if match is None:
            return None
        parts.append(match)
        cur = cur / match
    return "/".join(parts)


def ci_glob(root: Path, rel_dir: str, prefix: str) -> list[str]:
    d = ci_find(root, rel_dir)
    if d is None:
        return []
    try:
        return sorted(f"{d}/{n}" for n in os.listdir(root / d) if n.lower().startswith(prefix.lower()))
    except OSError:
        return []


def mount_dir(iso_id: int) -> Path:
    return config.MNT_DIR / str(iso_id)


def is_mounted(iso_id: int) -> bool:
    d = mount_dir(iso_id)
    if config.DEV:
        return d.is_dir() and any(d.iterdir())
    return os.path.ismount(d)


def mount(row) -> None:
    if is_mounted(row["id"]):
        return
    system.helper("mount", row["relpath"], str(row["id"]))


def umount(iso_id: int) -> None:
    if config.DEV:
        return
    try:
        system.helper("umount", str(iso_id))
    except system.HelperError as exc:
        log.warning("umount %s: %s", iso_id, exc)


# ---------------------------------------------------------------- detection

@dataclass
class Detection:
    method: str
    info: dict


def detect(root: Path) -> Detection:
    f = lambda p: ci_find(root, p)  # noqa: E731

    # Windows setup / WinPE
    wim = f("sources/boot.wim")
    bcd = f("boot/bcd") or f("efi/microsoft/boot/bcd")
    sdi = f("boot/boot.sdi")
    if wim and bcd and sdi:
        info = {"wim": wim, "bcd": bcd, "sdi": sdi, "bootmgr": f("bootmgr") or "",
                "setup": f("setup.exe") or f("sources/setup.exe") or "",
                "install": f("sources/install.wim") or f("sources/install.esd") or f("sources/install.swm") or ""}
        name = ""
        if info["install"]:
            name = "Windows"
        return Detection("windows", info | {"label": name})

    # Proxmox VE / PBS / PMG
    if f("proxmox") and f("boot/linux26") and f("boot/initrd.img"):
        product = ""
        cdinfo = f(".cd-info")
        if cdinfo:
            try:
                for line in (root / cdinfo).read_text(errors="ignore").splitlines():
                    if line.startswith("PRODUCT="):
                        product = line.split("=", 1)[1].strip("'\"")
            except OSError:
                pass
        return Detection("proxmox", {"kernel": f("boot/linux26"), "initrd": f("boot/initrd.img"), "product": product})

    # Ubuntu and derivatives (casper)
    k = ci_glob(root, "casper", "vmlinuz")
    i = ci_glob(root, "casper", "initrd")
    if k and i:
        return Detection("ubuntu", {"kernel": k[0], "initrd": i[0]})

    # Debian live (live-boot), also Kali/GParted/Clonezilla style
    k = ci_glob(root, "live", "vmlinuz")
    i = ci_glob(root, "live", "initrd")
    sq = f("live/filesystem.squashfs")
    if k and i and sq:
        return Detection("debian_live", {"kernel": k[0], "initrd": i[0], "squashfs": sq})

    # Debian installer CD/DVD
    if f("install.amd/vmlinuz") and f("install.amd/initrd.gz"):
        codename = ""
        dists = f("dists")
        if dists:
            for n in sorted(os.listdir(root / dists)):
                p = root / dists / n
                if p.is_dir() and not p.is_symlink() and n not in ("stable", "testing", "unstable", "oldstable"):
                    codename = n
                    break
        return Detection("debian_installer", {"kernel": f("install.amd/vmlinuz"), "initrd": f("install.amd/initrd.gz"),
                                              "codename": codename})

    # RHEL / Rocky / Alma / Fedora installer, and Fedora live
    k = f("images/pxeboot/vmlinuz")
    i = f("images/pxeboot/initrd.img")
    if k and i:
        if f("LiveOS/squashfs.img") and not f(".treeinfo"):
            return Detection("fedora_live", {"kernel": k, "initrd": i, "squashfs": f("LiveOS/squashfs.img")})
        return Detection("rhel", {"kernel": k, "initrd": i})

    # Arch Linux
    k = f("arch/boot/x86_64/vmlinuz-linux")
    i = f("arch/boot/x86_64/initramfs-linux.img")
    if k and i:
        return Detection("arch", {"kernel": k, "initrd": i, "basedir": f("arch") or "arch"})

    return Detection("sanboot", {})


def pretty_name(filename: str) -> str:
    stem = re.sub(ISO_RE, "", filename)
    stem = re.sub(r"[_]+", " ", stem)
    return stem.strip() or filename


# ---------------------------------------------------------------- library

def folders() -> list[str]:
    res = []
    base = config.ISO_DIR
    for dirpath, dirnames, _ in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel = os.path.relpath(dirpath, base)
        if rel != ".":
            res.append(rel.replace(os.sep, "/"))
    return res


def list_isos() -> list:
    return db.q("SELECT * FROM isos ORDER BY relpath COLLATE NOCASE")


def get(iso_id: int):
    return db.q1("SELECT * FROM isos WHERE id=?", iso_id)


def iso_info(row) -> dict:
    return json.loads(row["info"] or "{}")


PATH_KEYS = ("wim", "bcd", "sdi", "bootmgr", "setup", "kernel", "initrd", "squashfs")


def missing_files(row) -> list[str]:
    """Boot files recorded at detection time that cannot be found in the mount right now."""
    root = mount_dir(row["id"])
    info = iso_info(row)
    return [info[k] for k in PATH_KEYS if info.get(k) and not (root / info[k]).is_file()]


def ensure_ready(row):
    """Called right before a client boots an ISO. Mounts it when needed and, when the recorded
    paths no longer match the mount (case differs between the UDF and ISO 9660 views, or the
    mount is gone), mounts and detects again. Returns the fresh row."""
    if row is None:
        return None
    if is_mounted(row["id"]) and not missing_files(row):
        return row
    log.warning("ISO %s: boot files not found (%s), mounting and detecting again",
                row["id"], ", ".join(missing_files(row)) or "not mounted")
    process(row["id"])
    return get(row["id"])


def process(iso_id: int) -> None:
    """Mount and detect one ISO. Safe to call repeatedly."""
    row = get(iso_id)
    if row is None:
        return
    db.run("UPDATE isos SET status='mounting', error='' WHERE id=?", iso_id)
    try:
        mount(row)
        det = detect(mount_dir(iso_id))
        db.run("UPDATE isos SET method=?, info=?, status='ready', error='' WHERE id=?",
               det.method, json.dumps(det.info), iso_id)
        if det.method == "debian_installer":
            from . import netboot
            netboot.ensure_debian_async(det.info.get("codename", ""))
    except Exception as exc:  # noqa: BLE001
        log.exception("processing ISO %s failed", iso_id)
        db.run("UPDATE isos SET status='error', error=? WHERE id=?", str(exc)[:500], iso_id)


def add_file(relpath: str) -> int:
    path = config.ISO_DIR / relpath
    st = path.stat()
    existing = db.q1("SELECT id FROM isos WHERE relpath=?", relpath)
    if existing:
        return existing["id"]
    iso_id = db.run("INSERT INTO isos(relpath,name,size,mtime,added) VALUES(?,?,?,?,?)",
                    relpath, pretty_name(path.name), st.st_size, st.st_mtime, time.time())
    return iso_id


def scan() -> dict:
    """Synchronise the database with the files on disk."""
    with _lock:
        found: dict[str, os.stat_result] = {}
        base = config.ISO_DIR
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith(".") or not ISO_RE.search(fn):
                    continue
                full = Path(dirpath) / fn
                rel = full.relative_to(base).as_posix()
                try:
                    found[rel] = full.stat()
                except OSError:
                    continue
        added = removed = changed = 0
        known = {r["relpath"]: r for r in list_isos()}
        for rel, row in known.items():
            if rel not in found:
                umount(row["id"])
                db.run("DELETE FROM isos WHERE id=?", row["id"])
                removed += 1
        todo = []
        for rel, st in found.items():
            row = known.get(rel)
            if row is None:
                todo.append(add_file(rel))
                added += 1
            elif row["size"] != st.st_size or abs(row["mtime"] - st.st_mtime) > 1:
                umount(row["id"])
                db.run("UPDATE isos SET size=?, mtime=? WHERE id=?", st.st_size, st.st_mtime, row["id"])
                todo.append(row["id"])
                changed += 1
            elif row["status"] != "ready" or not is_mounted(row["id"]):
                todo.append(row["id"])
        for iso_id in todo:
            process(iso_id)
        return {"added": added, "removed": removed, "changed": changed, "total": len(found)}


def scan_async() -> None:
    threading.Thread(target=scan, daemon=True, name="iso-scan").start()


def delete(iso_id: int) -> None:
    row = get(iso_id)
    if row is None:
        return
    umount(iso_id)
    try:
        (config.ISO_DIR / row["relpath"]).unlink()
    except FileNotFoundError:
        pass
    db.run("DELETE FROM isos WHERE id=?", iso_id)


def move(iso_id: int, folder: str) -> None:
    row = get(iso_id)
    if row is None:
        raise ValueError("not found")
    folder = safe_relpath(folder)
    src = config.ISO_DIR / row["relpath"]
    dst_rel = f"{folder}/{src.name}" if folder else src.name
    dst = config.ISO_DIR / dst_rel
    if dst.exists():
        raise ValueError("exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Mounts reference the old path through the loop device, so remount after moving.
    umount(iso_id)
    shutil.move(src, dst)
    db.run("UPDATE isos SET relpath=? WHERE id=?", dst_rel, iso_id)
    process(iso_id)


def create_folder(path: str) -> str:
    rel = safe_relpath(path)
    if not rel:
        raise ValueError("empty")
    (config.ISO_DIR / rel).mkdir(parents=True, exist_ok=True)
    return rel


def delete_folder(path: str) -> None:
    rel = safe_relpath(path)
    p = config.ISO_DIR / rel
    if rel and p.is_dir():
        p.rmdir()  # only empty folders


def os_tag(row) -> str:
    m = method_of(row)
    n = (row["name"] + " " + row["relpath"]).lower()
    for key, tag in (("win", "WIN"), ("ubuntu", "UBU"), ("debian", "DEB"), ("proxmox", "PVE"), ("rocky", "RKY"),
                     ("alma", "ALM"), ("fedora", "FED"), ("arch", "ARC"), ("mint", "MNT"), ("kali", "KAL"),
                     ("gparted", "GP"), ("clonezilla", "CZ"), ("memtest", "MEM")):
        if key in n:
            return tag
    return {"windows": "WIN", "proxmox": "PVE", "ubuntu": "UBU"}.get(m, "ISO")
