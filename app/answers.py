"""Answer files for unattended Linux and Proxmox installs.

One profile (stored as structured data) is rendered into the format the
installer of the linked ISO understands:

  ubuntu            -> autoinstall (cloud-init NoCloud: user-data + meta-data)
  debian_installer  -> preseed.cfg
  rhel              -> kickstart (ks.cfg)
  proxmox           -> answer.toml (fetched over HTTP by the Proxmox auto-installer)
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time

from . import config, db, isos, windows

log = logging.getLogger("pxe.answers")

FORMATS = {
    "autoinstall": "Ubuntu autoinstall (cloud-init)",
    "preseed": "Debian preseed",
    "kickstart": "Kickstart",
    "proxmox": "Proxmox answer.toml",
    "autounattend": "Windows autounattend.xml",
}

DEFAULT_DATA = {
    "hostname": "server01", "domain": "lan", "timezone": "Europe/Amsterdam", "locale": "en_US.UTF-8",
    "keyboard": "us", "country": "nl",
    "root_password_hash": "", "username": "admin", "user_password_hash": "", "ssh_key": "",
    "disk": "auto", "layout": "lvm", "zfs_raid": "raid0",
    "network": "dhcp", "address": "", "gateway": "", "dns": "1.1.1.1",
    "software": "server", "packages": "", "post_command": "",
    "mirror": "deb.debian.org", "mailto": "", "reboot": True,
    # Windows
    "win_index": "1", "win_key": "", "win_user": "Admin", "win_password": "",
    "win_disk": "ask", "win_disk_id": "0", "win_bypass": True,
}

TIMEZONES = ["Europe/Amsterdam", "Europe/Brussels", "Europe/Berlin", "Europe/London", "Europe/Paris", "UTC",
             "America/New_York", "America/Los_Angeles", "Asia/Singapore", "Australia/Sydney"]
LOCALES = [("en_US.UTF-8", "English (US)"), ("en_GB.UTF-8", "English (UK)"), ("nl_NL.UTF-8", "Nederlands"),
           ("de_DE.UTF-8", "Deutsch"), ("fr_FR.UTF-8", "Français")]
KEYBOARDS = [("us", "US"), ("us-intl", "US international"), ("gb", "UK"), ("be", "Belgian"), ("de", "German"),
             ("fr", "French")]
PROXMOX_KEYMAPS = {"us": "en-us", "us-intl": "en-us", "gb": "en-gb", "be": "fr-be", "de": "de", "fr": "fr"}


def format_for_method(method: str) -> str | None:
    return isos.METHODS.get(method, {}).get("answers")


def list_answers():
    return db.q("SELECT answers.*, isos.name AS iso_name, isos.method AS iso_method, isos.method_override AS iso_override "
                "FROM answers JOIN isos ON isos.id = answers.iso_id ORDER BY answers.name COLLATE NOCASE")


def get(aid: int):
    return db.q1("SELECT * FROM answers WHERE id=?", aid)


def data_of(row) -> dict:
    d = dict(DEFAULT_DATA)
    d.update(json.loads(row["data"] or "{}"))
    return d


def hash_password(pw: str) -> str:
    """SHA-512 crypt hash, the format every Linux installer accepts."""
    if not pw:
        return ""
    res = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=pw, capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        raise RuntimeError("openssl passwd failed")
    return res.stdout.strip()


def clean_hostname(h: str) -> str:
    h = re.sub(r"[^a-zA-Z0-9-]", "-", (h or "").strip()).strip("-").lower()
    return h[:63] or "server"


def mac_slug(mac: str) -> str:
    mac = (mac or "").lower()
    return mac.replace(":", "-") if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) else "-"


def effective_data(row, mac: str = "") -> dict:
    """Profile data, with the hostname of a linked machine when one matches the MAC address."""
    d = data_of(row)
    if mac and mac != "-":
        m = db.q1("SELECT name FROM machines WHERE mac=?", mac.replace("-", ":"))
        if m and m["name"]:
            d["hostname"] = m["name"] if row["format"] == "autounattend" else clean_hostname(m["name"])
    return d


# ---------------------------------------------------------------- kernel arguments

def answer_base(row, b: str, mac: str) -> str:
    return f"{b}/answers/{row['id']}/{mac_slug(mac)}/"


def kernel_args(row, b: str, ctx: dict) -> str:
    fmt = row["format"]
    url = answer_base(row, b, ctx.get("mac", ""))
    d = effective_data(row, ctx.get("mac", ""))
    if fmt == "autoinstall":
        return f"autoinstall ds=nocloud-net;s={url}"
    if fmt == "preseed":
        return (f"auto=true priority=critical url={url}preseed.cfg hostname={d['hostname']} "
                f"domain={d['domain']} interface=auto")
    if fmt == "kickstart":
        return f"inst.ks={url}ks.cfg"
    if fmt == "proxmox":
        return "proxmox-start-auto-installer"
    return ""


def proxmox_iso_url(row, b: str) -> str | None:
    p = config.DERIVED_DIR / f"answer-{row['id']}.iso"
    return f"{b}/derived/answer-{row['id']}.iso" if p.is_file() else None


# ---------------------------------------------------------------- renderers

def _pkgs(d: dict) -> list[str]:
    return [p for p in re.split(r"[\s,]+", d.get("packages", "")) if re.fullmatch(r"[A-Za-z0-9.+:@_-]+", p)]


def _callback(b: str, row, mac: str) -> str:
    return f"{b}/api/callback/{row['id']}/{mac_slug(mac)}/done"


def _static(d: dict):
    import ipaddress
    try:
        itf = ipaddress.IPv4Interface(d["address"])
        return itf, d["gateway"], [x for x in re.split(r"[\s,]+", d["dns"]) if x]
    except ValueError:
        return None


def render_autoinstall(row, b: str, mac: str) -> dict[str, str]:
    d = effective_data(row, mac)
    lines = ["#cloud-config", f"# Generated by PXE Boot Server - {row['name']}", "autoinstall:", "  version: 1",
             f"  locale: {d['locale']}", f"  keyboard: {{layout: {d['keyboard'].split('-')[0]}}}",
             f"  timezone: {d['timezone']}", "  identity:", f"    hostname: {d['hostname']}",
             f"    username: {d['username']}", f"    password: \"{d['user_password_hash']}\"",
             "  ssh:", f"    install-server: {'true' if d['software'] != 'minimal' or d['ssh_key'] else 'false'}",
             f"    allow-pw: {'false' if d['ssh_key'] else 'true'}"]
    if d["ssh_key"]:
        lines.append(f"    authorized-keys: [{json.dumps(d['ssh_key'])}]")
    match = "{size: largest}" if d["disk"] == "auto" else f"{{path: {d['disk']}}}"
    lines += ["  storage:", f"    layout: {{name: {'lvm' if d['layout'] == 'lvm' else 'direct'}, match: {match}}}"]
    st = _static(d) if d["network"] == "static" else None
    lines += ["  network:", "    version: 2", "    ethernets:", "      any:", "        match: {name: \"e*\"}"]
    if st:
        itf, gw, dns = st
        lines += [f"        addresses: [{itf.with_prefixlen}]", f"        routes: [{{to: default, via: {gw}}}]",
                  f"        nameservers: {{addresses: [{', '.join(dns)}]}}"]
    else:
        lines.append("        dhcp4: true")
    pk = _pkgs(d)
    if d["software"] == "desktop":
        pk = ["ubuntu-desktop-minimal", *pk]
    if pk:
        lines.append(f"  packages: [{', '.join(pk)}]")
    late = []
    if d["post_command"]:
        late.append(f"curtin in-target -- sh -c {json.dumps(d['post_command'])}")
    late.append(f"curl -fsS -m 5 {_callback(b, row, mac)} || true")
    lines.append("  late-commands:")
    lines += [f"    - {json.dumps(c)}" for c in late]
    if d["root_password_hash"]:
        lines += ["  user-data:", "    chpasswd:", "      expire: false",
                  f"      users: [{{name: root, password: \"{d['root_password_hash']}\", type: HASH}}]"]
    lines.append("  shutdown: reboot" if d.get("reboot", True) else "  shutdown: poweroff")
    user_data = "\n".join(lines) + "\n"
    meta = f"instance-id: pxe-{row['id']}-{int(row['updated'])}\nlocal-hostname: {d['hostname']}\n"
    return {"user-data": user_data, "meta-data": meta, "vendor-data": ""}


def render_preseed(row, b: str, mac: str) -> str:
    d = effective_data(row, mac)
    lang = d["locale"].split(".")[0]
    pk = _pkgs(d)
    tasks = {"minimal": "standard", "server": "standard, ssh-server", "desktop": "standard, gnome-desktop"}[d["software"]]
    if d["ssh_key"] and "ssh-server" not in tasks:
        tasks += ", ssh-server"
    out = [f"# Generated by PXE Boot Server - {row['name']}",
           f"d-i debian-installer/locale string {lang}",
           f"d-i keyboard-configuration/xkb-keymap select {d['keyboard'].split('-')[0]}",
           "d-i netcfg/choose_interface select auto",
           f"d-i netcfg/get_hostname string {d['hostname']}",
           f"d-i netcfg/get_domain string {d['domain']}",
           f"d-i netcfg/hostname string {d['hostname']}"]
    st = _static(d) if d["network"] == "static" else None
    if st:
        itf, gw, dns = st
        out += ["d-i netcfg/disable_autoconfig boolean true",
                f"d-i netcfg/get_ipaddress string {itf.ip}", f"d-i netcfg/get_netmask string {itf.netmask}",
                f"d-i netcfg/get_gateway string {gw}", f"d-i netcfg/get_nameservers string {' '.join(dns)}",
                "d-i netcfg/confirm_static boolean true"]
    out += ["d-i mirror/country string manual", f"d-i mirror/http/hostname string {d['mirror']}",
            "d-i mirror/http/directory string /debian", "d-i mirror/http/proxy string"]
    if d["root_password_hash"]:
        out.append(f"d-i passwd/root-password-crypted password {d['root_password_hash']}")
    else:
        out.append("d-i passwd/root-login boolean false")
    out += [f"d-i passwd/user-fullname string {d['username']}", f"d-i passwd/username string {d['username']}",
            f"d-i passwd/user-password-crypted password {d['user_password_hash']}",
            "d-i clock-setup/utc boolean true", f"d-i time/zone string {d['timezone']}", "d-i clock-setup/ntp boolean true"]
    if d["disk"] != "auto":
        out.append(f"d-i partman-auto/disk string {d['disk']}")
    out += [f"d-i partman-auto/method string {'lvm' if d['layout'] == 'lvm' else 'regular'}",
            "d-i partman-auto-lvm/guided_size string max",
            "d-i partman-lvm/device_remove_lvm boolean true", "d-i partman-md/device_remove_md boolean true",
            "d-i partman-lvm/confirm boolean true", "d-i partman-lvm/confirm_nooverwrite boolean true",
            "d-i partman-auto/choose_recipe select atomic", "d-i partman-partitioning/confirm_write_new_label boolean true",
            "d-i partman/choose_partition select finish", "d-i partman/confirm boolean true",
            "d-i partman/confirm_nooverwrite boolean true", "d-i partman-efi/non_efi_system boolean true",
            "d-i apt-setup/cdrom/set-first boolean false", "d-i apt-setup/disable-cdrom-entries boolean true",
            f"tasksel tasksel/first multiselect {tasks}",
            f"d-i pkgsel/include string {' '.join(pk + (['curl'] if 'curl' not in pk else []))}",
            "d-i pkgsel/upgrade select full-upgrade", "popularity-contest popularity-contest/participate boolean false",
            "d-i grub-installer/only_debian boolean true", "d-i grub-installer/bootdev string default"]
    late = []
    if d["ssh_key"]:
        home = f"/home/{d['username']}"
        late.append(f"in-target sh -c 'mkdir -p {home}/.ssh && echo \"{d['ssh_key']}\" >> {home}/.ssh/authorized_keys "
                    f"&& chown -R {d['username']}: {home}/.ssh && chmod 700 {home}/.ssh && chmod 600 {home}/.ssh/authorized_keys'")
    if d["post_command"]:
        late.append(f"in-target sh -c {json.dumps(d['post_command'])}")
    late.append(f"wget -q -O /dev/null {_callback(b, row, mac)} || true")
    out.append("d-i preseed/late_command string " + "; ".join(late))
    out.append("d-i finish-install/reboot_in_progress note")
    if not d.get("reboot", True):
        out.append("d-i debian-installer/exit/poweroff boolean true")
    return "\n".join(out) + "\n"


def render_kickstart(row, b: str, mac: str) -> str:
    d = effective_data(row, mac)
    out = [f"# Generated by PXE Boot Server - {row['name']}", "text", f"lang {d['locale']}",
           f"keyboard {d['keyboard'].split('-')[0]}", f"timezone {d['timezone']} --utc"]
    st = _static(d) if d["network"] == "static" else None
    if st:
        itf, gw, dns = st
        out.append(f"network --bootproto=static --ip={itf.ip} --netmask={itf.netmask} --gateway={gw} "
                   f"--nameserver={','.join(dns)} --hostname={d['hostname']}.{d['domain']} --activate")
    else:
        out.append(f"network --bootproto=dhcp --hostname={d['hostname']}.{d['domain']} --activate")
    out.append(f"rootpw --iscrypted {d['root_password_hash']}" if d["root_password_hash"] else "rootpw --lock")
    out.append(f"user --name={d['username']} --groups=wheel --iscrypted --password={d['user_password_hash']}")
    if d["ssh_key"]:
        out.append(f"sshkey --username={d['username']} {json.dumps(d['ssh_key'])}")
    if d["disk"] != "auto":
        dev = d["disk"].removeprefix("/dev/")
        out += [f"ignoredisk --only-use={dev}", f"clearpart --all --initlabel --drives={dev}"]
    else:
        out.append("clearpart --all --initlabel")
    out += ["zerombr", f"autopart --type={'lvm' if d['layout'] == 'lvm' else 'plain'}",
            "bootloader --location=mbr", "firstboot --disable", "selinux --enforcing", "firewall --enabled --ssh"]
    out.append("reboot" if d.get("reboot", True) else "poweroff")
    env = {"minimal": "minimal-environment", "server": "server-product-environment",
           "desktop": "workstation-product-environment"}[d["software"]]
    out += ["", "%packages", f"@^{env}", *_pkgs(d), "%end", ""]
    if d["post_command"]:
        out += ["%post --log=/root/ks-post.log", d["post_command"], "%end", ""]
    out += ["%post --nochroot", f"curl -fsS -m 5 {_callback(b, row, mac)} || true", "%end", ""]
    return "\n".join(out)


def render_proxmox(row, b: str, mac: str) -> str:
    d = effective_data(row, mac)
    fs = "zfs" if d["layout"] == "zfs" else ("ext4" if d["layout"] == "plain" else "ext4")
    out = [f"# Generated by PXE Boot Server - {row['name']}", "[global]",
           f"keyboard = \"{PROXMOX_KEYMAPS.get(d['keyboard'], 'en-us')}\"", f"country = \"{d['country']}\"",
           f"fqdn = \"{d['hostname']}.{d['domain']}\"", f"mailto = \"{d['mailto'] or 'root@' + d['domain']}\"",
           f"timezone = \"{d['timezone']}\"", f"root-password-hashed = \"{d['root_password_hash']}\""]
    if d["ssh_key"]:
        out.append(f"root-ssh-keys = [{json.dumps(d['ssh_key'])}]")
    out.append(f"reboot-on-error = false")
    out += ["", "[network]"]
    st = _static(d) if d["network"] == "static" else None
    if st:
        itf, gw, dns = st
        out += ["source = \"from-answer\"", f"cidr = \"{itf.with_prefixlen}\"", f"gateway = \"{gw}\"",
                f"dns = \"{dns[0] if dns else gw}\"", "filter.ID_NET_NAME_MAC = \"*\""]
    else:
        out.append("source = \"from-dhcp\"")
    out += ["", "[disk-setup]", f"filesystem = \"{fs}\""]
    if fs == "zfs":
        out.append(f"zfs.raid = \"{d.get('zfs_raid', 'raid0')}\"")
    if d["disk"] != "auto":
        out.append(f"disk-list = [\"{d['disk'].removeprefix('/dev/')}\"]")
    else:
        out.append("filter.ID_SERIAL = \"*\"")
        out.append("filter-match = \"any\"")
    if d["post_command"]:
        out += ["", "[first-boot]", "source = \"from-url\"", f"url = \"{b}/answers/{row['id']}/{mac_slug(mac)}/first-boot.sh\""]
    return "\n".join(out) + "\n"


def render_autounattend(row, b: str, mac: str, platform: str = "efi") -> str:
    d = effective_data(row, mac)
    iso = isos.get(row["iso_id"])
    eds = isos.iso_info(iso).get("editions", []) if iso else []
    edition = next((e for e in eds if str(e["index"]) == str(d.get("win_index"))), None)
    return windows.render(d, edition, platform, _callback(b, row, mac), row["name"])


def render(row, b: str, mac: str = "", platform: str = "efi") -> dict[str, str]:
    """All files of a profile, keyed by file name (used by the preview and by the HTTP endpoints)."""
    fmt = row["format"]
    if fmt == "autounattend":
        return {"autounattend.xml": render_autounattend(row, b, mac, platform)}
    if fmt == "autoinstall":
        return render_autoinstall(row, b, mac)
    if fmt == "preseed":
        return {"preseed.cfg": render_preseed(row, b, mac)}
    if fmt == "kickstart":
        return {"ks.cfg": render_kickstart(row, b, mac)}
    if fmt == "proxmox":
        files = {"answer.toml": render_proxmox(row, b, mac)}
        d = data_of(row)
        files["first-boot.sh"] = "#!/bin/sh\n" + (d["post_command"] or "true") + "\n" + \
            f"curl -fsS -m 5 {_callback(b, row, mac)} || true\n"
        return files
    return {}


def preview_name(fmt: str) -> str:
    return {"autoinstall": "user-data", "preseed": "preseed.cfg", "kickstart": "ks.cfg", "proxmox": "answer.toml",
            "autounattend": "autounattend.xml"}[fmt]


# ---------------------------------------------------------------- persistence

def save(aid: int | None, name: str, iso_id: int, data: dict) -> int:
    iso = isos.get(iso_id)
    if iso is None:
        raise ValueError("iso")
    fmt = format_for_method(isos.method_of(iso))
    if not fmt:
        raise ValueError("format")
    clean = {k: data.get(k, v) for k, v in DEFAULT_DATA.items()}
    clean["hostname"] = windows.computer_name(clean["hostname"]) if fmt == "autounattend" \
        else clean_hostname(clean["hostname"])
    now = time.time()
    status = "building" if fmt == "proxmox" else "ready"
    if aid:
        db.run("UPDATE answers SET name=?, iso_id=?, format=?, data=?, status=?, updated=? WHERE id=?",
               name, iso_id, fmt, json.dumps(clean), status, now, aid)
    else:
        aid = db.run("INSERT INTO answers(name,iso_id,format,data,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                     name, iso_id, fmt, json.dumps(clean), status, now, now)
    if fmt == "proxmox":
        threading.Thread(target=build_proxmox_iso, args=(aid,), daemon=True).start()
    return aid


def delete(aid: int) -> None:
    db.run("DELETE FROM answers WHERE id=?", aid)
    try:
        (config.DERIVED_DIR / f"answer-{aid}.iso").unlink()
    except FileNotFoundError:
        pass


def build_proxmox_iso(aid: int) -> None:
    """Copy the Proxmox ISO with an auto-installer-mode.toml that points at our answer URL."""
    from . import system
    row = get(aid)
    if row is None:
        return
    iso = isos.get(row["iso_id"])
    try:
        b = f"http://{system.server_ip()}"
        toml = f'mode = "http"\n\n[http]\nurl = "{b}/answers/{aid}/-/answer.toml"\n'
        config.DERIVED_DIR.mkdir(parents=True, exist_ok=True)
        mode_file = config.DERIVED_DIR / f"answer-{aid}.toml"
        mode_file.write_text(toml)
        out = config.DERIVED_DIR / f"answer-{aid}.iso"
        tmp = out.with_suffix(".part")
        if not shutil.which("xorriso"):
            raise RuntimeError("xorriso is not installed")
        res = subprocess.run(["xorriso", "-indev", str(config.ISO_DIR / iso["relpath"]), "-outdev", str(tmp),
                              "-map", str(mode_file), "/auto-installer-mode.toml", "-boot_image", "any", "replay"],
                             capture_output=True, text=True, timeout=3600)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "xorriso failed")
        tmp.rename(out)
        db.run("UPDATE answers SET status='ready' WHERE id=?", aid)
    except Exception as exc:  # noqa: BLE001
        log.exception("building Proxmox ISO for answer %s failed", aid)
        db.run("UPDATE answers SET status=? WHERE id=?", "error: " + str(exc)[:200], aid)
