"""Manage and introspect systemd units via systemctl.

The Flask app typically runs as a non-root user, so write operations
(start/stop/restart/enable/disable/edit) require elevated privileges.
We obtain them by piping the user's app password to ``sudo -S``.

Read operations (list, status, show, cat, log) do not require sudo and
are cached for a short window to keep the UI snappy on hosts with
hundreds of unit files.
"""
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("SystemServices")

# Unit types exposed in the UI
UNIT_TYPES = ("service", "timer", "socket", "path", "mount")

# Properties requested for the "details" view. ``systemctl show`` accepts a
# comma-separated list, returning one KEY=VALUE per line.
_DETAIL_PROPS = (
    "Id,Description,LoadState,ActiveState,SubState,UnitFileState,"
    "MainPID,ExecMainStartTimestamp,ExecMainExitTimestamp,"
    "FragmentPath,DropInPaths,Requires,Wants,RequiredBy,WantedBy,"
    "After,Before,TriggeredBy,TriggeredByNames,CanStart,CanStop,"
    "CanReload,CanIsolate,Type,Restart,ExecStart,Slice,MemoryCurrent,"
    "MemoryPeak,CPUUsageNSec,ActiveEnterTimestamp,InactiveEnterTimestamp"
)

# Cache TTL for read operations (seconds)
_CACHE_TTL = 2.0
_CACHE: Dict[str, Tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()


class SystemServicesError(Exception):
    """Raised when an operation against systemctl fails."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


@dataclass
class Unit:
    name: str
    type: str
    description: str
    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str
    main_pid: int
    triggered_by: str
    requires: str
    wants: str
    after: str
    path: str
    has_override: bool
    can_start: bool
    can_stop: bool
    can_reload: bool
    fragment_path: str = ""
    drop_in_paths: str = ""
    exec_start: str = ""
    cpu_usage: int = 0
    memory_current: int = 0
    memory_peak: int = 0
    active_enter_timestamp: str = ""
    inactive_enter_timestamp: str = ""
    type_kind: str = ""

    @property
    def is_active(self) -> bool:
        return self.active_state == "active"

    @property
    def is_running(self) -> bool:
        return self.active_state == "active" and self.sub_state == "running"

    @property
    def is_failed(self) -> bool:
        return self.active_state == "failed"

    @property
    def is_enabled(self) -> bool:
        return self.unit_file_state in ("enabled", "enabled-runtime", "alias")

    @property
    def state_label(self) -> str:
        if self.is_failed:
            return "failed"
        if self.active_state == "inactive" and self.unit_file_state == "static":
            return "static"
        if self.active_state == "active":
            return self.sub_state or "active"
        return self.active_state or self.sub_state or "unknown"

    def to_summary(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "active_state": self.active_state,
            "sub_state": self.sub_state,
            "unit_file_state": self.unit_file_state,
            "state_label": self.state_label,
            "main_pid": self.main_pid,
            "triggered_by": self.triggered_by,
            "is_active": self.is_active,
            "is_running": self.is_running,
            "is_failed": self.is_failed,
            "is_enabled": self.is_enabled,
            "has_override": self.has_override,
        }

    def to_detail(self) -> dict:
        return {**self.to_summary(), **{
            "load_state": self.load_state,
            "requires": self.requires,
            "wants": self.wants,
            "after": self.after,
            "path": self.path,
            "can_start": self.can_start,
            "can_stop": self.can_stop,
            "can_reload": self.can_reload,
            "fragment_path": self.fragment_path,
            "drop_in_paths": self.drop_in_paths,
            "exec_start": self.exec_start,
            "cpu_usage": self.cpu_usage,
            "memory_current": self.memory_current,
            "memory_peak": self.memory_peak,
            "active_enter_timestamp": self.active_enter_timestamp,
            "inactive_enter_timestamp": self.inactive_enter_timestamp,
        }}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(cmd: List[str], timeout: int = 10, password: Optional[str] = None) -> str:
    """Run a command, optionally piping a password to sudo."""
    try:
        if password is not None and cmd and cmd[0] == "sudo":
            proc = subprocess.run(
                cmd,
                input=password + "\n",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        raise SystemServicesError(f"Command timed out: {' '.join(cmd)}", code="timeout")
    except FileNotFoundError as e:
        raise SystemServicesError(str(e), code="missing_tool")

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raw_message = err or out or f"Command failed with code {proc.returncode}"
        # Truncate to the first informative line for friendlier UI
        first_line = next(
            (ln.strip() for ln in raw_message.splitlines() if ln.strip() and "password for" not in ln),
            raw_message.splitlines()[0] if raw_message.splitlines() else raw_message,
        )
        lower = raw_message.lower()
        if "password" in lower or "authentication" in lower or "permission" in lower or "not in the sudoers" in lower or "incorrect password" in lower:
            code = "permission"
        else:
            code = "error"
        raise SystemServicesError(first_line, code=code)
    return out


def _cache_get(key: str):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        ts, value = entry
        if time.time() - ts > _CACHE_TTL:
            _CACHE.pop(key, None)
            return None
        return value


def _cache_set(key: str, value) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)


def cache_invalidate(prefix: Optional[str] = None) -> None:
    with _CACHE_LOCK:
        if prefix is None:
            _CACHE.clear()
        else:
            for key in list(_CACHE.keys()):
                if key.startswith(prefix):
                    _CACHE.pop(key, None)


def _parse_show(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _unit_type(name: str) -> str:
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1]


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def list_units(
    unit_type: str = "all",
    state: str = "all",
    search: str = "",
    only_user: bool = False,
) -> List[Unit]:
    """Return units, optionally filtered."""
    types = UNIT_TYPES if unit_type in ("all", "") else (unit_type,)
    cache_key = f"list:{unit_type}:{state}:{search.lower()}:{int(only_user)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    cmd = ["systemctl", "list-units", "--no-pager", "--no-legend", "--plain", "--all"]
    if types:
        cmd.append(f"--type={','.join(types)}")
    if only_user:
        cmd.append("--user")

    try:
        raw = _run(cmd, timeout=15)
    except SystemServicesError as e:
        logger.warning(f"list-units failed: {e}")
        return []

    units: List[Unit] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        name = parts[0]
        if _unit_type(name) not in UNIT_TYPES:
            continue
        load_state = parts[1]
        active_state = parts[2]
        sub_state = parts[3]
        description = " ".join(parts[4:]) if len(parts) > 4 else ""
        if state != "all" and active_state != state:
            continue
        if search and search.lower() not in name.lower():
            continue

        units.append(Unit(
            name=name,
            type=_unit_type(name),
            description=description,
            load_state=load_state,
            active_state=active_state,
            sub_state=sub_state,
            unit_file_state="",
            main_pid=0,
            triggered_by="",
            requires="",
            wants="",
            after="",
            path="",
            has_override=False,
            can_start=True,
            can_stop=True,
            can_reload=False,
        ))

    # Enrich each unit (one show call per unit; the alternative batch
    # call loses per-unit field separation)
    for u in units:
        _enrich_single(u)

    _cache_set(cache_key, units)
    return units


def _enrich_single(u: Unit) -> Unit:
    try:
        raw = _run(
            ["systemctl", "show", u.name, "--no-pager", "--property", _DETAIL_PROPS],
            timeout=10,
        )
    except SystemServicesError:
        return u

    data = _parse_show(raw)
    if not data:
        return u
    u.description = data.get("Description", u.description) or u.description
    u.load_state = data.get("LoadState", u.load_state) or u.load_state
    u.active_state = data.get("ActiveState", u.active_state) or u.active_state
    u.sub_state = data.get("SubState", u.sub_state) or u.sub_state
    u.unit_file_state = data.get("UnitFileState", u.unit_file_state)
    try:
        u.main_pid = int(data.get("MainPID", "0") or "0")
    except ValueError:
        pass
    u.triggered_by = data.get("TriggeredByNames", "") or data.get("TriggeredBy", "")
    u.requires = data.get("Requires", "")
    u.wants = data.get("Wants", "")
    u.after = data.get("After", "")
    u.fragment_path = data.get("FragmentPath", "")
    u.drop_in_paths = data.get("DropInPaths", "")
    u.exec_start = data.get("ExecStart", "")
    u.active_enter_timestamp = data.get("ActiveEnterTimestamp", "")
    u.inactive_enter_timestamp = data.get("InactiveEnterTimestamp", "")
    try:
        u.cpu_usage = int(data.get("CPUUsageNSec", "0") or "0")
    except ValueError:
        pass
    try:
        u.memory_current = int(data.get("MemoryCurrent", "0") or "0")
    except ValueError:
        pass
    try:
        u.memory_peak = int(data.get("MemoryPeak", "0") or "0")
    except ValueError:
        pass

    u.path = u.fragment_path
    u.has_override = bool(u.drop_in_paths)
    u.can_start = data.get("CanStart", "yes") in ("yes", "disallow-inhibit")
    u.can_stop = data.get("CanStop", "yes") in ("yes", "disallow-inhibit")
    u.can_reload = data.get("CanReload", "no") in ("yes", "disallow-inhibit")
    u.type_kind = data.get("Type", "")
    return u


def get_unit(name: str) -> Optional[Unit]:
    """Return full detail for a single unit, or None if it doesn't exist."""
    cache_key = f"unit:{name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    try:
        show_raw = _run(
            ["systemctl", "show", name, "--no-pager", "--property", _DETAIL_PROPS],
            timeout=10,
        )
    except SystemServicesError:
        return None

    data = _parse_show(show_raw)
    if not data.get("Id"):
        return None
    # Reject if the unit is genuinely missing on the system
    if data.get("LoadState") in ("not-found", "") and not data.get("FragmentPath"):
        return None

    u = Unit(
        name=name,
        type=_unit_type(name),
        description="",
        load_state="",
        active_state="",
        sub_state="",
        unit_file_state="",
        main_pid=0,
        triggered_by="",
        requires="",
        wants="",
        after="",
        path="",
        has_override=False,
        can_start=True,
        can_stop=True,
        can_reload=False,
    )
    _enrich_single(u)
    _cache_set(cache_key, u)
    return u


def get_unit_file(name: str) -> str:
    """Return the rendered unit file (vendor + drop-ins) via ``systemctl cat``."""
    cache_key = f"cat:{name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        out = _run(["systemctl", "cat", name], timeout=10)
    except SystemServicesError as e:
        # systemctl cat returns exit 1 with "No files found" for unknown units
        if "no files" in str(e).lower() or e.code == "error":
            raise SystemServicesError(str(e), code="not_found")
        raise
    if not out:
        raise SystemServicesError(f"No files found for {name}.", code="not_found")
    _cache_set(cache_key, out)
    return out


def get_unit_logs(name: str, lines: int = 100) -> List[str]:
    """Return the most recent log lines for a unit via journalctl."""
    if lines < 1:
        lines = 1
    if lines > 5000:
        lines = 5000
    cmd = ["journalctl", "-u", name, "-n", str(lines), "--no-pager", "-o", "short"]
    try:
        raw = _run(cmd, timeout=10)
    except SystemServicesError as e:
        if e.code == "missing_tool":
            return ["(journalctl is not installed on this system)"]
        return [f"(failed to read logs: {e})"]
    return raw.splitlines()


# ---------------------------------------------------------------------------
# Write API (requires sudo + password)
# ---------------------------------------------------------------------------

ACTIONS = {
    "start", "stop", "restart", "reload",
    "enable", "disable", "mask", "unmask",
    "daemon-reload",
}


def control(name: str, action: str, password: str) -> dict:
    """Perform a privileged action on a unit.

    Returns ``{ok, output}``; raises ``SystemServicesError`` on failure.
    """
    if action not in ACTIONS:
        raise SystemServicesError(f"Unknown action: {action}", code="invalid")

    cache_invalidate(f"unit:{name}")
    cache_invalidate(f"cat:{name}")
    cache_invalidate("list:")

    if action == "daemon-reload":
        out = _run(
            ["sudo", "-S", "systemctl", "daemon-reload"],
            timeout=20,
            password=password,
        )
        return {"ok": True, "output": out or "daemon-reload complete"}

    cmd = ["sudo", "-S", "systemctl", action, name]
    out = _run(cmd, timeout=30, password=password)
    return {"ok": True, "output": out or f"{action} {name}: ok"}


def verify_password(password: str) -> bool:
    """Check whether ``password`` is a valid sudo password."""
    try:
        proc2 = subprocess.run(
            ["sudo", "-S", "-v"],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc2.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def edit_unit_file(name: str, content: str, password: str) -> dict:
    """Write a unit-file drop-in via sudo and reload systemd.

    The drop-in lives at ``/etc/systemd/system/<name>.d/99-manager.conf``.
    After writing, runs ``systemctl daemon-reload`` so the new directives
    take effect immediately.
    """
    safe_name = re.sub(r"[^A-Za-z0-9_.@:\-]", "", name)
    if not safe_name or safe_name != name:
        raise SystemServicesError("Invalid unit name", code="invalid")

    override_dir = f"/etc/systemd/system/{safe_name}.d"
    override_path = f"{override_dir}/99-manager.conf"

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".conf") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        os.chmod(tmp_path, 0o644)
        cmd = [
            "sudo", "-S", "bash", "-c",
            f"mkdir -p {override_dir} && cp {tmp_path} {override_path} && chmod 644 {override_path}",
        ]
        out = _run(cmd, timeout=20, password=password)

        # Always reload systemd after writing a drop-in
        try:
            _run(["sudo", "-S", "systemctl", "daemon-reload"], timeout=20, password=password)
        except SystemServicesError as e:
            logger.warning(f"daemon-reload after edit failed: {e}")
            out = (out + "\n" if out else "") + f"daemon-reload: {e}"

        cache_invalidate(f"unit:{name}")
        cache_invalidate(f"cat:{name}")
        cache_invalidate("list:")
        return {"ok": True, "output": out or f"wrote {override_path}"}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
