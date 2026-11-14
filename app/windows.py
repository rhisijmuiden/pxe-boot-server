"""Windows unattended installs: editions in an ISO and autounattend.xml.

The XML is handed to WinPE by wimboot (it lands in X:\\Windows\\System32) and install.cmd
starts Setup with /unattend. One profile works for UEFI and Legacy BIOS: the disk layout is
chosen per client from the firmware type iPXE reports.
"""
from __future__ import annotations

import base64
import glob
import logging
import re
import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger("pxe.windows")

# Microsoft's public generic installation keys. They select the edition and let Setup run
# without asking; they do not activate Windows.
GENERIC_KEYS = {
    "professional": "VK7JG-NPHTM-C97JM-9MPGT-3V66T",
    "professionaln": "2B87N-8KFHP-DKV6R-Y2C8J-PKCKT",
    "core": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7",
    "coren": "4CPRK-NM3K3-X6XXQ-RXX86-WXCHW",
    "coresinglelanguage": "BT79Q-G7N6G-PGBYW-4YWX6-6F4BT",
    "education": "YNMGQ-8RYV3-4PGQ3-C8XTP-7CFBY",
    "educationn": "84NGF-MHBT6-FXBX8-QWJK7-DRR8H",
    "enterprise": "XGVPP-NMH47-7TTHJ-W3FW7-8HV2C",
    "enterprisen": "WGGHN-J84D6-QYCPR-T7PJ7-X766F",
    "professionalworkstation": "DXG7C-N36C4-C4HTG-X4T3X-2YV77",
    "professionaleducation": "8PTT6-RNW4C-6V7J2-C2D3X-MHBPB",
}

# IANA zone (as used by the Linux profiles) -> Windows time zone ID
TIMEZONES = {
    "Europe/Amsterdam": "W. Europe Standard Time", "Europe/Berlin": "W. Europe Standard Time",
    "Europe/Brussels": "Romance Standard Time", "Europe/Paris": "Romance Standard Time",
    "Europe/London": "GMT Standard Time", "UTC": "UTC",
    "America/New_York": "Eastern Standard Time", "America/Los_Angeles": "Pacific Standard Time",
    "Asia/Singapore": "Singapore Standard Time", "Australia/Sydney": "AUS Eastern Standard Time",
}

# keyboard choice of the wizard -> Windows input locale (language:layout)
KEYBOARDS = {
    "us": "0409:00000409", "us-intl": "0409:00020409", "gb": "0809:00000809",
    "be": "0813:0000080c", "de": "0407:00000407", "fr": "040c:0000040c",
}


# ---------------------------------------------------------------- editions

def editions(root: Path, install_rel: str) -> list[dict]:
    """Images in install.wim/.esd/.swm, read with wimlib. Empty when wimlib is missing."""
    exe = shutil.which("wimlib-imagex")
    if not exe or not install_rel:
        return []
    path = root / install_rel
    cmd = [exe, "info", str(path)]
    if path.suffix.lower() == ".swm":
        # split image: install.swm, install2.swm, ... (wimlib expands the glob itself)
        stem = re.sub(r"\d*$", "", path.stem)
        cmd.append(f"--ref={glob.escape(str(path.parent))}/{stem}*.swm")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("wimlib on %s: %s", path, exc)
        return []
    return parse_wiminfo(out)


def parse_wiminfo(text: str) -> list[dict]:
    images, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z ]+?):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if key == "index":
            if val.isdigit():
                cur = {"index": int(val), "name": "", "edition": "", "lang": "", "arch": ""}
                images.append(cur)
            continue
        if cur is None:
            continue
        if key == "name" and not cur["name"]:
            cur["name"] = val
        elif key == "edition id":
            cur["edition"] = val
        elif key == "default language":
            cur["lang"] = val
        elif key == "languages" and not cur["lang"]:
            cur["lang"] = val.split()[0] if val else ""
        elif key == "architecture":
            cur["arch"] = val
    return [i for i in images if i["name"]]


# ---------------------------------------------------------------- helpers

def computer_name(name: str) -> str:
    """Windows rules: 1-15 characters, letters, digits and hyphens, not only digits."""
    n = re.sub(r"[^A-Za-z0-9-]", "-", (name or "").strip()).strip("-")[:15].strip("-")
    if not n or n.isdigit():
        n = ("PC-" + n)[:15] if n else "PC"
    return n.upper()


def encode_password(pw: str, kind: str = "Password") -> str:
    """The unattend 'PlainText=false' form: base64 of UTF-16LE(password + element name).
    It only hides the password from a glance; it is not encryption."""
    return base64.b64encode((pw + kind).encode("utf-16-le")).decode()


def user_locale(locale: str) -> str:
    return (locale or "en_US.UTF-8").split(".")[0].replace("_", "-")


def product_key(d: dict, edition: str) -> str:
    key = (d.get("win_key") or "").strip().upper()
    if re.fullmatch(r"([A-Z0-9]{5}-){4}[A-Z0-9]{5}", key):
        return key
    return GENERIC_KEYS.get((edition or "").lower(), "")


# ---------------------------------------------------------------- autounattend.xml

def _component(name: str, body: str, arch: str) -> str:
    return (f'    <component name="{name}" processorArchitecture="{arch}" publicKeyToken="31bf3856ad364e35" '
            f'language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">\n'
            f"{body}    </component>\n")


def _disk_xml(d: dict, platform: str) -> tuple[str, str]:
    """DiskConfiguration and InstallTo. Empty when Setup should ask for the disk."""
    if d.get("win_disk") != "wipe":
        return "", ""
    disk = int(d.get("win_disk_id") or 0)
    if platform == "efi":
        parts = [("1", "EFI", "<Size>300</Size>"), ("2", "MSR", "<Size>16</Size>"),
                 ("3", "Primary", "<Extend>true</Extend>")]
        mods = [("1", "FAT32", "System", ""), ("3", "NTFS", "Windows", "<Letter>C</Letter>")]
        target = 3
    else:
        parts = [("1", "Primary", "<Size>500</Size>"), ("2", "Primary", "<Extend>true</Extend>")]
        mods = [("1", "NTFS", "System", "<Active>true</Active>"), ("2", "NTFS", "Windows", "<Letter>C</Letter>")]
        target = 2
    create = "".join(
        f'            <CreatePartition wcm:action="add"><Order>{o}</Order><Type>{t}</Type>{s}</CreatePartition>\n'
        for o, t, s in parts)
    modify = "".join(
        f'            <ModifyPartition wcm:action="add"><Order>{i + 1}</Order><PartitionID>{p}</PartitionID>'
        f"<Format>{f}</Format><Label>{lbl}</Label>{extra}</ModifyPartition>\n"
        for i, (p, f, lbl, extra) in enumerate(mods))
    cfg = ("      <DiskConfiguration>\n"
           f'        <Disk wcm:action="add"><DiskID>{disk}</DiskID><WillWipeDisk>true</WillWipeDisk>\n'
           f"          <CreatePartitions>\n{create}          </CreatePartitions>\n"
           f"          <ModifyPartitions>\n{modify}          </ModifyPartitions>\n"
           "        </Disk>\n      </DiskConfiguration>\n")
    to = f"<InstallTo><DiskID>{disk}</DiskID><PartitionID>{target}</PartitionID></InstallTo>"
    return cfg, to


def _commands(tag: str, item: str, cmds: list[str], extra: str = "") -> str:
    if not cmds:
        return ""
    rows = "".join(
        f'        <{item} wcm:action="add"><Order>{n}</Order>{extra}'
        f"<{'CommandLine' if item == 'SynchronousCommand' else 'Path'}>{escape(c)}"
        f"</{'CommandLine' if item == 'SynchronousCommand' else 'Path'}></{item}>\n"
        for n, c in enumerate(cmds, 1))
    return f"      <{tag}>\n{rows}      </{tag}>\n"


def render(d: dict, edition: dict | None, platform: str, callback: str, title: str) -> str:
    arch = "arm64" if (edition or {}).get("arch", "").lower() in ("arm64", "aarch64") else "amd64"
    lang = (edition or {}).get("lang") or "en-US"
    ulocale = user_locale(d.get("locale", ""))
    kb = KEYBOARDS.get(d.get("keyboard", "us"), "0409:00000409")
    tz = TIMEZONES.get(d.get("timezone", ""), "UTC")
    name = computer_name(d.get("hostname", ""))
    user = re.sub(r'[\\/\[\]:;|=,+*?<>"@]', "", d.get("win_user") or "Admin")[:20] or "Admin"
    pw = d.get("win_password") or ""  # stored already encoded (see encode_password)
    index = int(d.get("win_index") or 1)
    key = product_key(d, (edition or {}).get("edition", ""))
    disk_cfg, install_to = _disk_xml(d, platform)

    # windowsPE: language of Setup, disk, image and key
    pe_intl = (f"      <SetupUILanguage><UILanguage>{lang}</UILanguage></SetupUILanguage>\n"
               f"      <InputLocale>{kb}</InputLocale><SystemLocale>{ulocale}</SystemLocale>\n"
               f"      <UILanguage>{lang}</UILanguage><UserLocale>{ulocale}</UserLocale>\n")
    key_xml = f"<Key>{key}</Key>" if key else "<Key></Key>"
    pe_setup = (disk_cfg +
                "      <ImageInstall><OSImage>\n"
                f"        <InstallFrom><MetaData wcm:action=\"add\"><Key>/IMAGE/INDEX</Key><Value>{index}</Value>"
                "</MetaData></InstallFrom>\n"
                f"        {install_to}<WillShowUI>OnError</WillShowUI>\n"
                "      </OSImage></ImageInstall>\n"
                f"      <UserData><AcceptEula>true</AcceptEula><ProductKey>{key_xml}<WillShowUI>OnError</WillShowUI>"
                "</ProductKey></UserData>\n")
    if d.get("win_bypass", True):
        pe_setup += _commands("RunSynchronous", "RunSynchronousCommand", [
            rf"reg add HKLM\SYSTEM\Setup\LabConfig /v {v} /t REG_DWORD /d 1 /f"
            for v in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck")])

    # specialize: name, time zone, no Microsoft account requirement, tell the server
    spec_shell = (f"      <ComputerName>{name}</ComputerName>\n"
                  f"      <TimeZone>{escape(tz)}</TimeZone>\n"
                  f"      <RegisteredOwner>{escape(title)}</RegisteredOwner>\n")
    spec_cmds = [r"reg add HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE /v BypassNRO /t REG_DWORD /d 1 /f"]
    if callback:
        spec_cmds.append(f'cmd /c "for /l %i in (1,1,10) do (curl.exe -fsS -m 5 {callback} && exit 0 '
                         f'|| ping -n 6 127.0.0.1 >nul)"')
    spec_deploy = _commands("RunSynchronous", "RunSynchronousCommand", spec_cmds)

    # oobeSystem: local administrator, skip the questions
    pw_xml = (f"<Password><Value>{escape(pw)}</Value><PlainText>false</PlainText></Password>"
              if pw else "<Password><Value></Value><PlainText>true</PlainText></Password>")
    first = []
    for line in (d.get("post_command") or "").splitlines():
        if line.strip():
            first.append(f"cmd /c {line.strip()}")
    first_xml = ""
    if first:
        rows = "".join(
            f'        <SynchronousCommand wcm:action="add"><Order>{n}</Order>'
            f"<CommandLine>{escape(c)}</CommandLine></SynchronousCommand>\n" for n, c in enumerate(first, 1))
        first_xml = f"      <FirstLogonCommands>\n{rows}      </FirstLogonCommands>\n"
    oobe_shell = ("      <OOBE><HideEULAPage>true</HideEULAPage><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>"
                  "<HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true"
                  "</HideWirelessSetupInOOBE><ProtectYourPC>3</ProtectYourPC><SkipMachineOOBE>false"
                  "</SkipMachineOOBE></OOBE>\n"
                  "      <UserAccounts><LocalAccounts>\n"
                  f'        <LocalAccount wcm:action="add"><Name>{escape(user)}</Name><DisplayName>{escape(user)}'
                  f"</DisplayName><Group>Administrators</Group>{pw_xml}</LocalAccount>\n"
                  "      </LocalAccounts></UserAccounts>\n" + first_xml)
    oobe_intl = (f"      <InputLocale>{kb}</InputLocale><SystemLocale>{ulocale}</SystemLocale>\n"
                 f"      <UILanguage>{lang}</UILanguage><UserLocale>{ulocale}</UserLocale>\n")

    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            f"<!-- Generated by PXE Boot Server - {escape(title).replace('--', '- -')} ({'UEFI' if platform == 'efi' else 'Legacy BIOS'}) -->\n"
            '<unattend xmlns="urn:schemas-microsoft-com:unattend">\n'
            '  <settings pass="windowsPE">\n'
            + _component("Microsoft-Windows-International-Core-WinPE", pe_intl, arch)
            + _component("Microsoft-Windows-Setup", pe_setup, arch)
            + "  </settings>\n"
            '  <settings pass="specialize">\n'
            + _component("Microsoft-Windows-Shell-Setup", spec_shell, arch)
            + _component("Microsoft-Windows-Deployment", spec_deploy, arch)
            + "  </settings>\n"
            '  <settings pass="oobeSystem">\n'
            + _component("Microsoft-Windows-International-Core", oobe_intl, arch)
            + _component("Microsoft-Windows-Shell-Setup", oobe_shell, arch)
            + "  </settings>\n"
            "</unattend>\n")
