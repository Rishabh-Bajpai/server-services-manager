"""Tests for the disk-usage analyzer (Phase 25).

These exercise ``app.disk_manager`` directly. They patch
``home_dir()`` so each test gets its own sandbox under
``tempfile.mkdtemp()`` and we never touch the user's real $HOME.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import disk_manager as dm


class TestHumanize(unittest.TestCase):
    def test_zero(self):
        assert dm.humanize(0) == "0 B"

    def test_bytes(self):
        assert dm.humanize(512) == "512 B"

    def test_kb(self):
        assert dm.humanize(1024) == "1.0 KB"

    def test_mb(self):
        assert dm.humanize(1024 * 1024) == "1.0 MB"

    def test_gb(self):
        assert dm.humanize(1024 ** 3 * 5) == "5.0 GB"

    def test_tb(self):
        assert dm.humanize(1024 ** 4) == "1.0 TB"

    def test_negative_or_none(self):
        assert "B" in dm.humanize(-1)
        assert "B" in dm.humanize(None)


class TestResolveUnderHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._patcher = patch.object(dm, "home_dir", return_value=self.tmp)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dot_resolves_to_home(self):
        assert dm.resolve_under_home(".") == self.tmp

    def test_empty_resolves_to_home(self):
        assert dm.resolve_under_home("") == self.tmp

    def test_subdir_resolves_inside(self):
        sub = os.path.join(self.tmp, "sub")
        os.makedirs(sub)
        assert dm.resolve_under_home("sub") == sub

    def test_absolute_path_rejected(self):
        with self.assertRaises(dm.DiskError) as ctx:
            dm.resolve_under_home("/etc/passwd")
        assert ctx.exception.code == "outside_home"

    def test_traversal_rejected(self):
        with self.assertRaises(dm.DiskError) as ctx:
            dm.resolve_under_home("../etc/passwd")
        assert ctx.exception.code == "outside_home"

    def test_deep_traversal_rejected(self):
        with self.assertRaises(dm.DiskError) as ctx:
            dm.resolve_under_home("../../../../etc/passwd")
        assert ctx.exception.code == "outside_home"


class TestGetUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._patcher = patch.object(dm, "home_dir", return_value=self.tmp)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        # build a small tree:
        #   <tmp>/a.txt          (1000 B)
        #   <tmp>/sub/big.bin    (5000 B)
        #   <tmp>/sub/small.txt  (200 B)
        #   <tmp>/empty/
        with open(os.path.join(self.tmp, "a.txt"), "w") as f:
            f.write("x" * 1000)
        sub = os.path.join(self.tmp, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "big.bin"), "wb") as f:
            f.write(b"y" * 5000)
        with open(os.path.join(sub, "small.txt"), "w") as f:
            f.write("z" * 200)
        os.makedirs(os.path.join(self.tmp, "empty"))

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_root_includes_files_and_dirs(self):
        out = dm.get_usage(".", depth=1)
        assert out["path"] == "."
        names = sorted(i["name"] for i in out["items"])
        assert names == ["a.txt", "empty", "sub"]
        # The total at depth=1 includes all immediate contents.
        assert out["total"] >= 1000 + 5000 + 200

    def test_children_have_relative_paths(self):
        out = dm.get_usage(".", depth=1)
        for it in out["items"]:
            assert not os.path.isabs(it["path"]), f"path should be relative: {it['path']}"

    def test_items_sorted_by_size_desc(self):
        out = dm.get_usage(".", depth=1)
        sizes = [i["size"] for i in out["items"]]
        assert sizes == sorted(sizes, reverse=True)

    def test_files_marked_correctly(self):
        out = dm.get_usage(".", depth=1)
        by_name = {i["name"]: i for i in out["items"]}
        assert by_name["a.txt"]["is_dir"] is False
        assert by_name["sub"]["is_dir"] is True
        assert by_name["empty"]["is_dir"] is True

    def test_nested_path(self):
        out = dm.get_usage("sub", depth=1)
        assert out["path"] == "sub"
        names = sorted(i["name"] for i in out["items"])
        assert names == ["big.bin", "small.txt"]
        # big.bin is 5000, small.txt is 200 — descending.
        assert out["items"][0]["size"] >= out["items"][-1]["size"]

    def test_nonexistent_path(self):
        with self.assertRaises(dm.DiskError) as ctx:
            dm.get_usage("does-not-exist", depth=1)
        assert ctx.exception.code == "not_found"

    def test_file_path_rejected(self):
        # If `path` points at a file, get_usage should raise
        # not_a_directory (not crash).
        with self.assertRaises(dm.DiskError) as ctx:
            dm.get_usage("a.txt", depth=1)
        assert ctx.exception.code == "not_a_directory"

    def test_depth_clamped_to_max(self):
        out = dm.get_usage(".", depth=99)
        assert out["depth"] == dm._MAX_DEPTH

    def test_depth_floor_at_one(self):
        out = dm.get_usage(".", depth=0)
        assert out["depth"] == 1

    def test_invalid_depth_defaults_to_one(self):
        out = dm.get_usage(".", depth="not-a-number")
        assert out["depth"] == 1


class TestLargestItems(unittest.TestCase):
    def test_returns_top_n(self):
        items = [{"name": f"f{i}", "path": f"f{i}", "is_dir": False, "size": 10 - i} for i in range(10)]
        top = dm.largest_items(items, n=3)
        assert [t["name"] for t in top] == ["f0", "f1", "f2"]
        for t in top:
            assert "size_human" in t

    def test_empty_list(self):
        assert dm.largest_items([]) == []

    def test_n_clamped(self):
        items = [{"name": f"f{i}", "size": 10 - i} for i in range(200)]
        top = dm.largest_items(items, n=5000)
        assert len(top) == 100  # _largest_items caps at 100

    def test_size_human_format(self):
        top = dm.largest_items([{"name": "x", "size": 2048}])
        assert top[0]["size_human"] == "2.0 KB"


class TestBreadcrumb(unittest.TestCase):
    def test_root(self):
        assert dm.breadcrumb(".") == [{"label": "~", "path": "."}]
        assert dm.breadcrumb("") == [{"label": "~", "path": "."}]

    def test_one_level(self):
        out = dm.breadcrumb("Documents")
        assert out == [
            {"label": "~", "path": "."},
            {"label": "Documents", "path": "Documents"},
        ]

    def test_nested(self):
        out = dm.breadcrumb("foo/bar/baz")
        labels = [c["label"] for c in out]
        paths = [c["path"] for c in out]
        assert labels == ["~", "foo", "bar", "baz"]
        assert paths == [".", "foo", "foo/bar", "foo/bar/baz"]


class TestParseDuLines(unittest.TestCase):
    """Direct unit tests for the du-output parser."""

    def test_total_only(self):
        total, items = dm._parse_du_lines("1234\t/tmp/foo\n", "/tmp/foo")
        assert total == 1234
        assert items == []

    def test_files_and_dirs(self):
        stdout = (
            "1500\t/tmp/foo\n"
            "1000\t/tmp/foo/a.txt\n"
            "500\t/tmp/foo/sub\n"
        )
        total, items = dm._parse_du_lines(stdout, "/tmp/foo")
        assert total == 1500
        assert len(items) == 2
        names = [i["name"] for i in items]
        assert names == ["a.txt", "sub"]

    def test_skips_nested(self):
        # When du is asked depth=2, it'll include deeper entries.
        # We should ignore anything whose parent isn't the target.
        stdout = (
            "1700\t/tmp/foo\n"
            "1200\t/tmp/foo/sub\n"
            "1000\t/tmp/foo/sub/inner\n"
            "500\t/tmp/foo/sub/inner/leaf\n"
        )
        total, items = dm._parse_du_lines(stdout, "/tmp/foo")
        # The line for /tmp/foo/sub is the only immediate child.
        assert [i["name"] for i in items] == ["sub"]

    def test_handles_blank_and_junk_lines(self):
        stdout = "\n1234\t/tmp/foo\nnot-a-number\t/tmp/foo\nfoo\n"
        total, items = dm._parse_du_lines(stdout, "/tmp/foo")
        assert total == 1234
        assert items == []


class TestGetUsageErrors(unittest.TestCase):
    """End-to-end error mapping tests, with mocked subprocess."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._patcher = patch.object(dm, "home_dir", return_value=self.tmp)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_du_missing(self):
        with patch("app.disk_manager.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(dm.DiskError) as ctx:
                dm.get_usage(".", depth=1)
            assert ctx.exception.code == "du_missing"

    def test_timeout(self):
        import subprocess as sp
        with patch("app.disk_manager.subprocess.run",
                   side_effect=sp.TimeoutExpired(cmd="du", timeout=15)):
            with self.assertRaises(dm.DiskError) as ctx:
                dm.get_usage(".", depth=1)
            assert ctx.exception.code == "timeout"

    def test_du_failed_with_empty_stdout(self):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "permission denied"
        with patch("app.disk_manager.subprocess.run", return_value=_R()):
            with self.assertRaises(dm.DiskError) as ctx:
                dm.get_usage(".", depth=1)
            assert ctx.exception.code == "du_failed"


if __name__ == "__main__":
    unittest.main()