"""Pinned-unit service overview for /monitor (idea borrowed from Laranode).

Laranode shows Apache/MySQL/PHP-FPM cards parsed from
``systemctl status X`` with awk (see ``SystemStatsService.php``). That
parsing is fragile across systemd versions, so we port the *idea* but
use ``systemctl show --property=...`` which is machine-readable and
already used by ``app/system_services.py``.

``getSummaries([units])`` returns one dict per unit:
``{name, exists, active_state, sub_state, main_pid, memory_bytes,
cpu_nsec, active_enter}``. Missing units return ``exists=False``
instead of raising, so the UI can hide them.
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Any, Dict, List

logger = logging.getLogger("ServiceOverview")

_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:\-]+\.(service|socket|timer|path|mount)$")
_MAX_UNITS = 10
_TIMEOUT = 8

_PROPS = (
    "Id,LoadState,ActiveState,SubState,MainPID,"
    "MemoryCurrent,MemoryPeak,CPUUsageNSec,"
    "ActiveEnterTimestamp,UnitFileState,Description"
)


def validate_unit(name: str) -> str:
    """Normalize + validate a unit name. Raises ValueError."""
    cleaned = (name or "").strip()
    if not _UNIT_RE.match(cleaned):
        raise ValueError(f"invalid unit name: {name!r}")
    if len(cleaned) > 128:
        raise ValueError(f"unit name too long: {name!r}")
    return cleaned


def _parse_show(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_summary(unit: str) -> Dict[str, Any]:
    """Return a summary dict for a single unit. Never raises."""
    try:
        name = validate_unit(unit)
    except ValueError as e:
        return {"name": unit, "exists": False, "error": str(e)}
    try:
        proc = subprocess.run(
            ["systemctl", "show", name, "--no-pager", "--property", _PROPS],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        return {"name": name, "exists": False, "error": "systemctl not found"}
    except subprocess.TimeoutExpired:
        return {"name": name, "exists": False, "error": "systemctl timed out"}
    if proc.returncode != 0:
        return {"name": name, "exists": False, "error": (proc.stderr or proc.stdout or "").strip()[:200]}
    data = _parse_show(proc.stdout or "")
    if not data.get("Id") or data.get("LoadState") == "not-found":
        return {"name": name, "exists": False, "error": "not found"}
    return {
        "name": name,
        "exists": True,
        "description": data.get("Description", ""),
        "load_state": data.get("LoadState", ""),
        "active_state": data.get("ActiveState", ""),
        "sub_state": data.get("SubState", ""),
        "unit_file_state": data.get("UnitFileState", ""),
        "main_pid": _to_int(data.get("MainPID", "0")),
        "memory_bytes": _to_int(data.get("MemoryCurrent", "0")),
        "memory_peak": _to_int(data.get("MemoryPeak", "0")),
        "cpu_nsec": _to_int(data.get("CPUUsageNSec", "0")),
        "active_enter": data.get("ActiveEnterTimestamp", ""),
    }


def get_summaries(units: List[str]) -> List[Dict[str, Any]]:
    """Return summaries for up to _MAX_UNITS units, preserving order."""
    cleaned: List[str] = []
    seen = set()
    for raw in units or []:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
        if len(cleaned) >= _MAX_UNITS:
            break
    return [get_summary(u) for u in cleaned]
