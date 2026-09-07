import os
import subprocess
import time
from unittest.mock import patch, MagicMock

import pytest

from app.system_services import (
    Unit, SystemServicesError,
    _run, _parse_show, _unit_type, _cache_get, _cache_set, cache_invalidate,
    list_units, get_unit, get_unit_file, get_unit_logs,
    control, verify_password, edit_unit_file, get_dependencies, _GRAPH_MAX_NODES,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache_invalidate()
    yield
    cache_invalidate()


class TestHelpers:
    def test_unit_type(self):
        assert _unit_type("cron.service") == "service"
        assert _unit_type("apt-daily.timer") == "timer"
        assert _unit_type("docker.socket") == "socket"
        assert _unit_type("garbage") == ""

    def test_parse_show(self):
        text = "Id=cron.service\nActiveState=active\nSubState=running\n"
        out = _parse_show(text)
        assert out == {
            "Id": "cron.service",
            "ActiveState": "active",
            "SubState": "running",
        }

    def test_parse_show_skips_garbage(self):
        out = _parse_show("no equals here\nId=cron.service\n")
        assert "no equals here" not in out
        assert out["Id"] == "cron.service"

    def test_cache_set_get(self):
        _cache_set("k", "v")
        assert _cache_get("k") == "v"
        assert _cache_get("missing") is None

    def test_cache_expires(self):
        # Manually set expired entry
        from app.system_services import _CACHE, _CACHE_TTL, _CACHE_LOCK
        with _CACHE_LOCK:
            _CACHE["old"] = (time.time() - _CACHE_TTL - 1, "expired")
        assert _cache_get("old") is None

    def test_cache_invalidate_prefix(self):
        _cache_set("unit:foo", "x")
        _cache_set("cat:foo", "y")
        _cache_set("other", "z")
        cache_invalidate("unit:")
        assert _cache_get("unit:foo") is None
        assert _cache_get("cat:foo") == "y"
        assert _cache_get("other") == "z"

    def test_cache_invalidate_all(self):
        _cache_set("a", 1)
        _cache_set("b", 2)
        cache_invalidate()
        assert _cache_get("a") is None
        assert _cache_get("b") is None


class TestRun:
    def test_success(self):
        out = _run(["echo", "hello"])
        assert out == "hello"

    def test_failure_raises(self):
        with pytest.raises(SystemServicesError) as e:
            _run(["false"])
        assert e.value.code == "error"

    def test_permission_error_code(self):
        # 'sudo' with no tty and no NOPASSWD set would fail with permission
        # code. We simulate by faking a subprocess.run that returns a
        # permission-denied message.
        fake = MagicMock(returncode=1, stdout="", stderr="sudo: incorrect password")
        with patch("subprocess.run", return_value=fake):
            with pytest.raises(SystemServicesError) as e:
                _run(["sudo", "-S", "systemctl", "status", "x"])
            assert e.value.code == "permission"

    def test_password_not_leaked_in_message(self):
        fake = MagicMock(returncode=1, stdout="", stderr="Sorry, try again.\n[sudo] password for x:")
        with patch("subprocess.run", return_value=fake):
            with pytest.raises(SystemServicesError) as e:
                _run(["sudo", "-S", "true"], password="supersecret")
            assert "supersecret" not in str(e.value)


class TestUnitDataclass:
    def test_state_label_running(self):
        u = Unit(name="x", type="service", description="", load_state="loaded",
                 active_state="active", sub_state="running", unit_file_state="enabled",
                 main_pid=0, triggered_by="", requires="", wants="", after="",
                 path="", has_override=False, can_start=True, can_stop=True, can_reload=False)
        assert u.state_label == "running"
        assert u.is_active
        assert u.is_running
        assert u.is_enabled

    def test_state_label_failed(self):
        u = Unit(name="x", type="service", description="", load_state="loaded",
                 active_state="failed", sub_state="failed", unit_file_state="enabled",
                 main_pid=0, triggered_by="", requires="", wants="", after="",
                 path="", has_override=False, can_start=True, can_stop=True, can_reload=False)
        assert u.state_label == "failed"
        assert u.is_failed

    def test_state_label_static(self):
        u = Unit(name="x", type="service", description="", load_state="loaded",
                 active_state="inactive", sub_state="dead", unit_file_state="static",
                 main_pid=0, triggered_by="", requires="", wants="", after="",
                 path="", has_override=False, can_start=True, can_stop=True, can_reload=False)
        assert u.state_label == "static"

    def test_to_summary_includes_state_flags(self):
        u = Unit(name="x", type="service", description="d", load_state="loaded",
                 active_state="active", sub_state="running", unit_file_state="enabled",
                 main_pid=42, triggered_by="", requires="", wants="", after="",
                 path="", has_override=False, can_start=True, can_stop=True, can_reload=False)
        s = u.to_summary()
        assert s["name"] == "x"
        assert s["main_pid"] == 42
        assert s["is_running"] is True
        assert s["is_enabled"] is True

    def test_to_detail_extends_summary(self):
        u = Unit(name="x", type="service", description="d", load_state="loaded",
                 active_state="active", sub_state="running", unit_file_state="enabled",
                 main_pid=0, triggered_by="", requires="", wants="", after="",
                 path="/lib/systemd/system/x", has_override=False, can_start=True, can_stop=True, can_reload=False)
        d = u.to_detail()
        assert d["path"] == "/lib/systemd/system/x"
        assert d["can_start"] is True
        assert d["state_label"] == "running"


class TestListUnits:
    LIST_UNITS_OUTPUT = (
        "cron.service loaded active running Regular background program processing daemon\n"
        "anacron.service loaded inactive dead Run anacron jobs\n"
        "sshd.service not-found inactive dead OpenBSD Secure Shell server\n"
        "apt-daily.timer loaded active waiting Daily apt download activities\n"
    )

    SHOW_OUTPUT = (
        "Id=cron.service\nDescription=Regular background program processing daemon\n"
        "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n"
        "MainPID=1234\nFragmentPath=/lib/systemd/system/cron.service\nDropInPaths=\n"
        "Requires=sysinit.target\nWants=\nAfter=remote-fs.target\nType=simple\n"
        "ExecStart={ path=/usr/sbin/cron }\nMemoryCurrent=1234567\nMemoryPeak=9876543\n"
        "CPUUsageNSec=5000\nActiveEnterTimestamp=Mon 2025-01-01 00:00:00 UTC\n"
    )

    @patch("app.system_services._run")
    def test_list_units_basic(self, mock_run):
        def fake_run(cmd, timeout=10, password=None):
            if cmd[0] == "systemctl" and cmd[1] == "list-units":
                return self.LIST_UNITS_OUTPUT
            if cmd[0] == "systemctl" and cmd[1] == "show":
                return self.SHOW_OUTPUT.replace("Id=cron.service", f"Id={cmd[2]}")
            return ""
        mock_run.side_effect = fake_run
        units = list_units()
        names = [u.name for u in units]
        assert "cron.service" in names
        assert "apt-daily.timer" in names
        assert "sshd.service" in names  # included because --all

    @patch("app.system_services._run")
    def test_list_units_filter_by_type(self, mock_run):
        mock_run.return_value = self.LIST_UNITS_OUTPUT
        # We delegate type filtering to systemctl itself (via --type=).
        # Verify the right flag is passed to systemctl.
        list_units(unit_type="timer")
        first_call_cmd = mock_run.call_args_list[0][0][0]
        assert any(arg == "--type=timer" for arg in first_call_cmd)
        # And our local type filter still strips non-matching types as a
        # safety net (cron.service is in the mock output but not a timer).
        # Note: we don't enrich further here, so units are bare.
        units = list_units(unit_type="all")
        types = {u.type for u in units}
        assert "service" in types
        assert "timer" in types

    @patch("app.system_services._run")
    def test_list_units_filter_by_state(self, mock_run):
        mock_run.return_value = self.LIST_UNITS_OUTPUT
        units = list_units(state="active")
        assert all(u.active_state == "active" for u in units)

    @patch("app.system_services._run")
    def test_list_units_search(self, mock_run):
        mock_run.return_value = self.LIST_UNITS_OUTPUT
        units = list_units(search="cron")
        names = [u.name for u in units]
        assert "cron.service" in names
        assert "anacron.service" in names
        assert "apt-daily.timer" not in names

    @patch("app.system_services._run")
    def test_list_units_handles_list_failure(self, mock_run):
        mock_run.side_effect = SystemServicesError("nope", code="error")
        units = list_units()
        assert units == []


class TestGetUnit:
    @patch("app.system_services._run")
    def test_returns_none_for_invalid_unit(self, mock_run):
        mock_run.side_effect = SystemServicesError("Failed to parse", code="error")
        assert get_unit("nonexistent.service") is None

    @patch("app.system_services._run")
    def test_returns_none_when_load_state_not_found(self, mock_run):
        # systemctl returns default fields for missing units; we must
        # detect them via LoadState=not-found or missing FragmentPath
        mock_run.return_value = (
            "Id=ghost.service\nLoadState=not-found\nActiveState=inactive\n"
        )
        assert get_unit("ghost.service") is None

    @patch("app.system_services._run")
    def test_returns_none_when_no_id(self, mock_run):
        mock_run.return_value = "SomeKey=val\nOtherKey=val\n"
        assert get_unit("missing.service") is None

    @patch("app.system_services._run")
    def test_returns_enriched_unit(self, mock_run):
        mock_run.return_value = (
            "Id=cron.service\nDescription=d\nLoadState=loaded\n"
            "ActiveState=active\nSubState=running\nUnitFileState=enabled\n"
            "MainPID=42\nFragmentPath=/x\n"
        )
        u = get_unit("cron.service")
        assert u is not None
        assert u.name == "cron.service"
        assert u.main_pid == 42
        assert u.is_running


class TestGetUnitFile:
    @patch("app.system_services._run")
    def test_caches_result(self, mock_run):
        mock_run.return_value = "[Unit]\nDescription=test\n"
        first = get_unit_file("foo.service")
        second = get_unit_file("foo.service")
        assert first == second
        assert mock_run.call_count == 1


class TestGetUnitLogs:
    @patch("app.system_services._run")
    def test_returns_lines(self, mock_run):
        mock_run.return_value = "line 1\nline 2\nline 3"
        logs = get_unit_logs("foo.service", 3)
        assert logs == ["line 1", "line 2", "line 3"]

    @patch("app.system_services._run")
    def test_clips_to_min(self, mock_run):
        mock_run.return_value = ""
        get_unit_logs("foo.service", 0)
        args = mock_run.call_args[0][0]
        # The -n argument should be 1 (clipped up)
        n_index = args.index("-n") + 1
        assert args[n_index] == "1"

    @patch("app.system_services._run")
    def test_clips_to_max(self, mock_run):
        mock_run.return_value = ""
        get_unit_logs("foo.service", 999_999)
        args = mock_run.call_args[0][0]
        n_index = args.index("-n") + 1
        assert args[n_index] == "5000"

    @patch("app.system_services._run")
    def test_handles_missing_journalctl(self, mock_run):
        mock_run.side_effect = SystemServicesError("not found", code="missing_tool")
        logs = get_unit_logs("foo.service")
        assert "not installed" in logs[0]


class TestControl:
    @patch("app.system_services._run")
    def test_invalid_action_raises(self, mock_run):
        with pytest.raises(SystemServicesError) as e:
            control("foo.service", "rm-rf", "pw")
        assert e.value.code == "invalid"

    @patch("app.system_services._run")
    def test_action_invalidates_cache(self, mock_run):
        _cache_set("unit:foo.service", "stale")
        _cache_set("cat:foo.service", "stale")
        _cache_set("list:something", "stale")
        mock_run.return_value = ""
        control("foo.service", "start", "pw")
        assert _cache_get("unit:foo.service") is None
        assert _cache_get("cat:foo.service") is None
        assert _cache_get("list:something") is None

    @patch("app.system_services._run")
    def test_daemon_reload_action(self, mock_run):
        mock_run.return_value = "ok"
        result = control("foo.service", "daemon-reload", "pw")
        assert result["ok"] is True
        args = mock_run.call_args[0][0]
        assert "daemon-reload" in args

    @patch("app.system_services._run")
    def test_password_passed_to_sudo(self, mock_run):
        mock_run.return_value = "ok"
        control("foo.service", "start", "secret")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("password") == "secret"


class TestVerifyPassword:
    @patch("subprocess.run")
    def test_returns_true_on_zero_returncode(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert verify_password("anything") is True

    @patch("subprocess.run")
    def test_returns_false_on_nonzero(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert verify_password("wrong") is False

    @patch("subprocess.run")
    def test_returns_false_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sudo", timeout=10)
        assert verify_password("x") is False


class TestEditUnitFile:
    @patch("app.system_services._run")
    def test_validates_name_with_path_traversal(self, mock_run):
        mock_run.return_value = "ok"
        with pytest.raises(SystemServicesError) as e:
            edit_unit_file("../../etc/passwd", "x", "pw")
        assert e.value.code == "invalid"

    @patch("app.system_services._run")
    def test_validates_empty_name(self, mock_run):
        mock_run.return_value = "ok"
        with pytest.raises(SystemServicesError) as e:
            edit_unit_file("", "x", "pw")
        assert e.value.code == "invalid"

    @patch("app.system_services._run")
    def test_writes_to_drop_in_dir(self, mock_run):
        mock_run.return_value = "ok"
        edit_unit_file("cron.service", "[Service]\nRestart=always\n", "pw")
        # First call: write the drop-in
        cmd = mock_run.call_args_list[0][0][0]
        assert "sudo" in cmd
        assert "bash" in cmd
        assert "/etc/systemd/system/cron.service.d" in " ".join(cmd)
        assert "99-manager.conf" in " ".join(cmd)
        # Second call: daemon-reload
        assert len(mock_run.call_args_list) == 2
        reload_cmd = mock_run.call_args_list[1][0][0]
        assert "daemon-reload" in reload_cmd

    @patch("app.system_services._run")
    def test_invalidates_cache(self, mock_run):
        mock_run.return_value = "ok"
        _cache_set("unit:foo.service", "x")
        _cache_set("cat:foo.service", "y")
        _cache_set("list:a:b", "z")
        edit_unit_file("foo.service", "x", "pw")
        assert _cache_get("unit:foo.service") is None
        assert _cache_get("cat:foo.service") is None
        assert _cache_get("list:a:b") is None


class TestIntegration:
    """Smoke tests that actually invoke systemctl, when available."""

    @pytest.mark.skipif(
        os.system("which systemctl >/dev/null 2>&1") != 0,
        reason="systemctl not available"
    )
    def test_list_real_units(self):
        units = list_units(unit_type="service", state="all", search="")
        assert isinstance(units, list)
        # Most linux systems have at least cron or systemd-journald
        # but we don't require a specific unit to exist.

    @pytest.mark.skipif(
        os.system("which systemctl >/dev/null 2>&1") != 0,
        reason="systemctl not available"
    )
    def test_get_real_unit(self):
        # Pick a unit we know exists on most systems
        for name in ("cron.service", "systemd-journald.service", "dbus.service"):
            u = get_unit(name)
            if u is not None:
                assert u.name == name
                assert u.description
                return
        pytest.skip("no common unit found to test against")


class TestGetDependencies:
    def test_missing_root(self):
        result = get_dependencies("nonexistent.service", depth=1)
        assert result["root"] == "nonexistent.service"
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["missing"] is True

    def test_skip_well_known_targets(self):
        def fake(name):
            if name == "test.service":
                m = MagicMock()
                m.requires = "multi-user.target basic.target real-dep.service"
                m.wants = "graphical.target real-dep2.service"
                m.triggered_by = "trigger.service"
                m.after = "multi-user.target after-dep.service"
                m.before = ""
                return m
            return None
        with patch("app.system_services.get_unit", side_effect=fake):
            result = get_dependencies("test.service", depth=1)
        targets = {e["to"] for e in result["edges"]}
        assert "multi-user.target" not in targets
        assert "basic.target" not in targets
        assert "graphical.target" not in targets
        assert "real-dep.service" in targets
        assert "real-dep2.service" in targets
        assert "after-dep.service" in targets
        assert "trigger.service" in targets

    def test_reverse_edges_from_triggeredby(self):
        def fake(name):
            if name == "test.service":
                m = MagicMock()
                m.requires = ""
                m.wants = ""
                m.triggered_by = "trigger.service"
                m.after = ""
                m.before = ""
                return m
            return None
        with patch("app.system_services.get_unit", side_effect=fake):
            result = get_dependencies("test.service", depth=1)
        # trigger.service -> test.service via TriggeredBy (reverse direction)
        rev = [e for e in result["edges"] if e["to"] == "test.service"]
        assert any(e["from"] == "trigger.service" and e["type"] == "TriggeredBy" for e in rev)

    def test_max_nodes_truncates(self):
        def fake(name):
            m = MagicMock()
            deps = [f"d-{name}-{i}" for i in range(5)]
            m.requires = " ".join(deps)
            m.wants = ""
            m.triggered_by = ""
            m.after = ""
            m.before = ""
            return m
        with patch("app.system_services.get_unit", side_effect=fake):
            result = get_dependencies("root", depth=4)
        assert len(result["nodes"]) <= _GRAPH_MAX_NODES
        if len(result["nodes"]) == _GRAPH_MAX_NODES:
            assert result["truncated"] is True

    def test_depth_clamped(self):
        with patch("app.system_services.get_unit", return_value=None):
            # depth=99 should be clamped to 4
            result = get_dependencies("x.service", depth=99)
            assert result["depth"] == 4
            # depth=0 should be clamped to 1
            result = get_dependencies("x.service", depth=0)
            assert result["depth"] == 1
