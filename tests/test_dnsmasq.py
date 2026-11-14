from app import db, dnsmasq


def test_proxy_mode():
    db.set_setting("dhcp_mode", "proxy")
    s = dnsmasq.render()
    assert "port=0" in s and "enable-tftp" in s
    assert 'pxe-service=x86PC,"Network boot (BIOS)",undionly.kpxe' in s
    assert "dhcp-authoritative" not in s


def test_full_mode():
    db.set_setting("dhcp_mode", "full")
    db.set_setting("dhcp_range_start", "10.0.60.100")
    db.set_setting("dhcp_range_end", "10.0.60.200")
    db.set_setting("dhcp_gateway", "10.0.60.1")
    db.set_setting("dhcp_dns", "1.1.1.1, 9.9.9.9")
    try:
        s = dnsmasq.render()
        assert "dhcp-range=10.0.60.100,10.0.60.200" in s
        assert "dhcp-option=option:dns-server,1.1.1.1,9.9.9.9" in s
        assert "dhcp-boot=tag:bios,undionly.kpxe" in s
        assert dnsmasq.validate_full_range() is None
        db.set_setting("dhcp_range_end", "10.0.60.1")
        assert dnsmasq.validate_full_range() == "err_range"
    finally:
        db.set_setting("dhcp_mode", "proxy")


def test_no_inline_comments():
    # dnsmasq does not allow comments after a value
    for line in dnsmasq.render().splitlines():
        if line and not line.startswith("#"):
            assert " #" not in line, line


def test_uefi_default_is_snponly():
    # ipxe.efi takes over USB on many PCs, so the keyboard stops working in the menu
    s = dnsmasq.render()
    assert 'pxe-service=X86-64_EFI,"Network boot (UEFI)",snponly.efi' in s


def test_migration_moves_ipxe_efi_to_snponly(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript(db.SCHEMA[0] + "PRAGMA user_version=1;")
    conn.execute("INSERT INTO settings(key,value) VALUES('uefi_loader','\"ipxe.efi\"')")
    for i, script in enumerate(db.SCHEMA[1:], start=2):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version={i};COMMIT;")
    assert conn.execute("SELECT value FROM settings WHERE key='uefi_loader'").fetchone()[0] == '"snponly.efi"'
