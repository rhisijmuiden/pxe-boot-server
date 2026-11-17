"""Generates the iPXE scripts: the entry script, the boot menu and one boot script per ISO."""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from . import answers as answers_mod
from . import config, db, isos, netboot

CLIENT_VARS = ("mac=${netX/mac}&platform=${platform}&arch=${buildarch}"
               "&manufacturer=${manufacturer:uristring}&product=${product:uristring}"
               "&serial=${serial:uristring}&hostname=${hostname:uristring}")


def _txt(s: str) -> str:
    """Make text safe for iPXE (no variable expansion, no newlines)."""
    return re.sub(r"[\r\n$]", "", s or "").strip()


def base(server_ip: str) -> str:
    return f"http://{server_ip}"


def entry_script(server_ip: str) -> str:
    b = base(server_ip)
    return f"""#!ipxe
# PXE Boot Server entry point
chain --autofree {b}/ipxe/menu.ipxe?{CLIENT_VARS} || goto failed
# Back here when the menu chose Boot from local disk: hand control back to the firmware.
exit 0
:failed
echo Could not load the boot menu from {b}
prompt --timeout 10000 Press any key to retry ... ||
chain --autofree {b}/ipxe/boot.ipxe
"""


def client_query(ctx: dict) -> str:
    return urlencode({k: ctx.get(k, "") for k in ("mac", "platform", "arch")})


def platform_ok(method: str, platform: str) -> bool:
    fw = isos.METHODS.get(method, {}).get("fw", [])
    return ("efi" if platform == "efi" else "bios") in fw


def menu_script(server_ip: str, ctx: dict, machine=None) -> str:
    """The interactive menu. `machine` is the boot rule (rules.effective) or a machine row; only a
    'default' rule matters here, 'once'/'always' rules boot without the menu."""
    b = base(server_ip)
    platform = ctx.get("platform", "pcbios")
    q = client_query(ctx)
    title = _txt(db.get_setting("menu_title") or db.get_setting("server_name") or "PXE Boot Server")
    timeout = int(db.get_setting("menu_timeout") or 0)
    default = str(db.get_setting("menu_default") or "local")
    if machine is not None and machine["mode"] == "default" and machine["iso_id"]:
        default = f"{machine['iso_id']}" + (f"a{machine['answer_id']}" if machine["answer_id"] else "")

    rows = [r for r in isos.list_isos() if r["enabled"] and r["status"] == "ready"]
    ans_by_iso: dict[int, list] = {}
    for a in answers_mod.list_answers():
        if a["status"] == "ready":
            ans_by_iso.setdefault(a["iso_id"], []).append(a)

    # Build folder tree: {folder: [entries]}
    tree: dict[str, list] = {}
    for r in rows:
        folder = r["relpath"].rsplit("/", 1)[0] if "/" in r["relpath"] else ""
        tree.setdefault(folder, []).append(r)

    def label(r, a=None) -> str:
        return f"iso{r['id']}" + (f"a{a['id']}" if a else "")

    def items(r) -> list[str]:
        method = isos.method_of(r)
        name = _txt(r["name"])
        suffix = "" if platform_ok(method, platform) else "  (BIOS only)"
        out = [f"item {label(r)} {name}{suffix}"]
        for a in ans_by_iso.get(r["id"], []):
            out.append(f"item {label(r, a)} {name} - {_txt(a['name'])} (unattended)")
        return out

    def targets(r) -> list[str]:
        out = []
        url = f"{b}/ipxe/iso/{r['id']}.ipxe?{q}"
        out.append(f":{label(r)}\nchain --autofree {url} || goto failed\ngoto start")
        for a in ans_by_iso.get(r["id"], []):
            out.append(f":{label(r, a)}\nchain --autofree {url}&answer={a['id']} || goto failed\ngoto start")
        return out

    # Top level folders, sorted; nested folders become nested submenus
    folder_names = sorted({f for f in tree if f} | {p for f in tree if f for p in _parents(f)}, key=str.lower)

    def children(parent: str) -> list[str]:
        return [f for f in folder_names if (f.rsplit("/", 1)[0] if "/" in f else "") == parent]

    def flabel(f: str) -> str:
        return "dir_" + re.sub(r"[^A-Za-z0-9]", "_", f)

    lines = ["#!ipxe", f"set server {b}", "set menu-timeout " + str(timeout * 1000),
             "iseq ${platform} efi && set fwname UEFI || set fwname Legacy BIOS", "", ":start"]
    lines.append(f"menu {title}  -  ${{fwname}}  -  ${{netX/ip}}")
    top = children("")
    if top:
        lines.append("item --gap -- Folders")
        for f in top:
            lines.append(f"item {flabel(f)} {_txt(f)}/  >")
    if tree.get(""):
        lines.append("item --gap -- ISO images")
        for r in tree[""]:
            lines += items(r)
    lines += ["item --gap --", "item local Boot from local disk", "item shell iPXE shell", "item reboot Reboot"]
    default_label = "local" if default == "local" else f"iso{default}"
    if timeout > 0:
        lines.append(f"choose --default {default_label} --timeout ${{menu-timeout}} target && goto ${{target}} || goto local")
    else:
        lines.append(f"choose --default {default_label} target && goto ${{target}} || goto local")
    lines.append("")

    for f in folder_names:
        parent = f.rsplit("/", 1)[0] if "/" in f else ""
        back = flabel(parent) if parent else "start"
        lines.append(f":{flabel(f)}")
        lines.append(f"menu {title}  /  {_txt(f)}")
        for c in children(f):
            lines.append(f"item {flabel(c)} {_txt(c.rsplit('/', 1)[-1])}/  >")
        for r in tree.get(f, []):
            lines += items(r)
        lines += ["item --gap --", f"item {back} < Back"]
        here = {f"iso{r['id']}" for r in tree.get(f, [])}
        dflt = f"--default {default_label} " if default_label.split("a")[0] in here else ""
        lines.append(f"choose {dflt}target && goto ${{target}} || goto {back}")
        lines.append("")

    for r in rows:
        lines += targets(r)
    lines += [
        "",
        ":local",
        "echo Booting from local disk ...",
        "exit 0",
        ":shell",
        "echo Type 'exit' to return to the menu",
        "shell",
        "goto start",
        ":reboot",
        "reboot",
        ":failed",
        "echo Boot failed: ${errno}",
        "prompt --timeout 15000 Press any key to return to the menu ... ||",
        "goto start",
        "",
    ]
    return "\n".join(lines)


def _parents(folder: str) -> list[str]:
    parts = folder.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def iso_url(b: str, row) -> str:
    return f"{b}/isos/" + quote(row["relpath"])


def mnt_url(b: str, row, path: str = "") -> str:
    return f"{b}/mnt/{row['id']}/" + quote(path)


def error_script(msg: str, b: str, q: str) -> str:
    return f"""#!ipxe
echo
echo {_txt(msg)}
echo
prompt --timeout 20000 Press any key to return to the menu ... ||
chain --autofree {b}/ipxe/menu.ipxe?{q}
"""


def boot_script(server_ip: str, row, ctx: dict, answer=None) -> str:
    b = base(server_ip)
    q = client_query(ctx)
    platform = ctx.get("platform", "pcbios")
    method = isos.method_of(row)
    info = isos.iso_info(row)
    name = _txt(row["name"])
    if not platform_ok(method, platform):
        return error_script(f"{name} cannot boot in UEFI mode with method {method}. Use Legacy BIOS.", b, q)

    head = [ "#!ipxe", f"echo Booting {name} ({isos.METHODS[method]['label']}) ..."]
    fail = ["boot || goto failed", ":failed", "echo Boot failed: ${errno}",
            "prompt --timeout 20000 Press any key to return to the menu ... ||",
            f"chain --autofree {b}/ipxe/menu.ipxe?{q}", ""]
    ans_args = answers_mod.kernel_args(answer, b, ctx) if answer is not None else ""
    m = lambda p: mnt_url(b, row, p)  # noqa: E731

    if method == "windows":
        aq = f"&answer={answer['id']}" if answer is not None and answer["format"] == "autounattend" else ""
        body = [
            f"kernel {b}/boot/wimboot gui",
            f"initrd -n winpeshl.ini {b}/ipxe/winpe/{row['id']}/winpeshl.ini winpeshl.ini",
            f"initrd -n install.cmd {b}/ipxe/winpe/{row['id']}/install.cmd?{q}{aq} install.cmd",
        ]
        if aq:
            # wimboot puts it in X:\Windows\System32, where install.cmd hands it to Setup
            body.append(f"initrd -n autounattend.xml {b}/ipxe/winpe/{row['id']}/autounattend.xml?{q}{aq} "
                        "autounattend.xml")
        body += [
            f"initrd -n BCD {m(info['bcd'])} BCD",
            f"initrd -n boot.sdi {m(info['sdi'])} boot.sdi",
            f"initrd -n boot.wim {m(info['wim'])} boot.wim",
        ]
    elif method == "ubuntu":
        args = f"ip=dhcp url={iso_url(b, row)} cloud-config-url=/dev/null"
        body = [f"kernel {m(info['kernel'])} initrd=initrd {args} {ans_args} ---".replace("  ", " "),
                f"initrd -n initrd {m(info['initrd'])}"]
    elif method == "debian_installer":
        code = info.get("codename", "")
        if not netboot.debian_ready(code):
            netboot.ensure_debian_async(code)
            return error_script(f"Debian {code or '?'} netboot files are still downloading. Try again in a minute.", b, q)
        body = [f"kernel {b}/boot/debian/{code}/linux initrd=initrd.gz {ans_args} ---".replace("  ", " "),
                f"initrd -n initrd.gz {b}/boot/debian/{code}/initrd.gz"]
    elif method == "debian_live":
        body = [f"kernel {m(info['kernel'])} initrd=initrd boot=live components fetch={m(info['squashfs'])} ip=dhcp",
                f"initrd -n initrd {m(info['initrd'])}"]
    elif method == "rhel":
        body = [f"kernel {m(info['kernel'])} initrd=initrd.img inst.repo={m('')} ip=dhcp {ans_args}".rstrip(),
                f"initrd -n initrd.img {m(info['initrd'])}"]
    elif method == "fedora_live":
        body = [f"kernel {m(info['kernel'])} initrd=initrd.img root=live:{m(info['squashfs'])} rd.live.image "
                f"rd.neednet=1 ip=dhcp",
                f"initrd -n initrd.img {m(info['initrd'])}"]
    elif method == "arch":
        body = [f"kernel {m(info['kernel'])} initrd=initramfs-linux.img archisobasedir={info.get('basedir', 'arch')} "
                f"archiso_http_srv={m('')} ip=dhcp",
                f"initrd -n initramfs-linux.img {m(info['initrd'])}"]
    elif method == "proxmox":
        iso = iso_url(b, row)
        if answer is not None:
            iso = answers_mod.proxmox_iso_url(answer, b) or iso
        body = [f"kernel {m(info['kernel'])} initrd=initrd.img initrd=proxmox.iso ramdisk_size=16777216 rw quiet "
                f"splash=silent {ans_args}".rstrip(),
                f"initrd -n initrd.img {m(info['initrd'])}",
                f"initrd -n proxmox.iso {iso} /proxmox.iso"]
    elif method == "memdisk":
        body = [f"kernel {b}/boot/memdisk iso raw", f"initrd {iso_url(b, row)}"]
        return "\n".join(head + body + fail)
    else:  # sanboot
        return "\n".join(head + [f"sanboot --no-describe {iso_url(b, row)} || goto failed"] + fail[1:])
    return "\n".join(head + body + fail)


def winpe_files(server_ip: str, row, ctx: dict, answer=None) -> dict[str, str]:
    """Startup files injected into WinPE by wimboot: connect the share and run setup.
    With a Windows answer profile, also autounattend.xml and an unattended Setup."""
    info = isos.iso_info(row)
    pw = db.get_setting("smb_password") or ""
    setup = info.get("setup") or "setup.exe"
    share = f"\\\\{server_ip}\\pxe"
    folder = str(row["id"])
    winpeshl = "[LaunchApps]\r\n\"install.cmd\"\r\n"
    unattended, setup_args, files = [], "", {}
    if answer is not None and answer["format"] == "autounattend":
        d = answers_mod.effective_data(answer, ctx.get("mac", ""))
        files["autounattend.xml"] = answers_mod.render(
            answer, base(server_ip), ctx.get("mac", ""), ctx.get("platform", "efi")).get("autounattend.xml", "")
        if d.get("win_bypass", True):
            # Windows 11 hardware checks, set before Setup starts
            unattended += [f"reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {v} /t REG_DWORD /d 1 /f >nul"
                           for v in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck")]
        unattended.append("echo Unattended installation: " + _txt(answer["name"]).replace("%", "%%"))
        setup_args = " /unattend:X:\\Windows\\System32\\autounattend.xml"
    install = "\r\n".join([
        "@echo off",
        "title PXE Boot Server - Windows Setup",
        "echo Starting network ...",
        "wpeinit",
        "set tries=0",
        ":net",
        "set /a tries+=1",
        f"ping -n 2 {server_ip} >nul",
        "if errorlevel 1 (if %tries% LSS 60 goto net)",
        f"echo Connecting to {share} ...",
        f"net use Z: {share} /user:pxe \"{pw}\" /persistent:no >nul 2>&1",
        "if errorlevel 1 (if %tries% LSS 60 (ping -n 3 127.0.0.1 >nul & goto net))",
        "if not exist Z:\\ (",
        "  echo Could not connect to the Windows share on the PXE server.",
        "  echo Check that TCP port 445 to the server is open.",
        "  cmd /k",
        ")",
        f"echo Starting Windows Setup from {share}\\{folder} ...",
        *unattended,
        f"Z:\\{folder}\\{setup.replace('/', chr(92))}{setup_args}",
        "if errorlevel 1 (",
        "  echo Windows Setup could not be started. Update the PXE Boot Server and try again.",
        ")",
        "cmd /k",
        "",
    ])
    return {"winpeshl.ini": winpeshl, "install.cmd": install, **files}
