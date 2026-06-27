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
    except (OSError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"backups.json load failed: {e}")
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


def _safe_path(p: str) -> str:
    """Reject shell-metacharacter-heavy values from paths."""
    if "\n" in p or "\0" in p:
        raise ValueError(f"invalid character in path: {p!r}")
    return p


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
    if not schedule:
        raise ValueError("schedule is required")
    _safe_path(source)
    _safe_path(destination)
    if retention < 1 or retention > 365:
        raise ValueError("retention must be between 1 and 365")

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
    _write_units(job)
    return job


def update_job(name: str, **changes) -> Optional[BackupJob]:
    with _LOCK:
        jobs = _load_jobs()
        for j in jobs:
            if j.name == name:
                for k, v in changes.items():
                    if hasattr(j, k):
                        setattr(j, k, v)
                _validate_type(j.type)
                _safe_path(j.source)
                _safe_path(j.destination)
                _save_jobs(jobs)
                _write_units(j)
                return j
    return None


def delete_job(name: str) -> bool:
    """Remove a job from disk + disable its timer."""
    with _LOCK:
        jobs = _load_jobs()
        new_jobs = [j for j in jobs if j.name != name]
        if len(new_jobs) == len(jobs):
            return False
        _save_jobs(new_jobs)
    _disable_and_remove_units(name)
    return True


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
        # Columns: NEXT LEFT UNIT ACTIVATES
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 1:
                info["next_run"] = parts[0]
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
    """Return the shell command to run for a backup job."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    if job.type == "directory":
        src = job.source.rstrip("/")
        base = os.path.basename(src) or "backup"
        archive = os.path.join(job.destination, f"{base}-{ts}.tar.gz")
        # Use --warning=no-file-changed so logs aren't noisy when files
        # are appended during the dump.
        return (
            f"tar --warning=no-file-changed -czf {shlex.quote(archive)} "
            f"-C {shlex.quote(os.path.dirname(src) or '.')} {shlex.quote(os.path.basename(src))}"
            f" && echo OK"
        )
    if job.type == "mysql":
        archive = os.path.join(job.destination, f"{job.name}-{ts}.sql.gz")
        # mysql/mariadb dump; assumes the binary is on PATH
        return (
            f"mysqldump --single-transaction --quick {shlex.quote(job.source)} "
            f"| gzip > {shlex.quote(archive)} && echo OK"
        )
    if job.type == "postgres":
        archive = os.path.join(job.destination, f"{job.name}-{ts}.sql.gz")
        return (
            f"pg_dump {shlex.quote(job.source)} | gzip > {shlex.quote(archive)}"
            f" && echo OK"
        )
    raise ValueError(f"unknown backup type: {job.type}")


def _prune_command(job: BackupJob) -> str:
    """Return a shell command that prunes old archives beyond retention."""
    if not job.destination:
        return "true"
    # Pattern matching the archives this job creates
    if job.type == "directory":
        base = os.path.basename(job.source.rstrip("/")) or "backup"
        glob = f"{job.destination}/{base}-*.tar.gz"
    else:
        glob = f"{job.destination}/{job.name}-*.sql.gz"
    glob_q = shlex.quote(glob)
    n = int(job.retention)
    # Keep the N newest (by mtime); delete the rest.
    return (
        f"ls -1tr {glob_q} 2>/dev/null | head -n -{n} | xargs -r rm -f"
    )


# ---------------------------------------------------------------------------
# systemd unit generation
# ---------------------------------------------------------------------------

def _unit_path(name: str, kind: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "_-.")
    return os.path.join(_USER_DIR, f"ssm-backup-{safe}.{kind}")


def _write_units(job: BackupJob) -> None:
    """Write the .service and .timer units to disk."""
    os.makedirs(_USER_DIR, exist_ok=True)
    os.makedirs(_BACKUP_LOGS_DIR, exist_ok=True)
    log_path = os.path.join(_BACKUP_LOGS_DIR, f"{job.name}.log")

    backup_cmd = _backup_command(job)
    prune_cmd = _prune_command(job)
    # Combine: run backup, then prune. Each command is a separate ExecStart
    # so systemd reports individual exit codes.
    full_backup = backup_cmd
    full_prune = prune_cmd

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
    try:
        with open(_unit_path(job.name, "service"), "w") as f:
            f.write(service_content)
        with open(_unit_path(job.name, "timer"), "w") as f:
            f.write(timer_content)
    except OSError as e:
        logger.warning(f"write units failed: {e}")
        return
    _run_systemctl_user(["daemon-reload"])


def _disable_and_remove_units(name: str) -> None:
    _run_systemctl_user(["disable", "--now", f"ssm-backup-{name}.timer"])
    for kind in ("timer", "service"):
        path = _unit_path(name, kind)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logger.warning(f"remove {path}: {e}")
    _run_systemctl_user(["daemon-reload"])


def _run_systemctl_user(args: List[str], password: Optional[str] = None) -> Tuple[int, str, str]:
    cmd = ["systemctl", "--user"] + args
    stdin_data = None
    if password:
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
    """Execute the backup command synchronously and return (rc, output)."""
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", _backup_command(job)],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)