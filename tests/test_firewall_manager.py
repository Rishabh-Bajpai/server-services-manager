"""Tests for app/firewall_manager.py.

Subprocess calls are mocked throughout. We exercise:
- is_available() detection (ufw, firewalld, neither)
- ufw status parsing (numbered + verbose)
- firewalld status parsing
- add/delete/enable/disable/reload/set_default
- error classification
"""
from unittest.mock import MagicMock, patch

import pytest

from app import firewall_manager as fm
from app.firewall_manager import (
    FirewallError,
    Rule,
    Status,
    add_rule,
    delete_rule,
    disable,
    enable,
    get_status,
    is_available,
    reload,
    set_default,
)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------

def test_is_available_ufw(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: "/usr/sbin/ufw" if c == "ufw" else None)
    det = is_available()
    assert det["available"] is True
    assert det["backend"] == "ufw"


def test_is_available_firewalld(monkeypatch):
    def which(c):
        return "/usr/bin/firewall-cmd" if c == "firewall-cmd" else None
    monkeypatch.setattr("shutil.which", which)
    det = is_available()
    assert det["available"] is True
    assert det["backend"] == "firewalld"


def test_is_available_none(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    det = is_available()
    assert det["available"] is False
    assert det["backend"] == "none"
    assert "ufw" in det["reason"].lower() or "firewalld" in det["reason"].lower()


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------

def test_run_success():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok\n"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        assert fm._run(["echo", "hi"]) == "ok"


def test_run_password_required():
    fake = MagicMock()
    fake.returncode = 1
    fake.stdout = ""
    fake.stderr = "sudo: a password is required"
    with patch.object(fm.subprocess, "run", return_value=fake):
        with pytest.raises(FirewallError) as exc_info:
            fm._run(["sudo", "-S", "ufw", "status"], password="x")
    assert exc_info.value.code == "permission"


def test_run_timeout():
    import subprocess
    with patch.object(fm.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 5)):
        with pytest.raises(FirewallError) as exc_info:
            fm._run(["ufw", "status"])
    assert exc_info.value.code == "timeout"


def test_run_missing_tool():
    with patch.object(fm.subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(FirewallError) as exc_info:
            fm._run(["ufw", "status"])
    assert exc_info.value.code == "missing_tool"


# ---------------------------------------------------------------------------
# ufw status parsing
# ---------------------------------------------------------------------------

def test_ufw_status_active_with_rules():
    sample = """\
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 80/tcp                     ALLOW IN    192.168.1.0/24
[ 3] Anywhere                   DENY IN     10.0.0.0/8
"""
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = sample
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        status = fm._ufw_status()
    assert status.backend == "ufw"
    assert status.enabled is True
    assert status.default_incoming == "deny"
    assert status.default_outgoing == "allow"
    assert status.default_routed == "disabled"
    assert len(status.rules) == 3
    assert status.rules[0].number == 1
    assert status.rules[0].port_proto == "22/tcp"
    assert status.rules[0].action == "ALLOW"
    assert status.rules[1].to == "192.168.1.0/24"
    assert status.rules[2].action == "DENY"


def test_ufw_status_inactive():
    sample = "Status: inactive\n"
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = sample
    with patch.object(fm.subprocess, "run", return_value=fake):
        status = fm._ufw_status()
    assert status.enabled is False
    assert status.rules == []


# ---------------------------------------------------------------------------
# ufw add/delete
# ---------------------------------------------------------------------------

def test_ufw_add_rule():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "Rule added\n"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        result = fm._ufw_add_rule(
            {"action": "allow", "port": "22", "protocol": "tcp"}, password="x",
        )
    cmd = run.call_args[0][0]
    assert "ufw" in cmd
    assert "allow" in cmd
    assert "22" in cmd
    assert "tcp" in cmd
    # The private function returns the raw stdout (string)
    assert isinstance(result, str)


def test_add_rule_public_returns_dict():
    """The public add_rule() wraps the private helper in a dict."""
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        result = fm.add_rule(
            {"action": "allow", "port": "22", "protocol": "tcp"}, password="x",
        )
    assert result["ok"] is True
    assert "ok" in result["output"].lower() or "rule" in result["output"].lower()


def test_ufw_add_rule_with_source():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        fm._ufw_add_rule(
            {"action": "allow", "port": "3306", "protocol": "tcp", "source": "10.0.0.5"},
            password="x",
        )
    cmd = run.call_args[0][0]
    assert "from" in cmd
    assert "10.0.0.5" in cmd
    assert "3306" in cmd


def test_ufw_add_rule_invalid_action():
    with pytest.raises(FirewallError) as exc_info:
        fm._ufw_add_rule({"action": "wat", "port": "22"}, password="x")
    assert exc_info.value.code == "invalid"


def test_ufw_add_rule_missing_port():
    with pytest.raises(FirewallError) as exc_info:
        fm._ufw_add_rule({"action": "allow", "port": ""}, password="x")
    assert exc_info.value.code == "invalid"


def test_ufw_add_rule_invalid_protocol():
    with pytest.raises(FirewallError) as exc_info:
        fm._ufw_add_rule({"action": "allow", "port": "22", "protocol": "icmp"}, password="x")
    assert exc_info.value.code == "invalid"


def test_ufw_delete_rule_by_number():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        fm._ufw_delete_rule({"number": 3}, password="x")
    cmd = run.call_args[0][0]
    assert "delete" in cmd
    assert "3" in cmd


def test_ufw_delete_rule_by_spec():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        result = fm.delete_rule(
            {"action": "allow", "port": "22", "protocol": "tcp"}, password="x",
        )
    cmd = run.call_args[0][0]
    assert "delete" in cmd
    assert "allow" in cmd
    assert result["ok"] is True


def test_ufw_set_default():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        fm._ufw_set_default("deny", "incoming", password="x")
    cmd = run.call_args[0][0]
    assert "default" in cmd
    assert "deny" in cmd
    assert "incoming" in cmd


# ---------------------------------------------------------------------------
# ufw apply
# ---------------------------------------------------------------------------

def test_ufw_enable():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "Firewall is active"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        result = fm._ufw_apply("enable", password="x")
    cmd = run.call_args[0][0]
    assert "ufw" in cmd
    assert "enable" in cmd
    assert isinstance(result, str)


def test_enable_public_returns_dict():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "active"
    fake.stderr = ""
    monkeypatch_ua = is_available
    fm.is_available = lambda: {"available": True, "backend": "ufw", "reason": ""}
    try:
        with patch.object(fm.subprocess, "run", return_value=fake):
            result = fm.enable(password="x")
        assert result["ok"] is True
    finally:
        fm.is_available = monkeypatch_ua


def test_ufw_apply_invalid():
    with pytest.raises(FirewallError):
        fm._ufw_apply("explode", password="x")


# ---------------------------------------------------------------------------
# firewalld status parsing
# ---------------------------------------------------------------------------

def test_firewalld_status_basic():
    state = MagicMock()
    state.returncode = 0
    state.stdout = "running\n"
    state.stderr = ""
    zone = MagicMock()
    zone.returncode = 0
    zone.stdout = "public\n"
    zone.stderr = ""
    list_all = MagicMock()
    list_all.returncode = 0
    list_all.stdout = """\
public (active)
  target: default
  icmp-block-inversion: no
  interfaces: eth0
  sources:
  services: cockpit dhcpv6-client ssh
  ports: 8080/tcp 9000/udp
  protocols:
  masquerade: no
  forward-ports:
  source-ports:
  icmp-blocks:
  rich rules:
"""
    list_all.stderr = ""
    with patch.object(fm.subprocess, "run", side_effect=[state, zone, list_all]):
        status = fm._firewalld_status()
    assert status.enabled is True
    assert status.backend == "firewalld"
    assert status.default_incoming == "public"
    assert len(status.rules) == 5
    rule_strs = " ".join(r.port_proto for r in status.rules)
    assert "service:cockpit" in rule_strs
    assert "8080/tcp" in rule_strs
    assert "9000/udp" in rule_strs


def test_firewalld_status_not_running():
    state = MagicMock()
    state.returncode = 252  # not running
    state.stdout = "not running\n"
    state.stderr = ""
    zone = MagicMock()
    zone.returncode = 0
    zone.stdout = "public\n"
    zone.stderr = ""
    list_all = MagicMock()
    list_all.returncode = 0
    list_all.stdout = "public (active)\n  ports:\n  services:\n  rich rules:\n"
    list_all.stderr = ""
    with patch.object(fm.subprocess, "run", side_effect=[state, zone, list_all]):
        status = fm._firewalld_status()
    assert status.enabled is False


# ---------------------------------------------------------------------------
# firewalld add/delete
# ---------------------------------------------------------------------------

def test_firewalld_add_port():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "success"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        fm._firewalld_add_rule({"action": "allow", "port": "8080", "protocol": "tcp"},
                                password="x")
    cmd = run.call_args[0][0]
    assert "--add-port" in cmd
    assert "8080/tcp" in cmd


def test_firewalld_add_port_with_source():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "success"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        fm._firewalld_add_rule({"action": "allow", "port": "22", "protocol": "tcp",
                                "source": "10.0.0.5"}, password="x")
    cmd = run.call_args[0][0]
    assert "--add-rich-rule" in cmd
    assert "10.0.0.5" in " ".join(cmd)


def test_firewalld_delete_port():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "success"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake) as run:
        fm._firewalld_delete_rule({"port": "8080", "protocol": "tcp"}, password="x")
    cmd = run.call_args[0][0]
    assert "--remove-port" in cmd
    assert "8080/tcp" in cmd


# ---------------------------------------------------------------------------
# Public API dispatch
# ---------------------------------------------------------------------------

def test_get_status_dispatches_ufw(monkeypatch):
    monkeypatch.setattr(fm, "is_available", lambda: {"available": True, "backend": "ufw", "reason": ""})
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "Status: inactive\n"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        s = get_status()
    assert s.backend == "ufw"


def test_get_status_unavailable(monkeypatch):
    monkeypatch.setattr(fm, "is_available", lambda: {"available": False, "backend": "none", "reason": "missing"})
    s = get_status()
    assert s.available is False
    assert s.reason == "missing"


def test_enable_dispatches(monkeypatch):
    monkeypatch.setattr(fm, "is_available", lambda: {"available": True, "backend": "ufw", "reason": ""})
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        result = enable(password="x")
    assert result["ok"] is True


def test_disable_dispatches(monkeypatch):
    monkeypatch.setattr(fm, "is_available", lambda: {"available": True, "backend": "ufw", "reason": ""})
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        result = disable(password="x")
    assert result["ok"] is True


def test_reload_dispatches(monkeypatch):
    monkeypatch.setattr(fm, "is_available", lambda: {"available": True, "backend": "firewalld", "reason": ""})
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        result = reload(password="x")
    assert result["ok"] is True


def test_add_rule_dispatches(monkeypatch):
    monkeypatch.setattr(fm, "is_available", lambda: {"available": True, "backend": "ufw", "reason": ""})
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        result = add_rule({"action": "allow", "port": "22", "protocol": "tcp"}, password="x")
    assert result["ok"] is True


def test_delete_rule_dispatches(monkeypatch):
    monkeypatch.setattr(fm, "is_available", lambda: {"available": True, "backend": "ufw", "reason": ""})
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(fm.subprocess, "run", return_value=fake):
        result = delete_rule({"number": 1}, password="x")
    assert result["ok"] is True


def test_set_default_unsupported_for_firewalld(monkeypatch):
    monkeypatch.setattr(fm, "is_available", lambda: {"available": True, "backend": "firewalld", "reason": ""})
    with pytest.raises(FirewallError) as exc_info:
        set_default("deny", "incoming", password="x")
    assert exc_info.value.code == "unsupported"


# ---------------------------------------------------------------------------
# Rule / Status dataclasses
# ---------------------------------------------------------------------------

def test_rule_to_dict():
    r = Rule(number=1, action="ALLOW", to="Anywhere", port_proto="22/tcp")
    d = r.to_dict()
    assert d["number"] == 1
    assert d["action"] == "ALLOW"
    assert d["port_proto"] == "22/tcp"


def test_status_to_dict():
    s = Status(
        available=True, backend="ufw", enabled=True,
        default_incoming="deny", default_outgoing="allow", default_routed="",
        rules=[Rule(1, "ALLOW", "Anywhere", "22/tcp")],
    )
    d = s.to_dict()
    assert d["enabled"] is True
    assert d["default_incoming"] == "deny"
    assert len(d["rules"]) == 1