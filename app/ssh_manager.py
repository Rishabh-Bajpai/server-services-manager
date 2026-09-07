"""SSH authorized_keys manager.

Manages ``~/.ssh/authorized_keys`` with safe read / write /
remove operations. Supports the public-key format from
:rfc:`4253` (algorithm + base64 + optional comment) plus the
common ``options`` prefix (e.g. ``command=...``, ``from=...``)
documented in ``sshd(8)``.

The fingerprint returned is the SHA256 base64 form
(``SHA256:...``) — the same one ``ssh-keygen -lf`` prints. We
compute it in-process from the decoded key bytes so we don't
have to shell out and so we can fingerprint a partially-pasted
key without writing it to disk first.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger("SSHManager")

# Algorithms permitted per RFC 4253 + the OpenSSH extensions
# used in practice. The set is intentionally narrow — anything
# else (custom protocols, weird prefixes) is rejected so a
# pasted line can't sneak in junk that sshd would silently
# accept.
_KNOWN_ALGORITHMS = frozenset({
    "ssh-rsa",
    "ssh-dss",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
})


class SSHKeyError(Exception):
    """Raised by the manager when a request can't be served."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def home_dir() -> str:
    """Return the realpath of the user's home directory."""
    return os.path.realpath(os.path.expanduser("~"))


def ssh_dir() -> str:
    return os.path.join(home_dir(), ".ssh")


def authorized_keys_path() -> str:
    return os.path.join(ssh_dir(), "authorized_keys")


def compute_fingerprint(algorithm: str, base64_data: str) -> str:
    """Compute the SSH SHA256 fingerprint from base64-encoded key data.

    Returns a string like ``"SHA256:abc123..."`` matching
    ``ssh-keygen -lf -E sha256``.
    """
    try:
        raw = base64.b64decode(base64_data, validate=True)
    except Exception as e:  # noqa: BLE001
        raise SSHKeyError("invalid_base64", f"key data is not valid base64: {e}")
    if not raw:
        raise SSHKeyError("invalid_base64", "key data is empty")
    digest = hashlib.sha256(raw).digest()
    b64 = base64.b64encode(digest).decode().rstrip("=")
    return f"SHA256:{b64}"


def parse_key_line(line: str, line_no: Optional[int] = None) -> Dict[str, Any]:
    """Parse a single authorized_keys line.

    Accepts the bare ``algo base64 [comment]`` form and the
    ``[options] algo base64 [comment]`` form. Returns a dict
    with: ``algorithm``, ``fingerprint``, ``comment``, ``options``,
    ``raw``, and ``line``.

    Raises :class:`SSHKeyError` for blank lines, comments, or
    malformed input. Callers should pre-filter blanks/comments
    if they want to skip them silently.
    """
    s = (line or "").strip()
    if not s:
        raise SSHKeyError("blank_or_comment", "blank line")
    if s.startswith("#"):
        raise SSHKeyError("blank_or_comment", "comment line")
    parts = s.split()
    if len(parts) < 2:
        raise SSHKeyError(
            "invalid_format",
            "expected 'algorithm base64 [comment]' (got too few fields)",
        )
    # Find the algorithm token. It's not always the first token
    # because the line may start with options like
    # `command="/bin/echo hi"` or `from="*.example.com"`.
    algo_idx = -1
    for i, tok in enumerate(parts):
        if tok in _KNOWN_ALGORITHMS:
            algo_idx = i
            break
    if algo_idx == -1:
        raise SSHKeyError(
            "unknown_algorithm",
            f"no recognized algorithm in: {s[:80]!r}",
        )
    if algo_idx + 1 >= len(parts):
        raise SSHKeyError(
            "invalid_format",
            f"missing base64 data after algorithm {parts[algo_idx]!r}",
        )
    algo = parts[algo_idx]
    data = parts[algo_idx + 1]
    options = parts[:algo_idx]
    comment = " ".join(parts[algo_idx + 2:]) if algo_idx + 2 < len(parts) else ""
    fingerprint = compute_fingerprint(algo, data)
    return {
        "algorithm": algo,
        "fingerprint": fingerprint,
        "comment": comment,
        "options": options,
        "raw": s,
        "line": line_no,
    }


def _is_protected_line(raw: str) -> bool:
    """Blank or comment line — never an SSH key."""
    s = raw.strip()
    return (not s) or s.startswith("#")


def list_keys() -> Dict[str, Any]:
    """Return all keys + structural metadata.

    The response shape is::

        {
            "ssh_dir_exists": bool,
            "file_exists": bool,
            "writable": bool,
            "keys": [ {algorithm, fingerprint, comment, options, raw, line}, ... ],
            "parse_errors": int,
        }

    We never raise here for a missing file — that's a normal
    state for a fresh user. Parse errors are counted but the
    offending lines are skipped (logged) so the UI can still
    show the valid keys.
    """
    ssh = ssh_dir()
    path = authorized_keys_path()
    writable = False
    if os.path.isfile(path):
        writable = os.access(path, os.W_OK)
    elif os.path.isdir(ssh):
        writable = os.access(ssh, os.W_OK)
    out: Dict[str, Any] = {
        "ssh_dir_exists": os.path.isdir(ssh),
        "file_exists": os.path.isfile(path),
        "writable": writable,
        "keys": [],
        "parse_errors": 0,
    }
    if not os.path.isfile(path):
        return out
    keys: List[Dict[str, Any]] = []
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError as e:
        raise SSHKeyError("read_failed", f"can't read {path}: {e}")
    for i, line in enumerate(lines, 1):
        if _is_protected_line(line):
            continue
        try:
            keys.append(parse_key_line(line, line_no=i))
        except SSHKeyError as e:
            if e.code == "blank_or_comment":
                continue
            out["parse_errors"] += 1
            logger.warning(f"unparseable authorized_keys line {i}: {e.message}")
    out["keys"] = keys
    return out


def _read_lines(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return f.readlines()


def _atomic_write(path: str, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Writes to a tempfile in the same directory, fsyncs, then
    renames over the destination. ``authorized_keys`` is set
    to mode 0600 because sshd will refuse keys that are
    group- or world-writable.
    """
    dirpath = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".authorized_keys.", dir=dirpath)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            # chown/chmod might fail if the file is on a mount
            # we don't own; sshd will still work if the file is
            # 0644 and we don't have group/other write. Warn
            # but don't fail the whole operation.
            logger.debug(f"could not chmod 0600 {path}")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup(path: str) -> Optional[str]:
    """Snapshot ``path`` to a timestamped backup before destructive ops.

    Returns the backup path, or ``None`` if the source didn't
    exist. We keep at most one recent backup at
    ``authorized_keys.bak`` so the file doesn't pile up.
    """
    if not os.path.isfile(path):
        return None
    bak = path + ".bak"
    try:
        shutil.copy2(path, bak)
        os.chmod(bak, 0o600)
    except OSError as e:
        logger.warning(f"backup failed for {path}: {e}")
        return None
    return bak


def _normalize(key_text: str) -> str:
    """Strip whitespace + collapse internal runs of spaces.

    Different SSH clients and copy-paste flows produce slightly
    different layouts (trailing spaces, tabs, CRLF endings);
    we want to compare key contents, not exact whitespace.
    """
    return " ".join((key_text or "").split())


def add_key(key_text: str) -> Dict[str, Any]:
    """Append a key to ``authorized_keys``.

    Refuses duplicate keys (matched by fingerprint) and refuses
    to operate if ``~/.ssh`` doesn't exist (we don't want to
    silently create a directory with permissive permissions).

    Returns the parsed key dict.
    """
    if not key_text or not key_text.strip():
        raise SSHKeyError("empty_input", "key text is empty")
    parsed = parse_key_line(key_text)
    ssh = ssh_dir()
    if not os.path.isdir(ssh):
        raise SSHKeyError(
            "ssh_dir_missing",
            "~/.ssh does not exist; create it first with "
            "'mkdir -p ~/.ssh && chmod 700 ~/.ssh'",
        )
    path = authorized_keys_path()
    lines = _read_lines(path)
    # Check duplicates by fingerprint (and by exact normalized text).
    normalized_new = _normalize(parsed["raw"])
    for line in lines:
        if _is_protected_line(line):
            continue
        try:
            ek = parse_key_line(line)
        except SSHKeyError:
            continue
        if ek["fingerprint"] == parsed["fingerprint"]:
            raise SSHKeyError(
                "duplicate",
                "a key with this fingerprint is already authorized",
            )
        if _normalize(ek["raw"]) == normalized_new:
            raise SSHKeyError(
                "duplicate",
                "this exact key line is already present",
            )
    # Make sure the file ends with a newline before we append.
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    lines.append(parsed["raw"] + "\n")
    _atomic_write(path, "".join(lines))
    return parsed


def remove_key(identifier: str, confirm_last: bool = False) -> Dict[str, Any]:
    """Remove a key from ``authorized_keys``.

    ``identifier`` may be either a fingerprint or the full
    comment of a key. If the removal would leave the file
    empty, refuses with ``lockout_risk`` unless
    ``confirm_last=True`` is passed — that protects the user
    from accidentally locking themselves out of the box.
    """
    if not identifier or not identifier.strip():
        raise SSHKeyError("empty_identifier", "identifier is required")
    path = authorized_keys_path()
    if not os.path.isfile(path):
        raise SSHKeyError("not_found", "authorized_keys does not exist")
    lines = _read_lines(path)
    # Parse every line and decide which ones match.
    parsed: List[Optional[Dict[str, Any]]] = []
    matched_indices: List[int] = []
    matched_keys: List[Dict[str, Any]] = []
    for i, line in enumerate(lines):
        if _is_protected_line(line):
            parsed.append(None)
            continue
        try:
            k = parse_key_line(line, line_no=i + 1)
        except SSHKeyError:
            parsed.append(None)
            continue
        parsed.append(k)
        if k["fingerprint"] == identifier or k["comment"] == identifier:
            matched_indices.append(i)
            matched_keys.append(k)
    if not matched_indices:
        raise SSHKeyError(
            "not_found",
            f"no key matches identifier {identifier!r}",
        )
    remaining = sum(1 for p in parsed if p is not None) - len(matched_indices)
    if remaining == 0 and not confirm_last:
        raise SSHKeyError(
            "lockout_risk",
            "refusing to remove the last key; "
            "pass confirm_last=true to override (you may lock yourself out)",
        )
    _backup(path)
    new_lines = [ln for i, ln in enumerate(lines) if i not in matched_indices]
    _atomic_write(path, "".join(new_lines))
    return {
        "removed": len(matched_indices),
        "removed_keys": matched_keys,
        "remaining": remaining,
        "identifier": identifier,
    }