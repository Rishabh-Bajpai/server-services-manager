"""Tests for app.ssh_manager.

We exercise every code path against an in-process fake
filesystem: ``app.ssh_manager`` is monkey-patched so its
``home_dir``/``ssh_dir``/``authorized_keys_path`` helpers
return paths inside a ``tempfile.mkdtemp()`` sandbox. That
keeps the test run hermetic — the user's real
``~/.ssh/authorized_keys`` is never touched.
"""
import base64
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app import ssh_manager as sm


# A handful of real-format key strings for tests. The base64
# payload doesn't have to be a structurally valid SSH key for
# the manager — we only validate the *envelope* (algo + b64
# + comment). The fingerprint will still be deterministic.
ED25519_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzhm0P207Xz/9M6xFjjlJSQ7d3kZb8ZrjJZmE alice@example.com"
ED25519_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA7rfQ+vD98P+L+h/jKQOcGaqWQ7RX2rKD3+1zF3Ckqo bob@example.com"
RSA_A = "ssh-rsa AAAAB3NzaC1yc2EAAAAFAKEKEYDATA000000000000000000000000000000000000000000000000000000000000000000 carol@example.com"
ECDSA_A = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBAKEDATA0000000000000000000000000000000000000A== dan@example.com"


def _patch_home_to(tmp):
    """Patch ssh_manager so its home helpers point inside ``tmp``."""
    home = os.path.realpath(tmp)
    ssh = os.path.join(home, ".ssh")
    ak = os.path.join(ssh, "authorized_keys")
    patches = [
        patch.object(sm, "home_dir", return_value=home),
        patch.object(sm, "ssh_dir", return_value=ssh),
        patch.object(sm, "authorized_keys_path", return_value=ak),
    ]
    for p in patches:
        p.start()
    return patches, home, ssh, ak


class TestParseKeyLine(unittest.TestCase):
    def test_ed25519_basic(self):
        out = sm.parse_key_line(ED25519_A)
        assert out["algorithm"] == "ssh-ed25519"
        assert out["comment"] == "alice@example.com"
        assert out["options"] == []
        assert out["fingerprint"].startswith("SHA256:")
        assert out["line"] is None

    def test_rsa_basic(self):
        out = sm.parse_key_line(RSA_A)
        assert out["algorithm"] == "ssh-rsa"
        assert out["comment"] == "carol@example.com"

    def test_ecdsa_basic(self):
        out = sm.parse_key_line(ECDSA_A)
        assert out["algorithm"] == "ecdsa-sha2-nistp256"
        assert out["comment"] == "dan@example.com"

    def test_no_comment(self):
        out = sm.parse_key_line("ssh-ed25519 " + "A" * 64)
        assert out["comment"] == ""
        assert out["algorithm"] == "ssh-ed25519"

    def test_options_prefix(self):
        line = 'from="*.example.com",no-pty ssh-rsa ' + "A" * 64 + " user@host"
        out = sm.parse_key_line(line)
        assert out["algorithm"] == "ssh-rsa"
        assert out["options"] == ['from="*.example.com",no-pty']
        assert out["comment"] == "user@host"

    def test_blank_line_rejected(self):
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.parse_key_line("   ")
        assert ctx.exception.code == "blank_or_comment"

    def test_comment_line_rejected(self):
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.parse_key_line("# this is a comment")
        assert ctx.exception.code == "blank_or_comment"

    def test_unknown_algorithm_rejected(self):
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.parse_key_line("not-an-algo AAAA comment")
        assert ctx.exception.code == "unknown_algorithm"

    def test_missing_base64_rejected(self):
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.parse_key_line("ssh-ed25519")
        assert ctx.exception.code == "invalid_format"

    def test_invalid_base64_rejected(self):
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.parse_key_line("ssh-ed25519 !!!notbase64!!! comment")
        assert ctx.exception.code == "invalid_base64"

    def test_empty_base64_rejected(self):
        # "comment" is interpreted as the base64 payload — it
        # decodes to 0 bytes, which compute_fingerprint rejects.
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.parse_key_line("ssh-ed25519 comment")
        assert ctx.exception.code == "invalid_base64"

    def test_fingerprint_deterministic(self):
        a = sm.parse_key_line(ED25519_A)["fingerprint"]
        b = sm.parse_key_line(ED25519_A)["fingerprint"]
        assert a == b

    def test_fingerprint_differs_per_key(self):
        a = sm.parse_key_line(ED25519_A)["fingerprint"]
        b = sm.parse_key_line(ED25519_B)["fingerprint"]
        assert a != b


class TestListKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._patches, self.home, self.ssh, self.ak = _patch_home_to(self.tmp)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_no_ssh_dir(self):
        out = sm.list_keys()
        assert out["ssh_dir_exists"] is False
        assert out["file_exists"] is False
        assert out["keys"] == []
        assert out["writable"] is False

    def test_no_authorized_keys(self):
        os.makedirs(self.ssh, mode=0o700)
        out = sm.list_keys()
        assert out["ssh_dir_exists"] is True
        assert out["file_exists"] is False
        assert out["keys"] == []
        assert out["writable"] is True

    def test_empty_file(self):
        os.makedirs(self.ssh, mode=0o700)
        open(self.ak, "w").close()
        out = sm.list_keys()
        assert out["keys"] == []
        assert out["parse_errors"] == 0

    def test_multiple_keys(self):
        os.makedirs(self.ssh, mode=0o700)
        with open(self.ak, "w") as f:
            f.write(ED25519_A + "\n")
            f.write("# this is a comment\n")
            f.write("\n")
            f.write(RSA_A + "\n")
        out = sm.list_keys()
        assert len(out["keys"]) == 2
        algos = {k["algorithm"] for k in out["keys"]}
        assert algos == {"ssh-ed25519", "ssh-rsa"}
        assert out["parse_errors"] == 0

    def test_malformed_line_counted_as_error(self):
        os.makedirs(self.ssh, mode=0o700)
        with open(self.ak, "w") as f:
            f.write(ED25519_A + "\n")
            f.write("garbage line with no algorithm\n")
            f.write(RSA_A + "\n")
        out = sm.list_keys()
        assert len(out["keys"]) == 2
        assert out["parse_errors"] == 1


class TestAddKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._patches, self.home, self.ssh, self.ak = _patch_home_to(self.tmp)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_add_to_missing_ssh_dir_rejected(self):
        # No ~/.ssh — manager must NOT create it.
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.add_key(ED25519_A)
        assert ctx.exception.code == "ssh_dir_missing"
        assert not os.path.exists(self.ssh)

    def test_add_creates_file(self):
        os.makedirs(self.ssh, mode=0o700)
        out = sm.add_key(ED25519_A)
        assert out["algorithm"] == "ssh-ed25519"
        with open(self.ak) as f:
            content = f.read()
        assert "alice@example.com" in content
        # Permission should be 0600.
        mode = os.stat(self.ak).st_mode & 0o777
        assert mode == 0o600

    def test_add_appends(self):
        os.makedirs(self.ssh, mode=0o700)
        sm.add_key(ED25519_A)
        sm.add_key(RSA_A)
        out = sm.list_keys()
        assert len(out["keys"]) == 2
        algos = {k["algorithm"] for k in out["keys"]}
        assert algos == {"ssh-ed25519", "ssh-rsa"}

    def test_duplicate_rejected(self):
        os.makedirs(self.ssh, mode=0o700)
        sm.add_key(ED25519_A)
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.add_key(ED25519_A)
        assert ctx.exception.code == "duplicate"

    def test_duplicate_with_extra_whitespace(self):
        os.makedirs(self.ssh, mode=0o700)
        sm.add_key(ED25519_A)
        weird = "  " + ED25519_A + "  "
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.add_key(weird)
        assert ctx.exception.code == "duplicate"

    def test_add_to_existing_file_without_trailing_newline(self):
        os.makedirs(self.ssh, mode=0o700)
        # File exists but no trailing newline.
        with open(self.ak, "w") as f:
            f.write(ED25519_A.rstrip("\n"))  # no newline
        sm.add_key(RSA_A)
        out = sm.list_keys()
        assert len(out["keys"]) == 2

    def test_empty_input_rejected(self):
        os.makedirs(self.ssh, mode=0o700)
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.add_key("")
        assert ctx.exception.code == "empty_input"

    def test_invalid_format_rejected(self):
        os.makedirs(self.ssh, mode=0o700)
        with self.assertRaises(sm.SSHKeyError):
            sm.add_key("not-a-key")


class TestRemoveKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._patches, self.home, self.ssh, self.ak = _patch_home_to(self.tmp)
        os.makedirs(self.ssh, mode=0o700)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_remove_by_fingerprint(self):
        sm.add_key(ED25519_A)
        sm.add_key(RSA_A)
        target = sm.parse_key_line(ED25519_A)
        out = sm.remove_key(target["fingerprint"])
        assert out["removed"] == 1
        assert out["remaining"] == 1
        remaining = sm.list_keys()["keys"]
        assert len(remaining) == 1
        assert remaining[0]["algorithm"] == "ssh-rsa"

    def test_remove_by_comment(self):
        sm.add_key(ED25519_A)
        sm.add_key(RSA_A)  # need a second key so lockout doesn't kick in
        out = sm.remove_key("alice@example.com")
        assert out["removed"] == 1
        # Only RSA_A remains.
        remaining = sm.list_keys()["keys"]
        assert len(remaining) == 1
        assert remaining[0]["algorithm"] == "ssh-rsa"

    def test_remove_last_key_blocked_by_default(self):
        sm.add_key(ED25519_A)
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.remove_key("alice@example.com")
        assert ctx.exception.code == "lockout_risk"
        # The key is still there.
        assert len(sm.list_keys()["keys"]) == 1

    def test_remove_last_key_with_confirm(self):
        sm.add_key(ED25519_A)
        out = sm.remove_key("alice@example.com", confirm_last=True)
        assert out["removed"] == 1
        assert out["remaining"] == 0
        assert sm.list_keys()["keys"] == []

    def test_remove_creates_backup(self):
        sm.add_key(ED25519_A)
        sm.add_key(RSA_A)
        sm.remove_key("alice@example.com")
        bak = self.ak + ".bak"
        assert os.path.isfile(bak)
        with open(bak) as f:
            bak_content = f.read()
        assert "alice@example.com" in bak_content  # backup has the removed key

    def test_remove_unknown_identifier(self):
        sm.add_key(ED25519_A)
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.remove_key("does-not-exist")
        assert ctx.exception.code == "not_found"

    def test_remove_when_file_missing(self):
        # Don't add anything — file doesn't exist.
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.remove_key("anything")
        assert ctx.exception.code == "not_found"

    def test_remove_empty_identifier(self):
        with self.assertRaises(sm.SSHKeyError) as ctx:
            sm.remove_key("")
        assert ctx.exception.code == "empty_identifier"

    def test_remove_with_options_preserves_other_lines(self):
        # Line with options + plain key.
        opts = 'from="*.example.com" ' + ED25519_A
        sm.add_key(opts)
        sm.add_key(RSA_A)
        # Remove by RSA comment.
        out = sm.remove_key("carol@example.com")
        assert out["remaining"] == 1
        # The remaining key still has its options intact.
        remaining = sm.list_keys()["keys"][0]
        assert remaining["options"] == ['from="*.example.com"']
        assert remaining["comment"] == "alice@example.com"

    def test_remove_multiple_matches(self):
        # Two keys with identical comments (unusual but legal).
        # Need a third key so the lockout check doesn't fire
        # after both matches are removed.
        sm.add_key(ED25519_A.replace("alice@example.com", "shared"))
        sm.add_key(RSA_A.replace("carol@example.com", "shared"))
        sm.add_key(ECDSA_A)  # stays behind — keeps "remaining > 0"
        out = sm.remove_key("shared")
        assert out["removed"] == 2
        assert out["remaining"] == 1
        remaining = sm.list_keys()["keys"]
        assert len(remaining) == 1
        assert remaining[0]["algorithm"] == "ecdsa-sha2-nistp256"


class TestComputeFingerprint(unittest.TestCase):
    def test_format(self):
        data = base64.b64encode(b"hello").decode()
        fp = sm.compute_fingerprint("ssh-ed25519", data)
        assert fp.startswith("SHA256:")
        # SHA256 base64 of "hello" is well-known.
        assert "LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ" in fp

    def test_invalid_base64_raises(self):
        with self.assertRaises(sm.SSHKeyError):
            sm.compute_fingerprint("ssh-ed25519", "!!!nope!!!")

    def test_empty_data_raises(self):
        with self.assertRaises(sm.SSHKeyError):
            sm.compute_fingerprint("ssh-ed25519", "")


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_creates_file_with_correct_mode(self):
        path = os.path.join(self.tmp, "authorized_keys")
        sm._atomic_write(path, "hello\n")
        with open(path) as f:
            assert f.read() == "hello\n"
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600

    def test_overwrites_existing(self):
        path = os.path.join(self.tmp, "authorized_keys")
        with open(path, "w") as f:
            f.write("old\n")
        sm._atomic_write(path, "new\n")
        with open(path) as f:
            assert f.read() == "new\n"

    def test_no_leftover_tmp_files_on_success(self):
        path = os.path.join(self.tmp, "authorized_keys")
        sm._atomic_write(path, "hi\n")
        remaining = [n for n in os.listdir(self.tmp) if n.startswith(".authorized_keys.")]
        assert remaining == []

    def test_cleans_up_tmp_on_failure(self):
        path = os.path.join(self.tmp, "authorized_keys")

        class _Boom(Exception):
            pass

        real_open = sm.os.fdopen

        def explode(fd, *a, **kw):
            raise _Boom("disk full")

        try:
            sm.os.fdopen = explode
            with self.assertRaises(_Boom):
                sm._atomic_write(path, "hi\n")
        finally:
            sm.os.fdopen = real_open
        remaining = [n for n in os.listdir(self.tmp) if n.startswith(".authorized_keys.")]
        assert remaining == []


if __name__ == "__main__":
    unittest.main()