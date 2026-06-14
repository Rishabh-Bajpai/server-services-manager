from unittest.mock import patch, MagicMock, mock_open

import pytest

from app.cron_manager import (
    CronError, parse_line, validate_expression,
    list_system_crontab, list_cron_d, list_user_crontab, list_all,
    toggle_system_job, describe_schedule,
)


class TestValidateExpression:
    def test_wildcards(self):
        assert validate_expression("* * * * *") is None
        assert validate_expression("0 0 1 1 0") is None

    def test_step_values(self):
        assert validate_expression("*/5 * * * *") is None
        assert validate_expression("0 */2 * * *") is None

    def test_ranges(self):
        assert validate_expression("0 9-17 * * *") is None
        assert validate_expression("0 9-17/2 * * *") is None

    def test_lists(self):
        assert validate_expression("0,15,30,45 * * * *") is None
        assert validate_expression("0 9-12,14-17 * * *") is None

    def test_invalid_field_count(self):
        assert validate_expression("* * * *") is not None
        assert validate_expression("* * * * * *") is not None

    def test_out_of_range(self):
        assert validate_expression("60 * * * *") is not None
        assert validate_expression("* 24 * * *") is not None
        assert validate_expression("* * 32 * *") is not None
        assert validate_expression("* * * 13 *") is not None
        assert validate_expression("* * * * 7") is not None

    def test_invalid_syntax(self):
        assert validate_expression("bad * * * *") is not None
        assert validate_expression("* * * * * *") is not None


class TestParseLine:
    def test_basic_user_crontab(self):
        job = parse_line("0 7 * * 1 /usr/bin/backup", 1, "user:root", "root")
        assert job is not None
        assert job.schedule == "0 7 * * 1"
        assert job.command == "/usr/bin/backup"
        assert job.user == "root"
        assert job.line_number == 1

    def test_system_crontab_with_user_field(self):
        job = parse_line(
            "0 5 * * * root /usr/bin/cleanup",
            1, "/etc/crontab", "root"
        )
        assert job is not None
        assert job.user == "root"
        assert job.command == "/usr/bin/cleanup"

    def test_comment_line_returns_none(self):
        assert parse_line("# this is a comment", 1, "user:root") is None
        assert parse_line("  # indented comment", 1, "user:root") is None

    def test_blank_line_returns_none(self):
        assert parse_line("", 1, "user:root") is None
        assert parse_line("   ", 1, "user:root") is None

    def test_env_var_returns_none(self):
        assert parse_line("SHELL=/bin/bash", 1, "user:root") is None

    def test_too_few_fields(self):
        assert parse_line("0 7 * *", 1, "user:root") is None

    def test_complex_command(self):
        job = parse_line(
            "*/5 * * * * /usr/bin/python3 -c 'print(1)'",
            1, "user:root", "root"
        )
        assert job is not None
        assert job.command == "/usr/bin/python3 -c 'print(1)'"


class TestDescribeSchedule:
    def test_descriptions(self):
        assert "every hour" in describe_schedule("* * * * *").lower()
        assert "weekday" in describe_schedule("0 7 * * 1")
        assert "5 min" in describe_schedule("*/5 * * * *")
        assert "every 5" in describe_schedule("*/5 * * * *")


class TestListSystemCrontab:
    def test_reads_real_crontab(self):
        jobs = list_system_crontab()
        # We don't know exact contents, but the function should not raise
        # and return a list
        assert isinstance(jobs, list)
        for j in jobs:
            assert j.source == "/etc/crontab"

    @patch("builtins.open", side_effect=PermissionError("nope"))
    def test_handles_permission_error(self, mock_open_fn):
        # If the file can't be read, return empty list and log
        assert list_system_crontab() == []


class TestListCronD:
    def test_reads_cron_d(self):
        jobs = list_cron_d()
        assert isinstance(jobs, list)
        for j in jobs:
            assert j.source.startswith("/etc/cron.d/")


class TestListUserCrontab:
    def test_no_crontab(self):
        # Most test environments don't have a user crontab
        jobs = list_user_crontab("nonexistent_user_xyz")
        assert jobs == []

    def test_with_username_env(self, monkeypatch):
        monkeypatch.setenv("USER", "nobody_xyz")
        assert list_user_crontab() == []


class TestListAll:
    def test_combines(self):
        jobs = list_all()
        assert isinstance(jobs, list)
        sources = {j.source for j in jobs}
        # At minimum we have /etc/crontab and /etc/cron.d/*
        assert any(s == "/etc/crontab" for s in sources)
        assert any(s.startswith("/etc/cron.d/") for s in sources)


class TestToggleSystemJob:
    def test_disables_job(self):
        original = "0 7 * * 1 /usr/bin/backup\n# comment\n"
        with patch("builtins.open", mock_open(read_data=original)):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                toggle_system_job(
                    "/etc/cron.d/test", 1, enabled=False, password="hunter2"
                )
                # Verify the cp command was run
                args = mock_run.call_args[0][0]
                assert "sudo" in args
                assert "cp" in args

    def test_enables_commented_job(self):
        original = "# 0 7 * * 1 /usr/bin/backup\n"
        with patch("builtins.open", mock_open(read_data=original)):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                toggle_system_job(
                    "/etc/cron.d/test", 1, enabled=True, password="hunter2"
                )
                args = mock_run.call_args[0][0]
                assert "sudo" in args

    def test_rejects_user_crontab(self):
        with pytest.raises(CronError, match="system crontabs"):
            toggle_system_job(
                "user:testuser", 1, enabled=False, password="hunter2"
            )

    def test_rejects_no_password(self):
        with pytest.raises(CronError, match="password"):
            toggle_system_job(
                "/etc/cron.d/test", 1, enabled=False, password=""
            )

    def test_wrong_password_raises_permission(self):
        original = "0 7 * * 1 /usr/bin/backup\n"
        with patch("builtins.open", mock_open(read_data=original)):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1,
                    stderr="Sorry, try again.\n[sudo] password for x:",
                    stdout="",
                )
                with pytest.raises(CronError) as exc:
                    toggle_system_job(
                        "/etc/cron.d/test", 1, enabled=False, password="wrong"
                    )
                assert exc.value.code == "permission"

    def test_invalid_line_number(self):
        original = "0 7 * * 1 /usr/bin/backup\n"
        with patch("builtins.open", mock_open(read_data=original)):
            with pytest.raises(CronError, match="line not found"):
                toggle_system_job(
                    "/etc/cron.d/test", 99, enabled=False, password="hunter2"
                )

    def test_rejects_non_system_path(self):
        with pytest.raises(CronError, match="system crontabs"):
            toggle_system_job(
                "/etc/passwd", 1, enabled=False, password="hunter2"
            )
