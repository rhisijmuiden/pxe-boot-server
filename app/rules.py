"""Boot rules: what a machine boots, from its own rule or from its group.

Precedence: a machine with its own ISO uses its own rule. Otherwise the rule of its group applies:
the group it is assigned to, or else the group whose MAC prefix matches (longest prefix wins).
Unknown machines caught by a prefix are registered on their first boot, so "once" works per machine.
"""
from __future__ import annotations

import re
import time

from . import db

MODES = ("once", "always", "default")
_HEX = re.compile(r"[^0-9a-f]")


def normalize_prefixes(text: str) -> str:
    """'52-54-00, AA:BB' -> '52:54:00,aa:bb'. Raises ValueError on anything that is not a MAC prefix."""
    out = []
    for part in re.split(r"[\s,;]+", (text or "").strip().lower()):
        if not part:
            continue
        h = _HEX.sub("", part)
        if len(h) < 2 or len(h) > 12 or len(h) % 2 or len(re.sub(r"[:\-.]", "", part)) != len(h):
            raise ValueError(part)
        out.append(":".join(h[i:i + 2] for i in range(0, len(h), 2)))
    return ",".join(dict.fromkeys(out))


def group_by_prefix(mac: str):
    best, best_len = None, 0
    for g in db.q("SELECT * FROM machine_groups WHERE prefixes<>''"):
        for p in g["prefixes"].split(","):
            if mac.startswith(p) and len(p) > best_len:
                best, best_len = g, len(p)
    return best


def get_group(gid):
    return db.q1("SELECT * FROM machine_groups WHERE id=?", gid) if gid else None


def effective(mac: str, machine=None) -> dict | None:
    """The rule that applies to mac: {source, mode, iso_id, answer_id, used, machine, group} or None."""
    if not mac:
        return None
    if machine is None:
        machine = db.q1("SELECT * FROM machines WHERE mac=?", mac)
    if machine is not None and machine["iso_id"]:
        return {"source": "machine", "mode": machine["mode"], "iso_id": machine["iso_id"],
                "answer_id": machine["answer_id"], "used": bool(machine["used"]), "machine": machine, "group": None}
    group = get_group(machine["group_id"]) if machine is not None and machine["group_id"] else group_by_prefix(mac)
    if group is None or not group["iso_id"]:
        return None
    return {"source": "group", "mode": group["mode"], "iso_id": group["iso_id"], "answer_id": group["answer_id"],
            "used": bool(machine["group_used"]) if machine is not None and machine["group_id"] == group["id"] else False,
            "machine": machine, "group": group}


def auto_boot(rule: dict | None) -> bool:
    return rule is not None and (rule["mode"] == "always" or (rule["mode"] == "once" and not rule["used"]))


def mark_used(mac: str, rule: dict) -> None:
    """Record that a 'once' rule was used. Registers machines caught by a group prefix."""
    if rule["mode"] != "once":
        return
    if rule["source"] == "machine":
        db.run("UPDATE machines SET used=1 WHERE id=?", rule["machine"]["id"])
        return
    gid = rule["group"]["id"]
    if rule["machine"] is None:
        db.run("INSERT INTO machines(mac,name,iso_id,answer_id,mode,created,group_id,group_used,last_seen) "
               "VALUES(?,'',NULL,NULL,'default',?,?,1,?) ON CONFLICT(mac) DO UPDATE SET group_id=excluded.group_id, "
               "group_used=1", mac, time.time(), gid, time.time())
    else:
        db.run("UPDATE machines SET group_id=?, group_used=1 WHERE id=?", gid, rule["machine"]["id"])


def rearm_group(gid: int) -> None:
    db.run("UPDATE machines SET group_used=0 WHERE group_id=?", gid)


def list_groups():
    return db.q("SELECT g.*, isos.name AS iso_name, answers.name AS answer_name, "
                "(SELECT COUNT(*) FROM machines m WHERE m.group_id=g.id) AS members, "
                "(SELECT COUNT(*) FROM machines m WHERE m.group_id=g.id AND m.group_used=1) AS used_count "
                "FROM machine_groups g LEFT JOIN isos ON isos.id=g.iso_id LEFT JOIN answers ON answers.id=g.answer_id "
                "ORDER BY g.name COLLATE NOCASE")
