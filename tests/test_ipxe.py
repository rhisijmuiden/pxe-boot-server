from app import ipxe

CTX = {"mac": "52:54:00:12:34:56", "platform": "efi", "arch": "x86_64", "manufacturer": "", "product": "",
       "serial": "", "hostname": ""}


def test_entry_script_points_to_menu():
    s = ipxe.entry_script("10.0.0.5")
    assert s.startswith("#!ipxe")
    assert "http://10.0.0.5/ipxe/menu.ipxe" in s


def test_menu_lists_isos_and_folders(iso_row):
    a = iso_row({"casper/vmlinuz": "", "casper/initrd": ""}, "Linux/ubuntu-24.04.iso")
    b = iso_row({"memtest.bin": ""}, "tools.iso")
    s = ipxe.menu_script("10.0.0.5", CTX)
    assert s.startswith("#!ipxe")
    assert "Linux" in s
    assert f"iso/{b['id']}.ipxe" in s or f"iso{b['id']}" in s


def test_ubuntu_boot_script_uses_http(iso_row):
    row = iso_row({"casper/vmlinuz": "", "casper/initrd": ""})
    s = ipxe.boot_script("10.0.0.5", row, CTX)
    assert f"http://10.0.0.5/mnt/{row['id']}/casper/vmlinuz" in s
    assert "url=http://10.0.0.5/isos/" in s


def test_windows_boot_uses_wimboot(iso_row):
    row = iso_row({"sources/boot.wim": "", "boot/bcd": "", "boot/boot.sdi": "", "setup.exe": ""})
    s = ipxe.boot_script("10.0.0.5", row, CTX)
    assert "/boot/wimboot" in s and "boot.wim" in s
    files = ipxe.winpe_files("10.0.0.5", row, CTX)
    assert "\\\\10.0.0.5\\pxe" in files["install.cmd"]
    assert files["install.cmd"].count("\r\n") > 5


def test_unknown_iso_uses_sanboot(iso_row):
    row = iso_row({"x.bin": ""})
    assert "sanboot" in ipxe.boot_script("10.0.0.5", row, CTX)


def test_entry_script_exits_after_menu():
    # "Boot from local disk" returns from the menu; the entry script must hand back to the firmware.
    lines = ipxe.entry_script("10.0.0.5").splitlines()
    chain = next(i for i, line in enumerate(lines) if line.startswith("chain "))
    assert lines[chain + 1:chain + 3][-1].strip() == "exit 0" or "exit 0" in lines[chain + 1:chain + 3]


def test_embedded_script_exits_after_menu():
    from pathlib import Path
    embed = (Path(__file__).resolve().parent.parent / "ipxe" / "embed.ipxe").read_text().splitlines()
    chain = next(i for i, line in enumerate(embed) if "/ipxe/boot.ipxe" in line)
    assert "exit 0" in [x.strip() for x in embed[chain + 1:chain + 3]]


def test_windows_paths_follow_the_mount_case(iso_row):
    """Recorded paths that no longer match the mount (UDF vs ISO 9660 case) are detected again."""
    import shutil
    from app import config, isos
    row = iso_row({"sources/boot.wim": "", "boot/bcd": "", "boot/boot.sdi": "", "setup.exe": ""})
    root = config.MNT_DIR / str(row["id"])
    shutil.move(root / "boot" / "bcd", root / "boot" / "BCD")
    assert isos.missing_files(row) == ["boot/bcd"]
    fresh = isos.ensure_ready(row)
    assert isos.iso_info(fresh)["bcd"] == "boot/BCD"
    s = ipxe.boot_script("10.0.0.5", fresh, CTX)
    assert f"/mnt/{row['id']}/boot/BCD" in s


def test_boot_endpoint_reports_missing_files(iso_row):
    from fastapi.testclient import TestClient
    from app import config
    from app.main import app
    row = iso_row({"sources/boot.wim": "", "boot/bcd": "", "boot/boot.sdi": "", "setup.exe": ""})
    (config.MNT_DIR / str(row["id"]) / "sources" / "boot.wim").unlink()
    r = TestClient(app).get(f"/ipxe/iso/{row['id']}.ipxe", headers={"host": "10.0.0.5"})
    assert r.status_code == 200
    assert "kernel" not in r.text and "boot.wim" not in r.text.split("Files missing")[0]
