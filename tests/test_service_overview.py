"""Tests for pinned-unit service overview."""
from unittest.mock import patch

from app import service_overview as so


def _result(stdout, returncode=0):
    from types import SimpleNamespace

    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_invalid_unit_rejected():
    out = so.get_summary("../../etc/passwd")
    assert out["exists"] is False


def test_not_found_unit():
    out_text = "Id=test.service\nLoadState=not-found\n"
    with patch("app.service_overview.subprocess.run", return_value=_result(out_text)):
        out = so.get_summary("test.service")
        assert out["exists"] is False


def test_parses_show_output():
    out_text = (
        "Id=docker.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
        "MainPID=123\nMemoryCurrent=1048576\nMemoryPeak=2097152\nCPUUsageNSec=5000000000\n"
        "ActiveEnterTimestamp=Thu 2026-09-04 00:00:00 UTC\nUnitFileState=enabled\nDescription=Docker\n"
    )
    with patch("app.service_overview.subprocess.run", return_value=_result(out_text)):
        out = so.get_summary("docker.service")
        assert out["exists"] is True
        assert out["main_pid"] == 123
        assert out["memory_bytes"] == 1048576
        assert out["active_state"] == "active"


def test_get_summaries_caps_and_dedupes():
    with patch("app.service_overview.get_summary", side_effect=lambda u: {"name": u, "exists": True}) as m:
        units = ["a.service", "a.service", "b.service"]
        out = so.get_summaries(units)
        assert [o["name"] for o in out] == ["a.service", "b.service"]
        assert m.call_count == 2
