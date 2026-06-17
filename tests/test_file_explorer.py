"""Tests for app.file_explorer (Phase 28).

Each test patches :func:`app.file_explorer.home_dir` to point
inside ``tempfile.mkdtemp()`` so the real home directory is
never touched. Test data is built with explicit file content
(so size / mode assertions are stable) and then exercised
through the public helpers.
"""
import os
import shutil
import tempfile
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch

from app import file_explorer as fe


def _sandbox_home():
    """Patch home_dir() to a fresh tempdir; return (patcher, tmp)."""
    tmp = tempfile.mkdtemp()
    p = patch.object(fe, "home_dir", return_value=tmp)
    p.start()
    return p, tmp


class _SandboxMixin:
    def setUp(self):
        self._patcher, self.tmp = _sandbox_home()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # Standard test tree:
        #   <tmp>/a.txt          "hi"            100 bytes after write
        #   <tmp>/.hidden        "secret"        (excluded unless hidden=True)
        #   <tmp>/sub/b.txt      "there"
        #   <tmp>/sub/deeper/c.txt "world"
        with open(os.path.join(self.tmp, "a.txt"), "w") as f:
            f.write("hi")
        with open(os.path.join(self.tmp, ".hidden"), "w") as f:
            f.write("secret")
        sub = os.path.join(self.tmp, "sub")
        deeper = os.path.join(sub, "deeper")
        os.makedirs(deeper)
        with open(os.path.join(sub, "b.txt"), "w") as f:
            f.write("there")
        with open(os.path.join(deeper, "c.txt"), "w") as f:
            f.write("world")
        # A binary file too, for preview tests.
        with open(os.path.join(self.tmp, "blob.bin"), "wb") as f:
            f.write(b"\x00\x01\x02\xff\xfe")


class TestResolve(_SandboxMixin, unittest.TestCase):
    def test_dot_is_home(self):
        assert fe._resolve(".") == self.tmp

    def test_subdir(self):
        target = fe._resolve("sub")
        assert target == os.path.join(self.tmp, "sub")

    def test_absolute_rejected(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe._resolve("/etc/passwd")
        assert ctx.exception.code == "outside_home"

    def test_traversal_rejected(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe._resolve("../../etc")
        assert ctx.exception.code == "outside_home"


class TestMimeGuess(unittest.TestCase):
    def test_text(self):
        assert fe.mime_guess("foo.txt").startswith("text/plain")
    def test_html(self):
        assert fe.mime_guess("foo.html").startswith("text/html")
    def test_python(self):
        assert fe.mime_guess("foo.py").startswith("text/")
    def test_unknown(self):
        assert fe.mime_guess("foo.unknownext") == "application/octet-stream"
    def test_empty(self):
        assert fe.mime_guess("") == "application/octet-stream"


class TestClassify(_SandboxMixin, unittest.TestCase):
    """The preview pane was returning nothing for .log/.conf/.bashrc
    because mimetypes returns (None, None) for those extensions
    and we used to fall through to "binary". Verify the new
    heuristics classify them as text."""

    def test_log_file(self):
        assert fe._classify(os.path.join(self.tmp, "server.log")) == "text"
    def test_yaml(self):
        path = os.path.join(self.tmp, "config.yaml")
        with open(path, "w") as f:
            f.write("foo: bar\n")
        assert fe._classify(path) == "text"
    def test_yml(self):
        path = os.path.join(self.tmp, "config.yml")
        with open(path, "w") as f:
            f.write("foo: bar\n")
        assert fe._classify(path) == "text"
    def test_json(self):
        path = os.path.join(self.tmp, "data.json")
        with open(path, "w") as f:
            f.write('{"k": 1}\n')
        assert fe._classify(path) == "text"
    def test_md(self):
        path = os.path.join(self.tmp, "README.md")
        with open(path, "w") as f:
            f.write("# hi\n")
        assert fe._classify(path) == "text"
    def test_extensionless_text(self):
        path = os.path.join(self.tmp, "bashrc")
        with open(path, "w") as f:
            f.write("export PATH=$PATH:/opt/bin\n")
        assert fe._classify(path) == "text"
    def test_conf(self):
        path = os.path.join(self.tmp, "app.conf")
        with open(path, "w") as f:
            f.write("port = 8881\n")
        assert fe._classify(path) == "text"
    def test_ini(self):
        path = os.path.join(self.tmp, "settings.ini")
        with open(path, "w") as f:
            f.write("[section]\nkey = value\n")
        assert fe._classify(path) == "text"

    def test_image_still_image(self):
        path = os.path.join(self.tmp, "pic.png")
        # Bytes don't need to be a real PNG; the classification
        # is by extension, not content.
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"fake" * 100)
        assert fe._classify(path) == "image"

    def test_audio_still_audio(self):
        path = os.path.join(self.tmp, "song.mp3")
        with open(path, "wb") as f:
            f.write(b"ID3" + b"x" * 100)
        assert fe._classify(path) == "audio"

    def test_video_still_video(self):
        path = os.path.join(self.tmp, "clip.mp4")
        with open(path, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
        assert fe._classify(path) == "video"

    def test_pdf_still_pdf(self):
        path = os.path.join(self.tmp, "doc.pdf")
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4\n" + b"x" * 100)
        assert fe._classify(path) == "pdf"

    def test_unknown_ext_with_null_byte_is_binary(self):
        # A file with no recognized extension that contains a
        # null byte is treated as binary.
        path = os.path.join(self.tmp, "blob.dat")
        with open(path, "wb") as f:
            f.write(b"abc\x00def")
        assert fe._classify(path) == "binary"

    def test_unknown_ext_no_nulls_is_text(self):
        # No null bytes in the first 8 KB → text.
        path = os.path.join(self.tmp, "weirdext.qwerty")
        with open(path, "w") as f:
            f.write("hello world\n" * 100)
        assert fe._classify(path) == "text"

    def test_empty_file_is_text(self):
        # An empty file has no bytes at all; treat as text so
        # the preview pane at least shows "this file is empty".
        path = os.path.join(self.tmp, "empty.dat")
        open(path, "w").close()
        assert fe._classify(path) == "text"


class TestTree(_SandboxMixin, unittest.TestCase):
    def test_root(self):
        # By default hidden files are excluded, so we see 3 entries.
        r = fe.tree(".", depth=0)
        names = sorted(e["name"] for e in r["entries"])
        assert names == ["a.txt", "blob.bin", "sub"]

    def test_depth_clamped(self):
        r = fe.tree(".", depth=99)
        assert r["depth"] == 6

    def test_depth_floor(self):
        r = fe.tree(".", depth=-5)
        assert r["depth"] == 0

    def test_invalid_depth_defaults_to_2(self):
        r = fe.tree(".", depth="garbage")
        assert r["depth"] == 2

    def test_hidden_excluded_by_default(self):
        r = fe.tree(".", depth=0)
        names = [e["name"] for e in r["entries"]]
        assert ".hidden" not in names

    def test_hidden_included(self):
        r = fe.tree(".", depth=0, hidden=True)
        names = [e["name"] for e in r["entries"]]
        assert ".hidden" in names

    def test_recursive(self):
        r = fe.tree(".", depth=3)
        sub = next(e for e in r["entries"] if e["name"] == "sub")
        assert "entries" in sub
        b = next(e for e in sub["entries"] if e["name"] == "b.txt")
        assert b["is_dir"] is False
        deeper = next(e for e in sub["entries"] if e["name"] == "deeper")
        assert "entries" in deeper
        c = next(e for e in deeper["entries"] if e["name"] == "c.txt")
        assert c["size"] == 5

    def test_not_a_directory(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.tree("a.txt")
        assert ctx.exception.code == "not_a_directory"

    def test_truncation(self):
        # 4 entries (incl. hidden); limit=2 → truncated.
        r = fe.tree(".", depth=0, limit=2)
        assert r["truncated"] is True


class TestSearch(_SandboxMixin, unittest.TestCase):
    def test_finds_by_substring(self):
        r = fe.search("b.txt", ".")
        names = sorted(m["name"] for m in r["matches"])
        # "b.txt" appears in sub/b.txt (and itself, depending on
        # which file we created). We also have `blob.bin` which
        # doesn't contain "b.txt" → should NOT match.
        assert "b.txt" in names
        assert "blob.bin" not in names

    def test_case_insensitive(self):
        r = fe.search("A.TXT", ".")
        names = [m["name"] for m in r["matches"]]
        assert "a.txt" in names

    def test_empty_query_rejected(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.search("")
        assert ctx.exception.code == "empty_query"

    def test_query_too_long(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.search("a" * 1000)
        assert ctx.exception.code == "query_too_long"

    def test_no_matches(self):
        r = fe.search("xyzzy_no_match", ".")
        assert r["matches"] == []


class TestPreview(_SandboxMixin, unittest.TestCase):
    def test_text(self):
        r = fe.preview("a.txt")
        assert r["kind"] == "text"
        assert r["content"] == "hi"
        assert r["encoding"] == "utf-8"
        assert r["truncated"] is False

    def test_text_with_unicode_replacement(self):
        # blob.bin has invalid utf-8 → falls back to latin-1.
        r = fe.preview("blob.bin")
        assert r["kind"] in ("text", "binary")
        if r["kind"] == "text":
            assert r["encoding"] in ("utf-8", "latin-1")

    def test_truncation(self):
        r = fe.preview("a.txt", max_bytes=1)
        assert r["truncated"] is True
        assert r["content"] == "h"

    def test_not_found(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.preview("does-not-exist")
        assert ctx.exception.code == "not_found"

    def test_not_a_file(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.preview("sub")
        assert ctx.exception.code == "not_found"

    def test_outside_home(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.preview("/etc/passwd")
        assert ctx.exception.code == "outside_home"


class TestBulkDelete(_SandboxMixin, unittest.TestCase):
    def test_delete_multiple(self):
        out = fe.bulk_delete(["a.txt", "sub/b.txt"])
        assert out["deleted"] == 2
        assert out["failed"] == 0
        assert not os.path.exists(os.path.join(self.tmp, "a.txt"))
        assert not os.path.exists(os.path.join(self.tmp, "sub", "b.txt"))

    def test_delete_directory(self):
        out = fe.bulk_delete(["sub"])
        assert out["deleted"] == 1
        assert not os.path.exists(os.path.join(self.tmp, "sub"))

    def test_missing_path_counted_as_failure(self):
        out = fe.bulk_delete(["a.txt", "does-not-exist"])
        assert out["deleted"] == 1
        assert out["failed"] == 1
        # The one that did exist was removed.
        assert not os.path.exists(os.path.join(self.tmp, "a.txt"))

    def test_empty_rejected(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.bulk_delete([])
        assert ctx.exception.code == "empty_paths"

    def test_too_many_rejected(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.bulk_delete(["a.txt"] * 2000)
        assert ctx.exception.code == "too_many"

    def test_outside_home_skipped(self):
        out = fe.bulk_delete(["a.txt", "/etc/passwd", "../escape"])
        # a.txt is deleted; the bad ones are reported as failures.
        assert out["deleted"] == 1
        assert out["failed"] == 2

    def test_symlink_removed_without_following(self):
        # Path must be RELATIVE — _resolve rejects absolute paths
        # outright (file explorer is chrooted to $HOME).
        target = "link"
        full = os.path.join(self.tmp, target)
        # Link to a file we control; deletion must unlink, not
        # follow the symlink.
        os.symlink(os.path.join(self.tmp, "a.txt"), full)
        out = fe.bulk_delete([target])
        assert out["deleted"] == 1
        assert not os.path.lexists(full)
        # The original file is untouched.
        assert os.path.exists(os.path.join(self.tmp, "a.txt"))

    def test_symlink_to_outside_home_rejected(self):
        # A symlink that points OUTSIDE the sandbox home must
        # be rejected by every entry point. Without this check
        # the explorer would happily open /etc/passwd via a
        # planted symlink.
        outside = os.path.join(tempfile.gettempdir(), "outside_target")
        with open(outside, "w") as f:
            f.write("sensitive")
        self.addCleanup(lambda: os.path.exists(outside) and os.unlink(outside))
        link_path = "evil_link"
        full = os.path.join(self.tmp, link_path)
        os.symlink(outside, full)
        # _resolve refuses it
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe._resolve(link_path)
        assert ctx.exception.code == "outside_home"
        # preview refuses it too
        with self.assertRaises(fe.FileExplorerError):
            fe.preview(link_path)
        # And bulk_delete marks the entry as a failure rather
        # than silently unlinking the outside target.
        out = fe.bulk_delete([link_path])
        assert out["deleted"] == 0
        assert out["failed"] == 1
        # The outside target is still on disk.
        assert os.path.exists(outside)


class TestChmod(_SandboxMixin, unittest.TestCase):
    def test_octal_string(self):
        r = fe.chmod("a.txt", "0644")
        assert r["mode"] == 0o644
        assert r["mode_str"] == "0o644"
        mode = os.stat(os.path.join(self.tmp, "a.txt")).st_mode & 0o777
        assert mode == 0o644

    def test_integer(self):
        r = fe.chmod("a.txt", 0o600)
        assert r["mode"] == 0o600
        mode = os.stat(os.path.join(self.tmp, "a.txt")).st_mode & 0o777
        assert mode == 0o600

    def test_invalid_octal_string(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.chmod("a.txt", "not-octal")
        assert ctx.exception.code == "invalid_mode"

    def test_out_of_range(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.chmod("a.txt", 0o10000)
        assert ctx.exception.code == "invalid_mode"

    def test_not_found(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.chmod("does-not-exist", 0o644)
        assert ctx.exception.code == "not_found"

    def test_outside_home(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.chmod("/etc/passwd", 0o644)
        assert ctx.exception.code == "outside_home"


class TestMakeZip(_SandboxMixin, unittest.TestCase):
    def test_zip_files(self):
        z, name = fe.make_zip(["a.txt", "sub/b.txt"])
        assert name == "selection.zip"
        with zipfile.ZipFile(BytesIO(z)) as zf:
            names = zf.namelist()
        assert "a.txt" in names
        assert "b.txt" in names

    def test_zip_directory(self):
        z, name = fe.make_zip(["sub"])
        with zipfile.ZipFile(BytesIO(z)) as zf:
            names = zf.namelist()
        # The directory contents are preserved under the
        # directory's basename.
        assert any("b.txt" in n for n in names)
        assert any("deeper/c.txt" in n for n in names)

    def test_empty_rejected(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.make_zip([])
        assert ctx.exception.code == "empty_paths"

    def test_too_many_rejected(self):
        with self.assertRaises(fe.FileExplorerError) as ctx:
            fe.make_zip(["a.txt"] * 2000)
        assert ctx.exception.code == "too_many"

    def test_size_cap(self):
        # Patch the cap to a tiny value, then try to zip our
        # standard test files (which together are well under
        # 256 MB but over a few bytes).
        original = fe._ZIP_BYTE_CAP
        fe._ZIP_BYTE_CAP = 4
        try:
            with self.assertRaises(fe.FileExplorerError) as ctx:
                fe.make_zip(["a.txt", "sub"])
            assert ctx.exception.code == "too_large"
        finally:
            fe._ZIP_BYTE_CAP = original


if __name__ == "__main__":
    unittest.main()