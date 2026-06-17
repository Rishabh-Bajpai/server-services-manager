"""Tests for app/package_manager.py.

The actual apt/dnf commands are mocked throughout. We exercise:
- detect_manager() via shutil.which monkeypatch
- apt output parsing
- dnf output parsing
- install job lifecycle (start, tail, complete)
- stale-data detection
- security upgrade detection
"""
import time
from unittest.mock import MagicMock, patch

import pytest

import app.package_manager as pm
from app.package_manager import (
    Update,
    UpdateState,
    detect_manager,
    get_state,
    list_updates,
    refresh,
    install_packages,
    get_job,
    list_jobs,
    _parse_apt_upgradable,
    _parse_dnf_check_update,
    _apt_security_package_names,
)


# ---------------------------------------------------------------------------
# detect_manager
# ---------------------------------------------------------------------------

def test_detect_manager_apt(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: "/usr/bin/apt-get" if c == "apt-get" else None)
    assert detect_manager() == "apt"


def test_detect_manager_dnf(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: "/usr/bin/dnf" if c == "dnf" else None)
    assert detect_manager() == "dnf"


def test_detect_manager_yum(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: "/usr/bin/yum" if c == "yum" else None)
    assert detect_manager() == "yum"


def test_detect_manager_unknown(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert detect_manager() == "unknown"


# ---------------------------------------------------------------------------
# Update dataclass
# ---------------------------------------------------------------------------

def test_update_to_dict():
    u = Update(name="vim", current_version="8.0", new_version="9.0", is_security=True, repo="noble-security")
    d = u.to_dict()
    assert d["name"] == "vim"
    assert d["is_security"] is True
    assert d["repo"] == "noble-security"


def test_update_state_stale_property():
    fresh = UpdateState(manager="apt", fetched_at=time.time())
    assert fresh.is_stale is False
    old = UpdateState(manager="apt", fetched_at=time.time() - 7200)
    assert old.is_stale is True


def test_update_state_to_dict_includes_counts():
    s = UpdateState(manager="apt", fetched_at=time.time(), updates=[
        Update(name="a", current_version="1", new_version="2", is_security=True),
        Update(name="b", current_version="1", new_version="2", is_security=False),
    ])
    d = s.to_dict()
    assert d["count"] == 2
    assert d["security_count"] == 1
    assert d["manager"] == "apt"


# ---------------------------------------------------------------------------
# apt parsing
# ---------------------------------------------------------------------------

def test_parse_apt_upgradable_basic():
    sample = """\
vim/noble-updates 9.0.1234-1~ubuntu1 amd64 [upgradable from: 9.0.1111-1]
curl/noble-security 8.5.0-2ubuntu1 amd64 [upgradable from: 8.4.0-1]
git/noble 1:2.42.0-1 amd64 [upgradable from: 1:2.41.0-1]
"""
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = sample
    with patch.object(pm.subprocess, "run", return_value=fake):
        with patch.object(pm, "_apt_security_package_names", return_value={"curl"}):
            updates = _parse_apt_upgradable()
    assert len(updates) == 3
    by_name = {u.name: u for u in updates}
    assert by_name["vim"].new_version == "9.0.1234-1~ubuntu1"
    assert by_name["vim"].current_version == "9.0.1111-1"
    assert by_name["vim"].is_security is False
    assert by_name["curl"].is_security is True  # in security packages
    assert by_name["git"].repo == "noble"


def test_parse_apt_upgradable_skips_listing_header():
    sample = """\
Listing... Done
vim/noble-updates 9.0 amd64 [upgradable from: 8.0]
"""
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = sample
    with patch.object(pm.subprocess, "run", return_value=fake):
        with patch.object(pm, "_apt_security_package_names", return_value=set()):
            updates = _parse_apt_upgradable()
    assert len(updates) == 1
    assert updates[0].name == "vim"


def test_parse_apt_upgradable_empty():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "Listing... Done\n"
    with patch.object(pm.subprocess, "run", return_value=fake):
        updates = _parse_apt_upgradable()
    assert updates == []


def test_parse_apt_upgradable_propagates_error():
    fake = MagicMock()
    fake.returncode = 100
    fake.stderr = "E: Unable to lock"
    with patch.object(pm.subprocess, "run", return_value=fake):
        with pytest.raises(Exception):
            _parse_apt_upgradable()


# ---------------------------------------------------------------------------
# dnf parsing
# ---------------------------------------------------------------------------

def test_parse_dnf_basic():
    sample = """\
Last metadata expiration check: 0:00:01 ago on ...
openssh.x86_64     9.0p1     updates
vim.x86_64         9.0.1234  security
curl.x86_64        8.5.0     base
"""
    fake = MagicMock()
    fake.returncode = 100
    fake.stdout = sample
    with patch.object(pm.subprocess, "run", return_value=fake):
        updates = _parse_dnf_check_update()
    by_name = {u.name: u for u in updates}
    assert "openssh.x86_64" in by_name
    assert by_name["vim.x86_64"].is_security is True
    assert by_name["openssh.x86_64"].is_security is False
    assert by_name["curl.x86_64"].new_version == "8.5.0"


def test_parse_dnf_skips_metadata_lines():
    sample = """\
Last metadata expiration check: 0:00:01 ago on ...
Obsoleting Packages
vim.x86_64  9.0  base
"""
    fake = MagicMock()
    fake.returncode = 100
    fake.stdout = sample
    with patch.object(pm.subprocess, "run", return_value=fake):
        updates = _parse_dnf_check_update()
    assert len(updates) == 1
    assert updates[0].name == "vim.x86_64"


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

def test_refresh_apt(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    monkeypatch.setattr(pm, "_run_apt_update", lambda: True)
    monkeypatch.setattr(pm, "_parse_apt_upgradable", lambda: [
        Update(name="vim", current_version="1", new_version="2"),
    ])
    state = refresh()
    assert state.manager == "apt"
    assert len(state.updates) == 1
    assert get_state() is state


def test_refresh_apt_cache_refresh_failed(monkeypatch):
    """When _run_apt_update returns False (no sudo), we still get the
    last known upgrade list with a warning message.
    """
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    monkeypatch.setattr(pm, "_run_apt_update", lambda: False)
    monkeypatch.setattr(pm, "_parse_apt_upgradable", lambda: [
        Update(name="vim", current_version="1", new_version="2"),
    ])
    state = refresh()
    assert state.manager == "apt"
    assert len(state.updates) == 1
    assert "sudo" in state.last_error.lower() or "cache" in state.last_error.lower()


def test_refresh_unknown_manager(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "unknown")
    state = refresh()
    assert state.manager == "unknown"
    assert state.updates == []
    assert "unsupported" in state.last_error


def test_refresh_swallows_apt_update_failure(monkeypatch):
    import subprocess
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    def fail():
        raise subprocess.CalledProcessError(1, "apt-get update")
    monkeypatch.setattr(pm, "_run_apt_update", fail)
    monkeypatch.setattr(pm, "_parse_apt_upgradable", lambda: [])
    state = refresh()
    assert state.last_error != ""
    assert state.updates == []


def test_refresh_swallows_timeout(monkeypatch):
    import subprocess
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    def timeout():
        raise subprocess.TimeoutExpired("apt-get", 60)
    monkeypatch.setattr(pm, "_run_apt_update", timeout)
    state = refresh()
    assert "timed out" in state.last_error


# ---------------------------------------------------------------------------
# list_updates
# ---------------------------------------------------------------------------

def test_list_updates_empty_without_refresh(monkeypatch):
    monkeypatch.setattr(pm, "_STATE", None)
    assert list_updates() == []


def test_list_updates_returns_cached(monkeypatch):
    monkeypatch.setattr(pm, "_STATE", UpdateState(
        manager="apt", fetched_at=time.time(),
        updates=[Update(name="vim", current_version="1", new_version="2")],
    ))
    updates = list_updates()
    assert len(updates) == 1
    assert updates[0].name == "vim"


# ---------------------------------------------------------------------------
# install_packages / jobs
# ---------------------------------------------------------------------------

def test_install_packages_creates_job(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    monkeypatch.setattr(pm.os.path, "expanduser", lambda p: "/tmp" if p.startswith("~") else p)
    job_id = install_packages(["vim", "curl"])
    job = get_job(job_id)
    assert job is not None
    assert job["packages"] == ["vim", "curl"]
    assert job["manager"] == "apt"
    assert job["ended_at"] == 0  # still running
    # Wait briefly for the thread (the underlying popen will likely fail in tests)
    deadline = time.time() + 3
    while time.time() < deadline:
        j = get_job(job_id)
        if j["ended_at"] != 0:
            break
        time.sleep(0.05)
    assert get_job(job_id)["ended_at"] != 0


def test_install_packages_empty_list_no_op(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    job_id = install_packages([])
    time.sleep(0.3)
    job = get_job(job_id)
    assert job is not None
    assert job["ended_at"] != 0


def test_install_packages_unsupported_manager(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "weird")
    job_id = install_packages(["vim"])
    time.sleep(0.3)
    job = get_job(job_id)
    assert "unsupported" in open(job["log_path"]).read()


def test_list_jobs_includes_recent(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    install_packages(["a"])
    time.sleep(0.1)
    jobs = list_jobs(limit=10)
    assert len(jobs) >= 1
    assert jobs[0]["running"] in (True, False)


def test_get_job_unknown_returns_none():
    assert get_job("nope") is None


# ---------------------------------------------------------------------------
# security-package detection
# ---------------------------------------------------------------------------

def test_apt_security_package_names(monkeypatch):
    sample = """\
vim/noble 1.0
curl/noble-security 2.0
git/noble-security 1.0
"""
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = sample
    with patch.object(pm.subprocess, "run", return_value=fake):
        names = _apt_security_package_names()
    assert "curl" in names
    assert "git" in names
    assert "vim" not in names


def test_apt_security_package_names_handles_error(monkeypatch):
    import subprocess
    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, "apt")
    with patch.object(pm.subprocess, "run", side_effect=fail):
        assert _apt_security_package_names() == set()
