"""Tests for app/docker_manager.py.

The Docker SDK is mocked throughout to keep the test suite hermetic.
We don't need a real Docker daemon — we exercise:
- availability detection (missing socket, permission denied, OK)
- list/get/inspect
- lifecycle actions (start, stop, restart, etc.)
- log retrieval
- stats calculation
- error classification
- the Container dataclass to_summary/to_detail
"""
import pytest
from unittest.mock import MagicMock, patch

from app import docker_manager
from app.docker_manager import (
    Container,
    DockerError,
    LIFECYCLE_ACTIONS,
    _attrs_to_container,
    _classify,
    _format_error,
    _format_ports,
    _format_mounts,
    _format_network,
    _format_status,
    _is_not_found,
    control,
    get_container,
    get_logs,
    get_stats,
    is_available,
    list_containers,
    list_images,
    remove_container,
)


# ---------------------------------------------------------------------------
# Container dataclass
# ---------------------------------------------------------------------------

def test_container_short_id_truncates():
    c = Container(
        id="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        name="web", image="nginx:latest", state="running",
        status="Up", created=0,
    )
    assert c.short_id == "abcdef123456"
    assert c.is_running is True


def test_container_not_running_state():
    c = Container(id="x", name="x", image="x", state="exited",
                  status="Exited (0)", created=0)
    assert c.is_running is False


def test_container_to_summary_keys():
    c = Container(
        id="abcdef1234567890", name="web", image="nginx", state="running",
        status="Up 5m", created=1234, ports="80/tcp", compose_project="wp",
    )
    s = c.to_summary()
    assert s["id"] == "abcdef123456"
    assert s["full_id"] == "abcdef1234567890"
    assert s["name"] == "web"
    assert s["image"] == "nginx"
    assert s["state"] == "running"
    assert s["is_running"] is True
    assert s["ports"] == "80/tcp"
    assert s["compose_project"] == "wp"


def test_container_to_detail_extends_summary():
    c = Container(
        id="abc", name="w", image="i", state="running",
        status="Up", created=1, labels={"k": "v"},
    )
    d = c.to_detail()
    assert d["labels"] == {"k": "v"}
    assert d["name"] == "w"  # inherited from summary


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def test_format_ports_with_bindings():
    ports = {
        "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
        "443/tcp": None,
    }
    s = _format_ports(ports)
    assert "*:8080->80/tcp" in s
    assert "443/tcp (unpublished)" in s


def test_format_ports_empty():
    assert _format_ports({}) == ""


def test_format_mounts_ro_rw():
    mounts = [
        {"Source": "/a", "Destination": "/b", "Mode": "ro"},
        {"Source": "/c", "Destination": "/d"},
    ]
    s = _format_mounts(mounts)
    assert "/a:/b(ro)" in s
    assert "/c:/d(rw)" in s


def test_format_mounts_empty():
    assert _format_mounts([]) == ""


def test_format_network():
    nets = {"bridge": {}, "host": {}}
    assert "bridge" in _format_network(nets)
    assert "host" in _format_network(nets)


def test_format_status_running():
    attrs = {"State": {"Status": "running", "StartedAt": "2024-01-01T00:00:00Z"}}
    assert _format_status(attrs) == "running"


def test_format_status_exited_with_code():
    attrs = {"State": {"Status": "exited", "ExitCode": 137,
                       "FinishedAt": "2024-01-01T00:00:00Z"}}
    s = _format_status(attrs)
    assert "exited" in s
    assert "137" in s


# ---------------------------------------------------------------------------
# _attrs_to_container
# ---------------------------------------------------------------------------

def test_attrs_to_container_basic():
    attrs = {
        "Id": "deadbeef" + "0" * 56,
        "Name": "/web",
        "Config": {"Image": "nginx", "Labels": {"env": "prod"}, "Cmd": ["nginx", "-g", "daemon off;"]},
        "State": {"Status": "running", "StartedAt": "2024-01-01T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z"},
        "Created": "2024-01-01T00:00:00Z",
        "Image": "sha256:cafebabe",
        "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}]},
                           "Networks": {"bridge": {}}},
        "Mounts": [],
    }
    c = _attrs_to_container(attrs)
    assert c.name == "web"
    assert c.image == "nginx"
    assert c.image_id == "cafebabe"
    assert c.command == "nginx -g daemon off;"
    assert "*:80->80/tcp" in c.ports
    assert "bridge" in c.network
    assert c.labels == {"env": "prod"}
    assert c.created > 0


def test_attrs_to_container_compose_project():
    attrs = {
        "Id": "x" * 64,
        "Name": "/wp_web_1",
        "Config": {"Image": "nginx",
                   "Labels": {"com.docker.compose.project": "wp"}},
        "State": {"Status": "running"},
        "Created": "2024-01-01T00:00:00Z",
        "Image": "sha256:abc",
        "NetworkSettings": {"Ports": {}, "Networks": {}},
        "Mounts": [],
    }
    c = _attrs_to_container(attrs)
    assert c.compose_project == "wp"


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def test_is_not_found():
    class FakeExc(Exception):
        pass
    assert _is_not_found(FakeExc("container not found")) is True
    assert _is_not_found(FakeExc("No such container")) is True
    assert _is_not_found(FakeExc("other error")) is False


def test_classify_error_codes():
    assert _classify(Exception("permission denied")) == "permission"
    assert _classify(Exception("No such container")) == "not_found"
    assert _classify(Exception("operation timed out")) == "timeout"
    assert _classify(Exception("container is not running")) == "not_running"
    assert _classify(Exception("something else")) == "error"


def test_format_error_takes_first_line():
    class E(Exception):
        def __str__(self):
            return "first line\nsecond line\nthird"
    assert _format_error(E()) == "first line"


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------

def test_is_available_no_socket(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)
    result = is_available(timeout=0.1)
    assert result["available"] is False
    assert "Docker socket" in result["reason"] or "socket" in result["reason"].lower()


def test_is_available_permission_denied(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: True)
    fake = MagicMock()
    fake.info.side_effect = Exception("Permission denied reading socket")
    with patch.object(docker_manager, "_client", return_value=fake):
        result = is_available(timeout=0.1)
    assert result["available"] is False
    assert "usermod" in result["reason"]


def test_is_available_connection_refused(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: True)
    fake = MagicMock()
    fake.info.side_effect = Exception("Cannot connect to the Docker daemon")
    with patch.object(docker_manager, "_client", return_value=fake):
        result = is_available(timeout=0.1)
    assert result["available"] is False
    assert "daemon" in result["reason"].lower() or "docker" in result["reason"].lower()


def test_is_available_ok(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: True)
    fake = MagicMock()
    fake.info.return_value = {"ServerVersion": "24.0.7"}
    with patch.object(docker_manager, "_client", return_value=fake):
        result = is_available(timeout=0.1)
    assert result["available"] is True
    assert result["version"] == "24.0.7"


# ---------------------------------------------------------------------------
# list_containers
# ---------------------------------------------------------------------------

def _make_fake_container(name="web", state="running", image="nginx", image_id="abc123",
                          ports=None, labels=None, mounts=None, networks=None, cmd=None):
    fake = MagicMock()
    fake.attrs = {
        "Id": "deadbeef" + "0" * 56,
        "Name": "/" + name,
        "Config": {"Image": image, "Labels": labels or {}, "Cmd": cmd or []},
        "State": {"Status": state, "StartedAt": "2024-01-01T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z"},
        "Created": "2024-01-01T00:00:00Z",
        "Image": "sha256:" + image_id,
        "NetworkSettings": {
            "Ports": ports or {},
            "Networks": networks or {},
        },
        "Mounts": mounts or [],
    }
    return fake


def test_list_containers_all_running(monkeypatch):
    client = MagicMock()
    client.containers.list.return_value = [
        _make_fake_container("web", state="running"),
        _make_fake_container("db", state="exited"),
    ]
    with patch.object(docker_manager, "_client", return_value=client):
        items = list_containers(all_containers=True)
    assert len(items) == 2
    assert items[0].name == "web"
    assert items[0].is_running is True
    assert items[1].is_running is False


def test_list_containers_running_only(monkeypatch):
    client = MagicMock()
    client.containers.list.return_value = [
        _make_fake_container("web", state="running"),
    ]
    with patch.object(docker_manager, "_client", return_value=client):
        list_containers(all_containers=False)
    client.containers.list.assert_called_with(all=False)


def test_list_containers_skips_broken_attrs(monkeypatch):
    broken = MagicMock()
    broken.attrs = None
    client = MagicMock()
    client.containers.list.return_value = [broken]
    with patch.object(docker_manager, "_client", return_value=client):
        items = list_containers()
    assert items == []


def test_list_containers_docker_error(monkeypatch):
    client = MagicMock()
    client.containers.list.side_effect = Exception("daemon offline")
    with patch.object(docker_manager, "_client", return_value=client):
        with pytest.raises(DockerError) as exc_info:
            list_containers()
    assert exc_info.value.code == "error"


# ---------------------------------------------------------------------------
# get_container
# ---------------------------------------------------------------------------

def test_get_container_found(monkeypatch):
    fake = _make_fake_container("web", state="running", image="nginx",
                                image_id="abc123def")
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        c = get_container("web")
    assert c is not None
    assert c.name == "web"
    assert c.image_id == "abc123def"


def test_get_container_not_found(monkeypatch):
    client = MagicMock()
    client.containers.get.side_effect = Exception("No such container: missing")
    with patch.object(docker_manager, "_client", return_value=client):
        c = get_container("missing")
    assert c is None


def test_get_container_other_error(monkeypatch):
    client = MagicMock()
    client.containers.get.side_effect = Exception("daemon offline")
    with patch.object(docker_manager, "_client", return_value=client):
        with pytest.raises(DockerError):
            get_container("web")


# ---------------------------------------------------------------------------
# get_logs
# ---------------------------------------------------------------------------

def test_get_logs_basic(monkeypatch):
    fake = MagicMock()
    fake.logs.return_value = b"line 1\nline 2\nline 3\n"
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        lines = get_logs("web", tail=10)
    assert lines == ["line 1", "line 2", "line 3"]
    fake.logs.assert_called_with(tail=10, timestamps=False)


def test_get_logs_clamps_tail():
    """Tail is clamped to a sane range before passing to docker."""
    fake = MagicMock()
    fake.logs.return_value = b""
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        get_logs("web", tail=99999)
    fake.logs.assert_called_with(tail=5000, timestamps=False)

    with patch.object(docker_manager, "_client", return_value=client):
        get_logs("web", tail=0)
    fake.logs.assert_called_with(tail=1, timestamps=False)


def test_get_logs_not_found(monkeypatch):
    client = MagicMock()
    client.containers.get.side_effect = Exception("No such container: gone")
    with patch.object(docker_manager, "_client", return_value=client):
        with pytest.raises(DockerError) as exc_info:
            get_logs("gone")
    assert exc_info.value.code == "not_found"


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

def test_get_stats_normalises_cpu(monkeypatch):
    # CPU calculation needs system_cpu_usage and total_usage deltas.
    fake = MagicMock()
    fake.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200_000_000},
            "system_cpu_usage": 1_000_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 800_000_000,
        },
        "memory_stats": {"usage": 100 * 1024 * 1024, "limit": 1024 * 1024 * 1024},
        "networks": {"eth0": {"rx_bytes": 1234, "tx_bytes": 5678}},
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "read", "value": 100},
                {"op": "write", "value": 200},
                {"op": "Read", "value": 50},
                {"op": "Write", "value": 75},
            ]
        },
    }
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        s = get_stats("web")
    # cpu_delta=100M, sys_delta=200M, 4 cpus => 100/200 * 4 * 100 = 200%
    assert s["cpu_percent"] == 200.0
    assert s["memory_usage"] == 100 * 1024 * 1024
    assert s["memory_limit"] == 1024 * 1024 * 1024
    assert s["memory_percent"] == pytest.approx(9.77, abs=0.01)
    assert s["network_rx"] == 1234
    assert s["network_tx"] == 5678
    # 100 + 200 + 50 + 75 = 425
    assert s["blk_read"] == 150
    assert s["blk_write"] == 275


def test_get_stats_zero_deltas():
    fake = MagicMock()
    fake.stats.return_value = {
        "cpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0, "online_cpus": 1},
        "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
        "memory_stats": {"usage": 0, "limit": 1},
        "networks": {},
        "blkio_stats": {"io_service_bytes_recursive": []},
    }
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        s = get_stats("web")
    assert s["cpu_percent"] == 0.0
    assert s["memory_percent"] == 0.0


def test_get_stats_handles_missing_keys():
    """Stats payload is missing several fields — return zeros, don't crash."""
    fake = MagicMock()
    fake.stats.return_value = {}
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        s = get_stats("web")
    assert s["cpu_percent"] == 0.0
    assert s["memory_usage"] == 0


# ---------------------------------------------------------------------------
# list_images
# ---------------------------------------------------------------------------

def test_list_images_basic(monkeypatch):
    img1 = MagicMock()
    img1.id = "sha256:abc"
    img1.attrs = {"RepoTags": ["nginx:latest"], "Created": "2024-01-01", "Size": 1024}
    img2 = MagicMock()
    img2.id = "sha256:def"
    img2.attrs = {"RepoTags": None, "Created": "2024-01-02", "Size": 2048}
    client = MagicMock()
    client.images.list.return_value = [img1, img2]
    with patch.object(docker_manager, "_client", return_value=client):
        out = list_images()
    assert len(out) == 2
    assert out[0]["id"] == "abc"
    assert out[0]["tags"] == ["nginx:latest"]
    assert out[1]["tags"] == []  # None coerced to []


def test_list_images_rfc3339_created(monkeypatch):
    """Docker returns ``Created`` as an RFC 3339 string; we should
    parse it to a Unix timestamp (not a raw string), so the UI
    doesn't have to redo the work in JS."""
    img = MagicMock()
    img.id = "sha256:abc"
    img.attrs = {
        "RepoTags": ["nginx:latest"],
        "Created": "2024-01-15T10:30:00.123456789Z",
        "Size": 1024,
    }
    client = MagicMock()
    client.images.list.return_value = [img]
    with patch.object(docker_manager, "_client", return_value=client):
        out = list_images()
    assert out[0]["created"] is not None
    assert isinstance(out[0]["created"], float)
    # 2024-01-15T10:30:00 UTC = 1705314600
    assert abs(out[0]["created"] - 1705314600) < 1


def test_list_images_unparseable_created(monkeypatch):
    """When ``Created`` is missing or unparseable, return None so the
    UI can show '—' instead of 'Invalid Date'."""
    img = MagicMock()
    img.id = "sha256:abc"
    img.attrs = {"RepoTags": ["nginx:latest"], "Created": "not-a-date", "Size": 1024}
    client = MagicMock()
    client.images.list.return_value = [img]
    with patch.object(docker_manager, "_client", return_value=client):
        out = list_images()
    assert out[0]["created"] is None


# ---------------------------------------------------------------------------
# Lifecycle (control)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", list(LIFECYCLE_ACTIONS))
def test_control_lifecycle_actions(monkeypatch, action):
    fake = MagicMock()
    setattr(fake, action, MagicMock())
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        result = control("web", action)
    assert result["ok"] is True
    getattr(fake, action).assert_called_once()


def test_control_invalid_action():
    with pytest.raises(DockerError) as exc_info:
        control("web", "explode")
    assert exc_info.value.code == "invalid"


def test_control_not_found(monkeypatch):
    client = MagicMock()
    client.containers.get.side_effect = Exception("No such container: gone")
    with patch.object(docker_manager, "_client", return_value=client):
        with pytest.raises(DockerError) as exc_info:
            control("gone", "stop")
    assert exc_info.value.code == "not_found"


# ---------------------------------------------------------------------------
# remove_container
# ---------------------------------------------------------------------------

def test_remove_container_ok(monkeypatch):
    fake = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        result = remove_container("web", force=False, volumes=False)
    assert result["ok"] is True
    fake.remove.assert_called_once_with(force=False, v=False)


def test_remove_container_force_with_volumes(monkeypatch):
    fake = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = fake
    with patch.object(docker_manager, "_client", return_value=client):
        remove_container("web", force=True, volumes=True)
    fake.remove.assert_called_once_with(force=True, v=True)


def test_remove_container_not_found(monkeypatch):
    client = MagicMock()
    client.containers.get.side_effect = Exception("No such container: gone")
    with patch.object(docker_manager, "_client", return_value=client):
        with pytest.raises(DockerError) as exc_info:
            remove_container("gone")
    assert exc_info.value.code == "not_found"


def test_remove_image_ok(monkeypatch):
    client = MagicMock()
    with patch.object(docker_manager, "_client", return_value=client):
        result = docker_manager.remove_image("abc123", force=False)
    assert result["ok"] is True
    client.images.remove.assert_called_once_with("abc123", force=False)


def test_remove_image_resolves_short_id(monkeypatch):
    """A short id that isn't found directly should be resolved via list."""
    client = MagicMock()
    # First call (direct) raises not-found, second call (after resolve) succeeds.
    client.images.remove.side_effect = [
        Exception("No such image: abc123"),
        None,
    ]
    img = MagicMock()
    img.id = "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890"
    img.attrs = {"RepoTags": ["nginx:latest"]}
    client.images.list.return_value = [img]
    with patch.object(docker_manager, "_client", return_value=client):
        result = docker_manager.remove_image("abc123def456", force=False)
    assert result["ok"] is True
    # The second call should use the full id
    assert client.images.remove.call_count == 2
    full_call = client.images.remove.call_args_list[1]
    assert full_call[0][0].startswith("sha256:")


def test_remove_image_not_found(monkeypatch):
    client = MagicMock()
    client.images.remove.side_effect = Exception("No such image: gone")
    client.images.list.return_value = []
    with patch.object(docker_manager, "_client", return_value=client):
        with pytest.raises(DockerError) as exc_info:
            docker_manager.remove_image("gone")
    assert exc_info.value.code == "not_found"


# ---------------------------------------------------------------------------
# daemon_info / prune_images / restart_daemon
# ---------------------------------------------------------------------------

def test_daemon_info_subset(monkeypatch):
    client = MagicMock()
    client.info.return_value = {
        "ServerVersion": "24.0.7",
        "OperatingSystem": "Ubuntu 22.04",
        "KernelVersion": "5.15.0",
        "Architecture": "x86_64",
        "NCPU": 4,
        "MemTotal": 8 * 1024 ** 3,
        "Driver": "overlay2",
        "ContainersRunning": 3,
        "ContainersPaused": 1,
        "ContainersStopped": 2,
        "Images": 10,
    }
    with patch.object(docker_manager, "_client", return_value=client):
        out = docker_manager.daemon_info()
    assert out["server_version"] == "24.0.7"
    assert out["cpus"] == 4
    assert out["storage_driver"] == "overlay2"
    assert out["containers_running"] == 3
    assert out["images"] == 10


def test_daemon_info_error(monkeypatch):
    client = MagicMock()
    client.info.side_effect = Exception("Cannot connect to the Docker daemon")
    with patch.object(docker_manager, "_client", return_value=client):
        with pytest.raises(DockerError):
            docker_manager.daemon_info()


def test_prune_images_ok(monkeypatch):
    client = MagicMock()
    client.images.prune.return_value = {
        "ImagesDeleted": [{"Deleted": "sha256:abc"}, {"Deleted": "sha256:def"}],
        "SpaceReclaimed": 123456,
    }
    with patch.object(docker_manager, "_client", return_value=client):
        out = docker_manager.prune_images()
    assert out["ok"] is True
    assert out["deleted"] == 2
    assert out["space_reclaimed"] == 123456


def test_prune_images_error(monkeypatch):
    client = MagicMock()
    client.images.prune.side_effect = Exception("permission denied")
    with patch.object(docker_manager, "_client", return_value=client):
        with pytest.raises(DockerError) as exc_info:
            docker_manager.prune_images()
    assert exc_info.value.code == "permission"


def test_restart_daemon_requires_password():
    with pytest.raises(DockerError) as exc_info:
        docker_manager.restart_daemon("")
    assert exc_info.value.code == "permission"


def test_restart_daemon_ok(monkeypatch):
    import subprocess as _sp
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = ""
    proc.stderr = ""
    with patch.object(_sp, "run", return_value=proc) as mock_run:
        out = docker_manager.restart_daemon("secret")
    assert out["ok"] is True
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["sudo", "-S", "systemctl"]
    assert mock_run.call_args[1]["input"] == "secret\n"


def test_restart_daemon_bad_password(monkeypatch):
    import subprocess as _sp
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = ""
    proc.stderr = "Sorry, try again.\n[sudo] password for user:"
    with patch.object(_sp, "run", return_value=proc):
        with pytest.raises(DockerError) as exc_info:
            docker_manager.restart_daemon("wrong")
    assert exc_info.value.code == "permission"
