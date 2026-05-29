import os
import tempfile


class TestPathTraversalProtection:
    def _simulate_check(self, path, base_dir):
        """Replicates the path traversal check from server.py"""
        target = os.path.realpath(os.path.join(base_dir, path))
        return target.startswith(os.path.realpath(base_dir)), target

    def test_normal_path_allowed(self):
        base = tempfile.mkdtemp()
        allowed, _ = self._simulate_check(".", base)
        assert allowed is True

    def test_subdirectory_allowed(self):
        base = tempfile.mkdtemp()
        subdir = os.path.join(base, "subdir")
        os.mkdir(subdir)
        allowed, _ = self._simulate_check("subdir", base)
        assert allowed is True

    def test_simple_traversal_blocked(self):
        base = tempfile.mkdtemp()
        allowed, _ = self._simulate_check("../../etc", base)
        assert allowed is False

    def test_deep_traversal_blocked(self):
        base = tempfile.mkdtemp()
        allowed, _ = self._simulate_check("../../../etc/passwd", base)
        assert allowed is False

    def test_absolute_path_traversal_blocked(self):
        base = tempfile.mkdtemp()
        allowed, _ = self._simulate_check("/etc/passwd", base)
        assert allowed is False

    def test_dot_path_allowed(self):
        base = tempfile.mkdtemp()
        allowed, _ = self._simulate_check(".", base)
        assert allowed is True

    def test_symlink_inside_base_allowed(self):
        base = tempfile.mkdtemp()
        target = os.path.join(base, "real_dir")
        os.mkdir(target)
        link = os.path.join(base, "link")
        os.symlink(target, link)
        allowed, resolved = self._simulate_check("link", base)
        assert allowed is True
        assert resolved == target

    def test_symlink_outside_base_blocked(self):
        base = tempfile.mkdtemp()
        outside = tempfile.mkdtemp()
        link = os.path.join(base, "evil_link")
        os.symlink(outside, link)
        allowed, _ = self._simulate_check("evil_link", base)
        assert allowed is False
