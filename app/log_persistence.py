"""Per-service log tail persistence.

The Program's in-memory :class:`collections.deque` of log lines is
lost on restart. This module writes each line to
``~/.server-services-manager/logs/<service>.log`` as it arrives, so
the last N lines survive across restarts. A size cap keeps the
files from growing without bound; when the cap is reached, the
file is rotated in place.
"""
import logging
import os
import threading
from typing import List

logger = logging.getLogger("LogPersistence")

_BASE = os.path.expanduser("~/.server-services-manager/logs")
_MAX_BYTES = 512 * 1024  # 512 KB per service

_LOCKS: dict = {}
_GLOBAL_LOCK = threading.Lock()


def _lock_for(name: str) -> threading.Lock:
    with _GLOBAL_LOCK:
        lk = _LOCKS.get(name)
        if lk is None:
            lk = threading.Lock()
            _LOCKS[name] = lk
        return lk


def _safe_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "_-.")


def _path(name: str) -> str:
    return os.path.join(_BASE, _safe_name(name) + ".log")


def append_line(name: str, line: str) -> None:
    """Append one log line; rotate if over the cap."""
    path = _path(name)
    lk = _lock_for(name)
    with lk:
        try:
            os.makedirs(_BASE, exist_ok=True)
        except OSError:
            return
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(line.rstrip("\n") + "\n")
            # Cheap rotation: if file too big, keep last half.
            try:
                size = os.path.getsize(path)
                if size > _MAX_BYTES:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    keep = content[-_MAX_BYTES // 2:]
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(keep)
            except OSError:
                pass
        except OSError:
            pass


def read_tail(name: str, lines: int = 200) -> List[str]:
    """Return the most recent ``lines`` lines for the service."""
    path = _path(name)
    lk = _lock_for(name)
    with lk:
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return []
    if not content:
        return []
    all_lines = content.splitlines()
    return all_lines[-lines:]


def clear(name: str) -> None:
    path = _path(name)
    lk = _lock_for(name)
    with lk:
        try:
            os.remove(path)
        except OSError:
            pass
