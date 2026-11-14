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
