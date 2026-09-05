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
