"""Historic system stats via sysstat/sar (borrowed pattern from Laranode).

Laranode parses ``/var/log/sysstat/saNN`` with ``sar -u/-r/-n DEV -f <file>``
piped through awk (see ``SarHistory.php``, ``CPUHistoryService.php``,
``MemoryHistoryService.php``, ``NetworkHistoryService.php``). We port the
idea to Python but parse in-process instead of shell pipes: safer, no
shell injection, and easier to test.

If ``sar`` or ``/var/log/sysstat`` is missing, every function returns a
graceful ``{"available": False, "reason": ...}`` payload instead of
raising, so the UI can show "install sysstat" rather than crashing.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("HistoryManager")

_SAR_DIR = "/var/log/sysstat"
_SAR_GLOB = os.path.join(_SAR_DIR, "sa[0-9][0-9]")
_SAR_TIMEOUT = 15

_TIME_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})")


class HistoryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_available() -> Dict[str, Any]:
    """Probe whether historic stats can be served."""
    sar = shutil.which("sar")
    if not sar:
        return {
            "available": False,
            "reason": "sysstat/sar is not installed. Install with: sudo apt install sysstat",
        }
    files = sorted(glob.glob(_SAR_GLOB))
    if not files:
        return {
            "available": False,
            "reason": (
                "No sar files found in /var/log/sysstat/sa[0-9]{2}. "
                "Enable sysstat collection (ENABLED=true in /etc/default/sysstat)."
            ),
        }
    return {"available": True, "reason": "", "reports": len(files)}


def list_reports() -> List[Dict[str, Any]]:
    """List available sar reports, newest first.

    Each entry: ``{"id": "sa10", "path": ..., "label": ..., "mtime": ...}``.
    ``id`` is the basename so the API never leaks absolute paths.
    """
    out: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(_SAR_GLOB)):
        base = os.path.basename(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        out.append(
            {
                "id": base,
                "label": base,
                "mtime": mtime,
            }
        )
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _resolve_report(report: Optional[str]) -> str:
    """Map a ``saNN`` id to an absolute path, or default to newest."""
    reports = list_reports()
    if not reports:
        raise HistoryError(
            "no_reports",
            "No sar files found in /var/log/sysstat/sa[0-9]{2}",
        )
    if not report:
        newest = reports[0]
        return os.path.join(_SAR_DIR, newest["id"])
    base = os.path.basename((report or "").strip())
    if not re.fullmatch(r"sa\d{2}", base):
        raise HistoryError("invalid_report", f"invalid report id: {report!r}")
    candidate = os.path.join(_SAR_DIR, base)
    if not os.path.isfile(candidate):
        raise HistoryError("not_found", f"report not found: {base}")
    return candidate


def _run_sar(args: List[str]) -> str:
    try:
        proc = subprocess.run(
            ["sar"] + args,
            capture_output=True,
            text=True,
            timeout=_SAR_TIMEOUT,
        )
    except FileNotFoundError:
        raise HistoryError("missing_tool", "sar binary not found")
    except subprocess.TimeoutExpired:
        raise HistoryError("timeout", "sar took too long")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or "sar failed"
        raise HistoryError("sar_failed", err[:500])
    return proc.stdout or ""


def _parse_float(tok: str) -> Optional[float]:
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def get_cpu_history(report: Optional[str] = None, limit: int = 500) -> Dict[str, Any]:
    """Parse ``sar -u`` for user/system/idle over time."""
    path = _resolve_report(report)
    out = _run_sar(["-u", "-f", path])
    metrics: List[Dict[str, Any]] = []
    for raw in out.splitlines():
        line = raw.strip()
        m = _TIME_RE.match(line)
        if not m:
            continue
        parts = line.split()
        # sar -u columns: time CPU %user %nice %system %iowait %steal %idle
        # Some versions omit CPU column when -u without -P. Handle both.
        nums = [_parse_float(t) for t in parts[1:]]
        nums = [n for n in nums if n is not None]
        if len(nums) < 3:
            continue
        # Heuristic: last value is %idle, first numeric after time/CPU is %user.
        # For "time CPU user nice system ... idle": nums = [cpu?, user, nice, system, ..., idle]
        # For "time user nice system ... idle": nums = [user, nice, system, ..., idle]
        idle = nums[-1]
        if len(nums) >= 7:
            user = nums[1]
            system = nums[3]
        elif len(nums) >= 6:
            user = nums[0]
            # nums = [user, nice, system, iowait, steal, idle] (no CPU col)
            system = nums[2]
        else:
            user = nums[0]
            system = nums[1] if len(nums) > 1 else 0.0
        total = max(0.0, min(100.0, 100.0 - idle))
        metrics.append(
            {
                "time": m.group(1),
                "user": round(user, 2),
                "system": round(system, 2),
                "idle": round(idle, 2),
                "total": round(total, 2),
            }
        )
        if len(metrics) >= limit:
            break
    return {
        "report": os.path.basename(path),
        "metrics": metrics,
        "count": len(metrics),
    }


def get_memory_history(report: Optional[str] = None, limit: int = 500) -> Dict[str, Any]:
    """Parse ``sar -r`` for available/used/percent over time."""
    path = _resolve_report(report)
    out = _run_sar(["-r", "-f", path])
    metrics: List[Dict[str, Any]] = []
    for raw in out.splitlines():
        line = raw.strip()
        m = _TIME_RE.match(line)
        if not m:
            continue
        parts = line.split()
        nums = [_parse_float(t) for t in parts[1:]]
        nums = [n for n in nums if n is not None]
        # sar -r: kbmemfree kbavail kbmemused %memused kbbuffers kbcached ...
        if len(nums) < 4:
            continue
        free_kb, avail_kb, used_kb, pct = nums[0], nums[1], nums[2], nums[3]
        metrics.append(
            {
                "time": m.group(1),
                "free_gb": round(free_kb / 1024 / 1024, 3),
                "avail_gb": round(avail_kb / 1024 / 1024, 3),
                "used_gb": round(used_kb / 1024 / 1024, 3),
                "percent": round(pct, 2),
            }
        )
        if len(metrics) >= limit:
            break
    return {
        "report": os.path.basename(path),
        "metrics": metrics,
        "count": len(metrics),
    }


def get_network_history(
    report: Optional[str] = None, limit: int = 1000
) -> Dict[str, Any]:
    """Parse ``sar -n DEV`` for per-interface rx/tx kB/s over time."""
    path = _resolve_report(report)
    out = _run_sar(["-n", "DEV", "-f", path])
    metrics: List[Dict[str, Any]] = []
    for raw in out.splitlines():
        line = raw.strip()
        m = _TIME_RE.match(line)
        if not m:
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        # time IFACE rxpck/s txpck/s rxkB/s txkB/s ...
        iface = parts[1]
        rx = _parse_float(parts[4])
        tx = _parse_float(parts[5])
        if rx is None or tx is None:
            continue
        # Skip aggregate / loopback noise like Laranode UI does per-iface.
        metrics.append(
            {
                "time": m.group(1),
                "interface": iface,
                "rx_kbs": round(rx, 2),
                "tx_kbs": round(tx, 2),
                "total_kbs": round(rx + tx, 2),
            }
        )
        if len(metrics) >= limit:
            break
    return {
        "report": os.path.basename(path),
        "metrics": metrics,
        "count": len(metrics),
    }
