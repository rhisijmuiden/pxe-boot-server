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


# ---------------------------------------------------------------- look and feel

# Text area of the framebuffer console, inside the frame drawn in static/img/ipxe-bg.png.
SCREEN = {"x": 1024, "y": 768, "left": 352, "right": 52, "top": 56, "bottom": 92}
ROW_WIDTH = 54  # characters per menu line; meta text is right aligned, clear of the countdown
TOOL_TAGS = ("MEM", "CZ", "GP")


def theme_lines(b: str) -> list[str]:
    """RHIS look: background picture on UEFI (framebuffer console), brand colours everywhere.
    Every command is optional (`||`): BIOS builds have no framebuffer and some firmware
    cannot do 1024x768, then the menu falls back to coloured text."""
    s = SCREEN
    return [
        "isset ${pxe_theme} && goto theme_done ||",
        "set pxe_theme text",
        f"iseq ${{platform}} efi && console --x {s['x']} --y {s['y']} --left {s['left']} --right {s['right']} "
        f"--top {s['top']} --bottom {s['bottom']} --picture {b}/static/img/ipxe-bg.png && set pxe_theme fb ||",
        "colour --rgb 0x14213d 4 ||",   # blue    -> nacht
        "colour --rgb 0xff6b57 1 ||",   # red     -> koraal
        "colour --rgb 0x9cc3ff 6 ||",   # cyan    -> lucht
        "colour --rgb 0xe6ecf7 7 ||",   # white   -> wolk
        "colour --rgb 0xb8392a 3 ||",   # yellow  -> koraal, dark (selected item; iPXE draws it bold,
                                        #            which lightens the text, so it needs a dark ground)
        "cpair --foreground 6 --background 9 0 ||",  # default (title, text): lucht, transparent
        "cpair --foreground 7 --background 9 1 ||",  # menu items
        "cpair --foreground 7 --background 3 2 ||",  # selected item: white on dark koraal
        "cpair --foreground 1 --background 9 3 ||",  # section headers: koraal
        "cpair --foreground 7 --background 4 4 ||",  # edit boxes
        "cpair --foreground 1 --background 9 5 ||",  # alerts
        ":theme_done",
    ]


# iPXE splits command lines on spaces, so runs of spaces collapse (and a no-break space breaks the
# menu layout). Dot leaders line up the right column instead.
def _row(label: str, meta: str = "") -> str:
    label = _txt(label)
    meta = _txt(meta)
    if not meta:
        return label
    room = ROW_WIDTH - len(meta) - 2
    if len(label) > room - 2:
        label = label[:max(0, room - 3)] + "~"
    return f"{label} {'.' * (room - len(label))} {meta}"


def _kv(key: str, value: str) -> str:
    return f"item --gap -- {key} {'.' * (14 - len(key))} {value}"


def _gap(text: str = "") -> str:
    """A section header or text line."""
    return f"item --gap -- {_txt(text)}" if text.strip() else "item --gap --"


def menu_script(server_ip: str, ctx: dict, machine=None) -> str:
    """The interactive menu. `machine` is the boot rule (rules.effective) or a machine row; only a
    'default' rule matters here, 'once'/'always' rules get autoboot_script()."""
    b = base(server_ip)
    platform = ctx.get("platform", "pcbios")
    q = client_query(ctx)
    title = _txt(db.get_setting("menu_title") or db.get_setting("server_name") or "PXE Boot Server")
    timeout = int(db.get_setting("menu_timeout") or 0)
    default = str(db.get_setting("menu_default") or "local")
    if machine is not None and machine["mode"] == "default" and machine["iso_id"]:
        default = f"{machine['iso_id']}" + (f"a{machine['answer_id']}" if machine["answer_id"] else "")

    rows = [r for r in isos.list_isos() if r["enabled"] and r["status"] == "ready"]
    by_id = {r["id"]: r for r in rows}
    ready_answers = [a for a in answers_mod.list_answers() if a["status"] == "ready" and a["iso_id"] in by_id]

    tree: dict[str, list] = {}
    for r in rows:
        folder = r["relpath"].rsplit("/", 1)[0] if "/" in r["relpath"] else ""
        tree.setdefault(folder, []).append(r)

    def label(r, a=None) -> str:
        return f"iso{r['id']}" + (f"a{a['id']}" if a else "")

    def fw_meta(r) -> str:
        fw = isos.METHODS.get(isos.method_of(r), {}).get("fw", [])
        if not platform_ok(isos.method_of(r), platform):
            return "BIOS only"
        return "UEFI+BIOS" if len(fw) == 2 else fw[0].upper() if fw else ""

    def iso_item(r) -> str:
        return f"item {label(r)} {_row(r['name'], fw_meta(r))}"

    folder_names = sorted({f for f in tree if f} | {p for f in tree if f for p in _parents(f)}, key=str.lower)

    def children(parent: str) -> list[str]:
        return [f for f in folder_names if (f.rsplit("/", 1)[0] if "/" in f else "") == parent]

    def count(f: str) -> int:
        return sum(len(v) for k, v in tree.items() if k == f or k.startswith(f + "/"))

    def flabel(f: str) -> str:
        return "dir_" + re.sub(r"[^A-Za-z0-9]", "_", f)

    def folder_item(f: str) -> str:
        n = count(f)
        return f"item {flabel(f)} {_row(f.rsplit('/', 1)[-1] + '/', f'{n} image' + ('s' if n != 1 else '') + '  >')}"

    def is_tool(r) -> bool:
        return isos.os_tag(r) in TOOL_TAGS or isos.method_of(r) == "memdisk"

    lines = ["#!ipxe", f"set server {b}", *theme_lines(b), "set menu-timeout " + str(timeout * 1000),
             "iseq ${platform} efi && set fwname UEFI || set fwname BIOS", "", ":start"]
    lines.append(f"menu {title}  //  ${{fwname}}  //  ${{netX/ip}}  //  ${{netX/mac}}")

    if machine is not None:
        m = machine.get("machine") if isinstance(machine, dict) else machine
        g = machine.get("group") if isinstance(machine, dict) else None
        who = []
        if m is not None and m["name"]:
            who.append(f"Recognised as {m['name']}")
        if g is not None:
            who.append(f"group {g['name']}")
        if who:
            lines += [_gap("// THIS MACHINE"), _gap("  " + "  -  ".join(who))]

    if ready_answers:
        lines.append(_gap("// UNATTENDED INSTALLS"))
        for a in ready_answers:
            r = by_id[a["iso_id"]]
            lines.append(f"item {label(r, a)} {_row(a['name'] + ' - ' + r['name'], 'AUTO')}")
    top = children("")
    root = tree.get("", [])
    systems = [r for r in root if not is_tool(r)]
    tools = [r for r in root if is_tool(r)]
    if top or systems:
        lines.append(_gap("// OPERATING SYSTEMS" if systems else "// FOLDERS"))
        lines += [folder_item(f) for f in top]
        lines += [iso_item(r) for r in systems]
    if tools:
        lines.append(_gap("// TOOLS"))
        lines += [iso_item(r) for r in tools]
    lines += [
        _gap("// SYSTEM"),
        f"item --key l local {_row('Boot from local disk', 'DEFAULT' if default == 'local' else '')}",
        f"item info {_row('System info', 'HOST')}",
        "item shell iPXE shell",
        "item reboot Reboot",
    ]
    default_label = "local" if default == "local" else f"iso{default}"
    if timeout > 0:
        names = {"local": "Boot from local disk"}
        for r in rows:
            names[f"iso{r['id']}"] = r["name"]
        for a in ready_answers:
            names[f"iso{a['iso_id']}a{a['id']}"] = a["name"]
        lines += [_gap(), _gap(f"  Auto-boot: {names.get(default_label, 'Boot from local disk')} after {timeout} s")]
        lines.append(f"choose --default {default_label} --timeout ${{menu-timeout}} target && goto ${{target}} || goto local")
    else:
        lines.append(f"choose --default {default_label} target && goto ${{target}} || goto local")
    lines.append("")

    for f in folder_names:
        parent = f.rsplit("/", 1)[0] if "/" in f else ""
        back = flabel(parent) if parent else "start"
        lines.append(f":{flabel(f)}")
        lines.append(f"menu {title}  //  {_txt(f)}/")
        subs, here_rows = children(f), tree.get(f, [])
        if subs:
            lines.append(_gap("// FOLDERS"))
            lines += [folder_item(c) for c in subs]
        if here_rows:
            lines.append(_gap("// IMAGES"))
            lines += [iso_item(r) for r in here_rows]
        lines += [_gap(), f"item --key b {back} {_row('< Back', 'ESC')}"]
        here = {f"iso{r['id']}" for r in here_rows}
        dflt = f"--default {default_label} " if default_label.split("a")[0] in here else ""
        lines.append(f"choose {dflt}target && goto ${{target}} || goto {back}")
        lines.append("")

    for r in rows:
        url = f"{b}/ipxe/iso/{r['id']}.ipxe?{q}"
        lines.append(f":{label(r)}\nchain --autofree {url} || goto failed\ngoto start")
    for a in ready_answers:
        r = by_id[a["iso_id"]]
        url = f"{b}/ipxe/iso/{r['id']}.ipxe?{q}&answer={a['id']}"
        lines.append(f":{label(r, a)}\nchain --autofree {url} || goto failed\ngoto start")
    lines += [
        "",
        ":local",
        "echo Booting from local disk ...",
        "exit 0",
        ":info",
        "menu System info",
        "item --gap -- // HARDWARE",
        _kv("Manufacturer", "${manufacturer}"),
        _kv("Product", "${product}"),
        _kv("Serial", "${serial}"),
        _kv("Firmware", "${fwname} ${buildarch}"),
        "item --gap --",
        "item --gap -- // NETWORK",
        _kv("MAC", "${netX/mac}"),
        _kv("IP", "${netX/ip} / ${netX/netmask}"),
        _kv("Gateway", "${netX/gateway}"),
        _kv("Boot server", "${server}"),
        "item --gap --",
        "item --key b start < Back",
        "choose target || goto start",
        "goto start",
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


AUTO_COUNTDOWN = 5


def autoboot_script(server_ip: str, ctx: dict, rule: dict, row, answer=None) -> str:
    """A machine with a once/always rule: show what is about to happen for a few seconds, then boot.
    Any key stops the countdown; the full menu stays one choice away."""
    b = base(server_ip)
    q = client_query(ctx)
    m, g = rule.get("machine"), rule.get("group")
    who = []
    if m is not None and m["name"]:
        who.append(f"Recognised as {m['name']}")
    if g is not None:
        who.append(f"group {g['name']}")
    mode = {"once": "next boot once", "always": "every boot"}.get(rule["mode"], rule["mode"])
    target = _txt(row["name"] + (f" - {answer['name']}" if answer is not None else ""))
    aq = f"&answer={answer['id']}" if answer is not None else ""
    lines = ["#!ipxe", f"set server {b}", *theme_lines(b),
             "iseq ${platform} efi && set fwname UEFI || set fwname BIOS", ":start",
             f"menu {_txt(db.get_setting('menu_title') or db.get_setting('server_name') or 'PXE Boot Server')}"
             "  //  ${fwname}  //  ${netX/ip}  //  ${netX/mac}",
             _gap("// THIS MACHINE")]
    if who:
        lines.append(_gap("  " + "  -  ".join(who)))
    lines += [_gap(f"  Rule: {mode}" + (" (group)" if rule["source"] == "group" else "")), _gap(),
              f"item auto {_row(target, 'NEXT BOOT' if rule['mode'] == 'once' else 'EVERY BOOT')}",
              f"item --key l local {_row('Boot from local disk')}",
              f"item --key m full {_row('Show the full menu', '>')}"]
    if answer is not None:
        lines += [_gap(), _gap("// INSTALL PLAN"), _gap("  Unattended install: " + _txt(answer["name"])),
                  _gap("  The disk of this machine will be wiped.")]
    lines += [_gap(), _gap(f"  Starts automatically in {AUTO_COUNTDOWN} s. Use the arrow keys to stop.")]
    lines += [
        f"choose --default auto --timeout {AUTO_COUNTDOWN * 1000} target && goto ${{target}} || goto local",
        ":auto",
        f"chain --autofree {b}/ipxe/iso/{row['id']}.ipxe?{q}{aq}&auto=1 || goto failed",
        "goto start",
        ":full",
        f"chain --autofree {b}/ipxe/menu.ipxe?{q}&manual=1 || goto failed",
        "goto start",
        ":local",
        "echo Booting from local disk ...",
        "exit 0",
        ":failed",
        "echo Boot failed: ${errno}",
        "prompt --timeout 15000 Press any key to return ... ||",
        "goto start",
        "",
    ]
    return "\n".join(lines)


def autoboot_example(server_ip: str, ctx: dict) -> str:
    """The auto-boot screen with an example machine, for the preview in the web interface.
    Prefers an unattended install, so the install plan shows too."""
    rows = {r["id"]: r for r in isos.list_isos() if r["enabled"] and r["status"] == "ready"}
    answer = next((a for a in answers_mod.list_answers() if a["status"] == "ready" and a["iso_id"] in rows), None)
    if answer is not None:
        row = rows[answer["iso_id"]]
    elif rows:
        row = next(iter(sorted(rows.values(), key=lambda r: r["name"].lower())))
    else:
        row = {"id": 0, "name": "Ubuntu Server 24.04"}
    rule = {"machine": {"name": "example-pc"}, "group": None, "mode": "once", "source": "machine"}
    return autoboot_script(server_ip, ctx, rule, row, answer)


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
    shim = isos.ci_find(isos.mount_dir(row["id"]), "efi/boot/bootx64.efi") if method in SHIM_METHODS else None
    if shim:
        # Secure Boot: let the distribution's own Microsoft-signed shim verify its kernel. The shim
        # command does nothing without Secure Boot, and `||` keeps builds without it working.
        body.append(f"iseq ${{platform}} efi && shim {m(shim)} ||")
    return "\n".join(head + body + fail)


SHIM_METHODS = ("ubuntu", "debian_live", "rhel", "fedora_live", "proxmox")


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
