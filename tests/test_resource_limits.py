import os
from unittest.mock import patch

import pytest

from app import resource_limits


@pytest.fixture
def fake_user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_limits, "_unit_dir",
                        lambda n: str(tmp_path / f"ssm-{n}.service.d"))
    yield tmp_path


def _enable_daemon_reload():
    """Patch _run_systemctl so it doesn't try the real bus."""
    return patch.object(resource_limits, "_run_systemctl",
                        return_value=(0, "", ""))


class TestValidateValue:
    def test_unknown_field(self):
        ok, err = resource_limits.validate_value("not_a_field", "x")
        assert ok is False
        assert "unsupported" in err

    def test_empty(self):
        ok, err = resource_limits.validate_value("cpu_quota", "")
        assert ok is False

    def test_shell_metacharacters(self):
        ok, err = resource_limits.validate_value("cpu_quota", "50%; rm -rf /")
        assert ok is False
        assert "metacharacter" in err

    def test_too_long(self):
        ok, err = resource_limits.validate_value("cpu_quota", "x" * 100)
        assert ok is False

    @pytest.mark.parametrize("field,value,expected", [
        ("cpu_quota", "50", True),
        ("cpu_quota", "50%", True),
        ("cpu_quota", "100", True),
        ("cpu_quota", "0", True),
        ("cpu_quota", "150", False),
        ("cpu_quota", "-1", False),
        ("cpu_quota", "abc", False),
        ("memory_max", "512M", True),
        ("memory_max", "2G", True),
        ("memory_max", "infinity", True),
        ("memory_max", "lots", False),
        ("memory_high", "256K", True),
        ("tasks_max", "100", True),
        ("tasks_max", "infinity", True),
        ("tasks_max", "abc", False),
        ("io_weight", "100", True),
        ("io_weight", "1", True),
        ("io_weight", "10000", True),
        ("io_weight", "0", False),
        ("io_weight", "10001", False),
        ("cpu_weight", "500", True),
        ("nice", "0", True),
        ("nice", "-20", True),
        ("nice", "19", True),
        ("nice", "20", False),
        ("nice", "-21", False),
        ("limit_nofile", "1024", True),
        ("limit_nofile", "0", False),
    ])
    def test_field_specific_validation(self, field, value, expected):
        ok, _ = resource_limits.validate_value(field, value)
        assert ok is expected


class TestGet:
    def test_get_no_drop_in(self, fake_user_dir):
        assert resource_limits.get("api") == {}

    def test_get_existing(self, fake_user_dir):
        # Set up a drop-in
        with _enable_daemon_reload():
            resource_limits.apply("api", {"cpu_quota": "50", "memory_max": "512M"})
        result = resource_limits.get("api")
        assert result["cpu_quota"] == "50"
        assert result["memory_max"] == "512M"

    def test_get_strips_percent(self, fake_user_dir):
        with _enable_daemon_reload():
            resource_limits.apply("api", {"cpu_quota": "75%"})
        assert resource_limits.get("api")["cpu_quota"] == "75"


class TestApply:
    def test_apply_single_field(self, fake_user_dir):
        with _enable_daemon_reload():
            ok, err = resource_limits.apply("api", {"cpu_quota": "50"})
        assert ok, err
        path = resource_limits._drop_in_path("api")
        assert os.path.exists(path)
        content = open(path).read()
        assert "[Service]" in content
        assert "CPUQuota=50%" in content

    def test_apply_multiple_fields(self, fake_user_dir):
        with _enable_daemon_reload():
            ok, err = resource_limits.apply("api", {
                "cpu_quota": "50",
                "memory_max": "512M",
                "tasks_max": "100",
                "nice": "5",
            })
        assert ok, err
        content = open(resource_limits._drop_in_path("api")).read()
        assert "CPUQuota=50%" in content
        assert "MemoryMax=512M" in content
        assert "TasksMax=100" in content
        assert "Nice=5" in content

    def test_apply_empty_value_removes_field(self, fake_user_dir):
        with _enable_daemon_reload():
            resource_limits.apply("api", {"cpu_quota": "50", "memory_max": "512M"})
        # Now clear cpu_quota by passing empty
        with _enable_daemon_reload():
            resource_limits.apply("api", {"cpu_quota": ""})
        content = open(resource_limits._drop_in_path("api")).read()
        assert "CPUQuota" not in content
        assert "MemoryMax=512M" in content

    def test_apply_invalid_value_rejected(self, fake_user_dir):
        with _enable_daemon_reload():
            ok, err = resource_limits.apply("api", {"cpu_quota": "abc"})
        assert ok is False
        assert "must be a number" in err

    def test_apply_calls_daemon_reload(self, fake_user_dir):
        with patch.object(resource_limits, "_run_systemctl") as mock:
            mock.return_value = (0, "", "")
            resource_limits.apply("api", {"cpu_quota": "50"})
            # daemon-reload should have been called
            calls = [c for c in mock.call_args_list
                     if "daemon-reload" in (c.args[0] if c.args else [])]
            assert len(calls) >= 1

    def test_apply_daemon_reload_failure(self, fake_user_dir):
        with patch.object(resource_limits, "_run_systemctl") as mock:
            mock.return_value = (1, "", "Failed to connect to bus")
            ok, err = resource_limits.apply("api", {"cpu_quota": "50"})
        assert ok is False
        assert "daemon-reload" in err

    def test_apply_preserves_existing_fields(self, fake_user_dir):
        with _enable_daemon_reload():
            resource_limits.apply("api", {"cpu_quota": "50", "memory_max": "256M"})
            resource_limits.apply("api", {"cpu_quota": "75"})
        content = open(resource_limits._drop_in_path("api")).read()
        # Both fields still present
        assert "CPUQuota=75%" in content
        assert "MemoryMax=256M" in content


class TestClear:
    def test_clear_removes_drop_in(self, fake_user_dir):
        with _enable_daemon_reload():
            resource_limits.apply("api", {"cpu_quota": "50"})
        assert os.path.exists(resource_limits._drop_in_path("api"))
        with _enable_daemon_reload():
            ok, err = resource_limits.clear("api")
        assert ok, err
        assert not os.path.exists(resource_limits._drop_in_path("api"))

    def test_clear_unknown_is_noop(self, fake_user_dir):
        with _enable_daemon_reload():
            ok, err = resource_limits.clear("never-existed")
        assert ok, err


class TestFieldChoices:
    def test_returns_list_of_dicts(self):
        choices = resource_limits.field_choices()
        assert isinstance(choices, list)
        assert len(choices) >= 5
        for c in choices:
            assert "field" in c
            assert "label" in c
            assert "hint" in c


class TestEndToEnd:
    def test_full_cycle(self, fake_user_dir):
        with _enable_daemon_reload():
            ok, _ = resource_limits.apply("api", {
                "cpu_quota": "75",
                "memory_max": "1G",
                "nice": "-5",
                "limit_nofile": "8192",
            })
            assert ok
        # Read back
        result = resource_limits.get("api")
        assert result["cpu_quota"] == "75"
        assert result["memory_max"] == "1G"
        assert result["nice"] == "-5"
        assert result["limit_nofile"] == "8192"
        # Clear one
        with _enable_daemon_reload():
            resource_limits.apply("api", {"memory_max": ""})
        result = resource_limits.get("api")
        assert "memory_max" not in result
        assert result["cpu_quota"] == "75"
        # Clear all
        with _enable_daemon_reload():
            resource_limits.clear("api")
        assert resource_limits.get("api") == {}
