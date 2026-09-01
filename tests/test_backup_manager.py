"""Tests for app/backup_manager.py.

The systemd calls are mocked throughout. We exercise:
- job creation, validation, persistence
- update / delete
- enable / disable / trigger_now
- status (read-only, is-enabled/is-active/list-timers)
- backup command generation for each type
- prune command generation
- retention validation
- safe path validation
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import app.backup_manager as bm
from app.backup_manager import (
    BackupJob,
    _backup_command,
    _prune_command,
    _safe_path,
    _validate_name,
    _validate_type,
    create_job,
    delete_job,
    disable_job,
    enable_job,
    get_job,
    get_status,
    list_jobs,
    run_now_blocking,
    trigger_now,
    update_job,
)


@pytest.fixture
def isolated_backups(monkeypatch, tmp_path):
    """Redirect backup metadata + systemd units to tmp paths."""
    jobs_file = tmp_path / "backups.json"
    user_dir = tmp_path / "systemd_user"
    logs_dir = tmp_path / "backup_logs"
    user_dir.mkdir()
    logs_dir.mkdir()
    monkeypatch.setattr(bm, "_BACKUP_JOBS_FILE", str(jobs_file))
    monkeypatch.setattr(bm, "_USER_DIR", str(user_dir))
    monkeypatch.setattr(bm, "_BACKUP_LOGS_DIR", str(logs_dir))
    yield jobs_file, user_dir, logs_dir


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_name():
    _validate_name("ok-name_1.0")
    with pytest.raises(ValueError):
        _validate_name("")
    with pytest.raises(ValueError):
        _validate_name("with space")
    with pytest.raises(ValueError):
        _validate_name("with/slash")
    with pytest.raises(ValueError):
        _validate_name("a" * 100)


def test_validate_type():
    _validate_type("directory")
    _validate_type("mysql")
    _validate_type("postgres")
    with pytest.raises(ValueError):
        _validate_type("redis")


def test_safe_path_rejects_newlines():
    assert _safe_path("/tmp/foo") == "/tmp/foo"
    with pytest.raises(ValueError):
        _safe_path("/tmp/foo\nbar")


# ---------------------------------------------------------------------------
# BackupJob dataclass
# ---------------------------------------------------------------------------

def test_job_to_from_dict():
    j = BackupJob(
        name="db1", type="mysql", source="app_db",
        destination="/var/backups", schedule="daily", retention=14,
    )
    d = j.to_dict()
    assert d["name"] == "db1"
    assert d["retention"] == 14
    j2 = BackupJob.from_dict(d)
    assert j2.name == j.name
    assert j2.type == j.type


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------

def test_create_job_writes_metadata_and_units(isolated_backups):
    jobs_file, user_dir, _ = isolated_backups
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        job = create_job(
            name="weekly-www",
            type_="directory",
            source="/var/www",
            destination="/var/backups/www",
            schedule="weekly",
            retention=5,
        )
    assert job.name == "weekly-www"
    assert job.retention == 5
    assert jobs_file.exists()
    # Units written
    assert (user_dir / "ssm-backup-weekly-www.service").exists()
    assert (user_dir / "ssm-backup-weekly-www.timer").exists()


def test_create_job_validates_inputs(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        with pytest.raises(ValueError):
            create_job("", "directory", "/x", "/y", "daily")
        with pytest.raises(ValueError):
            create_job("ok", "redis", "/x", "/y", "daily")
        with pytest.raises(ValueError):
            create_job("ok", "directory", "", "/y", "daily")
        with pytest.raises(ValueError):
            create_job("ok", "directory", "/x", "/y", "daily", retention=0)
        with pytest.raises(ValueError):
            create_job("ok", "directory", "/x", "/y", "daily", retention=500)


def test_create_job_rejects_duplicates(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("dup", "directory", "/a", "/b", "daily")
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        with pytest.raises(ValueError):
            create_job("dup", "directory", "/a", "/b", "daily")


def test_create_job_rejects_unsafe_paths(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        with pytest.raises(ValueError):
            create_job("ok", "directory", "/a\nb", "/c", "daily")


# ---------------------------------------------------------------------------
# list / get
# ---------------------------------------------------------------------------

def test_list_jobs_empty(isolated_backups):
    assert list_jobs() == []


def test_list_and_get_job(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("a", "directory", "/src", "/dst", "daily")
        create_job("b", "mysql", "app_db", "/dst", "hourly")
    jobs = list_jobs()
    assert {j.name for j in jobs} == {"a", "b"}
    assert get_job("a").source == "/src"
    assert get_job("nope") is None


# ---------------------------------------------------------------------------
# update_job
# ---------------------------------------------------------------------------

def test_update_job_changes_fields(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("u1", "directory", "/a", "/b", "daily")
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        result = update_job("u1", schedule="hourly", retention=14)
    assert result is not None
    assert result.schedule == "hourly"
    assert result.retention == 14
    assert get_job("u1").schedule == "hourly"


def test_update_job_unknown_returns_none(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        assert update_job("nope", schedule="daily") is None


def test_update_job_validates_after_change(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("v1", "directory", "/a", "/b", "daily")
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        with pytest.raises(ValueError):
            update_job("v1", type="redis")


# ---------------------------------------------------------------------------
# delete_job
# ---------------------------------------------------------------------------

def test_delete_job(isolated_backups):
    jobs_file, user_dir, _ = isolated_backups
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("d1", "directory", "/a", "/b", "daily")
    assert (user_dir / "ssm-backup-d1.timer").exists()
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        ok = delete_job("d1")
    assert ok is True
    assert get_job("d1") is None
    assert not (user_dir / "ssm-backup-d1.timer").exists()
    assert not (user_dir / "ssm-backup-d1.service").exists()


def test_delete_unknown_returns_false(isolated_backups):
    assert delete_job("nope") is False


# ---------------------------------------------------------------------------
# enable / disable / trigger_now
# ---------------------------------------------------------------------------

def test_enable_job(monkeypatch, isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("e1", "directory", "/a", "/b", "daily")
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        ok, err = enable_job("e1", password="x")
    assert ok is True
    assert err == ""
    assert get_job("e1").enabled is True


def test_enable_job_failure(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("e2", "directory", "/a", "/b", "daily")
    # The metadata default is enabled=True (assume it works). enable_job
    # failure leaves the metadata as-is because the underlying timer
    # state is the source of truth.
    with patch.object(bm, "_run_systemctl_user", return_value=(1, "", "permission denied")):
        ok, err = enable_job("e2", password="x")
    assert ok is False
    assert "permission" in err
    # update_job is not called on failure
    assert get_job("e2").enabled is True


def test_disable_job(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("d2", "directory", "/a", "/b", "daily")
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        ok, err = disable_job("d2", password="x")
    assert ok is True
    assert err == ""
    assert get_job("d2").enabled is False


def test_trigger_now(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("t1", "directory", "/a", "/b", "daily")
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        ok, err = trigger_now("t1", password="x")
    assert ok is True
    assert err == ""


def test_trigger_now_unknown_job(isolated_backups):
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        ok, err = trigger_now("nope", password="x")
    assert ok is False
    assert "not_found" in err or err  # any error message is fine


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def test_get_status_enabled_active(isolated_backups):
    # Simulate: is-enabled=0, is-active=0, list-timers has a row
    side_effects = [
        (0, "enabled\n", ""),
        (0, "active\n", ""),
        (0, "Wed 2024-01-01 12:00:00 UTC  1h ssm-backup-x.timer ssm-backup-x.service\n", ""),
        (0, "ExecMainExitTimestamp=Wed 2024-01-01 11:00:00 UTC\n", ""),
    ]
    with patch.object(bm, "_run_systemctl_user", side_effect=side_effects):
        s = get_status("x")
    assert s["enabled"] is True
    assert s["active"] is True
    assert s["next_run"] is not None
    assert s["last_run"] is not None


def test_get_status_disabled(isolated_backups):
    side_effects = [
        (1, "", "disabled"),
        (1, "", "inactive"),
        (0, "", ""),
        (0, "", ""),
    ]
    with patch.object(bm, "_run_systemctl_user", side_effect=side_effects):
        s = get_status("x")
    assert s["enabled"] is False
    assert s["active"] is False


# ---------------------------------------------------------------------------
# Command generation
# ---------------------------------------------------------------------------

def test_backup_command_directory():
    job = BackupJob(
        name="www", type="directory", source="/var/www",
        destination="/var/backups", schedule="daily",
    )
    cmd = _backup_command(job)
    assert "tar" in cmd
    assert "/var/backups/www-" in cmd
    assert "tar.gz" in cmd


def test_backup_command_mysql():
    job = BackupJob(
        name="db", type="mysql", source="app_db",
        destination="/var/backups", schedule="hourly",
    )
    cmd = _backup_command(job)
    assert "mysqldump" in cmd
    assert "app_db" in cmd
    assert ".sql.gz" in cmd


def test_backup_command_postgres():
    job = BackupJob(
        name="db", type="postgres", source="app_db",
        destination="/var/backups", schedule="daily",
    )
    cmd = _backup_command(job)
    assert "pg_dump" in cmd
    assert "app_db" in cmd


def test_backup_command_quotes_path_with_space():
    job = BackupJob(
        name="www", type="directory", source="/var/my data",
        destination="/var/backups", schedule="daily",
    )
    cmd = _backup_command(job)
    # The space-containing basename must be quoted so the shell doesn't
    # split it. We split the source into dirname/basename for tar's -C,
    # so the full path isn't in the command but its pieces are quoted.
    assert "'my data'" in cmd
    assert "'/var/backups/my data-" in cmd


# ---------------------------------------------------------------------------
# Prune command
# ---------------------------------------------------------------------------

def test_prune_command_directory():
    job = BackupJob(
        name="www", type="directory", source="/var/www",
        destination="/var/backups", schedule="daily", retention=7,
    )
    cmd = _prune_command(job)
    assert "/var/backups/www-" in cmd
    assert "head -n -7" in cmd
    assert "rm -f" in cmd


def test_prune_command_mysql():
    job = BackupJob(
        name="db", type="mysql", source="app_db",
        destination="/var/backups", schedule="hourly", retention=14,
    )
    cmd = _prune_command(job)
    assert "/var/backups/db-" in cmd
    assert ".sql.gz" in cmd
    assert "head -n -14" in cmd


# ---------------------------------------------------------------------------
# run_now_blocking
# ---------------------------------------------------------------------------

def test_run_now_blocking_success():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "OK\n"
    fake.stderr = ""
    job = BackupJob(
        name="t", type="directory", source="/x", destination="/y", schedule="daily",
    )
    with patch.object(bm.subprocess, "run", return_value=fake):
        rc, out = run_now_blocking(job)
    assert rc == 0
    assert "OK" in out


def test_run_now_blocking_timeout():
    job = BackupJob(
        name="t", type="directory", source="/x", destination="/y", schedule="daily",
    )
    with patch.object(bm.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
        rc, out = run_now_blocking(job)
    assert rc == 124
    assert "timeout" in out


def test_run_systemctl_user_with_password_passes_str_stdin():
    """User units never need sudo — password is ignored for --user (avoids DBUS loss)."""
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "ok"
    fake.stderr = ""
    with patch.object(bm.subprocess, "run", return_value=fake) as mock_run:
        rc, out, err = bm._run_systemctl_user(["enable", "x.timer"], password="secret")
    assert rc == 0
    cmd = mock_run.call_args[0][0]
    # --user units are user-writable, no sudo needed even with password
    assert cmd[0] == "systemctl"
    stdin = mock_run.call_args.kwargs.get("input")
    assert stdin is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_persistence_roundtrip(isolated_backups):
    jobs_file, _, _ = isolated_backups
    with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
        create_job("p1", "directory", "/a", "/b", "daily")
        create_job("p2", "mysql", "app", "/c", "hourly")
    # Read raw JSON
    raw = json.loads(jobs_file.read_text())
    assert {j["name"] for j in raw} == {"p1", "p2"}


def test_corrupt_jobs_file_returns_empty(isolated_backups, monkeypatch):
    jobs_file, _, _ = isolated_backups
    jobs_file.write_text("{not valid json")
    assert list_jobs() == []


def test_missing_dir_creates_on_save(isolated_backups, tmp_path):
    # Put jobs file in a non-existent subdirectory; _save_jobs creates it.
    new_path = tmp_path / "subdir" / "backups.json"
    with patch.object(bm, "_BACKUP_JOBS_FILE", str(new_path)):
        with patch.object(bm, "_run_systemctl_user", return_value=(0, "", "")):
            create_job("z", "directory", "/a", "/b", "daily")
        assert new_path.exists()