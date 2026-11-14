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
