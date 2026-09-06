"""Cron job introspection and management.

Covers the system-wide crontab (``/etc/crontab``) and the drop-in
files in ``/etc/cron.d/``. Per-user crontabs in
``/var/spool/cron/crontabs/<user>`` are listed when readable, but
editing them is intentionally not supported — that's what
``crontab -e`` is for. Editing system files requires the user's
sudo password, handled by the same flow as ``system_services``.
"""
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("CronManager")

# Standard cron field: minute(0-59) hour(0-23) dom(1-31) month(1-12) dow(0-6)
# Each field may be: * | */n | n | n-m | n,m,k | n-m/k
_CRON_FIELD = re.compile(
    r"^(?:\*|"
    r"\*/\d+|"
    r"\d+(?:-\d+)?(?:/\d+)?|"
    r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*"
    r")$"
)
_FIELD_NAMES = ("minute", "hour", "dom", "month", "dow")
_FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    # Standard cron accepts 7 as Sunday (0 and 7 both mean Sunday).
    "dow": (0, 7),
}


class CronError(Exception):
    """Raised when a cron operation fails."""
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


@dataclass
class CronJob:
    """A single cron job line."""
    id: str
    schedule: str          # raw 5-field expression
    command: str           # the command (may include args)
    user: str              # username (system crontabs only)
    enabled: bool
    source: str            # file path or "user:<name>" or "system"
    line_number: int
    raw: str               # original line
    comment: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "schedule": self.schedule,
            "command": self.command,
            "user": self.user,
            "enabled": self.enabled,
            "source": self.source,
            "line_number": self.line_number,
            "raw": self.raw,
            "comment": self.comment,
        }


def _validate_field(value: str, name: str) -> Optional[str]:
    """Return error message if invalid, None if valid."""
    if not _CRON_FIELD.match(value):
        return f"invalid {name}: {value!r}"
    low, high = _FIELD_RANGES[name]
    # Parse comma-separated values and ranges
    for part in value.split(","):
        # Check step values (a bare "5/10" means 5-high/10)
        step_part = part
        if "/" in part:
            head, _, step_str = part.partition("/")
            try:
                step = int(step_str)
                if step < 1 or step > high - low + 1:
                    return f"{name}: step {step} out of range"
            except ValueError:
                return f"{name}: invalid step {step_str}"
            step_part = head
        # Check range — may be a comma-separated list of ranges
        if step_part != "*":
            for sub in step_part.split(","):
                try:
                    if "-" in sub:
                        a, _, b = sub.partition("-")
                        a_i, b_i = int(a), int(b)
                        if not (low <= a_i <= high) or not (low <= b_i <= high) or a_i > b_i:
                            return f"{name}: range {a}-{b} out of bounds"
                    else:
                        n = int(sub)
                        if not (low <= n <= high):
                            return f"{name}: value {n} out of range"
                except ValueError:
                    return f"{name}: invalid number {sub}"
    return None


def validate_expression(expr: str) -> Optional[str]:
    """Validate a 5-field cron expression. Returns error message or None."""
    parts = expr.split()
    if len(parts) != 5:
        return f"expected 5 fields, got {len(parts)}"
    for part, name in zip(parts, _FIELD_NAMES):
        err = _validate_field(part, name)
        if err:
            return err
    return None


def parse_line(line: str, line_number: int, source: str, default_user: str = "") -> Optional[CronJob]:
    """Parse one line of a crontab. Returns None for blank/comment lines."""
    stripped = line.rstrip("\n").rstrip("\r")
    if not stripped.strip() or stripped.lstrip().startswith("#"):
        return None

    enabled = True
    comment = ""

    fields = stripped.split(None, 5)
    # cron has env-var assignments (NAME=VALUE) too — skip them
    if not fields or "=" in fields[0]:
        return None

    if len(fields) < 6:
        # Not enough fields to be a valid job
        return None

    schedule = " ".join(fields[:5])
    rest = fields[5]

    # system crontabs have a username field; user crontabs don't
    user = default_user
    if source.startswith("system") or source.startswith("/etc/"):
        # The 6th field is the user; 7th onwards is command
        parts6 = rest.split(None, 1)
        if len(parts6) == 2 and re.match(r"^[a-z_][a-z0-9_-]*$", parts6[0]):
            user = parts6[0]
            command = parts6[1]
        else:
            command = rest
    else:
        command = rest

    job_id = f"{source}:{line_number}"
    return CronJob(
        id=job_id,
        schedule=schedule,
        command=command,
        user=user,
        enabled=enabled,
        source=source,
        line_number=line_number,
        raw=stripped,
        comment=comment,
    )


def list_system_crontab() -> List[CronJob]:
    """Parse /etc/crontab."""
    jobs: List[CronJob] = []
    try:
        with open("/etc/crontab") as f:
            for i, line in enumerate(f, 1):
                job = parse_line(line, i, "/etc/crontab", default_user="root")
                if job:
                    jobs.append(job)
    except (OSError, PermissionError) as e:
        logger.warning(f"could not read /etc/crontab: {e}")
    return jobs


def list_cron_d() -> List[CronJob]:
    """Parse /etc/cron.d/* drop-in files."""
    jobs: List[CronJob] = []
    cron_d = "/etc/cron.d"
    if not os.path.isdir(cron_d):
        return jobs
    try:
        for fname in sorted(os.listdir(cron_d)):
            if fname.startswith("."):
                continue
            path = os.path.join(cron_d, fname)
            if not os.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    for i, line in enumerate(f, 1):
                        job = parse_line(line, i, path, default_user="root")
                        if job:
                            jobs.append(job)
            except (OSError, PermissionError) as e:
                logger.warning(f"could not read {path}: {e}")
    except (OSError, PermissionError) as e:
        logger.warning(f"could not list {cron_d}: {e}")
    return jobs


def list_user_crontab(username: Optional[str] = None) -> List[CronJob]:
    """List per-user crontab. Returns empty list if not readable.

    ``crontab -u <user> -l`` requires root, so we read
    ``/var/spool/cron/crontabs/<user>`` directly when readable.
    """
    if username is None:
        try:
            username = os.environ.get("USER") or os.getlogin()
        except Exception:
            return []
    if not username:
        return []
    path = f"/var/spool/cron/crontabs/{username}"
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            content = f.read()
    except (OSError, PermissionError):
        return []
    jobs: List[CronJob] = []
    for i, line in enumerate(content.splitlines(), 1):
        job = parse_line(line, i, f"user:{username}", default_user=username)
        if job:
            jobs.append(job)
    return jobs


def list_all() -> List[CronJob]:
    return list_system_crontab() + list_cron_d() + list_user_crontab()


def toggle_system_job(source: str, line_number: int, enabled: bool, password: str) -> dict:
    """Enable or disable a job in a system crontab by commenting it out.

    Only works for files in /etc/crontab and /etc/cron.d/. Per-user
    crontabs are intentionally not editable from the UI.
    """
    if not source.startswith("/etc/crontab") and not source.startswith("/etc/cron.d/"):
        raise CronError("only system crontabs are editable", code="invalid")
    if not password:
        raise CronError("password required", code="auth_required")

    try:
        with open(source) as f:
            lines = f.readlines()
    except (OSError, PermissionError) as e:
        raise CronError(f"cannot read {source}: {e}", code="not_found")

    if line_number < 1 or line_number > len(lines):
        raise CronError("line not found", code="invalid")

    idx = line_number - 1
    line = lines[idx].rstrip("\n")
    stripped = line.lstrip()
    is_commented = stripped.startswith("#")
    if enabled and is_commented:
        # Uncomment: strip the leading "# " if present
        new_line = line.lstrip("#").lstrip()
    elif not enabled and not is_commented:
        new_line = "# " + line
    else:
        return {"ok": True, "output": "no change"}

    lines[idx] = new_line + "\n"

    # Write to a tempfile, then sudo-cp into place
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".cron") as tmp:
        tmp.writelines(lines)
        tmp_path = tmp.name
    try:
        os.chmod(tmp_path, 0o644)
        cmd = ["sudo", "-S", "cp", tmp_path, source]
        proc = subprocess.run(
            cmd, input=password + "\n", capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip().splitlines()
            msg = next((ln for ln in stderr if ln.strip() and "password for" not in ln), "write failed")
            lower = (proc.stderr or "").lower()
            if "password" in lower or "permission" in lower:
                raise CronError(msg, code="permission")
            raise CronError(msg, code="error")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return {"ok": True, "output": f"updated {source}:{line_number}"}


def _expand_values(field: str, low: int, high: int) -> set:
    """Expand one cron field to its matching integer set."""
    values = set()
    for part in field.split(","):
        step = 1
        stepped = False
        if "/" in part:
            head, _, step_s = part.partition("/")
            step = int(step_s)
            part = head
            stepped = True
        if part == "*":
            lo, hi = low, high
        elif "-" in part:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
        elif stepped:
            # Bare "5/10" means 5-high/10, not just {5}.
            lo, hi = int(part), high
        else:
            lo = hi = int(part)
        values.update(range(lo, hi + 1, step))
    # Standard cron accepts 7 as Sunday too; normalize to 0.
    if low == 0 and high == 7 and 7 in values:
        values.discard(7)
        values.add(0)
    return values


def next_run(expr: str, now=None) -> Optional[str]:
    """Return the next run time as an ISO string, or None.

    Days are scanned (up to ~4 years, so Feb 29 works) with
    standard cron day semantics: if both dom and dow are
    restricted, a day matching *either* runs.
    """
    from datetime import datetime, timedelta

    if validate_expression(expr) is not None:
        return None
    base = now or datetime.now()
    base = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    fields = expr.split()
    minutes = sorted(_expand_values(fields[0], 0, 59))
    hours = sorted(_expand_values(fields[1], 0, 23))
    dom_raw, mon_raw, dow_raw = fields[2], fields[3], fields[4]
    doms = _expand_values(dom_raw, 1, 31)
    months = _expand_values(mon_raw, 1, 12)
    dows = _expand_values(dow_raw, 0, 7)
    dom_star = dom_raw == "*"
    dow_star = dow_raw == "*"

    def day_matches(day) -> bool:
        if day.month not in months:
            return False
        dom_ok = day.day in doms
        # Python Monday=0..Sunday=6; cron Sunday=0.
        dow_ok = ((day.weekday() + 1) % 7) in dows
        if dom_star and dow_star:
            return True
        if dom_star:
            return dow_ok
        if dow_star:
            return dom_ok
        return dom_ok or dow_ok

    for offset in range(0, 366 * 4 + 31):
        day = (base + timedelta(days=offset)).replace(hour=0, minute=0)
        if not day_matches(day):
            continue
        for hour in hours:
            if offset == 0 and hour < base.hour:
                continue
            for minute in minutes:
                if offset == 0 and hour == base.hour and minute < base.minute:
                    continue
                try:
                    return day.replace(hour=hour, minute=minute).isoformat()
                except ValueError:
                    continue
    return None


# No dots: Debian/Ubuntu cron silently ignores /etc/cron.d files
# containing a "." (run-parts namespace rules).
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")


def add_job(filename: str, schedule: str, user: str, command: str, password: str) -> dict:
    """Append a new job to a file in ``/etc/cron.d/`` (sudo write).

    ``filename`` is a bare file name (no directories); the file is
    created with a manager header comment when it doesn't exist yet.
    """
    err = validate_expression((schedule or "").strip())
    if err:
        raise CronError(f"invalid schedule: {err}", code="invalid")
    user = (user or "").strip()
    if not _USER_RE.match(user):
        raise CronError(f"invalid user: {user!r}", code="invalid")
    command = (command or "").strip()
    if not command:
        raise CronError("command is required", code="invalid")
    if "\n" in command or "\r" in command:
        raise CronError("command must be a single line", code="invalid")
    filename = (filename or "manager").strip()
    if not _FILENAME_RE.match(filename) or filename.startswith("."):
        raise CronError(f"invalid file name: {filename!r}", code="invalid")
    if not password:
        raise CronError("password required", code="auth_required")

    path = f"/etc/cron.d/{filename}"
    if os.path.exists(path):
        # The file exists: we must read it before appending. A read
        # failure (e.g. restrictive permissions) is a hard error —
        # starting from a blank header would silently destroy every
        # existing job in that file on sudo cp.
        try:
            with open(path) as f:
                lines = f.readlines()
        except (OSError, PermissionError) as e:
            raise CronError(f"cannot read {path}: {e}", code="not_found")
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    else:
        lines = ["# Managed by server-services-manager\n"]

    lines.append(f"{schedule.strip()} {user} {command}\n")

    # Keep the tempfile at its default 0600: the job command may
    # contain secrets, and root can read it for sudo cp regardless.
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".cron") as tmp:
        tmp.writelines(lines)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            ["sudo", "-S", "cp", tmp_path, path],
            input=password + "\n", capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip().splitlines()
            msg = next((ln for ln in stderr if ln.strip() and "password for" not in ln), "write failed")
            lower = (proc.stderr or "").lower()
            if "password" in lower or "permission" in lower:
                raise CronError(msg, code="permission")
            raise CronError(msg, code="error")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return {"ok": True, "output": f"added job to {path}", "source": path}


def describe_schedule(expr: str) -> str:
    """Return a human-readable description of a cron expression."""
    try:
        parts = expr.split()
    except Exception:
        return expr
    if len(parts) != 5:
        return expr
    out = []
    if parts[0] == "*":
        out.append("every minute")
    elif parts[0].startswith("*/"):
        out.append(f"every {parts[0][2:]} min")
    if parts[1] == "*":
        if out == ["every minute"]:
            out = ["every hour"]
        else:
            out.append("of every hour")
    elif parts[1].startswith("*/"):
        out.append(f"past every {parts[1][2:]}h")
    if parts[2] != "*":
        out.append(f"on day {parts[2]} of month")
    if parts[3] != "*":
        out.append(f"in {parts[3]}")
    if parts[4] != "*":
        out.append(f"on weekday {parts[4]}")
    return ", ".join(out) if out else expr
