"""Tests for app/alert_log.py.

Uses a temporary SQLite DB to verify the notification_events table is
created lazily and queries return what we expect. We exercise:
- record() inserts and never raises
- list_events() filters and ordering
- channel_stats() aggregation
- summary() counters
- log_delivery() with real Notifier instances (mocked send)
- fanout_with_logging() runs delivery in threads
"""
import os
import sqlite3
import tempfile
import threading
import time
from unittest.mock import MagicMock

import pytest

import app.activity as activity
import app.alert_log as alert_log
from app.notifier import (
    Event,
    NtfyNotifier,
    WebhookNotifier,
    TelegramNotifier,
    EmailNotifier,
)


@pytest.fixture
def fresh_db(monkeypatch):
    """Point activity._DB_PATH and activity._CONN at a fresh tmpfile."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(activity, "_DB_PATH", path)
    monkeypatch.setattr(activity, "_CONN", None)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# record / log_delivery
# ---------------------------------------------------------------------------

def test_record_creates_table_lazily(fresh_db):
    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_events'"
    ).fetchall()
    conn.close()
    assert rows == []  # Not created yet
    alert_log.record(channel="ntfy", service="api", success=True)
    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_events'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1  # Now exists


def test_record_inserts_and_never_raises(monkeypatch, fresh_db):
    alert_log.record(channel="ntfy", service="api", success=True, latency_ms=42.5)
    events = alert_log.list_events()
    assert len(events) == 1
    e = events[0]
    assert e["channel"] == "ntfy"
    assert e["service"] == "api"
    assert e["success"] == 1
    assert e["latency_ms"] == 42.5


def test_record_failure(monkeypatch, fresh_db):
    alert_log.record(channel="webhook", success=False, error="timeout")
    events = alert_log.list_events()
    assert events[0]["success"] == 0
    assert events[0]["error"] == "timeout"


def test_record_swallows_db_errors(monkeypatch):
    """A broken DB must not crash the caller."""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(alert_log, "_ensure_table", boom)
    # Should not raise
    alert_log.record(channel="ntfy")


def test_log_delivery_extracts_recipient_for_ntfy(monkeypatch, fresh_db):
    n = NtfyNotifier(topic="alerts", server="https://ntfy.sh")
    event = Event(service="api", kind="transition", state="unhealthy", timestamp=time.time())
    alert_log.log_delivery(n, event, success=True, latency_ms=10.0)
    events = alert_log.list_events()
    assert events[0]["channel"] == "ntfy"
    assert "alerts" in events[0]["recipient"]


def test_log_delivery_extracts_recipient_for_webhook(monkeypatch, fresh_db):
    n = WebhookNotifier(url="https://example.com/hook")
    event = Event(service="api", kind="transition", state="healthy", timestamp=time.time())
    alert_log.log_delivery(n, event, success=True, latency_ms=12.5)
    events = alert_log.list_events()
    assert events[0]["channel"] == "webhook"
    assert "example.com" in events[0]["recipient"]


def test_log_delivery_extracts_recipient_for_telegram(monkeypatch, fresh_db):
    n = TelegramNotifier(bot_token="x", chat_id="12345")
    event = Event(service="api", kind="transition", state="healthy", timestamp=time.time())
    alert_log.log_delivery(n, event, success=True, latency_ms=8.0)
    events = alert_log.list_events()
    assert events[0]["channel"] == "telegram"
    assert "12345" in events[0]["recipient"]


def test_log_delivery_extracts_recipient_for_email(monkeypatch, fresh_db):
    n = EmailNotifier(host="smtp.example.com", port=587, username="x", password="x",
                     from_addr="a@b", to_addrs=["c@d", "e@f"])
    event = Event(service="api", kind="transition", state="healthy", timestamp=time.time())
    alert_log.log_delivery(n, event, success=True, latency_ms=50.0)
    events = alert_log.list_events()
    assert events[0]["channel"] == "email"
    assert "c@d" in events[0]["recipient"]
    assert "e@f" in events[0]["recipient"]


def test_log_delivery_unknown_channel(monkeypatch, fresh_db):
    """Unknown notifier class still produces a row."""
    class Weird:
        pass
    n = Weird()
    event = Event(service="api", kind="transition", state="healthy", timestamp=time.time())
    alert_log.log_delivery(n, event, success=True, latency_ms=0.0)
    events = alert_log.list_events()
    assert len(events) == 1
    # The channel is derived from the class name (lowercased, "Notifier" stripped).
    # For "Weird" with no "Notifier" suffix, the channel is "weird".
    assert events[0]["channel"] == "weird"


# ---------------------------------------------------------------------------
# list_events filters
# ---------------------------------------------------------------------------

def test_list_events_filter_by_channel(fresh_db):
    alert_log.record(channel="ntfy", success=True)
    alert_log.record(channel="webhook", success=False)
    alert_log.record(channel="ntfy", success=False)
    only_ntfy = alert_log.list_events(channel="ntfy")
    assert len(only_ntfy) == 2
    assert all(e["channel"] == "ntfy" for e in only_ntfy)


def test_list_events_filter_by_service(fresh_db):
    alert_log.record(channel="ntfy", service="api", success=True)
    alert_log.record(channel="ntfy", service="web", success=True)
    matching = alert_log.list_events(service="api")
    assert len(matching) == 1
    assert matching[0]["service"] == "api"


def test_list_events_filter_by_success(fresh_db):
    alert_log.record(channel="ntfy", success=True)
    alert_log.record(channel="ntfy", success=False)
    failures = alert_log.list_events(success=False)
    assert len(failures) == 1
    assert failures[0]["success"] == 0


def test_list_events_filter_by_since(fresh_db):
    alert_log.record(channel="ntfy", success=True)
    future = time.time() + 1000
    recent = alert_log.list_events(since=future)
    assert recent == []


def test_list_events_limit(fresh_db):
    for i in range(10):
        alert_log.record(channel="ntfy", success=True)
    assert len(alert_log.list_events(limit=3)) == 3


def test_list_events_newest_first(fresh_db):
    alert_log.record(channel="ntfy", success=True, error="first")
    time.sleep(0.005)
    alert_log.record(channel="ntfy", success=True, error="second")
    events = alert_log.list_events()
    # The "second" record was added later, so it should be first
    assert events[0]["error"] == "second"


# ---------------------------------------------------------------------------
# channel_stats / summary
# ---------------------------------------------------------------------------

def test_channel_stats_aggregates(fresh_db):
    alert_log.record(channel="ntfy", success=True, latency_ms=10)
    alert_log.record(channel="ntfy", success=True, latency_ms=20)
    alert_log.record(channel="ntfy", success=False, latency_ms=5)
    alert_log.record(channel="webhook", success=True, latency_ms=100)
    stats = alert_log.channel_stats()
    by_chan = {s["channel"]: s for s in stats}
    ntfy = by_chan["ntfy"]
    assert ntfy["total"] == 3
    assert ntfy["ok"] == 2
    assert ntfy["failed"] == 1
    assert ntfy["success_rate"] == pytest.approx(2/3)
    # Average latency is over the successful ones: (10+20)/2 = 15
    assert ntfy["avg_latency_ms"] == pytest.approx(15.0)
    webhook = by_chan["webhook"]
    assert webhook["total"] == 1
    assert webhook["ok"] == 1
    assert webhook["failed"] == 0


def test_channel_stats_since_filter(fresh_db):
    alert_log.record(channel="ntfy", success=True)
    future = time.time() + 1000
    stats = alert_log.channel_stats(since=future)
    assert stats == []


def test_summary(fresh_db):
    alert_log.record(channel="ntfy", success=True)
    alert_log.record(channel="ntfy", success=True)
    alert_log.record(channel="webhook", success=False)
    s = alert_log.summary()
    assert s["total"] == 3
    assert s["success"] == 2
    assert s["failed"] == 1
    assert s["success_rate"] == pytest.approx(2/3)
    assert s["channels"] == 2


def test_summary_empty(fresh_db):
    s = alert_log.summary()
    assert s["total"] == 0
    assert s["success"] == 0
    assert s["failed"] == 0
    assert s["channels"] == 0


# ---------------------------------------------------------------------------
# fanout_with_logging
# ---------------------------------------------------------------------------

def _wait_for_threads(timeout=2.0):
    """Wait briefly for any notifier threads to finish writing."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [t for t in threading.enumerate() if t.name.startswith("notifier-")]
        if not alive:
            return
        time.sleep(0.02)


def test_fanout_with_logging_records_success(fresh_db):
    n = MagicMock()
    n.send.return_value = True
    n.__class__.__name__ = "NtfyNotifier"
    event = Event(service="api", kind="transition", state="healthy", timestamp=time.time())
    alert_log.fanout_with_logging([n], event)
    _wait_for_threads()
    events = alert_log.list_events()
    assert len(events) == 1
    assert events[0]["channel"] == "ntfy"
    assert events[0]["success"] == 1
    assert events[0]["service"] == "api"


def test_fanout_with_logging_records_failure(fresh_db):
    n = MagicMock()
    n.send.return_value = False
    n.__class__.__name__ = "WebhookNotifier"
    event = Event(service="web", kind="transition", state="unhealthy", timestamp=time.time())
    alert_log.fanout_with_logging([n], event)
    _wait_for_threads()
    events = alert_log.list_events()
    assert len(events) == 1
    assert events[0]["success"] == 0


def test_fanout_with_logging_records_crash(fresh_db):
    n = MagicMock()
    n.send.side_effect = RuntimeError("boom")
    n.__class__.__name__ = "TelegramNotifier"
    event = Event(service="api", kind="transition", state="healthy", timestamp=time.time())
    alert_log.fanout_with_logging([n], event)
    _wait_for_threads()
    events = alert_log.list_events()
    assert len(events) == 1
    assert events[0]["success"] == 0
    assert "boom" in events[0]["error"]


def test_fanout_with_logging_runs_in_parallel(fresh_db):
    """Two slow notifiers should run concurrently, not sequentially."""
    def slow_send(event):
        time.sleep(0.2)
        return True
    n1 = MagicMock()
    n1.send.side_effect = slow_send
    n1.__class__.__name__ = "NtfyNotifier"
    n2 = MagicMock()
    n2.send.side_effect = slow_send
    n2.__class__.__name__ = "WebhookNotifier"
    event = Event(service="api", kind="transition", state="healthy", timestamp=time.time())
    started = time.monotonic()
    alert_log.fanout_with_logging([n1, n2], event)
    _wait_for_threads(timeout=3.0)
    elapsed = time.monotonic() - started
    # Two 0.2s sleeps in parallel should take ~0.2s, not 0.4s
    assert elapsed < 0.35
    events = alert_log.list_events()
    assert len(events) == 2


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def test_export_csv(fresh_db):
    alert_log.record(channel="ntfy", service="api", success=True, latency_ms=12.0)
    alert_log.record(channel="webhook", service="web", success=False, error="timeout")
    csv = alert_log.export_csv(alert_log.list_events())
    assert "channel" in csv  # header
    assert "ntfy" in csv
    assert "webhook" in csv
    assert "timeout" in csv