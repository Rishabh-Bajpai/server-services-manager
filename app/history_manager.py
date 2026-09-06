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

_TIME_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})(?:\s+[AP]M)?")

# Sentinel: sar timestamps may be followed by an AM/PM token on 12-hour
# locales (e.g. "12:10:00 AM lo ..."). All parsers strip that token via
# _split_sar_line() and then locate values by header column name instead
# of hardcoded positions, so locale/version drift cannot mislabel data.


def _split_sar_line(line: str):
    """Split a sar data/header line into (time, rest_tokens).

    Returns ``None`` when the line is not timestamped (headers without
    timestamps, blank lines, "Average:" summaries, Linux banner, ...).
    The optional AM/PM marker on 12-hour locales is consumed here so
    callers never see it as an interface/CPU column.
    """
    m = _TIME_RE.match(line)
    if not m:
        return None
    rest = line[m.end():].split()
    return m.group(1), rest


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
    ``label`` adds a human-readable day (``sa10 (2026-09-04)``) so the
    UI dropdown is not a cryptic bare id.
    """
    import datetime

    out: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(_SAR_GLOB)):
        base = os.path.basename(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        try:
            day = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            label = f"{base} ({day})" if mtime else base
        except (OSError, OverflowError, ValueError):
            label = base
        out.append(
            {
                "id": base,
                "label": label,
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
    """Parse ``sar -u`` for user/system/idle over time.

    Header-driven: the ``%user``/``%system``/``%idle`` column positions
    are read from sar's own header row, so extra columns or a missing
    CPU column across sysstat versions cannot shift the values.
    """
    path = _resolve_report(report)
    out = _run_sar(["-u", "-f", path])
    metrics: List[Dict[str, Any]] = []
    truncated = False
    header: Optional[List[str]] = None
    user_idx = system_idx = idle_idx = -1
    for raw in out.splitlines():
        line = raw.strip()
        split = _split_sar_line(line)
        if not split:
            continue
        tstamp, rest = split
        if not rest:
            continue
        lowered = [t.lower() for t in rest]
        if any(t.startswith("%") for t in rest):
            # Header row, e.g. "CPU %user %nice %system ... %idle".
            header = lowered
            try:
                user_idx = header.index("%user")
                idle_idx = header.index("%idle")
                system_idx = header.index("%system")
            except ValueError:
                header = None
                user_idx = system_idx = idle_idx = -1
            continue
        if header is None or user_idx < 0 or idle_idx < 0 or system_idx < 0:
            continue
        if len(rest) <= max(user_idx, system_idx, idle_idx):
            continue
        # Skip per-CPU rows ("0", "1", ...) — only the "all" aggregate.
        cpu_col = header.index("cpu") if "cpu" in header else -1
        if cpu_col >= 0 and len(rest) > cpu_col:
            if rest[cpu_col].lower() != "all":
                continue
        user = _parse_float(rest[user_idx])
        system = _parse_float(rest[system_idx])
        idle = _parse_float(rest[idle_idx])
        if user is None or system is None or idle is None:
            continue
        total = max(0.0, min(100.0, 100.0 - idle))
        metrics.append(
            {
                "time": tstamp,
                "user": round(user, 2),
                "system": round(system, 2),
                "idle": round(idle, 2),
                "total": round(total, 2),
            }
        )
        if len(metrics) >= limit:
            truncated = True
            break
    return {
        "report": os.path.basename(path),
        "metrics": metrics,
        "count": len(metrics),
        "truncated": truncated,
    }


def get_memory_history(report: Optional[str] = None, limit: int = 500) -> Dict[str, Any]:
    """Parse ``sar -r`` for available/used/percent over time.

    Header-driven: ``kbmemfree``/``kbavail``/``kbmemused``/``%memused``
    positions come from sar's header row, tolerating columns added or
    removed across sysstat versions.
    """
    path = _resolve_report(report)
    out = _run_sar(["-r", "-f", path])
    metrics: List[Dict[str, Any]] = []
    truncated = False
    header: Optional[List[str]] = None
    idx_free = idx_avail = idx_used = idx_pct = -1
    for raw in out.splitlines():
        line = raw.strip()
        split = _split_sar_line(line)
        if not split:
            continue
        tstamp, rest = split
        if not rest:
            continue
        lowered = [t.lower() for t in rest]
        if "kbmemfree" in lowered or "%memused" in lowered:
            header = lowered
            try:
                idx_free = header.index("kbmemfree")
                idx_avail = header.index("kbavail")
                idx_used = header.index("kbmemused")
                idx_pct = header.index("%memused")
            except ValueError:
                header = None
                idx_free = idx_avail = idx_used = idx_pct = -1
            continue
        if header is None or min(idx_free, idx_avail, idx_used, idx_pct) < 0:
            continue
        if len(rest) <= max(idx_free, idx_avail, idx_used, idx_pct):
            continue
        free_kb = _parse_float(rest[idx_free])
        avail_kb = _parse_float(rest[idx_avail])
        used_kb = _parse_float(rest[idx_used])
        pct = _parse_float(rest[idx_pct])
        if free_kb is None or avail_kb is None or used_kb is None or pct is None:
            continue
        metrics.append(
            {
                "time": tstamp,
                "free_gb": round(free_kb / 1024 / 1024, 3),
                "avail_gb": round(avail_kb / 1024 / 1024, 3),
                "used_gb": round(used_kb / 1024 / 1024, 3),
                "percent": round(pct, 2),
            }
        )
        if len(metrics) >= limit:
            truncated = True
            break
    return {
        "report": os.path.basename(path),
        "metrics": metrics,
        "count": len(metrics),
        "truncated": truncated,
    }


def get_network_history(
    report: Optional[str] = None, limit: int = 1000
) -> Dict[str, Any]:
    """Parse ``sar -n DEV`` for per-interface rx/tx kB/s over time.

    Header-driven: the ``IFACE``/``rxkB/s``/``txkB/s`` positions come
    from sar's header row, and a 12-hour-locale ``AM``/``PM`` token is
    stripped by :func:`_split_sar_line`, so values can never be shifted
    into packet-rate columns. Loopback (``lo``) is excluded from the
    series, matching the UI's "all interfaces, totaled" chart.
    """
    path = _resolve_report(report)
    out = _run_sar(["-n", "DEV", "-f", path])
    metrics: List[Dict[str, Any]] = []
    truncated = False
    header: Optional[List[str]] = None
    idx_iface = idx_rx = idx_tx = -1
    for raw in out.splitlines():
        line = raw.strip()
        split = _split_sar_line(line)
        if not split:
            continue
        tstamp, rest = split
        if not rest:
            continue
        lowered = [t.lower() for t in rest]
        if "iface" in lowered:
            header = lowered
            try:
                idx_iface = header.index("iface")
                idx_rx = header.index("rxkb/s")
                idx_tx = header.index("txkb/s")
            except ValueError:
                header = None
                idx_iface = idx_rx = idx_tx = -1
            continue
        if header is None or min(idx_iface, idx_rx, idx_tx) < 0:
            continue
        if len(rest) <= max(idx_iface, idx_rx, idx_tx):
            continue
        iface = rest[idx_iface]
        if iface.upper() == "IFACE":
            continue
        if iface == "lo":
            continue
        rx = _parse_float(rest[idx_rx])
        tx = _parse_float(rest[idx_tx])
        if rx is None or tx is None:
            continue
        metrics.append(
            {
                "time": tstamp,
                "interface": iface,
                "rx_kbs": round(rx, 2),
                "tx_kbs": round(tx, 2),
                "total_kbs": round(rx + tx, 2),
            }
        )
        if len(metrics) >= limit:
            truncated = True
            break
    return {
        "report": os.path.basename(path),
        "metrics": metrics,
        "count": len(metrics),
        "truncated": truncated,
    }
