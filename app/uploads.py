"""Resumable chunked uploads for large ISO files.

Protocol (all JSON, authenticated):
  POST /api/uploads                     {filename, size, folder}   -> {id, received}
  GET  /api/uploads/{id}                                           -> {id, received, size}
  PUT  /api/uploads/{id}?offset=N       raw chunk body              -> {received}
  POST /api/uploads/{id}/complete                                   -> {iso_id}
  DELETE /api/uploads/{id}
The client asks for `received` before it continues, so a dropped connection
or a reloaded page resumes at the last byte the server has.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

from . import config, db, isos

NAME_RE = re.compile(r"^[\w .()+,@\-]+\.iso$", re.I)


def part_path(uid: str) -> Path:
    return config.UPLOAD_DIR / f"{uid}.part"


def create(filename: str, size: int, folder: str, user_id: int | None) -> dict:
    filename = os.path.basename(filename or "").strip()
    if not NAME_RE.match(filename):
        raise ValueError("name")
    if size <= 0:
        raise ValueError("size")
    folder = isos.safe_relpath(folder)
    target = config.ISO_DIR / folder / filename
    if target.exists():
        raise FileExistsError(filename)
    # Resume an unfinished upload of the same file
    row = db.q1("SELECT * FROM uploads WHERE filename=? AND folder=? AND size=?", filename, folder, size)
    if row:
        received = part_path(row["id"]).stat().st_size if part_path(row["id"]).exists() else 0
        db.run("UPDATE uploads SET received=?, updated=? WHERE id=?", received, time.time(), row["id"])
        return {"id": row["id"], "received": received, "size": size}
    free = _free_space()
    if free and size > free:
        raise OSError("disk full")
    uid = secrets.token_hex(12)
    part_path(uid).touch()
    now = time.time()
    db.run("INSERT INTO uploads(id,filename,folder,size,received,user_id,created,updated) VALUES(?,?,?,?,?,?,?,?)",
           uid, filename, folder, size, 0, user_id, now, now)
    return {"id": uid, "received": 0, "size": size}


def _free_space() -> int:
    try:
        st = os.statvfs(config.UPLOAD_DIR)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def get(uid: str):
    return db.q1("SELECT * FROM uploads WHERE id=?", uid)


async def write_chunk(uid: str, offset: int, stream) -> int:
    row = get(uid)
    if row is None:
        raise KeyError(uid)
    p = part_path(uid)
    current = p.stat().st_size if p.exists() else 0
    if offset != current:
        # The client is out of sync; it should ask for the current offset and continue there.
        raise ValueError(current)
    written = 0
    with open(p, "ab") as f:
        async for chunk in stream:
            written += len(chunk)
            if current + written > row["size"] or written > config.UPLOAD_CHUNK_MAX:
                f.truncate(current)
                raise OverflowError("too large")
            f.write(chunk)
    received = current + written
    db.run("UPDATE uploads SET received=?, updated=? WHERE id=?", received, time.time(), uid)
    return received


def complete(uid: str) -> int:
    row = get(uid)
    if row is None:
        raise KeyError(uid)
    p = part_path(uid)
    if p.stat().st_size != row["size"]:
        raise ValueError("incomplete")
    target_dir = config.ISO_DIR / row["folder"]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / row["filename"]
    if target.exists():
        raise FileExistsError(row["filename"])
    os.replace(p, target)
    os.chmod(target, 0o644)
    db.run("DELETE FROM uploads WHERE id=?", uid)
    rel = target.relative_to(config.ISO_DIR).as_posix()
    iso_id = isos.add_file(rel)
    return iso_id


def cancel(uid: str) -> None:
    try:
        part_path(uid).unlink()
    except FileNotFoundError:
        pass
    db.run("DELETE FROM uploads WHERE id=?", uid)


def cleanup(max_age: float = 7 * 86400) -> None:
    for row in db.q("SELECT id FROM uploads WHERE updated < ?", time.time() - max_age):
        cancel(row["id"])
