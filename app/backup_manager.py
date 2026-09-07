"""Local-disk backup scheduler.

A backup job has:
- name (id)
- type (directory | mysql | postgres)
- source (path or DSN)
- destination (directory)
- schedule (systemd OnCalendar expression)
- retention (N most recent backups kept; older ones pruned)
- enabled (bool)

The job is materialized as a pair of systemd units in
``~/.config/systemd/user/``:
- ``ssm-backup-<name>.service`` — oneshot, runs the backup command
- ``ssm-backup-<name>.timer`` — fires on the OnCalendar schedule

Job metadata lives in ``~/.server-services-manager/backups.json``.

Remote destinations (S3, SFTP, rsync-over-SSH) are deferred — see
ROADMAP.md Phase 23.
"""
import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

from app.schedules import is_valid_schedule

logger = logging.getLogger("BackupManager")

_USER_DIR = os.path.expanduser("~/.config/systemd/user")
_BASE_DIR = os.path.expanduser("~/.server-services-manager")
_BACKUP_JOBS_FILE = os.path.join(_BASE_DIR, "backups.json")
_BACKUP_LOGS_DIR = os.path.join(_BASE_DIR, "backup_logs")


VALID_TYPES = ("directory", "mysql", "postgres")


@dataclass
class BackupJob:
    name: str
    type: str          # "directory" | "mysql" | "postgres"
    source: str        # path or DSN
    destination: str   # local directory
    schedule: str      # OnCalendar expression
    retention: int = 7
    enabled: bool = True
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BackupJob":
        return cls(
            name=d["name"],
            type=d["type"],
            source=d["source"],
            destination=d["destination"],
            schedule=d["schedule"],
            retention=int(d.get("retention", 7)),
            enabled=bool(d.get("enabled", True)),
            created_at=float(d.get("created_at", 0.0)),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_LOCK = __import__("threading").RLock()


def _load_jobs() -> List[BackupJob]:
    if not os.path.exists(_BACKUP_JOBS_FILE):
        return []
    try:
        with open(_BACKUP_JOBS_FILE) as f:
            data = json.load(f)
        return [BackupJob.from_dict(d) for d in data]
    except (OSError, KeyError) as e:
        logger.warning(f"backups.json load failed: {e}")
        return []
    except json.JSONDecodeError as e:
        # A corrupt file must never silently become [] and then get
        # overwritten by the next create (losing every job). Move it
        # aside for manual recovery and start empty.
        try:
            aside = f"{_BACKUP_JOBS_FILE}.corrupt-{int(time.time())}"
            os.replace(_BACKUP_JOBS_FILE, aside)
            logger.error(f"backups.json corrupt ({e}); moved to {aside}")
        except OSError as move_err:
            logger.error(f"backups.json corrupt ({e}); could not move aside: {move_err}")
        return []


def _save_jobs(jobs: List[BackupJob]) -> None:
    os.makedirs(os.path.dirname(_BACKUP_JOBS_FILE) or _BASE_DIR, exist_ok=True)
    tmp = _BACKUP_JOBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump([j.to_dict() for j in jobs], f, indent=2)
    os.replace(tmp, _BACKUP_JOBS_FILE)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def _validate_name(name: str) -> None:
    if not name or not _NAME_RE.match(name):
        raise ValueError(f"invalid backup name: {name!r} (use letters, digits, _-.)")


def _validate_type(t: str) -> None:
    if t not in VALID_TYPES:
        raise ValueError(f"invalid type: {t!r} (must be one of {VALID_TYPES})")


def _validate_schedule(schedule: str) -> None:
    if not schedule:
        raise ValueError("schedule is required")
    if "\n" in schedule or "\r" in schedule:
        raise ValueError(f"invalid schedule: {schedule!r} (must be a single line)")
    if not is_valid_schedule(schedule.strip()):
        raise ValueError(
            f"invalid schedule: {schedule!r} (must be a systemd OnCalendar "
            f"expression like 'daily' or 'Mon..Fri 09:00:00')"
        )


def _validate_retention(retention: int) -> None:
    if not isinstance(retention, int) or isinstance(retention, bool):
        raise ValueError(f"invalid retention: {retention!r} (must be an integer 1-365)")
    if retention < 1 or retention > 365:
        raise ValueError("retention must be between 1 and 365")


def _safe_path(p: str) -> str:
    """Reject shell-metacharacter-heavy values from paths."""
    if "\n" in p or "\0" in p:
        raise ValueError(f"invalid character in path: {p!r}")
    return p


def _ensure_destination(destination: str) -> None:
    """Create the archive directory up front so the first run doesn't
    fail on a missing path (tar cannot create it)."""
    try:
        os.makedirs(os.path.expanduser(destination), exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"cannot create destination {destination!r}: {e}")


def _validate_layout(type_: str, source: str, destination: str) -> None:
    """Reject layouts where tar would archive its own output."""
    if type_ != "directory":
        return
    src = os.path.abspath(os.path.expanduser(source))
    dest = os.path.abspath(os.path.expanduser(destination))
    if dest == src or dest.startswith(src + os.sep):
        raise ValueError(
            f"destination {destination!r} must not be the source or inside it "
            f"(tar would archive its own output while it grows)"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_jobs() -> List[BackupJob]:
    with _LOCK:
        return list(_load_jobs())


def get_job(name: str) -> Optional[BackupJob]:
    with _LOCK:
        for j in _load_jobs():
            if j.name == name:
                return j
    return None


def create_job(
    name: str,
    type_: str,
    source: str,
    destination: str,
    schedule: str,
    retention: int = 7,
    enabled: bool = True,
) -> BackupJob:
    _validate_name(name)
    _validate_type(type_)
    if not source:
        raise ValueError("source is required")
    if not destination:
        raise ValueError("destination is required")
    _validate_schedule(schedule)
    _safe_path(source)
    _safe_path(destination)
    _validate_layout(type_, source, destination)
    _validate_retention(retention)
    _ensure_destination(destination)

    with _LOCK:
        jobs = _load_jobs()
        if any(j.name == name for j in jobs):
            raise ValueError(f"backup job {name!r} already exists")
        job = BackupJob(
            name=name, type=type_, source=source, destination=destination,
            schedule=schedule, retention=retention, enabled=enabled,
            created_at=time.time(),
        )
        jobs.append(job)
        _save_jobs(jobs)
    ok, err = _write_units(job)
    if not ok:
        # Don't leave a metadata job with no usable timer behind.
        with _LOCK:
            _save_jobs([j for j in _load_jobs() if j.name != name])
        raise RuntimeError(f"could not write systemd units: {err}")
    return job


def update_job(query_name: str, **changes) -> Optional[BackupJob]:
    """Update fields of a job (including rename via ``name=``).

    Renaming also drops the old systemd units so the old timer does
    not keep running orphaned. (The first parameter is not called
    ``name`` precisely so ``name`` can travel inside ``changes``.)
    """
    with _LOCK:
        jobs = _load_jobs()
        for j in jobs:
            if j.name == query_name:
                old_name = j.name
                for k, v in changes.items():
                    if hasattr(j, k):
                        setattr(j, k, v)
                _validate_name(j.name)
                _validate_type(j.type)
                _validate_schedule(j.schedule)
                _validate_retention(j.retention)
                _safe_path(j.source)
                _safe_path(j.destination)
                _validate_layout(j.type, j.source, j.destination)
                _ensure_destination(j.destination)
                _save_jobs(jobs)
                if j.name != old_name:
                    # Renamed: drop the old units or the old timer
                    # keeps running orphaned alongside the new one.
                    _disable_and_remove_units(old_name)
                ok, err = _write_units(j)
                if not ok:
                    raise RuntimeError(f"could not write systemd units: {err}")
                return j
    return None


def delete_job(name: str) -> Tuple[bool, str]:
    """Remove a job from metadata + disable/remove its units.

    Returns (ok, warning). Unit cleanup failures are reported as a
    warning rather than failing the delete, but they are never
    silent — an orphaned timer the UI can no longer see is worse
    than an error toast.
    """
    with _LOCK:
        jobs = _load_jobs()
        new_jobs = [j for j in jobs if j.name != name]
        if len(new_jobs) == len(jobs):
            return False, ""
        _save_jobs(new_jobs)
    warning = _disable_and_remove_units(name)
    return True, warning


def enable_job(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    job = get_job(name)
    if not job:
        return False, "no such job"
    ok, err = _systemctl_enable(name, password)
    if ok:
        update_job(name, enabled=True)
    return ok, err


def disable_job(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    ok, err = _systemctl_disable(name, password)
    if ok:
        update_job(name, enabled=False)
    return ok, err


def trigger_now(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    job = get_job(name)
    if not job:
        return False, "backup job not found"
    return _systemctl_start(name, password)


def get_status(name: str) -> dict:
    """Return enabled/active/next_run/last_run for the timer."""
    info = {"enabled": False, "active": False, "next_run": None, "last_run": None}
    rc, out, _ = _run_systemctl_user(["is-enabled", f"ssm-backup-{name}.timer"])
    if rc == 0:
        info["enabled"] = True
    rc, out, _ = _run_systemctl_user(["is-active", f"ssm-backup-{name}.timer"])
    if rc == 0:
        info["active"] = True
    rc, out, _ = _run_systemctl_user(
        ["list-timers", "--no-pager", "--no-legend", f"ssm-backup-{name}.timer"]
    )
    if rc == 0 and out.strip():
        # Columns: NEXT LEFT LAST PASSED UNIT ACTIVATES, where NEXT
        # itself is "Dow YYYY-MM-DD HH:MM:SS TZ" (4 tokens) or "-".
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "-":
                info["next_run"] = None
            elif len(parts) >= 4:
                info["next_run"] = " ".join(parts[0:4])
            break
    # Try to find the last_run via the journal for the service
    rc, out, _ = _run_systemctl_user(
        ["show", f"ssm-backup-{name}.service", "--property=ExecMainExitTimestamp,Result"]
    )
    if rc == 0 and out.strip():
        for line in out.splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "ExecMainExitTimestamp" and v.strip():
                info["last_run"] = v.strip()
    return info


# ---------------------------------------------------------------------------
# Backup command generation
# ---------------------------------------------------------------------------

def _backup_command(job: BackupJob) -> str:
    """Return the shell command to run for a backup job.

    The archive timestamp is a shell ``$(date ...)`` substitution so it
    is evaluated on every run. Baking a timestamp at unit-write time
    would make every scheduled run overwrite the same file.
    ``$ts`` is captured once per run so the backup and its failure
    cleanup target the same file even across a second boundary.
    On failure the partial archive is removed so a failed run never
    leaves a file that looks like a backup.
    """
    dest = os.path.expanduser(job.destination)
    if job.type == "directory":
        src = os.path.expanduser(job.source).rstrip("/")
        base = os.path.basename(src) or "backup"
        dq = shlex.quote(dest)
        bq = shlex.quote(base)
        return (
            "ts=$(date +%Y%m%d-%H%M%S); "
            # Use --warning=no-file-changed so logs aren't noisy when files
            # are appended during the dump.
            f"tar --warning=no-file-changed -czf {dq}/{bq}-$ts.tar.gz "
            f"-C {shlex.quote(os.path.dirname(src) or '.')} {shlex.quote(os.path.basename(src))}"
            f" && echo OK || {{ rm -f {dq}/{bq}-$ts.tar.gz; exit 1; }}"
        )
    if job.type == "mysql":
        # mysql/mariadb dump; assumes the binary is on PATH.
        # pipefail: without it the pipeline status is gzip's, so a
        # failed mysqldump (bad creds, missing binary) would still
        # report success with an empty archive.
        return (
            "set -o pipefail; ts=$(date +%Y%m%d-%H%M%S); "
            f"mysqldump --single-transaction --quick {shlex.quote(job.source)} "
            f"| gzip > {shlex.quote(dest)}/{shlex.quote(job.name)}-$ts.sql.gz"
            f" && echo OK || {{ rm -f {shlex.quote(dest)}/{shlex.quote(job.name)}-$ts.sql.gz; exit 1; }}"
        )
    if job.type == "postgres":
        return (
            "set -o pipefail; ts=$(date +%Y%m%d-%H%M%S); "
            f"pg_dump {shlex.quote(job.source)}"
            f" | gzip > {shlex.quote(dest)}/{shlex.quote(job.name)}-$ts.sql.gz"
            f" && echo OK || {{ rm -f {shlex.quote(dest)}/{shlex.quote(job.name)}-$ts.sql.gz; exit 1; }}"
        )
    raise ValueError(f"unknown backup type: {job.type}")


def _prune_command(job: BackupJob) -> str:
    """Return a shell command that prunes old archives beyond retention."""
    if not job.destination:
        return "true"
    dest = os.path.expanduser(job.destination)
    # Pattern matching the archives this job creates. Only the fixed
    # parts are quoted — quoting the whole glob would pass a literal
    # "*" to ls, match nothing, and silently disable pruning.
    if job.type == "directory":
        src = os.path.expanduser(job.source).rstrip("/")
        base = os.path.basename(src) or "backup"
        pattern = f"{shlex.quote(dest)}/{shlex.quote(base)}-*.tar.gz"
    else:
        pattern = f"{shlex.quote(dest)}/{shlex.quote(job.name)}-*.sql.gz"
    n = int(job.retention)
    # Keep the N newest (by mtime); delete the rest.
    return (
        f"ls -1tr {pattern} 2>/dev/null | head -n -{n} | xargs -r rm -f"
    )


# ---------------------------------------------------------------------------
# systemd unit generation
# ---------------------------------------------------------------------------

def _unit_path(name: str, kind: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "_-.")
    return os.path.join(_USER_DIR, f"ssm-backup-{safe}.{kind}")


def _escape_systemd_specifiers(cmd: str) -> str:
    """Escape ``%`` for embedding in a unit file.

    systemd performs specifier expansion (``%h``, ``%n``, ...) in
    ``ExecStart`` before invoking the shell, so a literal ``%`` in a
    path must be written ``%%``. Shell quoting does not protect it.
    """
    return cmd.replace("%", "%%")


def _write_units(job: BackupJob) -> Tuple[bool, str]:
    """Write the .service and .timer units to disk.

    Returns (ok, error). A failure leaves no half-written pair: on a
    partial write the created file is removed again.
    """
    try:
        os.makedirs(_USER_DIR, exist_ok=True)
        os.makedirs(_BACKUP_LOGS_DIR, exist_ok=True)
    except OSError as e:
        return False, f"cannot create unit directories: {e}"
    log_path = os.path.join(_BACKUP_LOGS_DIR, f"{job.name}.log")

    backup_cmd = _backup_command(job)
    prune_cmd = _prune_command(job)
    # Combine: run backup, then prune. Each command is a separate ExecStart
    # so systemd reports individual exit codes.
    full_backup = _escape_systemd_specifiers(backup_cmd)
    full_prune = _escape_systemd_specifiers(prune_cmd)

    service_content = (
        "[Unit]\n"
        f"Description=SSM backup: {job.name}\n"
        "[Service]\n"
        f"Type=oneshot\n"
        f'ExecStart=/bin/sh -c {shlex.quote(full_backup)}\n'
        f'ExecStart=/bin/sh -c {shlex.quote(full_prune)}\n'
        f'StandardOutput=append:{log_path}\n'
        f'StandardError=append:{log_path}\n'
    )
    timer_content = (
        "[Unit]\n"
        f"Description=SSM backup timer: {job.name}\n"
        "[Timer]\n"
        f"OnCalendar={job.schedule}\n"
        "Persistent=true\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    written = []
    try:
        for kind, content in (("service", service_content),
                              ("timer", timer_content)):
            path = _unit_path(job.name, kind)
            with open(path, "w") as f:
                f.write(content)
            written.append(path)
    except OSError as e:
        for path in written:
            try:
                os.remove(path)
            except OSError:
                pass
        return False, f"write units failed: {e}"
    rc, _, err = _run_systemctl_user(["daemon-reload"])
    if rc != 0:
        return False, f"daemon-reload failed: {(err or '').strip() or f'exit {rc}'}"
    return True, ""


def _disable_and_remove_units(name: str) -> str:
    """Disable the timer and remove both unit files.

    Returns a warning string ("" when everything worked). Failures
    are collected, not raised, so delete can report them instead of
    silently orphaning a live timer.
    """
    problems = []
    # Only disable when a timer file actually exists — otherwise a
    # delete of a job whose units were never written (or already
    # removed) would warn spuriously.
    if os.path.exists(_unit_path(name, "timer")):
        rc, _, err = _run_systemctl_user(["disable", "--now", f"ssm-backup-{name}.timer"])
        if rc != 0:
            problems.append(f"disable timer failed: {(err or '').strip() or f'exit {rc}'}")
    for kind in ("timer", "service"):
        path = _unit_path(name, kind)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            problems.append(f"remove {path}: {e}")
    rc, _, err = _run_systemctl_user(["daemon-reload"])
    if rc != 0:
        problems.append(f"daemon-reload failed: {(err or '').strip() or f'exit {rc}'}")
    if problems:
        logger.warning(f"cleanup units for {name!r}: " + "; ".join(problems))
    return "; ".join(problems)


def _run_systemctl_user(args: List[str], password: Optional[str] = None) -> Tuple[int, str, str]:
    cmd = ["systemctl", "--user"] + args
    stdin_data = None
    # User timers (ssm-backup-*) are in ~/.config/systemd/user — no sudo needed.
    # Using sudo would run systemctl --user as root and lose DBUS (No medium found).
    if password and "--user" not in cmd:
        cmd = ["sudo", "-S", "-k"] + cmd
        # text=True below requires a str, not bytes. The trailing
        # newline is what sudo reads before it execs the real command.
        stdin_data = password + "\n"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, input=stdin_data)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _systemctl_enable(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    rc, _, err = _run_systemctl_user(
        ["enable", "--now", f"ssm-backup-{name}.timer"], password=password,
    )
    if rc != 0:
        return False, (err or "").strip() or f"exit code {rc}"
    return True, ""


def _systemctl_disable(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    rc, _, err = _run_systemctl_user(
        ["disable", "--now", f"ssm-backup-{name}.timer"], password=password,
    )
    if rc != 0:
        return False, (err or "").strip() or f"exit code {rc}"
    return True, ""


def _systemctl_start(name: str, password: Optional[str] = None) -> Tuple[bool, str]:
    rc, _, err = _run_systemctl_user(
        ["start", f"ssm-backup-{name}.service"], password=password,
    )
    if rc != 0:
        return False, (err or "").strip() or f"exit code {rc}"
    return True, ""


# ---------------------------------------------------------------------------
# Run-now synchronous helper (for "test" button)
# ---------------------------------------------------------------------------

def run_now_blocking(job: BackupJob, timeout: int = 1800) -> Tuple[int, str]:
    """Execute the backup command synchronously and return (rc, output).

    Mirrors the systemd service (backup, then prune). Pruning runs
    only when the backup succeeded — a failed run must not delete
    older good archives while adding nothing new.
    """
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", _backup_command(job)],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return proc.returncode, out
        prune = subprocess.run(
            ["/bin/sh", "-c", _prune_command(job)],
            capture_output=True, text=True, timeout=300,
        )
        return proc.returncode, out + (prune.stdout or "") + (prune.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)