"""systemd drop-in overrides for managed services.

Lets the user set resource limits (CPUQuota, MemoryMax, etc.) on
a managed program without editing the manager's own service file.
We write ``/etc/systemd/system/<unit>.d/99-manager.conf`` (or
``~/.config/systemd/user/<unit>.d/...`` for user services) and
daemon-reload. The vendor unit is never touched.

Limits are applied to the *autostart* service that the manager
created (``ssm-<name>.service``) so the running subprocess gets
constrained on next start.
"""
import logging
import os
import re
import subprocess
from typing import Dict, Optional, Tuple

logger = logging.getLogger("ResourceLimits")

# Sanitized form of the value to write to the drop-in file. Just
# reject shell metacharacters and systemd comment/continuation chars —
# systemd's own parser will validate the actual format (e.g. ``50%`` vs ``200M``).
# Allowlist approach: keep % for cpu_quota, but reject # \ []{}()!*?~^ etc.
_BAD_CHARS = re.compile(r"[\n\r;|&`$<>\"'(){}#\[\]\\!*?~^]")


def _run_systemctl(args: list, password: Optional[str] = None) -> Tuple[int, str, str]:
    cmd = ["systemctl", "--user"] + args
    stdin_data = None
    # User drop-ins never need sudo (see schedules.py comment).
    if password and "--user" not in cmd:
        cmd = ["sudo", "-S", "-k"] + cmd
        stdin_data = password + "\n"
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            input=stdin_data,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _unit_dir(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "_-.")
    return os.path.expanduser(f"~/.config/systemd/user/ssm-{safe}.service.d")


def _drop_in_path(name: str) -> str:
    return os.path.join(_unit_dir(name), "99-manager.conf")


# Fields we allow in the drop-in. Keys map to the systemd
# directive name. The ``parse`` function normalises user input
# (e.g. ``50`` → ``50%`` for CPUQuota, ``512M`` → ``512M`` for
# MemoryMax).
SUPPORTED_FIELDS = {
    "cpu_quota": ("CPUQuota", lambda v: v if v.endswith("%") else f"{v}%"),
    "memory_max": ("MemoryMax", str),
    "memory_high": ("MemoryHigh", str),
    "tasks_max": ("TasksMax", str),
    "io_weight": ("IOWeight", str),
    "cpu_weight": ("CPUWeight", str),
    "limit_nofile": ("LimitNOFILE", str),
    "nice": ("Nice", str),
}


def validate_value(field: str, value: str) -> Tuple[bool, str]:
    if field not in SUPPORTED_FIELDS:
        return False, f"unsupported field: {field}"
    if not value or not value.strip():
        return False, "empty value"
    if _BAD_CHARS.search(value):
        return False, "value contains shell metacharacters"
    if len(value) > 64:
        return False, "value too long"
    # Field-specific sanity
    if field == "cpu_quota":
        v = value.rstrip("%")
        try:
            n = float(v)
        except ValueError:
            return False, "cpu_quota must be a number (optionally with %)"
        if n < 0 or n > 100:
            return False, "cpu_quota must be 0-100"
    if field in ("memory_max", "memory_high"):
        if not re.match(r"^(infinity|\d+[KMG]?)$", value):
            return False, "memory value must be number[K|M|G] or 'infinity'"
    if field in ("tasks_max",) and not value.isdigit() and value != "infinity":
        return False, "tasks_max must be a number or 'infinity'"
    if field in ("io_weight", "cpu_weight"):
        try:
            n = int(value)
        except ValueError:
            return False, f"{field} must be an integer 1-10000"
        if n < 1 or n > 10000:
            return False, f"{field} must be 1-10000"
    if field == "nice":
        try:
            n = int(value)
        except ValueError:
            return False, "nice must be an integer -20..19"
        if n < -20 or n > 19:
            return False, "nice must be in -20..19"
    if field == "limit_nofile":
        try:
            n = int(value)
        except ValueError:
            return False, "limit_nofile must be an integer"
        if n < 1:
            return False, "limit_nofile must be positive"
    return True, ""


def _read_drop_in(name: str) -> Dict[str, str]:
    """Read existing drop-in settings, return {field: value}."""
    path = _drop_in_path(name)
    if not os.path.exists(path):
        return {}
    try:
        content = open(path).read()
    except OSError:
        return {}
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Map directive back to field
        for field, (directive, _) in SUPPORTED_FIELDS.items():
            if directive == key:
                result[field] = val
                break
    return result


def _write_drop_in(name: str, settings: Dict[str, str]) -> Tuple[bool, str]:
    """Write the drop-in file with the given settings."""
    try:
        os.makedirs(_unit_dir(name), exist_ok=True)
    except OSError as e:
        return False, f"mkdir: {e}"
    lines = ["[Service]"]
    for field, value in settings.items():
        if field not in SUPPORTED_FIELDS:
            continue
        directive, normaliser = SUPPORTED_FIELDS[field]
        lines.append(f"{directive}={normaliser(value)}")
    try:
        with open(_drop_in_path(name), "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        return False, f"write: {e}"
    return True, ""


def apply(name: str, settings: Dict[str, str],
          password: Optional[str] = None) -> Tuple[bool, str]:
    """Apply a {field: value} dict to the program's drop-in.

    Empty / cleared values are removed; existing entries not in the
    dict are preserved. Triggers ``daemon-reload`` so the new limits
    take effect on the next start.
    """
    # Merge with existing
    current = _read_drop_in(name)
    for field, value in settings.items():
        if not value or not value.strip():
            current.pop(field, None)
        else:
            ok, err = validate_value(field, value)
            if not ok:
                return False, f"{field}: {err}"
            current[field] = value.strip()
    ok, err = _write_drop_in(name, current)
    if not ok:
        return False, err
    # User units don't need sudo; daemon-reload is --user (see _run_systemctl).
    # Using sudo would run systemctl --user as root and lose DBUS_SESSION_BUS_ADDRESS.
    rc, _, err = _run_systemctl(["daemon-reload"], password=None)
    if rc != 0:
        return False, f"daemon-reload: {err.strip()}"
    return True, ""


def get(name: str) -> Dict[str, str]:
    """Read the currently-applied limits (user-friendly form)."""
    current = _read_drop_in(name)
    out = {}
    for field, value in current.items():
        if field == "cpu_quota":
            # Strip trailing % for display
            out[field] = value.rstrip("%")
        else:
            out[field] = value
    return out


def clear(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    """Remove the drop-in entirely."""
    import shutil
    if os.path.exists(_unit_dir(name)):
        try:
            shutil.rmtree(_unit_dir(name))
        except OSError as e:
            return False, f"rmtree: {e}"
    rc, _, err = _run_systemctl(["daemon-reload"], password=None)
    if rc != 0:
        return False, f"daemon-reload: {err.strip()}"
    return True, ""


def field_choices() -> list:
    """Return a list of {field, label, default, hint} for the UI."""
    return [
        {"field": "cpu_quota",    "label": "CPU Quota",      "hint": "0-100% of one core", "default": ""},
        {"field": "memory_max",   "label": "Memory Max",     "hint": "e.g. 512M, 2G, infinity", "default": ""},
        {"field": "memory_high",  "label": "Memory High",    "hint": "soft limit; throttle above this", "default": ""},
        {"field": "tasks_max",    "label": "Tasks Max",      "hint": "max processes/threads", "default": ""},
        {"field": "io_weight",    "label": "I/O Weight",     "hint": "1-10000", "default": ""},
        {"field": "cpu_weight",   "label": "CPU Weight",     "hint": "1-10000", "default": ""},
        {"field": "limit_nofile", "label": "Open Files",     "hint": "max file descriptors", "default": ""},
        {"field": "nice",         "label": "Nice",           "hint": "-20 (high) to 19 (low)", "default": ""},
    ]
