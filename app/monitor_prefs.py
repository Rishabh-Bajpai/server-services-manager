"""Persisted monitor preferences (borrowed from Laranode top-sort).

Laranode stores ``ps_aux_sort_by`` (cpu|memory) in cache via
``GET /dashboard/admin/get/top-sort`` and ``PATCH /dashboard/admin/set/top-sort``.
We port the idea without a cache backend: a tiny JSON file under
``~/.server-services-manager/monitor.json`` holding the default sort
column + direction for the Top Processes table on ``/monitor``.

Graceful by design: missing/corrupt file -> defaults, never raises to caller.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict

logger = logging.getLogger("MonitorPrefs")

_VALID_SORTS = ("cpu", "memory", "name", "pid", "status")
_VALID_DIRS = ("asc", "desc")

_DEFAULTS: Dict[str, Any] = {"sort": "cpu", "direction": "desc"}


def _prefs_path() -> str:
    base = os.path.expanduser("~/.server-services-manager")
    return os.path.join(base, "monitor.json")


def get_prefs() -> Dict[str, Any]:
    """Return current prefs merged over defaults. Never raises."""
    prefs = dict(_DEFAULTS)
    try:
        with open(_prefs_path()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            sort = str(data.get("sort", prefs["sort"])).lower()
            direction = str(data.get("direction", prefs["direction"])).lower()
            if sort in _VALID_SORTS:
                prefs["sort"] = sort
            if direction in _VALID_DIRS:
                prefs["direction"] = direction
    except (OSError, ValueError, AttributeError) as e:
        logger.debug(f"monitor prefs read failed, using defaults: {e}")
    return prefs


def set_prefs(sort: str, direction: str | None = None) -> Dict[str, Any]:
    """Validate + persist prefs atomically. Raises ValueError on bad input."""
    sort = (sort or "").strip().lower()
    if sort not in _VALID_SORTS:
        raise ValueError(f"invalid sort: {sort!r} (choose from {', '.join(_VALID_SORTS)})")
    if direction is None:
        # Sensible defaults: numeric desc, text asc — mirrors monitor.html.
        direction = "asc" if sort in ("name", "status") else "desc"
    direction = direction.strip().lower()
    if direction not in _VALID_DIRS:
        raise ValueError(f"invalid direction: {direction!r}")
    prefs = {"sort": sort, "direction": direction}
    path = _prefs_path()
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        # Harden pre-existing dirs too: this file documents chmod 700 for
        # ~/.server-services-manager (it also holds activity.db with
        # potential credentials), but the default umask leaves it 755.
        os.chmod(parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".monitor.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(prefs, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return prefs
