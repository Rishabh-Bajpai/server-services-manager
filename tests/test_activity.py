import os
import tempfile
import time
from unittest.mock import patch

import pytest

from app import activity


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Use a fresh DB for each test."""
    db_path = tmp_path / "activity.db"
    monkeypatch.setattr(activity, "_DB_PATH", str(db_path))
    monkeypatch.setattr(activity, "_CONN", None)
    yield
    if activity._CONN is not None:
        try:
            activity._CONN.close()
        except Exception:
            pass
    monkeypatch.setattr(activity, "_CONN", None)


class TestLog:
    def test_basic(self):
        activity.log("test.action", target="x", status="ok", detail="hi")
        entries = activity.list_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["action"] == "test.action"
        assert e["target"] == "x"
        assert e["status"] == "ok"
        assert e["detail"] == "hi"
        assert e["timestamp"] > 0

    def test_default_status(self):
        activity.log("test.action")
        assert activity.list_entries()[0]["status"] == "ok"

    def test_default_user_and_ip(self):
        activity.log("test.action", target="x", user="alice", ip="10.0.0.1")
        e = activity.list_entries()[0]
        assert e["user"] == "alice"
        assert e["ip"] == "10.0.0.1"

    def test_log_does_not_raise_on_db_error(self):
        with patch.object(activity, "get_db", side_effect=Exception("db gone")):
            # Should not raise
            activity.log("test", "x")


class TestListEntries:
    def test_empty(self):
        assert activity.list_entries() == []

    def test_filter_by_action(self):
        activity.log("a.action", target="x")
        activity.log("b.action", target="y")
        assert len(activity.list_entries(action="a.action")) == 1
        assert activity.list_entries(action="b.action")[0]["target"] == "y"

    def test_filter_by_target_substring(self):
        activity.log("test", target="api-server")
        activity.log("test", target="worker")
        assert len(activity.list_entries(target="server")) == 1

    def test_filter_by_status(self):
        activity.log("test", status="ok")
        activity.log("test", status="error")
        assert len(activity.list_entries(status="error")) == 1

    def test_filter_by_since(self):
        activity.log("old", target="x")
        time.sleep(0.05)
        cutoff = time.time()
        time.sleep(0.05)
        activity.log("new", target="y")
        entries = activity.list_entries(since=cutoff)
        assert len(entries) == 1
        assert entries[0]["action"] == "new"

    def test_limit(self):
        for i in range(20):
            activity.log(f"e{i}")
        assert len(activity.list_entries(limit=5)) == 5

    def test_newest_first(self):
        activity.log("first", target="1")
        time.sleep(0.01)
        activity.log("second", target="2")
        entries = activity.list_entries()
        assert entries[0]["action"] == "second"
        assert entries[1]["action"] == "first"


class TestExport:
    def test_csv_format(self):
        activity.log("test.action", target="x", status="ok", detail="hi", ip="1.2.3.4")
        csv = activity.export_csv(activity.list_entries())
        lines = csv.strip().split("\n")
        # CSV writer uses \r\n on most platforms; header may be quoted and have BOM
        header = lines[0].lstrip("\ufeff").rstrip("\r").replace('"', "")
        assert header == "timestamp,user,ip,action,target,status,detail"
        assert "test.action" in lines[1]
        assert "1.2.3.4" in lines[1]
        assert "x" in lines[1]


class TestCountByAction:
    def test_groups_by_action(self):
        for _ in range(3):
            activity.log("a.action")
        for _ in range(2):
            activity.log("b.action")
        summary = activity.count_by_action()
        d = {s["action"]: s["n"] for s in summary}
        assert d["a.action"] == 3
        assert d["b.action"] == 2

    def test_empty(self):
        assert activity.count_by_action() == []


class TestConcurrentSafety:
    def test_concurrent_writes_dont_lose_data(self):
        import threading
        errors = []

        def writer(n):
            try:
                for i in range(10):
                    activity.log(f"thread{n}", target=f"x{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # 5 threads x 10 writes each = 50 entries
        assert len(activity.list_entries(limit=100)) == 50
