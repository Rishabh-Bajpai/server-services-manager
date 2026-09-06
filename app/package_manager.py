"""Package manager wrapper for the dashboard's "Updates" page.

Detects the host's package manager (apt, dnf, yum) and exposes:
- list of pending upgrades (security upgrades flagged)
- manual cache refresh (``apt update`` / ``dnf check-update``)
- install of selected packages with live progress streaming
- cached state with stale-data detection (warning at 1 hour)

The actual install runs in a background thread; output is written to
a temp file and can be streamed to the UI via a poll endpoint.
"""
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger("PackageManager")

STALE_SECONDS = 3600  # 1 hour


@dataclass
class Update:
    name: str
    current_version: str
    new_version: str
    is_security: bool = False
    repo: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "current_version": self.current_version,
            "new_version": self.new_version,
            "is_security": self.is_security,
            "repo": self.repo,
        }


@dataclass
class UpdateState:
    """Cached result of the most recent ``refresh`` call."""
    manager: str
    fetched_at: float
    updates: List[Update] = field(default_factory=list)
    last_error: str = ""

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.fetched_at) > STALE_SECONDS

    def to_dict(self) -> dict:
        return {
            "manager": self.manager,
            "fetched_at": self.fetched_at,
            "fetched_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.fetched_at)),
            "is_stale": self.is_stale,
            "last_error": self.last_error,
            "updates": [u.to_dict() for u in self.updates],
            "count": len(self.updates),
            "security_count": sum(1 for u in self.updates if u.is_security),
        }


# ---------------------------------------------------------------------------
# Manager detection
# ---------------------------------------------------------------------------

def detect_manager() -> str:
    """Return the package manager name on this host, or 'unknown'."""
    for cmd, name in (("apt-get", "apt"), ("dnf", "dnf"), ("yum", "yum")):
        if shutil.which(cmd):
            return name
    return "unknown"


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_STATE: Optional[UpdateState] = None


def get_state() -> Optional[UpdateState]:
    """Return the most recent cached state, or None if never refreshed."""
    return _STATE


def list_updates() -> List[Update]:
    """Return cached updates (or empty list if never refreshed)."""
    s = get_state()
    return list(s.updates) if s else []


class SudoAuthError(Exception):
    """Raised when sudo rejects the provided password."""


def refresh(manager: Optional[str] = None, password: Optional[str] = None) -> UpdateState:
    """Run ``apt update`` (or distro equivalent) and re-parse the upgrade list.

    Returns the new state. The result is cached and exposed via
    :func:`get_state`. If the cache-refresh step can't run (e.g. sudo
    needs a password and none was given), the existing cache is
    used and a warning is included in the state. When ``password`` is
    given it is piped to ``sudo -S`` for that single command and never
    stored.
    """
    global _STATE
    mngr = manager or detect_manager()
    updates: List[Update] = []
    error = ""

    try:
        if mngr == "apt":
            refreshed = _run_apt_update(password=password)
            if not refreshed:
                error = "could not refresh apt cache (sudo password required); showing last known list"
            updates = _parse_apt_upgradable()
        elif mngr == "dnf":
            _run_dnf_check_update()
            updates = _parse_dnf_check_update()
        elif mngr == "yum":
            _run_yum_check_update()
            updates = _parse_yum_check_update()
        else:
            error = f"unsupported package manager: {mngr}"
    except SudoAuthError as e:
        error = str(e)
    except subprocess.TimeoutExpired:
        error = "package update timed out"
    except subprocess.CalledProcessError as e:
        error = f"package update failed: {e}"
    except FileNotFoundError as e:
        error = f"package tool missing: {e}"
    except Exception as e:  # noqa: BLE001
        error = str(e) or type(e).__name__

    with _LOCK:
        _STATE = UpdateState(manager=mngr, fetched_at=time.time(),
                             updates=updates, last_error=error)
    return _STATE


# ---------------------------------------------------------------------------
# apt backend
# ---------------------------------------------------------------------------

def _run_apt_update(password: Optional[str] = None) -> bool:
    """Try to refresh the apt cache. Returns True on success, False if
    the call was attempted but couldn't run (e.g. sudo needs a password).
    Raises :class:`SudoAuthError` when an explicitly provided password is
    rejected, and raises on other failures.
    """
    if password:
        # -p '' suppresses sudo's "[sudo] password for ..." prompt on
        # stderr, so the word "password" below can only come from an
        # actual auth rejection ("Sorry, try again.").
        proc = subprocess.run(
            ["sudo", "-S", "-p", "", "apt-get", "update"],
            input=password + "\n",
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            return True
        err = (proc.stderr or "").lower()
        if "sorry" in err:
            raise SudoAuthError("sudo rejected the password; showing last known list")
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args,
            output=proc.stdout, stderr=proc.stderr,
        )
    proc = subprocess.run(
        ["sudo", "-n", "apt-get", "update"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode == 0:
        return True
    err = (proc.stderr or "").lower()
    if "password" in err:
        # sudo needs a password. Try running without sudo — works if
        # we're already root or if apt-get is NOPASSWD.
        proc2 = subprocess.run(
            ["apt-get", "update"],
            capture_output=True, text=True, timeout=60,
        )
        if proc2.returncode == 0:
            return True
        # Both failed; the cache may be stale but the list call below
        # will still work, so signal "couldn't refresh" rather than raising.
        return False
    raise subprocess.CalledProcessError(
        proc.returncode, proc.args,
        output=proc.stdout, stderr=proc.stderr,
    )


def _parse_apt_upgradable() -> List[Update]:
    proc = subprocess.run(
        ["apt", "list", "--upgradable"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr)
    out: List[Update] = []
    security_packages = _apt_security_package_names()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Listing") or "/" not in line:
            continue
        # Format: "package/source new-version arch [upgradable from: old]"
        name, _, rest = line.partition("/")
        if "upgradable" not in rest:
            continue
        # Tokens after the source
        parts = rest.split()
        if len(parts) < 2:
            continue
        new_version = parts[1] if len(parts) > 1 else ""
        old_version = ""
        if "upgradable from:" in rest:
            try:
                old_version = rest.split("upgradable from:")[1].split("]")[0].strip()
            except Exception:  # noqa: BLE001
                pass
        # Source repo (between the first / and the next space)
        repo = rest.split(" ")[0]
        is_security = name in security_packages or "security" in repo.lower()
        out.append(Update(
            name=name, current_version=old_version, new_version=new_version,
            is_security=is_security, repo=repo,
        ))
    return out


def _apt_security_package_names() -> set:
    """Return a set of package names that come from -security pocket.

    Uses ``apt list`` to look for packages whose repo ends in '-security'.
    Matches any codename (noble, jammy, bookworm, etc.) generically.
    """
    try:
        proc = subprocess.run(
            ["apt", "list", "--all-versions"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001
        return set()
    out = set()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if "/" not in line:
            continue
        name, _, rest = line.partition("/")
        if not name or not rest:
            continue
        # repo token is first word after slash, e.g. "noble-security ..."
        repo = rest.strip().split()[0] if rest.strip() else ""
        if not repo.lower().endswith("-security") and "-security" not in line.lower():
            continue
        # ensure repo actually ends with -security (avoid false positives like "not-security-related")
        if repo.lower().endswith("-security"):
            out.add(name)
        elif "-security" in line.lower():
            # fallback for unusual apt output formats
            if any(tok.lower().endswith("-security") for tok in rest.lower().split()):
                out.add(name)
    return out


# ---------------------------------------------------------------------------
# dnf / yum backend
# ---------------------------------------------------------------------------

def _run_dnf_check_update() -> None:
    # ``dnf check-update`` returns 0 when nothing to update, 100 when updates exist
    subprocess.run(
        ["dnf", "-q", "check-update"],
        capture_output=True, text=True, timeout=60,
    )


def _parse_dnf_check_update() -> List[Update]:
    proc = subprocess.run(
        ["dnf", "-q", "check-update"],
        capture_output=True, text=True, timeout=30,
    )
    out: List[Update] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Last metadata") or line.startswith("Obsoleting"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        # Format: name  version  repo
        name, new_version, repo = parts[0], parts[1], parts[2]
        is_security = "security" in repo.lower()
        out.append(Update(
            name=name, current_version="", new_version=new_version,
            is_security=is_security, repo=repo,
        ))
    return out


def _run_yum_check_update() -> None:
    subprocess.run(
        ["yum", "-q", "check-update"],
        capture_output=True, text=True, timeout=60,
    )


def _parse_yum_check_update() -> List[Update]:
    proc = subprocess.run(
        ["yum", "-q", "check-update"],
        capture_output=True, text=True, timeout=30,
    )
    out: List[Update] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Last metadata") or line.startswith("Obsoleting"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        name, new_version, repo = parts[0], parts[1], parts[2]
        is_security = "security" in repo.lower()
        out.append(Update(
            name=name, current_version="", new_version=new_version,
            is_security=is_security, repo=repo,
        ))
    return out


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

# Module-level install jobs, keyed by a job id
_JOBS_LOCK = threading.Lock()
_JOBS: dict = {}


def install_packages(packages: List[str], manager: Optional[str] = None,
                     password: Optional[str] = None,
                     on_done: Optional[Callable[[str, bool, str], None]] = None) -> str:
    """Start an install in the background. Returns a job id.

    The job's progress is exposed via :func:`get_job`. When done, the
    ``on_done(job_id, success, output)`` callback is invoked. When
    ``password`` is given it is piped to ``sudo -S`` for the install
    command only; it is never stored on the job dict or in any log.
    """
    mngr = manager or detect_manager()
    job_id = f"job-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    log_path = _create_log_path(job_id)
    job = {
        "id": job_id,
        "manager": mngr,
        "packages": list(packages),
        "started_at": time.time(),
        "ended_at": 0.0,
        "success": False,
        "log_path": log_path,
        "tail": "",
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    t = threading.Thread(
        target=_run_install, args=(job, on_done, password), daemon=True,
        name=f"pkg-install-{job_id}",
    )
    t.start()
    return job_id


def _create_log_path(job_id: str) -> str:
    base = os.path.expanduser("~/.server-services-manager/installs")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{job_id}.log")


def get_job(job_id: str) -> Optional[dict]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        out = dict(job)
    if os.path.exists(out["log_path"]):
        try:
            with open(out["log_path"], "r", errors="replace") as f:
                # Last 4KB for the polled tail
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096))
                out["tail"] = f.read()
                out["log_size"] = size
        except OSError:
            out["tail"] = ""
            out["log_size"] = 0
    else:
        out["tail"] = ""
        out["log_size"] = 0
    return out


def list_jobs(limit: int = 20) -> List[dict]:
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j["started_at"], reverse=True)[:limit]
    out = []
    for job in jobs:
        d = dict(job)
        d.pop("log_path", None)
        d["running"] = d["ended_at"] == 0
        out.append(d)
    return out


def _run_install(job: dict, on_done: Optional[Callable],
                 password: Optional[str] = None) -> None:
    mngr = job["manager"]
    packages = job["packages"]
    log_path = job["log_path"]
    success = False
    try:
        if not packages:
            _append_log(log_path, "no packages specified\n")
            with _JOBS_LOCK:
                job["ended_at"] = time.time()
                job["success"] = False
            return
        use_stdin_password = False
        if password:
            # Validate the password up front with a non-destructive
            # `sudo -v` so a wrong password fails fast with a clear
            # message instead of a misleading apt error mid-install.
            # -p '' suppresses the prompt on stderr (see _run_apt_update).
            probe = subprocess.run(
                ["sudo", "-S", "-p", "", "-v"],
                input=password + "\n",
                capture_output=True, text=True, timeout=15,
            )
            if probe.returncode != 0:
                _append_log(log_path, "sudo rejected the password; install aborted.\n")
                with _JOBS_LOCK:
                    job["ended_at"] = time.time()
                    job["success"] = False
                return
            use_stdin_password = True
        elif mngr == "apt":
            # Non-destructive probe: can we sudo without a password?
            # (Never probe with `apt-get install` itself — it would
            # actually install, and a timeout could leave a dpkg lock.)
            probe = subprocess.run(["sudo", "-n", "-v"],
                                   capture_output=True, text=True, timeout=10)
            if probe.returncode != 0:
                _append_log(log_path, "sudo requires a password; enter it in the dashboard to install.\n")
                with _JOBS_LOCK:
                    job["ended_at"] = time.time()
                    job["success"] = False
                return
        if mngr == "apt":
            # DEBIAN_FRONTEND is passed on the sudo command line (sudo
            # would strip it from the environment) so debconf never
            # blocks on stdin, which sudo already consumed (EOF).
            sudo = ["sudo", "-S", "-p", ""] if use_stdin_password else ["sudo", "-n"]
            cmd = sudo + ["DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                          "-o", "Dpkg::Options::=--force-confdef",
                          "-o", "Dpkg::Options::=--force-confold", "--"] + packages
        elif mngr in ("dnf", "yum"):
            # NB: no "--" delimiter here — dnf5 rejects it as an
            # unknown argument. Flag injection is already blocked by
            # the server-side name regex (must start alphanumeric).
            sudo = ["sudo", "-S", "-p", ""] if use_stdin_password else []
            cmd = sudo + [mngr, "install", "-y"] + packages
        else:
            _append_log(log_path, f"unsupported manager: {mngr}\n")
            return
        with open(log_path, "w") as logf:
            if use_stdin_password:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                        stdout=logf, stderr=subprocess.STDOUT, text=True)
                proc.communicate((password or "") + "\n")
            else:
                proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, text=True)
                proc.wait()
            success = proc.returncode == 0
        with _JOBS_LOCK:
            job["ended_at"] = time.time()
            job["success"] = success
    except Exception as e:  # noqa: BLE001
        _append_log(log_path, f"\n[error] {e}\n")
        with _JOBS_LOCK:
            job["ended_at"] = time.time()
            job["success"] = False
    finally:
        if on_done is not None:
            try:
                on_done(job["id"], success, "")
            except Exception:  # noqa: BLE001
                pass


def lookup_package(name: str) -> dict:
    """Return version info for a single package.

    Returns a dict with keys: name, installed, candidate, available_upgrade.
    Returns an error dict when the package isn't found.
    """
    result = {"name": name, "installed": None, "candidate": None, "available_upgrade": None}
    mngr = detect_manager()

    if mngr == "apt":
        try:
            proc = subprocess.run(
                ["apt-cache", "policy", name],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout:
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    # NB: the line is already stripped, so match without
                    # the two-space indent apt-cache emits.
                    if line.startswith("Installed:"):
                        val = line.split(":", 1)[1].strip()
                        result["installed"] = val if val != "(none)" else None
                    elif line.startswith("Candidate:"):
                        val = line.split(":", 1)[1].strip()
                        result["candidate"] = val if val != "(none)" else None
        except subprocess.TimeoutExpired:
            return {"name": name, "error": "lookup timed out"}
        except FileNotFoundError:
            return {"name": name, "error": "package manager not found"}

        if result["installed"] and result["candidate"] and result["installed"] != result["candidate"]:
            result["available_upgrade"] = result["candidate"]
    elif mngr in ("dnf", "yum"):
        try:
            proc = subprocess.run(
                ["rpm", "-q", "--queryformat", "%{VERSION}", name],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                result["installed"] = proc.stdout.strip()
            proc2 = subprocess.run(
                ["dnf", "-q", "list", "available", name] if mngr == "dnf" else ["yum", "-q", "list", "available", name],
                capture_output=True, text=True, timeout=10,
            )
            for line in (proc2.stdout or "").splitlines():
                line = line.strip()
                if name in line and len(line.split()) >= 3:
                    result["candidate"] = line.split()[1]
                    break
            if result["installed"] and result["candidate"] and result["installed"] != result["candidate"]:
                result["available_upgrade"] = result["candidate"]
        except subprocess.TimeoutExpired:
            return {"name": name, "error": "lookup timed out"}
        except FileNotFoundError:
            return {"name": name, "error": "package manager not found"}
    else:
        return {"name": name, "error": f"unsupported manager: {mngr}"}

    if not result["installed"] and not result["candidate"]:
        result["error"] = "package not found"
    return result


def _append_log(path: str, line: str) -> None:
    try:
        with open(path, "a") as f:
            f.write(line)
    except OSError:
        pass


