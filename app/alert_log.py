"""Notification delivery log (separate from user-action activity log).

When ``app.notifier.fanout()`` sends an event through each notifier,
each delivery attempt is recorded here with: timestamp, service,
channel (ntfy/webhook/telegram/email), recipient, success/failure,
latency, and any error message. This lets the /alerts page show
recent delivery history and per-notifier health stats.

Reuses the same SQLite file as :mod:`app.activity` so we don't
duplicate the file-locking machinery.
"""
import logging
import threading
import time
from typing import List, Optional

from app.activity import get_db

logger = logging.getLogger("AlertLog")

_LOCK = threading.RLock()


def _ensure_table() -> None:
    """Create the notification_events table on first use."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            service TEXT,
            channel TEXT NOT NULL,
            recipient TEXT,
            success INTEGER NOT NULL,
            latency_ms REAL,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notif_timestamp
            ON notification_events(timestamp DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notif_channel
            ON notification_events(channel, timestamp DESC)
    """)
    conn.commit()


def record(
    channel: str,
    service: str = "",
    recipient: str = "",
    success: bool = False,
    latency_ms: float = 0.0,
    error: str = "",
) -> None:
    """Record a single notification delivery attempt. Never raises."""
    try:
        with _LOCK:
            _ensure_table()
            conn = get_db()
            conn.execute(
                "INSERT INTO notification_events "
                "(timestamp, service, channel, recipient, success, latency_ms, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    service or "",
                    channel,
                    recipient or "",
                    1 if success else 0,
                    float(latency_ms) if latency_ms else 0.0,
                    error or "",
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"alert log failed: {e}")


def _describe_recipient(notifier) -> str:
    """Extract a friendly recipient identifier from a notifier instance."""
    cls = type(notifier).__name__
    try:
        if cls == "NtfyNotifier":
            url = getattr(notifier, "url", None)
            return str(url) if isinstance(url, str) else cls
        if cls == "WebhookNotifier":
            url = getattr(notifier, "url", None)
            return str(url) if isinstance(url, str) else cls
        if cls == "TelegramNotifier":
            chat = getattr(notifier, "chat_id", None)
            return f"chat={chat}" if isinstance(chat, str) else cls
        if cls == "EmailNotifier":
            addrs = getattr(notifier, "to_addrs", None)
            if isinstance(addrs, list):
                return ", ".join(str(a) for a in addrs if isinstance(a, str))
    except Exception:  # noqa: BLE001
        pass
    return cls


def log_delivery(notifier, event, success: bool, latency_ms: float, error: str = "") -> None:
    """Convenience wrapper: log a delivery for the given notifier + event."""
    channel = type(notifier).__name__.replace("Notifier", "").lower()
    record(
        channel=channel,
        service=getattr(event, "service", "") or "",
        recipient=_describe_recipient(notifier),
        success=success,
        latency_ms=latency_ms,
        error=error,
    )


def fanout_with_logging(notifiers, event) -> None:
    """Send ``event`` through all notifiers and record each delivery.

    Drop-in replacement for :func:`app.notifier.fanout` that also logs
    every delivery to the alert history table. Each send runs in its
    own thread so a slow SMTP server can't block the health-check loop.
    """
    for n in notifiers:
        t = threading.Thread(
            target=_send_and_log,
            args=(n, event),
            daemon=True,
            name=f"notifier-{type(n).__name__}",
        )
        t.start()


def _send_and_log(n, event) -> None:
    started = time.monotonic()
    success = False
    error = ""
    try:
        result = n.send(event)
        success = bool(result)
    except Exception as e:  # noqa: BLE001
        error = str(e) or type(e).__name__
        success = False
        logger.warning(f"notifier {type(n).__name__} crashed: {error}")
    latency_ms = (time.monotonic() - started) * 1000.0
    log_delivery(n, event, success=success, latency_ms=latency_ms, error=error)


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def list_events(
    channel: Optional[str] = None,
    service: Optional[str] = None,
    success: Optional[bool] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int = 200,
) -> List[dict]:
    """Return recent notification events, newest first."""
    clauses: List[str] = []
    args: list = []
    if channel:
        clauses.append("channel = ?")
        args.append(channel.lower())
    if service:
        clauses.append("service LIKE ?")
        args.append(f"%{service}%")
    if success is not None:
        clauses.append("success = ?")
        args.append(1 if success else 0)
    if since is not None:
        clauses.append("timestamp >= ?")
        args.append(since)
    if until is not None:
        clauses.append("timestamp <= ?")
        args.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM notification_events{where} ORDER BY timestamp DESC LIMIT ?"
    args.append(limit)
    try:
        with _LOCK:
            _ensure_table()
            conn = get_db()
            rows = conn.execute(sql, args).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"alert log list failed: {e}")
        return []


def channel_stats(since: Optional[float] = None) -> List[dict]:
    """Per-channel summary: total / successful / failed / avg latency.

    Returned channels are sorted by total attempts (most-used first).
    """
    where = ""
    args: list = []
    if since is not None:
        where = " WHERE timestamp >= ?"
        args.append(since)
    sql = (
        "SELECT channel, "
        "COUNT(*) AS total, "
        "SUM(success) AS ok, "
        "AVG(CASE WHEN success=1 THEN latency_ms ELSE NULL END) AS avg_latency_ms, "
        "MAX(timestamp) AS last_attempt, "
        "MAX(CASE WHEN success=0 THEN timestamp ELSE NULL END) AS last_failure "
        f"FROM notification_events{where} "
        "GROUP BY channel ORDER BY total DESC"
    )
    try:
        with _LOCK:
            _ensure_table()
            conn = get_db()
            rows = conn.execute(sql, args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                total = d.get("total") or 0
                ok = d.get("ok") or 0
                d["failed"] = total - ok
                d["success_rate"] = (ok / total) if total else 0.0
                d["avg_latency_ms"] = d.get("avg_latency_ms") or 0.0
                out.append(d)
            return out
    except Exception as e:
        logger.warning(f"alert log stats failed: {e}")
        return []


def summary(since: Optional[float] = None) -> dict:
    """Top-line counters: total/success/failure + active channels."""
    where = ""
    args: list = []
    if since is not None:
        where = " WHERE timestamp >= ?"
        args.append(since)
    sql = (
        "SELECT COUNT(*) AS total, "
        "SUM(success) AS ok "
        f"FROM notification_events{where}"
    )
    try:
        with _LOCK:
            _ensure_table()
            conn = get_db()
            row = conn.execute(sql, args).fetchone()
            total = (row["total"] or 0) if row else 0
            ok = (row["ok"] or 0) if row else 0
            return {
                "total": total,
                "success": ok,
                "failed": total - ok,
                "success_rate": (ok / total) if total else 0.0,
                "channels": len(channel_stats(since=since)),
            }
    except Exception as e:
        logger.warning(f"alert log summary failed: {e}")
        return {"total": 0, "success": 0, "failed": 0, "success_rate": 0.0, "channels": 0}


def export_csv(entries) -> str:
    """Format a CSV from a list of event dicts. Uses BOM and QUOTE_ALL for Excel safety."""
    import csv
    import io
    out = io.StringIO()
    fieldnames = ["timestamp", "service", "channel", "recipient", "success", "latency_ms", "error"]
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore",
                            quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    for e in entries:
        row = {}
        for k in fieldnames:
            v = e.get(k, "")
            if v is None:
                v = ""
            vs = str(v)
            if vs and vs[0] in ("=", "+", "-", "@"):
                vs = "'" + vs
            row[k] = vs
        writer.writerow(row)
    return "\ufeff" + out.getvalue()