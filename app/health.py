"""Health checks for managed services.

A ``health_check`` block in ``config.yaml`` describes how to probe a
service. The :class:`HealthMonitor` runs all configured checks in a
background thread, tracks each service's last-known state, and
notifies on transition (healthy <-> unhealthy) via :mod:`notifier`.
"""
import logging
import socket
import subprocess
import time
import threading
from dataclasses import dataclass, field
from typing import List, Optional
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError

logger = logging.getLogger("HealthMonitor")

_CHECK_TYPES = ("http", "tcp", "cmd")


@dataclass
class HealthCheck:
    type: str             # "http" | "tcp" | "cmd"
    target: str           # URL, host:port, or shell command
    interval: int = 30    # seconds
    timeout: int = 5      # seconds
    expect: int = 200     # for http: expected status code (or 0 = any 2xx)

    @classmethod
    def from_config(cls, cfg: dict) -> Optional["HealthCheck"]:
        if not isinstance(cfg, dict):
            return None
        kind = (cfg.get("type") or "").lower()
        if kind not in _CHECK_TYPES:
            return None
        target = cfg.get("target", "")
        if not target:
            return None
        return cls(
            type=kind,
            target=target,
            interval=max(5, int(cfg.get("interval", 30))),
            timeout=max(1, int(cfg.get("timeout", 5))),
            expect=int(cfg.get("expect", 200)) if kind == "http" else 0,
        )


@dataclass
class CheckResult:
    healthy: bool
    detail: str
    timestamp: float = 0.0


def run_check(check: HealthCheck) -> CheckResult:
    """Run a single check and return whether it passed."""
    if check.type == "http":
        return _run_http(check)
    if check.type == "tcp":
        return _run_tcp(check)
    if check.type == "cmd":
        return _run_cmd(check)
    return CheckResult(False, f"unknown check type: {check.type}", time.time())


def _run_http(check: HealthCheck) -> CheckResult:
    try:
        req = urlrequest.Request(check.target, method="GET")
        with urlrequest.urlopen(req, timeout=check.timeout) as resp:
            code = resp.status
            ok = (200 <= code < 300) if check.expect == 0 else code == check.expect
            return CheckResult(ok, f"HTTP {code}", time.time())
    except HTTPError as e:
        return CheckResult(False, f"HTTP {e.code}", time.time())
    except (URLError, OSError, TimeoutError) as e:
        return CheckResult(False, f"{type(e).__name__}: {e}", time.time())


def _run_tcp(check: HealthCheck) -> CheckResult:
    if ":" not in check.target:
        return CheckResult(False, f"bad target: {check.target!r}", time.time())
    host, _, port = check.target.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=check.timeout):
            return CheckResult(True, f"connected to {host}:{port}", time.time())
    except (OSError, TimeoutError) as e:
        return CheckResult(False, f"{type(e).__name__}: {e}", time.time())


def _run_cmd(check: HealthCheck) -> CheckResult:
    try:
        proc = subprocess.run(
            check.target, shell=True, capture_output=True, text=True,
            timeout=check.timeout,
        )
        ok = proc.returncode == 0
        detail = (proc.stdout or proc.stderr or "").strip().splitlines()
        detail = detail[0][:120] if detail else f"exit {proc.returncode}"
        return CheckResult(ok, detail, time.time())
    except subprocess.TimeoutExpired:
        return CheckResult(False, "timeout", time.time())
    except (OSError, subprocess.SubprocessError) as e:
        return CheckResult(False, f"{type(e).__name__}: {e}", time.time())


@dataclass
class ServiceState:
    name: str
    last_state: str = "unknown"   # "healthy" | "unhealthy" | "unknown"
    last_check: Optional[CheckResult] = None
    last_change: float = 0.0
    history: List[CheckResult] = field(default_factory=list)

    def push(self, result: CheckResult) -> Optional[str]:
        new_state = "healthy" if result.healthy else "unhealthy"
        changed = new_state != self.last_state
        self.last_state = new_state
        self.last_check = result
        if changed:
            self.last_change = result.timestamp
        self.history.append(result)
        if len(self.history) > 50:
            self.history = self.history[-50:]
        return new_state if changed else None


class HealthMonitor:
    """Background thread that runs health checks for configured services."""

    def __init__(self, get_services, notifiers, on_event=None):
        """``get_services`` is a callable returning a list of (name, HealthCheck)
        pairs; called each tick so config changes are picked up.
        ``notifiers`` is a list of :class:`app.notifier.Notifier`.
        ``on_event(event)`` is called synchronously on each transition
        (used by the WebSocket layer to push toasts to the UI)."""
        self._get_services = get_services
        self._notifiers = notifiers
        self._on_event = on_event
        self._states: dict = {}        # service name -> ServiceState
        self._events: list = []       # recent events for /notifications
        self._events_max = 200
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tick = 5  # seconds; the monitor wakes up this often
                        # and runs any check whose interval has elapsed

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="health-monitor", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def state(self, name: str) -> Optional[ServiceState]:
        with self._lock:
            return self._states.get(name)

    def all_states(self) -> List[ServiceState]:
        with self._lock:
            return list(self._states.values())

    def recent_events(self) -> list:
        with self._lock:
            return list(reversed(self._events))

    def _run(self) -> None:
        next_run: dict = {}  # name -> epoch time of next check
        while not self._stop.is_set():
            try:
                services = self._get_services()
            except Exception as e:
                logger.warning(f"health get_services failed: {e}")
                self._stop.wait(self._tick)
                continue

            now = time.time()
            for name, check in services:
                if next_run.get(name, 0) <= now:
                    try:
                        result = run_check(check)
                    except Exception as e:
                        logger.warning(f"check for {name} crashed: {e}")
                        result = CheckResult(False, f"crash: {e}", now)
                    self._record(name, result)
                    next_run[name] = now + check.interval
            self._stop.wait(self._tick)

    def _record(self, name: str, result: CheckResult) -> None:
        with self._lock:
            st = self._states.get(name)
            if st is None:
                st = ServiceState(name=name)
                self._states[name] = st
            changed_to = st.push(result)
            new_event = None
            if changed_to is not None:
                from app.notifier import Event
                new_event = Event(
                    service=name,
                    kind="transition",
                    state=changed_to,
                    detail=result.detail,
                    timestamp=result.timestamp,
                )
                self._events.append(new_event)
                if len(self._events) > self._events_max:
                    self._events = self._events[-self._events_max:]
            notifiers = list(self._notifiers)
            callback = self._on_event

        if new_event is not None:
            from app.notifier import fanout
            fanout(notifiers, new_event)
            if callback is not None:
                try:
                    callback(new_event)
                except Exception as e:
                    logger.warning(f"health on_event failed: {e}")
