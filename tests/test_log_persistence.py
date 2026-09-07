import os

import pytest

from app import log_persistence


@pytest.fixture(autouse=True)
def _isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(log_persistence, "_BASE", str(tmp_path))
    yield


class TestAppendAndRead:
    def test_write_then_read(self):
        log_persistence.append_line("api", "line 1")
        log_persistence.append_line("api", "line 2")
        log_persistence.append_line("api", "line 3")
        assert log_persistence.read_tail("api") == ["line 1", "line 2", "line 3"]

    def test_read_tail_limits(self):
        for i in range(20):
            log_persistence.append_line("api", f"line {i}")
        assert len(log_persistence.read_tail("api", lines=5)) == 5
        # Last 5 are 15..19
        assert log_persistence.read_tail("api", lines=5)[-1] == "line 19"

    def test_unknown_service(self):
        assert log_persistence.read_tail("nonexistent") == []

    def test_safe_name_with_specials(self):
        log_persistence.append_line("a/b\\c name", "hi")
        # Should be saved to a sanitized filename
        files = os.listdir(log_persistence._BASE)
        assert len(files) == 1
        assert log_persistence.read_tail("a/b\\c name") == ["hi"]


class TestRotation:
    def test_rotation_when_over_cap(self, tmp_path):
        # Force a smaller cap by patching
        original_cap = log_persistence._MAX_BYTES
        log_persistence._MAX_BYTES = 256
        try:
            for i in range(50):
                log_persistence.append_line("api", f"this is line {i} with extra padding " * 2)
            # File should still exist and be readable
            tail = log_persistence.read_tail("api", lines=100)
            assert len(tail) > 0
            # After rotation, only the last half is kept
            assert log_persistence._path("api") and os.path.exists(log_persistence._path("api"))
        finally:
            log_persistence._MAX_BYTES = original_cap

    def test_rotation_preserves_last_lines(self, tmp_path):
        original_cap = log_persistence._MAX_BYTES
        log_persistence._MAX_BYTES = 200
        try:
            for i in range(100):
                log_persistence.append_line("api", f"line-{i:03d}")
            tail = log_persistence.read_tail("api", lines=5)
            # Should be the last 5 lines
            assert "line-099" in tail[-1]
        finally:
            log_persistence._MAX_BYTES = original_cap


class TestClear:
    def test_clear_removes_file(self):
        log_persistence.append_line("api", "x")
        assert os.path.exists(log_persistence._path("api"))
        log_persistence.clear("api")
        assert not os.path.exists(log_persistence._path("api"))

    def test_clear_unknown_is_noop(self):
        log_persistence.clear("never-existed")  # should not raise

    def test_read_after_clear_returns_empty(self):
        log_persistence.append_line("api", "x")
        log_persistence.clear("api")
        assert log_persistence.read_tail("api") == []


class TestConcurrency:
    def test_concurrent_writes_dont_corrupt(self):
        import threading
        errors = []

        def writer(n):
            try:
                for i in range(50):
                    log_persistence.append_line("api", f"thread{n}-line{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # 5 threads * 50 lines = 250 lines, no corruption
        lines = log_persistence.read_tail("api", lines=1000)
        assert len(lines) == 250


class TestEdgeCases:
    def test_empty_line(self):
        log_persistence.append_line("api", "")
        log_persistence.append_line("api", "")
        assert log_persistence.read_tail("api") == ["", ""]

    def test_line_with_trailing_newline(self):
        log_persistence.append_line("api", "hello\n")
        # read splits on lines so no extra empty line
        assert log_persistence.read_tail("api") == ["hello"]

    def test_unicode_content(self):
        log_persistence.append_line("api", "héllo wörld 🦀")
        assert log_persistence.read_tail("api") == ["héllo wörld 🦀"]
