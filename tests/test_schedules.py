import os
from unittest.mock import MagicMock, patch

import pytest

from app import schedules


@pytest.fixture
def fake_user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(schedules, "_USER_DIR", str(tmp_path))
    yield tmp_path


class TestValidation:
    def test_empty_is_valid(self):
        assert schedules.is_valid_schedule("") is True

    def test_presets_valid(self):
        for preset in ("minutely", "hourly", "daily", "weekly", "monthly", "yearly"):
            assert schedules.is_valid_schedule(preset) is True

    def test_cron_style_valid(self):
        assert schedules.is_valid_schedule("*-*-* *:00/15") is True
        assert schedules.is_valid_schedule("Mon..Fri 09:00:00") is True

    def test_invalid_characters(self):
        assert schedules.is_valid_schedule("rm -rf /") is False
        assert schedules.is_valid_schedule("; bad") is False

    def test_too_long_rejected(self):
        assert schedules.is_valid_schedule("a" * 201) is False


class TestPresetSuggestions:
    def test_returns_list(self):
        suggestions = schedules.preset_suggestions()
        assert isinstance(suggestions, list)
        assert len(suggestions) >= 5
        for value, label in suggestions:
            assert isinstance(value, str) and value
            assert isinstance(label, str) and label


class TestUnitPaths:
    def test_safe_name(self, fake_user_dir):
        path = schedules._unit_path("my-service", "timer")
        assert path.endswith("ssm-my-service.timer")

    def test_special_chars_sanitized(self, fake_user_dir):
        path = schedules._unit_path("a/b c!", "service")
        # No slashes or spaces in the filename
        assert "/" not in os.path.basename(path)
        assert " " not in os.path.basename(path)


class TestWriteAndRemove:
    def test_write_units_creates_files(self, fake_user_dir):
        # Mock the daemon-reload so it doesn't hit the real bus
        with patch.object(schedules, "_run_systemctl") as mock_run:
            mock_run.return_value = (0, "", "")
            ok, err = schedules.write_units(
                "api", "/usr/bin/foo", "/tmp", "hourly",
                environment={"KEY": "VAL"},
            )
        assert ok, err
        svc_path = os.path.join(str(fake_user_dir), "ssm-api.service")
        tim_path = os.path.join(str(fake_user_dir), "ssm-api.timer")
        assert os.path.exists(svc_path)
        assert os.path.exists(tim_path)
        svc = open(svc_path).read()
        assert "Type=oneshot" in svc
        assert "ExecStart=/usr/bin/foo" in svc
        assert 'Environment="KEY=VAL"' in svc
        tim = open(tim_path).read()
        assert "OnCalendar=hourly" in tim
        assert "Persistent=true" in tim

    def test_write_units_invalid_schedule(self, fake_user_dir):
        ok, err = schedules.write_units("api", "x", "/", "rm -rf /")
        assert ok is False
        assert "invalid" in err.lower()

    def test_remove_units(self, fake_user_dir):
        with patch.object(schedules, "_run_systemctl") as mock_run:
            mock_run.return_value = (0, "", "")
            schedules.write_units("api", "/bin/true", "/tmp", "daily")
        assert os.path.exists(os.path.join(str(fake_user_dir), "ssm-api.service"))
        with patch.object(schedules, "_run_systemctl") as mock_run:
            mock_run.return_value = (0, "", "")
            ok, err = schedules.remove_units("api")
        assert ok, err
        assert not os.path.exists(os.path.join(str(fake_user_dir), "ssm-api.service"))
        assert not os.path.exists(os.path.join(str(fake_user_dir), "ssm-api.timer"))

    def test_remove_units_idempotent(self, fake_user_dir):
        with patch.object(schedules, "_run_systemctl") as mock_run:
            mock_run.return_value = (0, "", "")
            ok, err = schedules.remove_units("never-existed")
        assert ok, err


class TestRunSystemctl:
    def test_no_password(self, fake_user_dir):
        # No real systemctl call: schedule a command that succeeds
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            rc, out, err = schedules._run_systemctl(["status", "x.service"])
            assert rc == 0
            # First arg of the call should be systemctl --user
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "systemctl"
            assert cmd[1] == "--user"

    def test_with_password_uses_sudo(self, fake_user_dir):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            rc, out, err = schedules._run_systemctl(["enable", "x.timer"], password="secret")
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "sudo"
            assert "-S" in cmd
            stdin = mock_run.call_args.kwargs.get("input")
            assert stdin == b"secret\n"


class TestTimerStatus:
    def test_status_when_no_units(self, fake_user_dir):
        with patch.object(schedules, "_run_systemctl") as mock:
            mock.return_value = (1, "", "Failed to ...\n")
            status = schedules.timer_status("api")
            assert status["enabled"] is False
            assert status["active"] is False

    def test_status_enabled(self, fake_user_dir):
        def fake(args):
            if "is-enabled" in args:
                return (0, "enabled\n", "")
            if "is-active" in args:
                return (0, "active\n", "")
            if "list-timers" in args:
                # systemd --no-legend line: NEXT LEFT UNIT
                return (0, "Mon 2026-06-15 00:00:00 CST  2h ssm-api.timer ssm-api.service\n", "")
            return (1, "", "")
        with patch.object(schedules, "_run_systemctl", side_effect=fake):
            status = schedules.timer_status("api")
            assert status["enabled"] is True
            assert status["active"] is True
            # next_run captures the first whitespace-delimited token
            assert status["next_run"] == "Mon"
