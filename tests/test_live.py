import time

from app import db, live


def _run_sampler(monkeypatch):
    monkeypatch.setattr(live.system, "net_counters", lambda: {})
    monkeypatch.setattr(live, "_read_nginx_log", lambda: None)
    monkeypatch.setattr(live, "_inflight", lambda: {})
    live._sample({}, time.time() - 1)


def test_windows_client_is_done_when_share_session_ends(monkeypatch):
    live.CLIENTS.clear()
    c = live.touch("10.0.0.50", "loading", mac="50:eb:f6:2e:69:2d", iso_name="Win11")
    monkeypatch.setattr(live, "_smb", {"10.0.0.50": "3/sources/install.swm"})
    monkeypatch.setattr(live, "_smb_sessions", {"10.0.0.50"})
    _run_sampler(monkeypatch)
    assert c["phase"] == "installer"

    # the PC restarted: the session is gone for longer than SMB_GONE_AFTER
    monkeypatch.setattr(live, "_smb", {})
    monkeypatch.setattr(live, "_smb_sessions", set())
    c["smb_seen"] = time.time() - live.SMB_GONE_AFTER - 5
    before = db.q1("SELECT COUNT(*) AS n FROM boot_events WHERE event='done'")["n"]
    _run_sampler(monkeypatch)
    assert c["phase"] == "done"
    assert db.q1("SELECT COUNT(*) AS n FROM boot_events WHERE event='done'")["n"] == before + 1


def test_done_clients_leave_the_list(monkeypatch):
    live.CLIENTS.clear()
    c = live.touch("10.0.0.51", "done")
    c["updated"] = time.time() - live.IDLE_LIMIT["done"] - 1
    _run_sampler(monkeypatch)
    assert "10.0.0.51" not in live.CLIENTS


def test_stuck_share_session_has_a_safety_net(monkeypatch):
    live.CLIENTS.clear()
    c = live.touch("10.0.0.52", "installer")
    c["started"] = time.time() - live.MAX_AGE - 1
    monkeypatch.setattr(live, "_smb", {"10.0.0.52": "3/sources/wpx.dll"})
    monkeypatch.setattr(live, "_smb_sessions", {"10.0.0.52"})
    _run_sampler(monkeypatch)
    assert "10.0.0.52" not in live.CLIENTS
