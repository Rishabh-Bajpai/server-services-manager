"""Manage Docker containers via the Python docker SDK.

The Flask app typically runs as a non-root user, so the manager only
needs the standard Docker socket (``/var/run/docker.sock``) to be
readable by the user. Most operations (list, inspect, logs, stats) are
read-only and safe; lifecycle actions (start/stop/restart/remove) and
``docker exec`` go through the same socket without sudo.

If the socket is missing or unreadable, ``is_available()`` returns
``False`` and the page renders a hint card rather than crashing.
"""
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("DockerManager")

DEFAULT_SOCKET = "unix:///var/run/docker.sock"
DOCKER_PACKAGE_MIN_VERSION = "5.0"  # Loose floor; works on any modern SDK


class DockerError(Exception):
    """Raised when a Docker operation fails."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


# Docker emits RFC 3339 timestamps with up to nanosecond precision
# (e.g. "2024-01-15T10:30:00.123456789Z"), but datetime.fromisoformat
# on Python < 3.11 accepts at most 6 fractional digits. Truncate any
# longer fraction to microseconds first so parsing works on 3.10 too.
_RFC3339_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _parse_rfc3339_ts(raw: str) -> Optional[float]:
    """Parse a Docker RFC 3339 timestamp to a Unix timestamp.

    Returns None when missing or unparseable; naive datetimes are
    assumed to be UTC.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = _RFC3339_FRAC_RE.sub(r"\1", raw.strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class Container:
    id: str
    name: str
    image: str
    state: str
    status: str
    created: int
    ports: str = ""
    labels: Dict[str, str] = field(default_factory=dict)
    mounts: str = ""
    network: str = ""
    command: str = ""
    image_id: str = ""
    compose_project: str = ""

    @property
    def short_id(self) -> str:
        return self.id[:12] if self.id else ""

    @property
    def full_id(self) -> str:
        return self.id

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    def to_summary(self) -> dict:
        return {
            "id": self.short_id,
            "full_id": self.id,
            "name": self.name,
            "image": self.image,
            "state": self.state,
            "status": self.status,
            "created": self.created,
            "ports": self.ports,
            "is_running": self.is_running,
            "compose_project": self.compose_project,
        }

    def to_detail(self) -> dict:
        return {**self.to_summary(), **{
            "labels": self.labels,
            "mounts": self.mounts,
            "network": self.network,
            "command": self.command,
            "image_id": self.image_id,
        }}


# ---------------------------------------------------------------------------
# Availability detection
# ---------------------------------------------------------------------------

def is_available(timeout: float = 1.0) -> dict:
    """Probe whether the Docker daemon is reachable.

    Returns ``{available, reason, version}``. ``reason`` is human-readable
    and is shown to the user when the daemon is unreachable so they know
    what to fix (typically: add user to docker group, start the daemon).
    """
    # Cheap import test first: if the docker SDK isn't installed, fail fast
    try:
        import docker  # noqa: F401  — checked via ImportError, only side-effect import
    except ImportError:
        return {
            "available": False,
            "reason": "Python `docker` package is not installed. Run: pip install docker",
            "version": None,
        }

    if not os.path.exists("/var/run/docker.sock"):
        return {
            "available": False,
            "reason": "Docker socket /var/run/docker.sock not found. Is Docker installed?",
            "version": None,
        }

    try:
        client = _client(timeout=timeout)
        info = client.info()
        version = info.get("ServerVersion") or ""
        return {"available": True, "reason": "", "version": version}
    except Exception as e:  # noqa: BLE001 — any failure means "unavailable"
        reason = str(e)
        if "Permission denied" in reason or "permission" in reason.lower():
            reason = (
                "Permission denied reading /var/run/docker.sock. "
                "Add your user to the docker group: "
                "`sudo usermod -aG docker $USER` (then log out and back in)"
            )
        elif "Cannot connect" in reason or "Connection refused" in reason or "Errno 111" in reason:
            reason = "Docker daemon is not running. Start it with: `sudo systemctl start docker`"
        elif "No such file" in reason:
            reason = "Docker socket missing. Is Docker installed and the daemon running?"
        return {"available": False, "reason": reason, "version": None}


# ---------------------------------------------------------------------------
# Client factory (mockable in tests)
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()


def _client(timeout: float = 5.0):
    """Return a fresh DockerClient. The SDK client is cheap to construct."""
    import docker  # type: ignore
    from docker.errors import APIError  # noqa: F401  (imported for caller visibility)
    try:
        return docker.DockerClient(base_url=DEFAULT_SOCKET, timeout=timeout)
    except Exception as e:  # noqa: BLE001  (version probe needs socket, may raise DockerException)
        raise DockerError(_format_error(e), code=_classify(e))


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def list_containers(all_containers: bool = True) -> List[Container]:
    """List containers, by default including stopped ones."""
    client = _client()
    try:
        items = client.containers.list(all=all_containers)
    except Exception as e:  # noqa: BLE001
        raise DockerError(_format_error(e), code=_classify(e))
    out: List[Container] = []
    for c in items:
        attrs = _safe_attrs(c)
        if not attrs:
            continue
        out.append(_attrs_to_container(attrs))
    return out


def get_container(id_or_name: str) -> Optional[Container]:
    """Return a single container's detail, or None if not found."""
    client = _client()
    try:
        c = client.containers.get(id_or_name)
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            return None
        raise DockerError(_format_error(e), code=_classify(e))
    attrs = _safe_attrs(c)
    if not attrs:
        return None
    return _attrs_to_container(attrs)


def get_logs(id_or_name: str, tail: int = 100) -> List[str]:
    """Return the most recent log lines (one-shot, no streaming)."""
    if tail < 1:
        tail = 1
    if tail > 5000:
        tail = 5000
    client = _client()
    try:
        c = client.containers.get(id_or_name)
        raw = c.logs(tail=tail, timestamps=False).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            raise DockerError(f"Container {id_or_name} not found", code="not_found")
        raise DockerError(_format_error(e), code=_classify(e))
    return raw.splitlines()


def stream_logs(id_or_name: str, tail: int = 100):
    """Generator yielding log lines as they arrive. Stops when the
    container stops or the generator is closed.
    """
    client = _client(timeout=None)
    try:
        c = client.containers.get(id_or_name)
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            raise DockerError(f"Container {id_or_name} not found", code="not_found")
        raise DockerError(_format_error(e), code=_classify(e))

    def gen():
        # ``stream=True`` makes logs() an iterator. follow=True keeps
        # tailing until the consumer breaks out.
        try:
            stream = c.logs(stream=True, follow=True, tail=tail, timestamps=False)
            for chunk in stream:
                try:
                    text = chunk.decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                for line in text.splitlines():
                    if line:
                        yield line
        except Exception as e:  # noqa: BLE001
            # The stream breaks when the container stops; that's normal.
            logger.debug(f"log stream for {id_or_name} ended: {e}")

    return gen()


def get_stats(id_or_name: str) -> dict:
    """Return a one-shot stats snapshot for a single container.

    Returns ``{cpu_percent, memory_usage, memory_limit, network_rx, network_tx, blk_read, blk_write}``.
    """
    client = _client()
    try:
        c = client.containers.get(id_or_name)
        # stream=False returns a single dict, not a generator
        raw = c.stats(stream=False)
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            raise DockerError(f"Container {id_or_name} not found", code="not_found")
        raise DockerError(_format_error(e), code=_classify(e))

    cpu_total = 0
    cpu_sys = 0
    cpu_delta = 0
    try:
        cpu_stats = raw.get("cpu_stats", {}) or {}
        precpu = raw.get("precpu_stats", {}) or {}
        cpu_total = (cpu_stats.get("cpu_usage", {}) or {}).get("total_usage", 0)
        cpu_sys = (cpu_stats.get("system_cpu_usage", 0) or 0)
        cpu_prev = (precpu.get("cpu_usage", {}) or {}).get("total_usage", 0)
        sys_prev = (precpu.get("system_cpu_usage", 0) or 0)
        cpu_delta = max(cpu_total - cpu_prev, 0)
        sys_delta = max(cpu_sys - sys_prev, 0)
    except Exception:  # noqa: BLE001
        pass
    if sys_delta > 0 and cpu_delta > 0:
        ncpu = (raw.get("cpu_stats", {}) or {}).get("online_cpus", 1) or 1
        cpu_percent = (cpu_delta / sys_delta) * ncpu * 100.0
    else:
        cpu_percent = 0.0

    mem_stats = (raw.get("memory_stats", {}) or {})
    mem_usage = mem_stats.get("usage", 0) or 0
    mem_limit = mem_stats.get("limit", 1) or 1
    mem_percent = (mem_usage / mem_limit) * 100.0 if mem_limit else 0.0

    net_rx = 0
    net_tx = 0
    try:
        nets = (raw.get("networks", {}) or {})
        for iface, data in nets.items():
            net_rx += int((data or {}).get("rx_bytes", 0) or 0)
            net_tx += int((data or {}).get("tx_bytes", 0) or 0)
    except Exception:  # noqa: BLE001
        pass

    blk_read = 0
    blk_write = 0
    try:
        blkio = (raw.get("blkio_stats", {}) or {}).get("io_service_bytes_recursive", []) or []
        for entry in blkio:
            op = (entry or {}).get("op", "")
            v = int((entry or {}).get("value", 0) or 0)
            if op == "read" or op == "Read":
                blk_read += v
            elif op == "write" or op == "Write":
                blk_write += v
    except Exception:  # noqa: BLE001
        pass

    return {
        "cpu_percent": round(cpu_percent, 2),
        "memory_usage": mem_usage,
        "memory_limit": mem_limit,
        "memory_percent": round(mem_percent, 2),
        "network_rx": net_rx,
        "network_tx": net_tx,
        "blk_read": blk_read,
        "blk_write": blk_write,
    }


def list_images() -> List[dict]:
    """Return all images as a flat list of summary dicts."""
    client = _client()
    try:
        images = client.images.list()
    except Exception as e:  # noqa: BLE001
        raise DockerError(_format_error(e), code=_classify(e))
    out = []
    for img in images:
        try:
            tags = img.attrs.get("RepoTags") or []
            # ``Created`` from the Docker API is an RFC 3339 string
            # (e.g. "2024-01-15T10:30:00.123456789Z"), not a Unix
            # timestamp. Parse it once here so the frontend doesn't
            # have to redo the work — and fall back to None when
            # unparseable so the UI can show "—".
            created_raw = img.attrs.get("Created", "")
            created_ts = _parse_rfc3339_ts(created_raw) if created_raw else None
            out.append({
                "id": (img.id or "").replace("sha256:", "")[:12],
                "full_id": img.id or "",
                "tags": tags,
                "created": created_ts,
                "size": img.attrs.get("Size", 0),
            })
        except Exception:  # noqa: BLE001
            continue
    return out


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

LIFECYCLE_ACTIONS = {"start", "stop", "restart", "kill", "pause", "unpause"}


def control(id_or_name: str, action: str) -> dict:
    """Perform a lifecycle action on a container."""
    if action not in LIFECYCLE_ACTIONS:
        raise DockerError(f"Unknown action: {action}", code="invalid")
    client = _client(timeout=20)
    try:
        c = client.containers.get(id_or_name)
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            raise DockerError(f"Container {id_or_name} not found", code="not_found")
        raise DockerError(_format_error(e), code=_classify(e))
    try:
        fn = getattr(c, action)
        fn()
    except Exception as e:  # noqa: BLE001
        raise DockerError(_format_error(e), code=_classify(e))
    return {"ok": True, "output": f"{action} {id_or_name}: ok"}


def remove_container(id_or_name: str, force: bool = False, volumes: bool = False) -> dict:
    """Remove a container (must be stopped unless ``force=True``)."""
    client = _client(timeout=20)
    try:
        c = client.containers.get(id_or_name)
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            raise DockerError(f"Container {id_or_name} not found", code="not_found")
        raise DockerError(_format_error(e), code=_classify(e))
    try:
        c.remove(force=force, v=volumes)
    except Exception as e:  # noqa: BLE001
        raise DockerError(_format_error(e), code=_classify(e))
    return {"ok": True, "output": f"removed {id_or_name}"}


def remove_image(id_or_name: str, force: bool = False) -> dict:
    """Remove a Docker image by id (short or full) or tag.

    ``id_or_name`` is what the user passed in; we re-resolve the full id
    by looking it up in ``client.images.list()`` so the underlying
    ``client.images.remove()`` always gets a stable identifier (some
    short ids collide when truncated).
    """
    client = _client(timeout=20)
    target = id_or_name
    try:
        # Try direct remove first; if it fails with a not-found we'll
        # try resolving via list.
        client.images.remove(target, force=force)
        return {"ok": True, "output": f"removed {id_or_name}"}
    except Exception as e:  # noqa: BLE001
        if not _is_not_found(e):
            raise DockerError(_format_error(e), code=_classify(e))
    # Resolve via list: match short id prefix or tag.
    try:
        for img in client.images.list():
            short_id = (img.id or "").replace("sha256:", "")[:12]
            if short_id == id_or_name or img.id == id_or_name:
                target = img.id
                break
            for tag in img.attrs.get("RepoTags") or []:
                if tag == id_or_name:
                    target = img.id
                    break
            if target != id_or_name:
                break
        else:
            raise DockerError(f"Image {id_or_name} not found", code="not_found")
        client.images.remove(target, force=force)
    except DockerError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DockerError(_format_error(e), code=_classify(e))
    return {"ok": True, "output": f"removed {id_or_name}"}


# ---------------------------------------------------------------------------
# Daemon info, image prune, daemon restart
# ---------------------------------------------------------------------------

def daemon_info() -> dict:
    """Return a small subset of ``docker info`` for the Daemon view."""
    client = _client()
    try:
        info = client.info()
    except Exception as e:  # noqa: BLE001
        raise DockerError(_format_error(e), code=_classify(e))
    return {
        "server_version": info.get("ServerVersion", ""),
        "os": info.get("OperatingSystem", ""),
        "kernel": info.get("KernelVersion", ""),
        "arch": info.get("Architecture", ""),
        "cpus": info.get("NCPU", 0),
        "memory_total": info.get("MemTotal", 0),
        "storage_driver": info.get("Driver", ""),
        "containers_running": info.get("ContainersRunning", 0),
        "containers_paused": info.get("ContainersPaused", 0),
        "containers_stopped": info.get("ContainersStopped", 0),
        "images": info.get("Images", 0),
    }


def prune_images() -> dict:
    """Delete dangling images; report count + reclaimed bytes."""
    client = _client(timeout=60)
    try:
        result = client.images.prune() or {}
    except Exception as e:  # noqa: BLE001
        raise DockerError(_format_error(e), code=_classify(e))
    deleted = result.get("ImagesDeleted") or []
    return {
        "ok": True,
        "deleted": len(deleted),
        "space_reclaimed": result.get("SpaceReclaimed", 0),
        "output": f"pruned {len(deleted)} image(s), reclaimed {result.get('SpaceReclaimed', 0)} bytes",
    }


def restart_daemon(password: str) -> dict:
    """Restart the Docker daemon via ``sudo systemctl restart docker``.

    The Flask process stays unprivileged — the user's sudo password
    (not the app login) is piped to ``sudo -S`` for this one command
    (same pattern as ``system_services`` write operations).
    """
    import subprocess

    if not password:
        raise DockerError("password required", code="permission")
    try:
        proc = subprocess.run(
            ["sudo", "-S", "systemctl", "restart", "docker"],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise DockerError("timed out restarting docker", code="timeout")
    except FileNotFoundError as e:
        raise DockerError(str(e), code="missing_tool")
    if proc.returncode != 0:
        raw = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        first = next(
            (ln.strip() for ln in raw.splitlines()
             if ln.strip() and "password for" not in ln),
            "failed to restart docker",
        )
        lower = raw.lower()
        if ("password" in lower or "authentication" in lower
                or "permission" in lower or "not in the sudoers" in lower):
            raise DockerError(first, code="permission")
        raise DockerError(first, code="error")
    return {"ok": True, "output": "docker daemon restart requested"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_attrs(container) -> dict:
    """Call ``container.attrs`` defensively — some container states make
    this raise (e.g. just-deleted). Return {} on any failure so the
    list view degrades gracefully.
    """
    try:
        return container.attrs or {}
    except Exception:  # noqa: BLE001
        return {}


def _attrs_to_container(attrs: dict) -> Container:
    name = (attrs.get("Name") or "").lstrip("/")
    state_obj = attrs.get("State", {}) or {}
    state = state_obj.get("Status", "unknown") or "unknown"
    status = _format_status(attrs)
    image = attrs.get("Config", {}).get("Image", "") or ""
    image_id = (attrs.get("Image") or "").replace("sha256:", "")[:12]
    created = attrs.get("Created", "") or ""
    # Docker uses ISO 8601 with sub-second precision (up to
    # nanoseconds); _parse_rfc3339_ts truncates to microseconds so
    # this also works on Python 3.10.
    _ts = _parse_rfc3339_ts(created)
    created_ts = int(_ts) if _ts is not None else 0
    ports = _format_ports(attrs.get("NetworkSettings", {}).get("Ports", {}) or {})
    labels = attrs.get("Config", {}).get("Labels", {}) or {}
    mounts = _format_mounts(attrs.get("Mounts", []) or [])
    network = _format_network(attrs.get("NetworkSettings", {}).get("Networks", {}) or {})
    command = (attrs.get("Config", {}) or {}).get("Cmd", []) or []
    command_str = " ".join(command) if isinstance(command, list) else str(command)
    return Container(
        id=attrs.get("Id", "") or "",
        name=name,
        image=image,
        state=state,
        status=status,
        created=created_ts,
        ports=ports,
        labels=labels,
        mounts=mounts,
        network=network,
        command=command_str,
        image_id=image_id,
        compose_project=labels.get("com.docker.compose.project", ""),
    )


def _format_status(attrs: dict) -> str:
    state = attrs.get("State", {}) or {}
    parts = []
    status = state.get("Status", "")
    if status:
        parts.append(status)
    started_at = state.get("StartedAt", "")
    finished_at = state.get("FinishedAt", "")
    if status == "running" and started_at and started_at.startswith("0001"):
        parts.append("(starting)")
    if status in ("exited", "dead") and finished_at and not finished_at.startswith("0001"):
        exit_code = state.get("ExitCode", 0)
        parts.append(f"code {exit_code}")
    return " ".join(parts) or status or "unknown"


def _format_ports(ports: dict) -> str:
    if not ports:
        return ""
    pieces = []
    for container_port, bindings in ports.items():
        if not bindings:
            pieces.append(f"{container_port} (unpublished)")
        else:
            for b in bindings:
                host = b.get("HostIp", "0.0.0.0")
                host_port = b.get("HostPort", "")
                if host == "0.0.0.0":
                    host = "*"
                pieces.append(f"{host}:{host_port}->{container_port}")
    return ", ".join(pieces)


def _format_mounts(mounts: list) -> str:
    if not mounts:
        return ""
    pieces = []
    for m in mounts:
        src = m.get("Source", "")
        dst = m.get("Destination", "")
        mode = m.get("Mode", "ro") if "Mode" in m else "rw"
        if mode == "ro":
            mode = "ro"
        else:
            mode = "rw"
        pieces.append(f"{src}:{dst}({mode})")
    return ", ".join(pieces)


def _format_network(networks: dict) -> str:
    if not networks:
        return ""
    return ", ".join(networks.keys())


def _is_not_found(exc: Exception) -> bool:
    name = type(exc).__name__
    msg = str(exc).lower()
    return (
        "NotFound" in name
        or "not found" in msg
        or "no such" in msg
        or "404" in msg
    )


def _classify(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if "permission" in msg:
        return "permission"
    if "not found" in msg or "no such" in msg or "NoSuch" in name or "NotFound" in name or "404" in msg:
        return "not_found"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "is not running" in msg or "not running" in msg:
        return "not_running"
    if "already in use" in msg or "conflict" in msg:
        return "conflict"
    return "error"


def _format_error(exc: Exception) -> str:
    """Strip noisy lines from docker SDK errors (e.g. traceback prefixes)."""
    text = str(exc) or type(exc).__name__
    text = text.splitlines()[0] if text else type(exc).__name__
    return text
