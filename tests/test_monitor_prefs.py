"""Tests for persisted monitor sort prefs."""
import json
import os
import tempfile
from unittest.mock import patch

from app import monitor_prefs as mp


def _tmp_path(monkey=None):
    tmp = tempfile.mkdtemp()
    return os.path.join(tmp, "monitor.json")


def test_defaults_when_missing():
    with patch("app.monitor_prefs._prefs_path", return_value="/nonexistent/monitor.json"):
        out = mp.get_prefs()
        assert out == {"sort": "cpu", "direction": "desc"}


def test_set_and_get_roundtrip():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "monitor.json")
    with patch("app.monitor_prefs._prefs_path", return_value=path):
        out = mp.set_prefs("memory")
        assert out == {"sort": "memory", "direction": "desc"}
        assert mp.get_prefs() == out
        # Text sorts default asc.
        out2 = mp.set_prefs("name")
        assert out2 == {"sort": "name", "direction": "asc"}


def test_invalid_sort_rejected():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "monitor.json")
    with patch("app.monitor_prefs._prefs_path", return_value=path):
        try:
            mp.set_prefs("bogus")
        except ValueError as e:
            assert "invalid sort" in str(e)
        else:
            raise AssertionError("expected ValueError")


def test_corrupt_file_falls_back():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "monitor.json")
    with open(path, "w") as f:
        f.write("{not json")
    with patch("app.monitor_prefs._prefs_path", return_value=path):
        assert mp.get_prefs() == {"sort": "cpu", "direction": "desc"}


def test_invalid_values_in_file_ignored():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "monitor.json")
    with open(path, "w") as f:
        json.dump({"sort": "bogus", "direction": "sideways"}, f)
    with patch("app.monitor_prefs._prefs_path", return_value=path):
        assert mp.get_prefs() == {"sort": "cpu", "direction": "desc"}
