import json
import xml.etree.ElementTree as ET

from app import answers, ipxe, windows

NS = {"u": "urn:schemas-microsoft-com:unattend"}
CTX = {"mac": "52:54:00:12:34:56", "platform": "efi", "arch": "x86_64", "manufacturer": "", "product": "",
       "serial": "", "hostname": ""}

WIMINFO = """WIM Information:
----------------
Path:           /x/sources/install.wim
Image Count:    2

Available Images:
-----------------
Index:                  1
Name:                   Windows 11 Home
Description:            Windows 11 Home
Architecture:           x86_64
Edition ID:             Core
Languages:              nl-NL
Default Language:       nl-NL

Index:                  2
Name:                   Windows 11 Pro
Architecture:           x86_64
Edition ID:             Professional
Languages:              nl-NL
Default Language:       nl-NL
"""


def test_parse_wiminfo():
    eds = windows.parse_wiminfo(WIMINFO)
    assert [e["index"] for e in eds] == [1, 2]
    assert eds[1]["name"] == "Windows 11 Pro"
    assert eds[1]["edition"] == "Professional"
    assert eds[1]["lang"] == "nl-NL"


def test_computer_name():
    assert windows.computer_name("kantoor-pc-01.example") == "KANTOOR-PC-01-E"
    assert windows.computer_name("12345") == "PC-12345"
    assert windows.computer_name("") == "PC"
    assert len(windows.computer_name("a" * 40)) == 15


def test_encode_password():
    assert windows.encode_password("geheim") == "ZwBlAGgAZQBpAG0AUABhAHMAcwB3AG8AcgBkAA=="


def _render(platform="efi", **data):
    d = dict(answers.DEFAULT_DATA, **{"hostname": "ws01", "win_index": "2", "win_password": windows.encode_password("x"), **data})
    ed = {"index": 2, "name": "Windows 11 Pro", "edition": "Professional", "lang": "nl-NL", "arch": "x86_64"}
    xml = windows.render(d, ed, platform, "http://10.0.0.5/api/callback/1/525400123456/done", "Office -- PCs & co")
    return xml, ET.fromstring(xml.encode())


def test_xml_ask_disk_is_valid_and_has_no_disk_config():
    xml, root = _render()
    assert root.find(".//u:DiskConfiguration", NS) is None
    assert root.find(".//u:InstallTo", NS) is None
    assert root.find(".//u:ComputerName", NS).text == "WS01"
    assert root.find(".//u:InstallFrom/u:MetaData/u:Value", NS).text == "2"
    assert root.find(".//u:ProductKey/u:Key", NS).text == windows.GENERIC_KEYS["professional"]
    assert "BypassTPMCheck" in xml and "BypassNRO" in xml
    assert "/api/callback/1/" in xml


def test_xml_wipe_efi_uses_gpt_layout():
    _, root = _render("efi", win_disk="wipe", win_disk_id="1")
    types = [e.text for e in root.findall(".//u:CreatePartition/u:Type", NS)]
    assert types == ["EFI", "MSR", "Primary"]
    assert root.find(".//u:InstallTo/u:PartitionID", NS).text == "3"
    assert root.find(".//u:Disk/u:DiskID", NS).text == "1"


def test_xml_wipe_bios_uses_mbr_layout():
    _, root = _render("pcbios", win_disk="wipe")
    types = [e.text for e in root.findall(".//u:CreatePartition/u:Type", NS)]
    assert types == ["Primary", "Primary"]
    assert root.find(".//u:InstallTo/u:PartitionID", NS).text == "2"


def test_xml_without_bypass_and_with_first_logon():
    xml, root = _render(win_bypass=False, post_command="echo a & echo b\nwinget install 7zip.7zip")
    assert "LabConfig" not in xml
    cmds = [e.text for e in root.findall(".//u:FirstLogonCommands/u:SynchronousCommand/u:CommandLine", NS)]
    assert cmds == ["cmd /c echo a & echo b", "cmd /c winget install 7zip.7zip"]


def test_custom_key_wins():
    _, root = _render(win_key="aaaaa-bbbbb-ccccc-ddddd-eeeee")
    assert root.find(".//u:ProductKey/u:Key", NS).text == "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"


WIN_TREE = {"sources/boot.wim": "", "sources/install.wim": "", "boot/bcd": "", "boot/boot.sdi": "", "setup.exe": ""}


def _profile(iso_id):
    d = dict(answers.DEFAULT_DATA, hostname="ws01", win_password=windows.encode_password("x"))
    return {"id": 9, "iso_id": iso_id, "format": "autounattend", "data": json.dumps(d), "name": "win",
            "status": "ready", "updated": 1700000000.0, "created": 1700000000.0}


def test_windows_boot_with_answer_adds_initrd(iso_row):
    row = iso_row(WIN_TREE)
    s = ipxe.boot_script("10.0.0.5", row, CTX, _profile(row["id"]))
    assert "initrd -n autounattend.xml" in s and "answer=9" in s


def test_install_cmd_runs_setup_unattended(iso_row):
    row = iso_row(WIN_TREE)
    files = ipxe.winpe_files("10.0.0.5", row, CTX, _profile(row["id"]))
    cmd = files["install.cmd"]
    assert "/unattend:X:\\Windows\\System32\\autounattend.xml" in cmd
    assert "reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassTPMCheck" in cmd
    ET.fromstring(files["autounattend.xml"].encode())


def test_answers_render_autounattend(iso_row):
    row = iso_row(WIN_TREE)
    files = answers.render(_profile(row["id"]), "http://10.0.0.5", "52:54:00:12:34:56")
    assert list(files) == ["autounattend.xml"]


def test_password_is_encoded_once():
    xml, root = _render()
    assert root.find(".//u:LocalAccount/u:Password/u:Value", NS).text == windows.encode_password("x")


def test_linked_machine_name_becomes_computer_name(iso_row):
    from app import db
    row = iso_row(WIN_TREE)
    db.run("INSERT INTO machines(mac,name,created) VALUES(?,?,?)", "52:54:00:aa:bb:cc", "Receptie laptop", 1.0)
    xml = answers.render(_profile(row["id"]), "http://10.0.0.5", "52:54:00:aa:bb:cc")["autounattend.xml"]
    assert "<ComputerName>RECEPTIE-LAPTOP</ComputerName>" in xml
