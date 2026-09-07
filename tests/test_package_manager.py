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
    monkeypatch.setattr(pm, "_run_apt_update", lambda **kw: True)
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
    monkeypatch.setattr(pm, "_run_apt_update", lambda **kw: False)
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
    def timeout(**kw):
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


# ---------------------------------------------------------------------------
# lookup_package
# ---------------------------------------------------------------------------

def _fake_policy(stdout, returncode=0):
    fake = MagicMock()
    fake.returncode = returncode
    fake.stdout = stdout
    return fake


def test_lookup_package_up_to_date(monkeypatch):
    # Real apt-cache output is indented with two spaces; the parser
    # strips lines before matching, so the prefixes must match the
    # stripped form.
    sample = "bash:\n  Installed: 5.2.21-2ubuntu4\n  Candidate: 5.2.21-2ubuntu4\n"
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    with patch.object(pm.subprocess, "run", return_value=_fake_policy(sample)):
        result = pm.lookup_package("bash")
    assert result["installed"] == "5.2.21-2ubuntu4"
    assert result["candidate"] == "5.2.21-2ubuntu4"
    assert result["available_upgrade"] is None
    assert "error" not in result


def test_lookup_package_upgrade_available(monkeypatch):
    sample = "vim:\n  Installed: 9.0\n  Candidate: 9.1\n"
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    with patch.object(pm.subprocess, "run", return_value=_fake_policy(sample)):
        result = pm.lookup_package("vim")
    assert result["installed"] == "9.0"
    assert result["available_upgrade"] == "9.1"


def test_lookup_package_not_installed(monkeypatch):
    sample = "htop:\n  Installed: (none)\n  Candidate: 3.3.0\n"
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    with patch.object(pm.subprocess, "run", return_value=_fake_policy(sample)):
        result = pm.lookup_package("htop")
    assert result["installed"] is None
    assert result["candidate"] == "3.3.0"
    assert result["available_upgrade"] is None


def test_lookup_package_not_found(monkeypatch):
    sample = "N: Unable to locate package nosuchpkg\n"
    fake = _fake_policy(sample, returncode=100)
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    with patch.object(pm.subprocess, "run", return_value=fake):
        result = pm.lookup_package("nosuchpkg")
    assert result["error"] == "package not found"


def test_lookup_package_unsupported_manager(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "unknown")
    result = pm.lookup_package("bash")
    assert "unsupported" in result["error"]


# ---------------------------------------------------------------------------
# sudo password paths
# ---------------------------------------------------------------------------

def test_run_apt_update_with_password_success():
    fake = MagicMock()
    fake.returncode = 0
    with patch.object(pm.subprocess, "run", return_value=fake) as run:
        assert pm._run_apt_update(password="secret") is True
    args, kwargs = run.call_args
    assert "-S" in args[0]
    assert kwargs["input"] == "secret\n"


def test_run_apt_update_with_password_rejected():
    fake = MagicMock()
    fake.returncode = 1
    fake.stderr = "[sudo] password for user: Sorry, try again.\n"
    with patch.object(pm.subprocess, "run", return_value=fake):
        with pytest.raises(pm.SudoAuthError):
            pm._run_apt_update(password="wrong")


def test_refresh_with_password_threads_through(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    seen = {}
    def fake_update(password=None):
        seen["password"] = password
        return True
    monkeypatch.setattr(pm, "_run_apt_update", fake_update)
    monkeypatch.setattr(pm, "_parse_apt_upgradable", lambda: [])
    state = pm.refresh(password="secret")
    assert seen["password"] == "secret"
    assert state.last_error == ""


def _make_job(tmp_path, job_id="job-1"):
    log_path = str(tmp_path / f"{job_id}.log")
    return {"id": job_id, "manager": "apt", "packages": ["vim"],
            "started_at": 0.0, "ended_at": 0.0, "success": False,
            "log_path": log_path, "tail": ""}


def test_run_install_with_password_uses_sudo_S(tmp_path):
    job = _make_job(tmp_path)
    validator = MagicMock()
    validator.returncode = 0
    proc = MagicMock()
    proc.returncode = 0
    with patch.object(pm.subprocess, "run", return_value=validator) as run, \
         patch.object(pm.subprocess, "Popen", return_value=proc) as popen:
        pm._run_install(job, None, password="secret")
    # Non-destructive `sudo -v` pre-check first ...
    run_args, run_kwargs = run.call_args
    assert run_args[0][:4] == ["sudo", "-S", "-p", ""]
    assert run_args[0][4] == "-v"
    assert run_kwargs["input"] == "secret\n"
    # ... then the install with end-of-options delimiter ...
    popen_args, popen_kwargs = popen.call_args
    assert "-S" in popen_args[0]
    assert "-n" not in popen_args[0]
    assert "--" in popen_args[0]
    assert "DEBIAN_FRONTEND=noninteractive" in popen_args[0]
    assert popen_kwargs["stdin"] == pm.subprocess.PIPE
    proc.communicate.assert_called_once_with("secret\n")
    assert job["success"] is True


def test_run_install_with_wrong_password_fails_fast(tmp_path):
    job = _make_job(tmp_path, "job-2")
    validator = MagicMock()
    validator.returncode = 1
    validator.stderr = "Sorry, try again.\n"
    with patch.object(pm.subprocess, "run", return_value=validator), \
         patch.object(pm.subprocess, "Popen") as popen:
        pm._run_install(job, None, password="wrong")
    popen.assert_not_called()
    assert job["success"] is False
    assert job["ended_at"] != 0
    with open(job["log_path"]) as f:
        assert "sudo rejected the password" in f.read()


def test_run_install_without_password_probes_sudo_n_v(tmp_path):
    job = _make_job(tmp_path, "job-3")
    probe = MagicMock()
    probe.returncode = 0
    proc = MagicMock()
    proc.returncode = 0
    with patch.object(pm.subprocess, "run", return_value=probe) as run, \
         patch.object(pm.subprocess, "Popen", return_value=proc) as popen:
        pm._run_install(job, None)
    # Non-destructive probe, never `apt-get install` via run().
    run_args, _ = run.call_args
    assert run_args[0] == ["sudo", "-n", "-v"]
    popen_args, _ = popen.call_args
    assert "install" in popen_args[0]
    assert "--" in popen_args[0]
    assert job["success"] is True


def test_run_install_without_password_probe_failed(tmp_path):
    job = _make_job(tmp_path, "job-4")
    probe = MagicMock()
    probe.returncode = 1
    with patch.object(pm.subprocess, "run", return_value=probe), \
         patch.object(pm.subprocess, "Popen") as popen:
        pm._run_install(job, None)
    popen.assert_not_called()
    assert job["success"] is False
    with open(job["log_path"]) as f:
        assert "sudo requires a password" in f.read()


def test_run_apt_update_apt_failure_is_not_auth_error():
    # Correct password but broken mirror: must raise, not SudoAuthError.
    import subprocess
    fake = MagicMock()
    fake.returncode = 100
    fake.stdout = ""
    fake.stderr = "Err:1 http://mirror/ noble Release\n  404 Not Found\n"
    with patch.object(pm.subprocess, "run", return_value=fake):
        with pytest.raises(subprocess.CalledProcessError):
            pm._run_apt_update(password="correct")


def test_refresh_reports_rejected_password(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    def fail(password=None):
        raise pm.SudoAuthError("sudo rejected the password; showing last known list")
    monkeypatch.setattr(pm, "_run_apt_update", fail)
    monkeypatch.setattr(pm, "_parse_apt_upgradable", lambda: [])
    state = pm.refresh(password="wrong")
    assert "sudo rejected" in state.last_error


def test_run_install_dnf_with_password_no_double_dash(tmp_path):
    log_path = str(tmp_path / "dnf-job.log")
    job = {"id": "job-dnf", "manager": "dnf", "packages": ["vim"],
           "started_at": 0.0, "ended_at": 0.0, "success": False,
           "log_path": log_path, "tail": ""}
    validator = MagicMock()
    validator.returncode = 0
    proc = MagicMock()
    proc.returncode = 0
    with patch.object(pm.subprocess, "run", return_value=validator), \
         patch.object(pm.subprocess, "Popen", return_value=proc) as popen:
        pm._run_install(job, None, password="secret")
    popen_args, popen_kwargs = popen.call_args
    assert popen_args[0][:4] == ["sudo", "-S", "-p", ""]
    assert popen_args[0][4:7] == ["dnf", "install", "-y"]
    assert "--" not in popen_args[0]
    proc.communicate.assert_called_once_with("secret\n")
    assert job["success"] is True


def test_install_packages_passes_password_to_worker(monkeypatch):
    monkeypatch.setattr(pm, "detect_manager", lambda: "apt")
    seen = {}
    def fake_run(job, on_done, password=None):
        seen["password"] = password
        with pm._JOBS_LOCK:
            job["ended_at"] = 1.0
            job["success"] = True
    monkeypatch.setattr(pm, "_run_install", fake_run)
    pm.install_packages(["vim"], password="secret")
    for _ in range(100):
        if seen.get("password"):
            break
        time.sleep(0.05)
    assert seen.get("password") == "secret"
