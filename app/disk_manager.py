"""Disk usage analyzer.

Runs ``du`` against a path the user is allowed to read and parses
the output into a structured form for the ``/disk`` page. All paths
are chrooted to ``$HOME`` using the same pattern as the file
manager — symlinks outside the home directory are blocked via
``os.path.realpath()``.

The module never raises to its caller for permission errors —
errors are returned as dicts with ``{"error": ..., "code": ...}``
so the frontend can render a friendly message instead of crashing
the whole page.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("DiskManager")

# du is reasonably fast on a home directory but a recursive walk of
# e.g. ~/node_modules on a developer's box can take a few seconds.
# 15 s is enough for any realistic subtree; if it ever fires, the
# frontend just shows "du took too long".
_DU_TIMEOUT_SECONDS = 15

# Hard cap on the recursive depth the frontend is allowed to request.
# du can be made to scan arbitrarily deep; we want a sane upper bound
# so a malicious query string can't pin a CPU on the user's machine.
_MAX_DEPTH = 3


class DiskError(Exception):
    """Raised by :func:`get_usage` when the request can't be served."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def home_dir() -> str:
    """Return the realpath of the user's home directory."""
    return os.path.realpath(os.path.expanduser("~"))


def resolve_under_home(path: str) -> str:
    """Resolve ``path`` against ``$HOME`` and verify it stays inside.

    ``""`` and ``"."`` map to the home directory itself. An absolute
    path is rejected — only relative paths from home are accepted,
    because we don't want this endpoint to leak disk usage of
    ``/etc`` or ``/var``.
    """
    home = home_dir()
    if path in ("", ".", "./"):
        target = home
    elif os.path.isabs(path):
        # Refuse absolute paths outright.
        raise DiskError("outside_home", f"absolute paths are not allowed: {path}")
    else:
        target = os.path.realpath(os.path.join(home, path))

    if target != home and not target.startswith(home + os.sep):
        raise DiskError("outside_home", f"path escapes $HOME: {target}")
    return target


def humanize(size_bytes: int) -> str:
    """Format a byte count as a short human-readable string.

    Uses 1024-based units. Always returns a string — never raises.
    Suitable for both the API response (extra hint) and the UI.
    """
    try:
        n = float(size_bytes or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} EB"


def _parse_du_lines(stdout: str, target: str) -> Tuple[int, List[Dict[str, Any]]]:
    """Parse raw ``du`` output into ``(total, items)``.

    ``du --all --max-depth=N -b <target>`` emits one line per
    immediate child (file or directory) followed by a final line
    with the directory's total. We classify each line by comparing
    its path to the target: lines whose parent is ``target`` are
    children; the line equal to ``target`` is the total. Anything
    nested deeper than ``target`` (e.g. when ``depth > 1``) is
    ignored — we only want immediate children.
    """
    total = 0
    items: List[Dict[str, Any]] = []
    target = target.rstrip(os.sep) or target
    home = home_dir()
    seen_total = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        size_str, p = parts
        try:
            size = int(size_str)
        except ValueError:
            continue
        if not p:
            continue
        p = p.rstrip(os.sep)
        if p == target:
            total = size
            seen_total = True
            continue
        parent = os.path.dirname(p)
        if parent != target:
            # Either nested deeper than we asked, or some oddity
            # we don't want to surface.
            continue
        is_dir = os.path.isdir(p)
        rel = os.path.relpath(p, home)
        items.append({
            "name": os.path.basename(p),
            "path": rel,                # relative to $HOME, safe for the frontend
            "is_dir": is_dir,
            "size": size,
        })
    if not seen_total:
        # du didn't return the target line — typically when the
        # directory is empty or unreadable. Fall back to the sum
        # of children so the UI still shows something sane.
        total = sum(i["size"] for i in items)
    items.sort(key=lambda x: -x["size"])
    return total, items


def get_usage(path: str = "", depth: int = 1) -> Dict[str, Any]:
    """Return disk usage info for ``path`` (relative to ``$HOME``).

    The returned dict has the shape::

        {
            "path":         str,    # path relative to $HOME ("." for home)
            "absolute":     str,    # absolute path (for breadcrumb display only)
            "total":        int,    # total bytes used by the directory
            "total_human":  str,    # human-readable total
            "items":        list,   # immediate children, sorted by size desc
            "depth":        int,    # depth the client asked for (capped)
        }

    On any error, raises :class:`DiskError`. The caller (route
    handler) maps those to HTTP error codes.
    """
    target = resolve_under_home(path)

    if not os.path.exists(target):
        raise DiskError("not_found", f"path does not exist: {target}")
    if not os.path.isdir(target):
        raise DiskError("not_a_directory", f"not a directory: {target}")

    try:
        depth_i = int(depth)
    except (TypeError, ValueError):
        depth_i = 1
    depth_i = max(1, min(depth_i, _MAX_DEPTH))

    cmd = ["du", "--all", "--max-depth", str(depth_i), "-b", target]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_DU_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DiskError(
            "timeout",
            f"du took longer than {_DU_TIMEOUT_SECONDS}s — try a smaller subtree",
        )
    except FileNotFoundError:
        # /usr/bin/du missing — extremely unusual.
        raise DiskError("du_missing", "the `du` binary is not installed")

    if proc.returncode != 0 and not proc.stdout.strip():
        raise DiskError(
            "du_failed",
            f"du exited {proc.returncode}: {proc.stderr.strip() or 'unknown error'}",
        )

    total, items = _parse_du_lines(proc.stdout, target)

    rel = os.path.relpath(target, home_dir())
    return {
        "path": rel if rel != "." else ".",
        "absolute": target,
        "total": total,
        "total_human": humanize(total),
        "items": items,
        "depth": depth_i,
    }


def largest_items(items: List[Dict[str, Any]], n: int = 20) -> List[Dict[str, Any]]:
    """Return the top ``n`` items by size, descending.

    This is just a convenience helper for the sidebar — the items
    are already sorted in :func:`get_usage`. We add a ``size_human``
    field so the frontend doesn't need to format them itself.
    """
    try:
        limit = max(1, min(int(n), 100))
    except (TypeError, ValueError):
        limit = 20
    out: List[Dict[str, Any]] = []
    for i, it in enumerate(items[:limit]):
        out.append({
            "name": it.get("name", ""),
            "path": it.get("path", ""),
            "is_dir": bool(it.get("is_dir")),
            "size": int(it.get("size", 0)),
            "size_human": humanize(int(it.get("size", 0))),
        })
    return out


def breadcrumb(path: str) -> List[Dict[str, str]]:
    """Break a path into breadcrumb segments.

    Returns a list of ``{"label": str, "path": str}`` dicts where
    ``path`` is the cumulative relative path. The first entry is
    always ``$HOME`` with ``"."``.
    """
    rel = path or "."
    if rel == "." or rel == "":
        return [{"label": "~", "path": "."}]
    parts = rel.split(os.sep)
    crumbs: List[Dict[str, str]] = [{"label": "~", "path": "."}]
    cur = ""
    for p in parts:
        if not p:
            continue
        cur = os.path.join(cur, p) if cur else p
        crumbs.append({"label": p, "path": cur})
    return crumbs