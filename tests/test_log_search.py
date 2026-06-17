"""Tests for the log search routes.

We exercise the in-memory and on-disk filtering for /api/programs/.../logs/search
and the journalctl-backed /api/system-services/.../logs/search. The actual
subprocess calls are mocked.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.process_manager import ProgramConfig


@pytest.fixture
def client(process_manager, monkeypatch):
    """Build a Flask test client wired to the real process_manager."""
    import sys
    server_mod = sys.modules.get('server')
    if server_mod is None:
        from server import app
        server_mod = sys.modules['server']
    # Replace server.pm with the test's ProcessManager so routes see test data.
    monkeypatch.setattr(server_mod, "pm", process_manager)
    app = server_mod.app
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c, process_manager


def _login(client):
    """Inject a logged-in session for the test client."""
    with client.session_transaction() as sess:
        sess['logged_in'] = True


# ---------------------------------------------------------------------------
# /api/programs/<name>/logs/search
# ---------------------------------------------------------------------------

def test_program_logs_search_basic(client):
    cli, pm = client
    pm.add_program(ProgramConfig(name="svc1", command="echo hi", cwd="/tmp", autostart=False, environment={}))
    prog = pm.get_program("svc1")
    for line in ["info: starting up", "warning: low memory", "error: timeout reached"]:
        prog.log(line)
    _login(cli)
    r = cli.get("/api/programs/svc1/logs/search")
    assert r.status_code == 200
    data = r.get_json()
    assert "error" not in data
    assert data["matched"] == 3
    assert data["total"] == 3
    assert data["has_more"] is False


def test_program_logs_search_filter_by_substring(client):
    cli, pm = client
    pm.add_program(ProgramConfig(name="svc2", command="echo hi", cwd="/tmp", autostart=False, environment={}))
    prog = pm.get_program("svc2")
    for line in ["alpha one", "beta two", "alpha three", "gamma four"]:
        prog.log(line)
    _login(cli)
    r = cli.get("/api/programs/svc2/logs/search?search=alpha")
    data = r.get_json()
    assert data["matched"] == 2
    assert all("alpha" in ln.lower() for ln in data["logs"])


def test_program_logs_search_case_insensitive(client):
    cli, pm = client
    pm.add_program(ProgramConfig(name="svc3", command="echo hi", cwd="/tmp", autostart=False, environment={}))
    pm.get_program("svc3").log("UPPERCASE error")
    _login(cli)
    r = cli.get("/api/programs/svc3/logs/search?search=uppercase")
    data = r.get_json()
    assert data["matched"] == 1


def test_program_logs_search_pagination(client):
    cli, pm = client
    pm.add_program(ProgramConfig(name="svc4", command="echo hi", cwd="/tmp", autostart=False, environment={}))
    prog = pm.get_program("svc4")
    for i in range(50):
        prog.log(f"line {i}")
    _login(cli)
    r = cli.get("/api/programs/svc4/logs/search?limit=10&offset=0")
    data = r.get_json()
    assert data["matched"] == 50
    assert len(data["logs"]) == 10
    assert data["has_more"] is True
    # Last page
    r = cli.get("/api/programs/svc4/logs/search?limit=10&offset=40")
    data = r.get_json()
    assert data["has_more"] is False
    assert len(data["logs"]) == 10


def test_program_logs_search_not_found(client):
    cli, pm = client
    _login(cli)
    r = cli.get("/api/programs/nonexistent/logs/search")
    assert r.status_code == 404


def test_program_logs_search_filter_by_since_with_iso_timestamps(client):
    cli, pm = client
    pm.add_program(ProgramConfig(name="svc5", command="echo hi", cwd="/tmp", autostart=False, environment={}))
    prog = pm.get_program("svc5")
    prog.log("2024-01-01T10:00:00 first")
    prog.log("2024-06-15T10:00:00 mid")
    prog.log("2025-12-31T10:00:00 last")
    _login(cli)
    # Since = mid 2024 => drop the 2024-01 line
    r = cli.get("/api/programs/svc5/logs/search?since=2024-06-01")
    data = r.get_json()
    assert data["matched"] == 2
    assert not any("2024-01-01" in ln for ln in data["logs"])


def test_program_logs_search_keeps_lines_without_timestamp(client):
    """Lines without a parseable timestamp should be kept (not dropped)."""
    cli, pm = client
    pm.add_program(ProgramConfig(name="svc6", command="echo hi", cwd="/tmp", autostart=False, environment={}))
    prog = pm.get_program("svc6")
    prog.log("2024-01-01T10:00:00 dated")
    prog.log("no timestamp here")
    _login(cli)
    r = cli.get("/api/programs/svc6/logs/search?since=2024-12-01")
    data = r.get_json()
    # Dated line should be dropped (before since), undated line kept
    assert data["matched"] == 1
    assert data["logs"][0] == "no timestamp here"


def test_program_logs_search_source_disk(client, tmp_path, monkeypatch):
    """The 'source=disk' branch reads from the persistence layer."""
    cli, pm = client
    pm.add_program(ProgramConfig(name="svc7", command="echo hi", cwd="/tmp"))
    # Redirect the log persistence dir to a tmp path
    import app.log_persistence as lp
    monkeypatch.setattr(lp, "_BASE", str(tmp_path / "logs"))
    lp.append_line("svc7", "persisted one")
    lp.append_line("svc7", "persisted two")
    _login(cli)
    r = cli.get("/api/programs/svc7/logs/search?source=disk")
    data = r.get_json()
    assert data["matched"] == 2
    assert "persisted" in " ".join(data["logs"]).lower()


def test_program_logs_search_clamps_limit(client):
    cli, pm = client
    pm.add_program(ProgramConfig(name="svc8", command="echo hi", cwd="/tmp", autostart=False, environment={}))
    prog = pm.get_program("svc8")
    for i in range(10):
        prog.log(f"line {i}")
    _login(cli)
    r = cli.get("/api/programs/svc8/logs/search?limit=99999")
    data = r.get_json()
    # Server clamps to 5000
    assert data["limit"] == 5000


# ---------------------------------------------------------------------------
# /api/system-services/<name>/logs/search
# ---------------------------------------------------------------------------

def test_system_services_logs_search_basic(client):
    cli, pm = client
    _login(cli)
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "alpha one\nbeta two\nalpha three\n"
    with patch("subprocess.run", return_value=fake_proc):
        r = cli.get("/api/system-services/cron.service/logs/search?search=alpha")
    data = r.get_json()
    assert data["matched"] == 2
    assert data["has_more"] is False


def test_system_services_logs_search_passes_priority(client):
    cli, pm = client
    _login(cli)
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok\n"
    with patch("subprocess.run", return_value=fake_proc) as run_mock:
        cli.get("/api/system-services/cron.service/logs/search?priority=err")
    cmd = run_mock.call_args[0][0]
    assert "-p" in cmd
    err_idx = cmd.index("-p")
    assert cmd[err_idx + 1] == "err"


def test_system_services_logs_search_journalctl_failure(client):
    cli, pm = client
    _login(cli)
    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stderr = "Failed to access journal"
    with patch("subprocess.run", return_value=fake_proc):
        r = cli.get("/api/system-services/cron.service/logs/search")
    assert r.status_code == 500
    data = r.get_json()
    assert "Failed" in data["error"]


def test_system_services_logs_search_journalctl_missing(client):
    cli, pm = client
    _login(cli)
    with patch("subprocess.run", side_effect=FileNotFoundError):
        r = cli.get("/api/system-services/cron.service/logs/search")
    assert r.status_code == 500
    data = r.get_json()
    assert data["code"] == "missing_tool"
