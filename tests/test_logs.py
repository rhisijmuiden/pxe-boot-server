import json

from app import config, logs


def _j(ident, msg, ts=1_700_000_000_000_000):
    return json.dumps({"SYSLOG_IDENTIFIER": ident, "MESSAGE": msg, "__REALTIME_TIMESTAMP": str(ts)})


JOURNAL = "\n".join([
    _j("dnsmasq-tftp", "sent /srv/pxe/tftp/snponly.efi to 10.0.0.42"),
    _j("dnsmasq-tftp", "file /srv/pxe/tftp/foo.efi not found for 10.0.0.43"),
    _j("dnsmasq-dhcp", "PXE(eth0) 10.0.0.42 52:54:00:aa:bb:cc snponly.efi"),
    _j("dnsmasq", "started, version 2.91 DNS disabled"),
    "not json",
])


def test_parse_journal():
    e = logs.parse_journal(JOURNAL)
    assert [x["kind"] for x in e] == ["tftp", "tftp", "dhcp", "dnsmasq"]
    assert e[0]["ok"] and e[0]["file"] == "snponly.efi" and e[0]["ip"] == "10.0.0.42"
    assert not e[1]["ok"] and e[1]["ip"] == "10.0.0.43"
    assert e[2]["mac"] == "52:54:00:aa:bb:cc"
    assert e[0]["ts"] == 1_700_000_000


def test_dnsmasq_filters_kind_and_query():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "dnsmasq.journal.json").write_text(JOURNAL)
    tftp, err = logs.dnsmasq("tftp")
    assert err == "" and len(tftp) == 2 and tftp[0]["ip"] == "10.0.0.43"  # newest first
    dhcp, _ = logs.dnsmasq("dhcp", q="52:54")
    assert len(dhcp) == 1


def test_http_log():
    config.NGINX_ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    config.NGINX_ACCESS_LOG.write_text(
        '1700000000.123 10.0.0.42 200 1234 "/ipxe/menu.ipxe"\n'
        '1700000001.000 10.0.0.42 206 99999 "/mnt/3/casper/vmlinuz"\n'
        '1700000002.000 10.0.0.9 200 10 "/api/live"\n'
        'garbage\n')
    rows = logs.http()
    assert [r["path"] for r in rows] == ["/mnt/3/casper/vmlinuz", "/ipxe/menu.ipxe"]
    assert len(logs.http(all_requests=True)) == 3
    assert [r["ip"] for r in logs.http(q="10.0.0.9")] == ["10.0.0.9"]
