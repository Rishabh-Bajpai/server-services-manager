"""SQLite-backed activity log / audit trail.

Every privileged or state-changing action taken through the app is
recorded with: timestamp, action, target, user, source IP, status
(success/failure), and detail. Records are append-only.

The DB lives at ``~/.server-services-manager/activity.db`` and is
opened lazily on first call to :func:`get_db`.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Iterable, List, Optional

logger = logging.getLogger("ActivityLog")

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_DB_PATH = os.path.expanduser("~/.server-services-manager/activity.db")


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)


def get_db() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        _ensure_dir()
        _CONN = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode = WAL")
        _CONN.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                user TEXT,
                ip TEXT,
                action TEXT NOT NULL,
                target TEXT,
                status TEXT NOT NULL,
                detail TEXT
            )
        """)
        _CONN.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_timestamp
                ON activity(timestamp DESC)
        """)
        _CONN.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_action
                ON activity(action, timestamp DESC)
        """)
        _CONN.commit()
    return _CONN


def log(action: str, target: str = "", status: str = "ok", detail: str = "",
        user: str = "", ip: str = "") -> None:
    """Record a single activity entry. Never raises — logging is best-effort."""
    try:
        with _LOCK:
            conn = get_db()
            conn.execute(
                "INSERT INTO activity (timestamp, user, ip, action, target, status, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), user or "", ip or "", action, target or "", status, detail or ""),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"activity log failed: {e}")


def list_entries(
    action: Optional[str] = None,
    target: Optional[str] = None,
    user: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int = 200,
) -> List[dict]:
    """Return recent activity entries, newest first, with optional filters."""
    clauses: List[str] = []
    args: list = []
    if action:
        # Substring match (not exact): badges render uppercase via
        # CSS while values are stored lowercase, so users type what
        # they see. SQLite LIKE is ASCII case-insensitive.
        clauses.append("action LIKE ?")
        args.append(f"%{action}%")
    if target:
        clauses.append("target LIKE ?")
        args.append(f"%{target}%")
    if user:
        clauses.append("user = ?")
        args.append(user)
    if status:
        clauses.append("status = ?")
        args.append(status)
    if since:
        clauses.append("timestamp >= ?")
        args.append(since)
    if until:
        clauses.append("timestamp <= ?")
        args.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM activity{where} ORDER BY timestamp DESC LIMIT ?"
    args.append(limit)
    try:
        conn = get_db()
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"activity list failed: {e}")
        return []


def export_csv(entries: Iterable[dict]) -> str:
    """Format a CSV from a list of entry dicts. Uses BOM for Excel, QUOTE_ALL, and str coercion."""
    import csv
    import io
    out = io.StringIO()
    fieldnames = ["timestamp", "user", "ip", "action", "target", "status", "detail"]
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore",
                            quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    for e in entries:
        row = {}
        for k in fieldnames:
            v = e.get(k, "")
            if v is None:
                v = ""
            # Prevent CSV injection for detail/status fields starting with = + - @
            vs = str(v)
            if vs and vs[0] in ("=", "+", "-", "@"):
                vs = "'" + vs
            row[k] = vs
        writer.writerow(row)
    return "\ufeff" + out.getvalue()


def count_by_action() -> List[dict]:
    """Summary of how many events of each action have been logged."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT action, COUNT(*) as n FROM activity GROUP BY action ORDER BY n DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"activity count failed: {e}")
        return []
