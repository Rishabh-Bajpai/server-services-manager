"""Tests for firewall rule preview helper (Laranode BuildUfwRuleSpec port)."""
import pytest

from app import firewall_manager as fm


def test_valid_allow():
    out = fm.build_rule_spec(action="allow", port="22", protocol="tcp", source="", direction="in")
    assert out["spec"]["port"] == "22"
    assert "allow" in out["preview"]
    assert out["lockout_warning"] is False


def test_ssh_deny_warns():
    out = fm.build_rule_spec(action="deny", port="22", protocol="tcp", source="", direction="in")
    assert out["lockout_warning"] is True


def test_ssh_deny_with_source_no_warn():
    out = fm.build_rule_spec(action="deny", port="22", protocol="tcp", source="1.2.3.4", direction="in")
    assert out["lockout_warning"] is False


def test_bad_port_rejected():
    with pytest.raises(fm.FirewallError) as e:
        fm.build_rule_spec(action="allow", port="99999")
    assert e.value.code == "invalid"


def test_bad_action_rejected():
    with pytest.raises(fm.FirewallError):
        fm.build_rule_spec(action="explode", port="80")


def test_preview_endpoint():
    import server

    app = server.app
    app.config["TESTING"] = True
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["logged_in"] = True
        resp = client.post("/api/firewall/rule-preview", json={"action": "deny", "port": "22"})
        assert resp.status_code == 200
        assert resp.get_json()["lockout_warning"] is True
        bad = client.post("/api/firewall/rule-preview", json={"action": "allow", "port": "bogus"})
        assert bad.status_code == 400


def test_bad_source_rejected():
    with pytest.raises(fm.FirewallError) as e:
        fm.build_rule_spec(action="allow", port="80", source="not-an-ip")
    assert e.value.code == "invalid"


def test_cidr_source_accepted():
    out = fm.build_rule_spec(action="allow", port="80", source="192.168.1.0/24")
    assert out["spec"]["source"] == "192.168.1.0/24"


def test_port_with_suffix_normalized():
    out = fm.build_rule_spec(action="allow", port="22/tcp")
    assert out["spec"]["port"] == "22"


def test_range_covering_ssh_warns():
    out = fm.build_rule_spec(action="deny", port="20:25")
    assert out["lockout_warning"] is True


def test_add_rule_requires_lockout_confirm(monkeypatch):
    from unittest.mock import MagicMock, patch

    monkeypatch.setattr(fm, "is_available", lambda: {"available": True, "backend": "ufw", "reason": ""})
    with pytest.raises(fm.FirewallError) as e:
        fm.add_rule({"action": "deny", "port": "22"}, password="x")
    assert e.value.code == "confirm_required"
    # Confirmed + valid source-restricted rule passes validation to backend.
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        out = fm.add_rule(
            {"action": "deny", "port": "22", "confirm_lockout": True}, password="x",
        )
    assert out["ok"] is True
    cmd = run.call_args[0][0]
    assert "22" in cmd
    # "22/tcp" input must not produce a doubled protocol arg.
    with patch.object(fm.subprocess, "run", return_value=fake) as run2:
        fm.add_rule({"action": "allow", "port": "22/tcp"}, password="x")
    cmd2 = run2.call_args[0][0]
    assert "22/tcp" not in cmd2
    assert cmd2.count("tcp") == 1
