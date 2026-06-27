"""Systemd timer units for scheduled (cron-style) managed services.

A program in config.yaml can have a ``schedule:`` field. When set,
the program is exposed as a "scheduled task": the manager creates
two sister systemd units in ``~/.config/systemd/user/`` — a
``ssm-<name>.service`` (one-shot, runs the command once) and a
``ssm-<name>.timer`` (triggers it on schedule). The dashboard's
"Start" button always spawns the subprocess directly (so manual
trigger still works), and "Start at boot" instead enables the
timer, not the underlying service.

This module is independent of :mod:`app.process_manager`; the
process manager keeps its current "long-lived service" model and
just gets a UI hint that the program is scheduled.
"""
import logging
import os
import re
import subprocess
from typing import Optional, Tuple

logger = logging.getLogger("Schedules")

_USER_DIR = os.path.expanduser("~/.config/systemd/user")

_PRESETS = {
    "minutely": "*:0/1:0",
    "hourly":   "hourly",
    "daily":    "daily",
    "weekly":   "weekly",
    "monthly":  "monthly",
    "yearly":   "yearly",
}

# Lightweight validation: OnCalendar accepts a rich grammar. We don't
# reimplement it; we just reject obviously bad strings to avoid
# writing a malformed unit. systemd itself returns a clear error if
# the expression is malformed when the timer is loaded.
_OK_RE = re.compile(r"^[A-Za-z0-9*.,/\-\s:]+$")


def is_valid_schedule(expr: str) -> bool:
    if not expr:
        return True  # empty means "not scheduled"
    expr = expr.strip()
    if expr in _PRESETS:
        return True
    if not _OK_RE.match(expr) or len(expr) > 200:
        return False
    # Reject obvious shell / path patterns that pass the charset
    # check (e.g. "rm -rf /" looks like a valid string).
    # We require at least one digit OR a recognised pattern.
    if "/" in expr and not any(c.isdigit() for c in expr):
        return False
    if "//" in expr:
        return False
    return True


def preset_suggestions() -> list:
    return [
        ("minutely",  "Every minute"),
        ("hourly",    "Every hour"),
        ("daily",     "Every day at midnight"),
        ("weekly",    "Every Monday at midnight"),
        ("monthly",   "First day of the month"),
        ("yearly",    "January 1st"),
    ]


def _unit_path(name: str, kind: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "_-.")
    return os.path.join(_USER_DIR, f"ssm-{safe}.{kind}")


def _run_systemctl(args: list, password: Optional[str] = None) -> Tuple[int, str, str]:
    """Run ``systemctl --user`` with optional sudo for write ops."""
    cmd = ["systemctl", "--user"] + args
    stdin_data = None
    if password:
        cmd = ["sudo", "-S", "-k"] + cmd
        # text=True below requires a str, not bytes. The trailing newline
        # is what sudo reads before it execs the real command.
        stdin_data = password + "\n"
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            input=stdin_data,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def write_units(name: str, command: str, cwd: str, schedule: str,
                environment: Optional[dict] = None) -> Tuple[bool, str]:
    """Create or update ssm-<name>.service and .timer for a program.

    Returns (ok, error_message).
    """
    if not is_valid_schedule(schedule):
        return False, f"invalid schedule expression: {schedule!r}"

    try:
        os.makedirs(_USER_DIR, exist_ok=True)
    except OSError as e:
        return False, f"cannot create {_USER_DIR}: {e}"

    env = environment or {}
    env_lines = "\n".join(f'Environment="{k}={v}"' for k, v in env.items())

    service_content = (
        "[Unit]\n"
        f"Description=SSM scheduled task: {name}\n"
        "[Service]\n"
        f'Type=oneshot\nWorkingDirectory={cwd}\n'
        f'ExecStart={command}\n'
        f"{env_lines}\n"
    )
    timer_content = (
        "[Unit]\n"
        f"Description=SSM timer: {name}\n"
        "[Timer]\n"
        f"OnCalendar={schedule}\n"
        "Persistent=true\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    try:
        with open(_unit_path(name, "service"), "w") as f:
            f.write(service_content)
        with open(_unit_path(name, "timer"), "w") as f:
            f.write(timer_content)
    except OSError as e:
        return False, f"write failed: {e}"

    rc, _, err = _run_systemctl(["daemon-reload"])
    if rc != 0:
        return False, f"daemon-reload failed: {err.strip()}"
    return True, ""


def remove_units(name: str) -> Tuple[bool, str]:
    """Remove the timer + service units; disable first if needed."""
    # Try to disable (ignore failures if not enabled)
    _run_systemctl(["disable", "--now", f"ssm-{name}.timer"])
    for kind in ("timer", "service"):
        path = _unit_path(name, kind)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                return False, f"remove {path}: {e}"
    _run_systemctl(["daemon-reload"])
    return True, ""


def enable_timer(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    rc, _, err = _run_systemctl(
        ["enable", "--now", f"ssm-{name}.timer"], password=password,
    )
    if rc != 0:
        return False, err.strip() or f"exit code {rc}"
    return True, ""


def disable_timer(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    rc, _, err = _run_systemctl(
        ["disable", "--now", f"ssm-{name}.timer"], password=password,
    )
    if rc != 0:
        return False, err.strip() or f"exit code {rc}"
    return True, ""


def timer_status(name: str) -> dict:
    """Return {enabled, active, next_run, last_run} for a program.

    Returns sensible defaults if the unit doesn't exist (likely
    because the user hasn't enabled the timer yet).
    """
    info = {
        "enabled": False,
        "active": False,
        "next_run": None,
        "last_run": None,
    }
    rc, out, _ = _run_systemctl(["is-enabled", f"ssm-{name}.timer"])
    if rc == 0:
        info["enabled"] = True
    rc, out, _ = _run_systemctl(["is-active", f"ssm-{name}.timer"])
    if rc == 0:
        info["active"] = True
    rc, out, _ = _run_systemctl(["list-timers", "--no-pager", "--no-legend",
                                 f"ssm-{name}.timer"])
    if rc == 0 and out.strip():
        # Columns: NEXT LEFT UNIT ACTIVATES
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 1:
                info["next_run"] = parts[0]
            break
    return info


def trigger_now(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    """Run the .service unit on demand (like 'systemctl start')."""
    rc, _, err = _run_systemctl(
        ["start", f"ssm-{name}.service"], password=password,
    )
    if rc != 0:
        return False, err.strip() or f"exit code {rc}"
    return True, ""
