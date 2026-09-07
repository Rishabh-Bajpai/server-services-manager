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

    def test_sibling_prefix_bypass_blocked(self):
        """Sibling dirs like /home/user2 should not be reachable from /home/user (previous bug used startswith without sep)."""
        base = tempfile.mkdtemp()
        sibling = base + "2"
        os.makedirs(sibling, exist_ok=True)
        # Simulate the old buggy check: startswith without sep would allow sibling
        target = os.path.realpath(os.path.join(base, "../" + os.path.basename(sibling)))
        # old check would be True (bug), new check must be False
        old_allowed = target.startswith(os.path.realpath(base))
        new_allowed = target == os.path.realpath(base) or target.startswith(os.path.realpath(base) + os.sep)
        assert old_allowed is True  # demonstrates the bug existed
        assert new_allowed is False
        assert target == sibling


class TestFileRoutes:
    """Integration against /api/files routes with Flask test client."""

    def test_list_files_sibling_escape_blocked(self, tmp_path, monkeypatch):
        import server
        # Use tmp_path as fake HOME
        fake_home = str(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: fake_home if p == "~" else p)
        # sibling outside is tmp_path + "2"
        sibling = fake_home + "2"
        import os as _os
        _os.makedirs(sibling, exist_ok=True)
        open(_os.path.join(sibling, "evil.txt"), "w").write("evil")
        app = server.app
        app.config["TESTING"] = True
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["logged_in"] = True
            evil = "../" + _os.path.basename(sibling)
            resp = client.get(f"/api/files?path={evil}")
            assert resp.status_code == 403
            resp2 = client.get(f"/api/files/download?path={evil}/evil.txt")
            assert resp2.status_code == 403

    def test_list_files_normal_ok(self, tmp_path, monkeypatch):
        import server
        fake_home = str(tmp_path)
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "hello.txt").write_text("hi")
        monkeypatch.setattr("os.path.expanduser", lambda p: fake_home if p == "~" else p)
        app = server.app
        app.config["TESTING"] = True
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["logged_in"] = True
            resp = client.get("/api/files?path=subdir")
            assert resp.status_code == 200
            data = resp.get_json()
            assert any(f["name"] == "hello.txt" for f in data["files"])
