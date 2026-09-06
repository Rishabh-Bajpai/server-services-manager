"""Tests for sysstat/sar history manager (borrowed from Laranode SarHistory)."""
import os
import tempfile
from unittest.mock import patch

from app import history_manager as hm


class TestIsAvailable:
    def test_missing_sar(self):
        with patch("app.history_manager.shutil.which", return_value=None):
            out = hm.is_available()
            assert out["available"] is False
            assert "sysstat" in out["reason"]

    def test_no_reports(self):
        with patch("app.history_manager.shutil.which", return_value="/usr/bin/sar"):
            with patch("app.history_manager.glob.glob", return_value=[]):
                out = hm.is_available()
                assert out["available"] is False
                assert "sa[" in out["reason"] or "sar files" in out["reason"]


class TestListReports:
    def test_sorts_newest_first(self):
        tmp = tempfile.mkdtemp()
        try:
            p1 = os.path.join(tmp, "sa10")
            p2 = os.path.join(tmp, "sa11")
            open(p1, "w").close()
            open(p2, "w").close()
            # Make sa10 newer.
            os.utime(p1, (2000000000, 2000000000))
            os.utime(p2, (1000000000, 1000000000))
            with patch("app.history_manager._SAR_GLOB", os.path.join(tmp, "sa[0-9][0-9]")):
                out = hm.list_reports()
                assert [r["id"] for r in out] == ["sa10", "sa11"]
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestResolveReport:
    def test_invalid_id_rejected(self):
        with patch("app.history_manager.list_reports", return_value=[{"id": "sa10", "mtime": 1}]):
            try:
                hm._resolve_report("../../etc/passwd")
            except hm.HistoryError as e:
                assert e.code == "invalid_report"
            else:
                raise AssertionError("expected HistoryError")

    def test_no_reports_raises(self):
        with patch("app.history_manager.list_reports", return_value=[]):
            try:
                hm._resolve_report(None)
            except hm.HistoryError as e:
                assert e.code == "no_reports"
            else:
                raise AssertionError("expected HistoryError")


class TestParsers:
    def _mock_reports(self, path="/var/log/sysstat/sa10"):
        return patch("app.history_manager._resolve_report", return_value=path)

    def test_cpu_parsing(self):
        sar_out = (
            "Linux 6.8 (host) 09/04/2026 _x86_64_\n"
            "10:00:01 CPU %user %nice %system %iowait %steal %idle\n"
            "10:00:01 all 5.00 0.00 3.00 0.50 0.00 91.50\n"
            "10:10:01 all 10.00 0.00 5.00 1.00 0.00 84.00\n"
        )
        with self._mock_reports():
            with patch("app.history_manager._run_sar", return_value=sar_out):
                out = hm.get_cpu_history("sa10")
                assert out["count"] == 2
                assert out["metrics"][0]["time"] == "10:00:01"
                assert out["metrics"][0]["total"] == round(100.0 - 91.5, 2)

    def test_memory_parsing(self):
        sar_out = (
            "10:00:01 kbmemfree kbavail kbmemused %memused kbbuffers kbcached\n"
            "10:00:01 1000000 2000000 4000000 50.00 100000 500000\n"
        )
        with self._mock_reports():
            with patch("app.history_manager._run_sar", return_value=sar_out):
                out = hm.get_memory_history("sa10")
                assert out["count"] == 1
                assert out["metrics"][0]["percent"] == 50.0
                assert out["metrics"][0]["used_gb"] > 0

    def test_network_parsing(self):
        sar_out = (
            "10:00:01 IFACE rxpck/s txpck/s rxkB/s txkB/s rxcmp/s txcmp/s rxmcst/s\n"
            "10:00:01 eth0 10.00 5.00 100.00 50.00 0.00 0.00 0.00\n"
            "10:00:01 lo 1.00 1.00 5.00 5.00 0.00 0.00 0.00\n"
        )
        with self._mock_reports():
            with patch("app.history_manager._run_sar", return_value=sar_out):
                out = hm.get_network_history("sa10")
                # Loopback is excluded from the series.
                assert out["count"] == 1
                eth0 = [m for m in out["metrics"] if m["interface"] == "eth0"][0]
                assert eth0["rx_kbs"] == 100.0
                assert eth0["total_kbs"] == 150.0
                assert all(m["interface"] != "lo" for m in out["metrics"])
                assert out["truncated"] is False

    def test_network_parsing_12h_locale(self):
        # 12-hour locale inserts an AM/PM token after the timestamp.
        sar_out = (
            "12:10:00 AM IFACE rxpck/s txpck/s rxkB/s txkB/s rxcmp/s txcmp/s rxmcst/s\n"
            "12:10:00 AM eth0 10.00 5.00 100.00 50.00 0.00 0.00 0.00\n"
            "12:10:00 AM lo 121.00 121.00 1020.84 1020.84 0.00 0.00 0.00\n"
        )
        with self._mock_reports():
            with patch("app.history_manager._run_sar", return_value=sar_out):
                out = hm.get_network_history("sa10")
                assert out["count"] == 1
                assert out["metrics"][0]["interface"] == "eth0"
                assert out["metrics"][0]["rx_kbs"] == 100.0
                assert out["metrics"][0]["tx_kbs"] == 50.0

    def test_network_parsing_column_reorder(self):
        # Header-driven: txkB/s before rxkB/s must still map correctly.
        sar_out = (
            "10:00:01 IFACE txkB/s rxkB/s txpck/s rxpck/s\n"
            "10:00:01 eth0 50.00 100.00 5.00 10.00\n"
        )
        with self._mock_reports():
            with patch("app.history_manager._run_sar", return_value=sar_out):
                out = hm.get_network_history("sa10")
                assert out["metrics"][0]["rx_kbs"] == 100.0
                assert out["metrics"][0]["tx_kbs"] == 50.0

    def test_cpu_parsing_12h_locale(self):
        sar_out = (
            "Linux 6.8 (host) 09/04/2026 _x86_64_\n"
            "12:00:01 AM CPU %user %nice %system %iowait %steal %idle\n"
            "12:00:01 AM all 5.00 0.00 3.00 0.50 0.00 91.50\n"
        )
        with self._mock_reports():
            with patch("app.history_manager._run_sar", return_value=sar_out):
                out = hm.get_cpu_history("sa10")
                assert out["count"] == 1
                assert out["metrics"][0]["time"] == "12:00:01"
                assert out["metrics"][0]["total"] == round(100.0 - 91.5, 2)

    def test_truncated_flag(self):
        sar_out = (
            "10:00:01 CPU %user %nice %system %iowait %steal %idle\n"
            "10:00:01 all 5.00 0.00 3.00 0.50 0.00 91.50\n"
            "10:10:01 all 10.00 0.00 5.00 1.00 0.00 84.00\n"
        )
        with self._mock_reports():
            with patch("app.history_manager._run_sar", return_value=sar_out):
                out = hm.get_cpu_history("sa10", limit=1)
                assert out["count"] == 1
                assert out["truncated"] is True
