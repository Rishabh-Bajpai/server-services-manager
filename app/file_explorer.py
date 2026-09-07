"""File-explorer backend helpers (Phase 28).

Adds a few operations on top of the basic ``/api/files/*``
routes that already exist in ``server.py``:

  - ``tree()`` — lazy-loadable directory tree (capped depth,
    skips hidden files when ``hidden=False``).
  - ``search()`` — recursive filename search inside a subtree.
  - ``preview()`` — text/binary preview with a size cap; returns
    base64 for binary types so the browser can render
    images / pdfs.
  - ``bulk_delete()`` — delete several paths atomically.
  - ``chmod()`` — change file mode bits.
  - ``make_zip()`` — bundle a list of paths into a single zip
    in memory (cap on total uncompressed size to avoid
    unbounded memory growth).
  - ``mime_guess()`` — simple extension-based MIME detector.
  - ``mkdir()`` / ``rename()`` / ``move()`` / ``copy()`` —
    the create/rename/cut-copy-paste primitives the explorer
    UI needs (``server.py`` never had these routes).
  - Resumable uploads — ``init_upload()`` / ``append_chunk()``
    / ``upload_status()`` / ``complete_upload()`` /
    ``cancel_upload()``. Drive-style protocol: the client
    declares the total size up front, the server preallocates
    a sparse staging file, and each chunk is written at an
    explicit offset by streaming the request body in 1 MiB
    blocks — so a 100 GB upload on a 4 GB box never holds
    more than one block in RAM. Interrupted uploads resume
    from ``received`` (the offset survives server restarts
    via a JSON sidecar), and the file appears under its real
    name only on ``complete_upload()`` via an atomic rename.

All paths are chrooted to ``$HOME`` the same way the existing
file routes do (see ``list_files`` in server.py).
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import stat
import time
import zipfile
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

# Preview cap. Files larger than this return ``truncated=True``
# and only the first N bytes; over 50 MB the browser tab would
# crash trying to render a 2 GB log file as text.
_PREVIEW_BYTE_CAP = 50 * 1024 * 1024  # 50 MB

# Default cap for tree() and search() to keep responses bounded.
_DEFAULT_TREE_LIMIT = 5000
_DEFAULT_SEARCH_LIMIT = 500

# Cap on uncompressed bytes that ``make_zip()`` is willing to
# pull into memory. Larger selections return an error.
_ZIP_BYTE_CAP = 256 * 1024 * 1024  # 256 MB


class FileExplorerError(Exception):
    """Raised by file-explorer helpers for user-visible errors."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def home_dir() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def _resolve(path: str) -> str:
    """Resolve ``path`` against $HOME and verify it stays inside.

    Same pattern as the file routes in server.py. Reject
    absolute paths outright — the explorer must only see
    things under the user's home directory.

    Important: we deliberately use :func:`os.path.normpath`
    rather than :func:`os.path.realpath` so symlinks are NOT
    followed. Otherwise ``bulk_delete`` of a symlink would
    unlink its target, which is both surprising and a
    privilege-escalation footgun (a user could plant a link
    in their home and delete any file the app process can
    see). Path-traversal (../) is still caught by the
    startswith(home) check.

    For symlinks, we ALSO verify that the link target (after
    resolution) stays inside $HOME. That way the explorer
    rejects home-relative symlinks whose target is somewhere
    outside the home tree (e.g. a "link" pointing to
    /etc/passwd) without changing which path we operate on.
    """
    home = home_dir()
    if path in ("", ".", "./"):
        target = home
    elif os.path.isabs(path):
        raise FileExplorerError(
            "outside_home", f"absolute paths are not allowed: {path}",
        )
    else:
        target = os.path.normpath(os.path.join(home, path))
    if target != home and not target.startswith(home + os.sep):
        raise FileExplorerError(
            "outside_home", f"path escapes $HOME: {target}",
        )
    # Canonicalize the nearest existing ancestor. Checking only
    # ``os.path.islink(target)`` misses INTERMEDIATE symlinks:
    # ``~/link -> /etc`` plus ``"link/passwd"`` passes the prefix
    # check above while pointing at /etc/passwd. realpath() of
    # the ancestor follows every intermediate link; nonexistent
    # trailing components cannot be links, so the ancestor check
    # covers mkdir/init-dest cases too.
    probe = target
    while not os.path.lexists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    real = os.path.realpath(probe)
    if real != home and not real.startswith(home + os.sep):
        raise FileExplorerError(
            "outside_home",
            f"symlink target escapes $HOME: {target} -> {real}",
        )
    return target


# ---------------------------------------------------------------------------
# Tree + search
# ---------------------------------------------------------------------------

def _entry_dict(name: str, full: str, base: str) -> Dict[str, Any]:
    """Build the standard entry dict used by tree()/list() responses."""
    try:
        st = os.lstat(full)
    except OSError as e:
        return {
            "name": name,
            "path": os.path.relpath(full, base),
            "error": str(e),
        }
    is_dir = stat.S_ISDIR(st.st_mode)
    is_link = stat.S_ISLNK(st.st_mode)
    return {
        "name": name,
        "path": os.path.relpath(full, base),
        "is_dir": is_dir,
        "is_link": is_link,
        "size": st.st_size if not is_dir else 0,
        "mtime": int(st.st_mtime),
        "mode": stat.S_IMODE(st.st_mode),
    }


def tree(path: str = ".", depth: int = 2, hidden: bool = False,
         limit: int = _DEFAULT_TREE_LIMIT) -> Dict[str, Any]:
    """Return a recursive directory tree up to ``depth`` levels.

    The result is ``{"path": str, "entries": [{...}, ...]}``;
    each entry is either a leaf (file) or a sub-tree with its
    own ``entries`` field. When a directory has more children
    than ``limit``, the response carries ``truncated=True`` so
    the frontend can offer a "show more" button instead of
    pretending it enumerated everything.
    """
    target = _resolve(path)
    if not os.path.isdir(target):
        raise FileExplorerError("not_a_directory", f"not a directory: {target}")
    base = home_dir()
    try:
        depth_i = int(depth)
    except (TypeError, ValueError):
        depth_i = 2
    depth_i = max(0, min(depth_i, 6))
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = _DEFAULT_TREE_LIMIT
    limit_i = max(1, min(limit_i, _DEFAULT_TREE_LIMIT))

    truncated = [False]
    count = [0]

    def build(p: str, remaining: int) -> List[Dict[str, Any]]:
        if count[0] >= limit_i:
            truncated[0] = True
            return []
        out: List[Dict[str, Any]] = []
        try:
            entries = sorted(
                os.scandir(p),
                key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()),
            )
        except OSError as e:
            return [{"error": str(e), "path": p}]
        for entry in entries:
            if count[0] >= limit_i:
                truncated[0] = True
                break
            if not hidden and entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            d = _entry_dict(entry.name, entry.path, base)
            count[0] += 1
            if is_dir and remaining > 0:
                d["entries"] = build(entry.path, remaining - 1)
            out.append(d)
        return out

    return {
        "path": os.path.relpath(target, base),
        "entries": build(target, depth_i),
        "truncated": truncated[0],
        "depth": depth_i,
        "limit": limit_i,
    }


def search(query: str, path: str = ".",
           limit: int = _DEFAULT_SEARCH_LIMIT) -> Dict[str, Any]:
    """Recursive filename search by substring (case-insensitive)."""
    q = (query or "").strip()
    if not q:
        raise FileExplorerError("empty_query", "query is required")
    if len(q) > 256:
        raise FileExplorerError("query_too_long", "query is unreasonably long")
    target = _resolve(path)
    if not os.path.isdir(target):
        raise FileExplorerError("not_a_directory", f"not a directory: {target}")
    base = home_dir()
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = _DEFAULT_SEARCH_LIMIT
    limit_i = max(1, min(limit_i, _DEFAULT_SEARCH_LIMIT))

    q_lower = q.lower()
    matches: List[Dict[str, Any]] = []
    truncated = [False]

    def walk(p: str) -> None:
        if len(matches) >= limit_i:
            truncated[0] = True
            return
        try:
            entries = list(os.scandir(p))
        except OSError:
            return
        for entry in entries:
            if len(matches) >= limit_i:
                truncated[0] = True
                return
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if q_lower in entry.name.lower():
                matches.append(_entry_dict(entry.name, entry.path, base))
            if is_dir:
                walk(entry.path)

    walk(target)
    return {
        "query": q,
        "path": os.path.relpath(target, base),
        "matches": matches,
        "truncated": truncated[0],
        "limit": limit_i,
    }


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

# Extensions we treat as binary that the browser can render
# inline. Anything else falls back to "text" preview (or
# "unknown" if the file is too large to be a useful text dump).
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
_PDF_EXTS = {".pdf"}
_VIDEO_EXTS = {".mp4", ".webm", ".ogg", ".mov"}
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}

# Common text extensions. We keep this list conservative —
# these are the extensions that mimetypes.py either doesn't
# recognize (.log, .conf) or labels as application/* despite
# being plaintext (.yaml, .json). Without this list, the
# preview pane would refuse to show a 200-line .log file.
_TEXT_EXTS = frozenset({
    ".txt", ".log", ".md", ".rst", ".csv", ".tsv",
    ".yaml", ".yml", ".json", ".xml", ".toml", ".ini",
    ".conf", ".cfg", ".env", ".properties",
    ".html", ".htm", ".css", ".scss", ".less",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".py", ".pyi", ".pyx", ".rb", ".go", ".rs",
    ".java", ".kt", ".kts", ".scala", ".groovy",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".sql", ".graphql", ".gql", ".proto",
    ".diff", ".patch", ".tex", ".bib",
    ".dockerfile", ".editorconfig", ".gitignore",
    ".npmrc", ".yarnrc", ".babelrc", ".eslintrc",
})

# Bytes used to detect "is this text or binary?" when the
# extension doesn't tell us. 8 KB is enough to find a null
# byte in any non-text file with negligible cost.
_TEXT_PEEK_BYTES = 8192

_TEXT_MAX = 1 * 1024 * 1024   # 1 MB — anything bigger becomes a download hint


def mime_guess(path: str) -> str:
    """Return the MIME type for ``path`` based on its extension.

    Falls back to ``application/octet-stream``.
    """
    if not path:
        return "application/octet-stream"
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


# Editable MIME allowlist (borrowed from Laranode config/laranode.php).
# Gates the in-browser editor so binaries can't be corrupted by a text save.
# ``preview()`` kind=text is still the primary signal; this list is the
# conservative server-side gate exposed via /api/files/editable-types.
EDITABLE_MIME_TYPES = frozenset({
    "text/plain",
    "text/html",
    "text/css",
    "text/csv",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/x-httpd-php",
    "application/x-sh",
    "application/x-sql",
    "text/x-php",
    "text/x-python",
    "text/x-shellscript",
    "text/x-sql",
    "text/markdown",
    "text/x-typescript",
    "inode/x-empty",
    "application/x-empty",
})


def is_editable_mime(mime: str) -> bool:
    """Return True if ``mime`` is safe to open in the text editor."""
    if not mime:
        return False
    m = mime.split(";")[0].strip().lower()
    if m in EDITABLE_MIME_TYPES:
        return True
    # text/* is editable by default (covers text/plain, text/yaml, etc.).
    return m.startswith("text/")


def is_editable_path(path: str) -> bool:
    """Return True if ``path`` looks editable by extension/MIME."""
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in _TEXT_EXTS:
        return True
    if ext in _IMAGE_EXTS or ext in _PDF_EXTS or ext in _VIDEO_EXTS or ext in _AUDIO_EXTS:
        return False
    return is_editable_mime(mime_guess(path))


def resolve_api_path(path: str) -> str:
    """Public wrapper for route handlers: resolve + chroot-check ``path``."""
    return _resolve(path)


# Cap on bytes accepted by ``save_text()`` — matches the inline
# text preview cap so anything you can read you can write back.
_EDIT_SAVE_MAX = 1 * 1024 * 1024  # 1 MB


def save_text(path: str, content: str) -> Dict[str, Any]:
    """Overwrite a text file from the in-browser editor.

    Gated by the same editable allowlist as the editor UI
    (binaries can't be corrupted by a text save). Writes to a
    pid-unique temp file in the same directory + atomic rename,
    preserving the original mode bits (``os.replace`` would
    otherwise reset them to the umask default).
    """
    target = _resolve(path)
    if not os.path.lexists(target):
        raise FileExplorerError("not_found", f"path not found: {path}")
    if os.path.isdir(target) and not os.path.islink(target):
        raise FileExplorerError("not_a_file", f"not a file: {path}")
    if not is_editable_path(target):
        raise FileExplorerError(
            "not_editable", "this file type is not editable as text",
        )
    if not isinstance(content, str):
        raise FileExplorerError("invalid_content", "content must be a string")
    try:
        data = content.encode("utf-8")
    except (UnicodeEncodeError, ValueError) as e:
        raise FileExplorerError("invalid_content", f"content is not valid text: {e}")
    if len(data) > _EDIT_SAVE_MAX:
        raise FileExplorerError(
            "too_large",
            f"content exceeds {_EDIT_SAVE_MAX // (1024 * 1024)} MB cap; "
            "edit large files over SSH instead",
        )
    try:
        old_mode = stat.S_IMODE(os.lstat(target).st_mode)
    except OSError:
        old_mode = 0o644
    tmp = f"{target}.edit-{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.chmod(tmp, old_mode)
        os.replace(tmp, target)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise FileExplorerError("save_failed", f"could not save: {e}")
    try:
        size = os.path.getsize(target)
    except OSError:
        size = len(data)
    return {"path": os.path.relpath(target, home_dir()), "size": size}


def classify_api_path(path: str) -> str:
    """Public wrapper for route handlers: classify an absolute path.

    Note: peeks up to 8KB for null bytes when the extension is unknown,
    so callers should treat this as a metadata read on a $HOME-chrooted
    path, not a content oracle outside the chroot.
    """
    return _classify(path)


def _classify(path: str) -> str:
    """Classify a file as text, image, pdf, video, audio, or binary.

    The decision tree:

      1. Recognized binary extension (image / pdf / video /
         audio) wins immediately.
      2. Recognized text extension (or mimetypes says
         ``text/...``) → text.
      3. Otherwise, peek at the first :data:`_TEXT_PEEK_BYTES`
         bytes. A null byte anywhere in that sample is taken
         as proof of binary; otherwise we call it text.

    This is the reason ``.log``, ``.conf``, ``.bashrc`` and
    other extensionless or unknown files preview correctly —
    the heuristic catches them while still flagging actual
    binary blobs (images-with-wrong-extension, tarballs,
    compiled objects, …) as binary.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _TEXT_EXTS:
        return "text"
    if mime_guess(path).startswith("text/"):
        return "text"
    # No recognized extension and mimetypes came up empty —
    # peek at the bytes. The previous behaviour of returning
    # "binary" silently is what hid .log files from the
    # preview pane; this is the fix.
    try:
        with open(path, "rb") as f:
            chunk = f.read(_TEXT_PEEK_BYTES)
    except OSError:
        return "binary"
    if b"\x00" in chunk:
        return "binary"
    return "text"


def preview(path: str, max_bytes: Optional[int] = None) -> Dict[str, Any]:
    """Return preview bytes for a file.

    Response shape::

        {
            "path": str,           # relative to $HOME
            "name": str,
            "kind": "text|image|pdf|video|audio|binary",
            "mime": str,
            "size": int,
            "truncated": bool,
            "encoding": "utf-8"|None,
            "content": str|None,   # for text — the text itself
            "data_b64": str|None,  # for image/pdf — base64
        }

    ``max_bytes`` defaults to :data:`_PREVIEW_BYTE_CAP` (50 MB).
    """
    target = _resolve(path)
    if not os.path.isfile(target):
        raise FileExplorerError("not_found", f"file not found: {target}")
    base = home_dir()
    try:
        cap = int(max_bytes) if max_bytes is not None else _PREVIEW_BYTE_CAP
    except (TypeError, ValueError):
        cap = _PREVIEW_BYTE_CAP
    cap = max(1, min(cap, _PREVIEW_BYTE_CAP))
    kind = _classify(target)
    mime = mime_guess(target)
    size = os.path.getsize(target)
    truncated = size > cap
    out: Dict[str, Any] = {
        "path": os.path.relpath(target, base),
        "name": os.path.basename(target),
        "kind": kind,
        "mime": mime,
        "size": size,
        "truncated": truncated,
    }
    if kind == "text":
        # Enforce _TEXT_MAX for inline text to avoid slurping huge logs
        # into memory — files larger than 1 MB get a hint, not full content.
        text_cap = min(cap, _TEXT_MAX)
        if size > _TEXT_MAX:
            out["too_large_for_inline_text"] = True
            # still read at most text_cap for a small peek
            out["truncated"] = True
        with open(target, "rb") as f:
            data = f.read(text_cap if size > _TEXT_MAX else cap)
        out["encoding"] = "utf-8"
        try:
            out["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            # Try latin-1 as a permissive fallback. latin-1
            # maps every byte to a valid character so this
            # never raises — the only "downside" is that any
            # binary blob will be rendered as mojibake. The
            # user can still download the file.
            try:
                out["content"] = data.decode("latin-1")
                out["encoding"] = "latin-1"
            except Exception:
                out["kind"] = "binary"
                out["content"] = None
                out["encoding"] = None
        if size > _TEXT_MAX and not truncated:
            out["too_large_for_inline_text"] = True
    elif kind in ("image", "pdf"):
        import base64
        # Cap image/pdf inline preview to 10 MB to avoid 67 MB base64 strings
        inline_cap = min(cap, 10 * 1024 * 1024)
        if size > inline_cap:
            out["truncated"] = True
        with open(target, "rb") as f:
            data = f.read(inline_cap)
        out["data_b64"] = base64.b64encode(data).decode("ascii")
    else:
        # Video/audio/binary: just hand back metadata; the
        # browser can stream via the existing download endpoint.
        out["data_b64"] = None
    return out


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------

def bulk_delete(paths: List[str]) -> Dict[str, Any]:
    """Delete multiple files / directories. Refuses to remove
    anything outside ``$HOME``. Returns a per-path result list.

    Directories are removed with ``shutil.rmtree``; symlinks are
    ``unlink()``'d, never followed. If any single delete fails
    we keep going — the response tells the caller which ones
    succeeded.
    """
    import shutil
    if not paths:
        raise FileExplorerError("empty_paths", "no paths provided")
    if len(paths) > 1000:
        raise FileExplorerError("too_many", "refusing to delete more than 1000 entries at once")
    results: List[Dict[str, Any]] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            results.append({"path": p, "ok": False, "error": "invalid path"})
            continue
        try:
            target = _resolve(p)
        except FileExplorerError as e:
            results.append({"path": p, "ok": False, "error": e.message, "code": e.code})
            continue
        if not os.path.lexists(target):
            results.append({"path": p, "ok": False, "error": "not found"})
            continue
        try:
            if os.path.islink(target) or not os.path.isdir(target):
                os.unlink(target)
            else:
                shutil.rmtree(target)
            results.append({"path": p, "ok": True})
        except OSError as e:
            results.append({"path": p, "ok": False, "error": str(e)})
    return {
        "results": results,
        "deleted": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
    }


def chmod(path: str, mode: int) -> Dict[str, Any]:
    """Change file mode. Accepts mode as octal string ('0755')
    or integer (493)."""
    target = _resolve(path)
    if not os.path.lexists(target):
        raise FileExplorerError("not_found", f"path not found: {target}")
    if isinstance(mode, str):
        try:
            mode_i = int(mode, 8)
        except ValueError:
            raise FileExplorerError("invalid_mode", f"mode must be octal (got {mode!r})")
    else:
        try:
            mode_i = int(mode)
        except (TypeError, ValueError):
            raise FileExplorerError("invalid_mode", f"mode must be a number (got {mode!r})")
    if not 0 <= mode_i <= 0o7777:
        raise FileExplorerError("invalid_mode", f"mode out of range: {oct(mode_i)}")
    try:
        os.chmod(target, mode_i)
    except OSError as e:
        raise FileExplorerError("chmod_failed", f"chmod failed: {e}")
    return {
        "path": os.path.relpath(target, home_dir()),
        "mode": mode_i,
        "mode_str": oct(mode_i),
    }


def make_zip(paths: List[str]) -> Tuple[bytes, str]:
    """Bundle ``paths`` into a zip in memory.

    Returns ``(bytes, suggested_filename)``. The zip includes
    each path with its basename, so the resulting archive is
    a flat collection regardless of where the sources lived in
    the home tree.

    Refuses if the total uncompressed size exceeds
    :data:`_ZIP_BYTE_CAP` — bundling a 10 GB log directory into
    a zip in memory would OOM the process.
    """
    if not paths:
        raise FileExplorerError("empty_paths", "no paths provided")
    if len(paths) > 1000:
        raise FileExplorerError("too_many", "refusing to zip more than 1000 entries")
    buf = BytesIO()
    used = [0]

    def _check_size(n: int) -> None:
        used[0] += n
        if used[0] > _ZIP_BYTE_CAP:
            raise FileExplorerError(
                "too_large",
                f"selection exceeds {_ZIP_BYTE_CAP // (1024 * 1024)} MB cap; aborting",
            )

    def _is_zippable(path: str) -> bool:
        """Only regular files (or links to them) go in the zip.

        FIFOs/sockets/devices report size 0 (bypassing the cap)
        and would hang ``zf.write()`` forever reading; anything
        else surprising is skipped rather than followed.
        """
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                return True
            return stat.S_ISREG(os.stat(path).st_mode)
        except OSError:
            return False

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            if not isinstance(p, str) or not p.strip():
                continue
            target = _resolve(p)
            if not os.path.lexists(target):
                continue
            arcname = os.path.basename(target.rstrip(os.sep)) or "file"
            if os.path.isdir(target) and not os.path.islink(target):
                # Walk the directory; flat layout in the zip
                # is too lossy if the user has two dirs of the
                # same name, so we preserve relative structure
                # under the dir's basename.
                for root, _dirs, files in os.walk(target):
                    for name in files:
                        full = os.path.join(root, name)
                        if not _is_zippable(full):
                            continue
                        rel = os.path.relpath(full, target)
                        _check_size(os.path.getsize(full))
                        zf.write(full, os.path.join(arcname, rel))
            elif _is_zippable(target):
                _check_size(os.path.getsize(target))
                zf.write(target, arcname)
    return buf.getvalue(), "selection.zip"


# ---------------------------------------------------------------------------
# Create / rename / move / copy
# ---------------------------------------------------------------------------

def _sanitize_name(name: str, what: str = "name") -> str:
    """Validate a single path component (no slashes, no dot-dots)."""
    if not isinstance(name, str):
        raise FileExplorerError("invalid_name", f"{what} must be a string")
    cleaned = name.strip()
    if not cleaned or cleaned in (".", ".."):
        raise FileExplorerError("invalid_name", f"{what} is empty or reserved")
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise FileExplorerError(
            "invalid_name", f"{what} must be a single path component",
        )
    if len(cleaned) > 255:
        raise FileExplorerError("invalid_name", f"{what} is too long (max 255)")
    return cleaned


def mkdir(path: str, name: str) -> Dict[str, Any]:
    """Create a single directory ``name`` inside ``path``."""
    parent = _resolve(path)
    if not os.path.isdir(parent):
        raise FileExplorerError("not_a_directory", f"not a directory: {path}")
    leaf = _sanitize_name(name, "directory name")
    target = os.path.join(parent, leaf)
    if os.path.lexists(target):
        raise FileExplorerError("exists", f"already exists: {leaf}")
    try:
        os.mkdir(target, 0o755)
    except OSError as e:
        raise FileExplorerError("mkdir_failed", f"could not create directory: {e}")
    return {"path": os.path.relpath(target, home_dir()), "name": leaf}


def rename(path: str, new_name: str) -> Dict[str, Any]:
    """Rename a file/directory, staying in the same parent directory."""
    target = _resolve(path)
    if not os.path.lexists(target):
        raise FileExplorerError("not_found", f"path not found: {path}")
    if os.path.realpath(home_dir()) == os.path.realpath(target):
        raise FileExplorerError("invalid_name", "refusing to rename $HOME itself")
    leaf = _sanitize_name(new_name, "new name")
    dest = os.path.join(os.path.dirname(target), leaf)
    if os.path.lexists(dest):
        raise FileExplorerError("exists", f"already exists: {leaf}")
    try:
        os.replace(target, dest)
    except OSError as e:
        raise FileExplorerError("rename_failed", f"could not rename: {e}")
    return {
        "old_path": os.path.relpath(target, home_dir()),
        "path": os.path.relpath(dest, home_dir()),
        "name": leaf,
    }


def _move_one(src: str, dest_dir: str) -> None:
    """Move one resolved path into a resolved directory (streamed)."""
    base = os.path.basename(src.rstrip(os.sep))
    dest = os.path.join(dest_dir, base)
    if os.path.lexists(dest):
        raise FileExplorerError("exists", f"already exists at destination: {base}")
    if os.path.isdir(src) and not os.path.islink(src):
        # Refuse to move a directory into itself (or a child of itself).
        if dest == src or dest.startswith(src + os.sep):
            raise FileExplorerError(
                "invalid_move", "cannot move a directory into itself",
            )
    try:
        os.replace(src, dest)
        return
    except OSError as e:
        import errno
        if e.errno != errno.EXDEV:
            raise FileExplorerError("move_failed", f"could not move: {e}")
    # Cross-device: streamed copy then unlink (constant memory).
    try:
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dest, symlinks=True)
        elif os.path.islink(src):
            linkto = os.readlink(src)
            os.symlink(linkto, dest)
        else:
            shutil.copyfile(src, dest)
            shutil.copystat(src, dest)
    except OSError as e:
        raise FileExplorerError("move_failed", f"could not move: {e}")
    try:
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.rmtree(src)
        else:
            os.unlink(src)
    except OSError as e:
        raise FileExplorerError(
            "move_failed", f"moved but could not remove source: {e}",
        )


def move(paths: List[str], dest: str) -> Dict[str, Any]:
    """Move several paths into directory ``dest`` (cut + paste)."""
    dest_dir = _resolve(dest)
    if not os.path.isdir(dest_dir):
        raise FileExplorerError("not_a_directory", f"not a directory: {dest}")
    if not paths:
        raise FileExplorerError("empty_paths", "no paths provided")
    if len(paths) > 1000:
        raise FileExplorerError("too_many", "refusing to move more than 1000 entries at once")
    results: List[Dict[str, Any]] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            results.append({"path": p, "ok": False, "error": "invalid path"})
            continue
        try:
            src = _resolve(p)
        except FileExplorerError as e:
            results.append({"path": p, "ok": False, "error": e.message, "code": e.code})
            continue
        if not os.path.lexists(src):
            results.append({"path": p, "ok": False, "error": "not found"})
            continue
        try:
            _move_one(src, dest_dir)
            results.append({"path": p, "ok": True})
        except FileExplorerError as e:
            results.append({"path": p, "ok": False, "error": e.message, "code": e.code})
    return {
        "results": results,
        "moved": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
    }


def copy(paths: List[str], dest: str) -> Dict[str, Any]:
    """Copy several paths into directory ``dest`` (copy + paste).

    Files stream through ``shutil.copyfile`` (constant memory,
    safe for multi-GB files); directories use ``copytree``.
    """
    dest_dir = _resolve(dest)
    if not os.path.isdir(dest_dir):
        raise FileExplorerError("not_a_directory", f"not a directory: {dest}")
    if not paths:
        raise FileExplorerError("empty_paths", "no paths provided")
    if len(paths) > 1000:
        raise FileExplorerError("too_many", "refusing to copy more than 1000 entries at once")
    results: List[Dict[str, Any]] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            results.append({"path": p, "ok": False, "error": "invalid path"})
            continue
        try:
            src = _resolve(p)
        except FileExplorerError as e:
            results.append({"path": p, "ok": False, "error": e.message, "code": e.code})
            continue
        if not os.path.lexists(src):
            results.append({"path": p, "ok": False, "error": "not found"})
            continue
        base = os.path.basename(src.rstrip(os.sep))
        target = os.path.join(dest_dir, base)
        if os.path.lexists(target):
            results.append({"path": p, "ok": False, "error": "already exists at destination"})
            continue
        if os.path.isdir(src) and not os.path.islink(src):
            if target == src or target.startswith(src + os.sep):
                results.append({"path": p, "ok": False, "error": "cannot copy a directory into itself"})
                continue
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, target, symlinks=True)
            elif os.path.islink(src):
                os.symlink(os.readlink(src), target)
            else:
                shutil.copyfile(src, target)
                shutil.copystat(src, target)
            results.append({"path": p, "ok": True})
        except OSError as e:
            results.append({"path": p, "ok": False, "error": str(e)})
    return {
        "results": results,
        "copied": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
    }


# ---------------------------------------------------------------------------
# Resumable chunked uploads (Drive-style)
# ---------------------------------------------------------------------------

# Upper bound on a single resumable upload. 256 GiB comfortably
# covers the "100 GB file on a 4 GB box" case while still bounding
# abuse; free disk space is checked on top of this at init time.
_RESUMABLE_MAX_BYTES = 256 * 1024 * 1024 * 1024  # 256 GiB

# Largest request body a single chunk PUT may carry. The browser
# sends 8 MiB chunks; the cap only guards hand-rolled clients.
_RESUMABLE_PUT_MAX = 256 * 1024 * 1024  # 256 MiB

# Copy block size when streaming a chunk to disk — the steady-state
# memory cost of an upload, regardless of file size.
_RESUMABLE_BLOCK = 1024 * 1024  # 1 MiB

# Sessions idle longer than this are swept (on init + startup).
_RESUMABLE_STALE_SECS = 48 * 3600


def uploads_dir() -> str:
    """Session directory for resumable uploads (created on demand)."""
    d = os.path.join(
        os.path.realpath(os.path.expanduser("~/.server-services-manager")),
        "uploads",
    )
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _session_paths(session_id: str, sessions: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(staging_path, sidecar_path)`` for a session id.

    The id is validated as strict token format so it can never
    escape the sessions directory (no ``/``, no dots).
    """
    if not isinstance(session_id, str) or not session_id:
        raise FileExplorerError("unknown_upload", "unknown upload session")
    if len(session_id) > 64 or not all(
        c.isalnum() or c in ("-", "_") for c in session_id
    ):
        raise FileExplorerError("unknown_upload", "unknown upload session")
    root = sessions or uploads_dir()
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError:
        pass
    return (
        os.path.join(root, session_id + ".part"),
        os.path.join(root, session_id + ".json"),
    )


def _load_sidecar(session_id: str, sessions: Optional[str] = None) -> Dict[str, Any]:
    _, sidecar = _session_paths(session_id, sessions)
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise FileExplorerError("unknown_upload", "unknown upload session")
    if not isinstance(data, dict) or data.get("id") != session_id:
        raise FileExplorerError("unknown_upload", "unknown upload session")
    return data


def _save_sidecar(data: Dict[str, Any], sessions: Optional[str] = None) -> None:
    _, sidecar = _session_paths(data["id"], sessions)
    # Pid-unique scratch name: two writers (ever, on any session)
    # can never clobber each other's temp file; os.replace() keeps
    # the visible sidecar update atomic.
    tmp = f"{sidecar}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, sidecar)


def sweep_stale_uploads(
    sessions: Optional[str] = None, max_age_secs: int = _RESUMABLE_STALE_SECS,
) -> int:
    """Delete staging files + sidecars idle longer than ``max_age_secs``."""
    root = sessions or uploads_dir()
    now = time.time()
    swept = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        sidecar = os.path.join(root, name)
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                data = json.load(f)
            updated = float(data.get("updated", 0))
        except (OSError, ValueError, TypeError):
            updated = 0
        if now - updated < max_age_secs:
            continue
        sid = name[: -len(".json")]
        try:
            staging, _ = _session_paths(sid, root)
        except FileExplorerError:
            continue
        for p in (staging, sidecar, staging + ".lock"):
            try:
                os.unlink(p)
            except OSError:
                pass
        swept += 1
    # Orphaned staging files (sidecar lost/corrupt, crash between
    # prealloc and sidecar save) would otherwise leak disk forever:
    # anything .part without a live .json older than max_age goes too.
    for name in names:
        if not name.endswith(".part"):
            continue
        staging = os.path.join(root, name)
        sid = name[: -len(".part")]
        try:
            _, sidecar = _session_paths(sid, root)
        except FileExplorerError:
            continue
        if os.path.exists(sidecar):
            continue
        try:
            old = now - os.path.getmtime(staging) > max_age_secs
        except OSError:
            continue
        if old:
            try:
                os.unlink(staging)
                swept += 1
            except OSError:
                pass
    return swept


def init_upload(
    name: str,
    size: int,
    path: str = ".",
    subpath: Optional[str] = None,
    sha256: Optional[str] = None,
    sessions: Optional[str] = None,
) -> Dict[str, Any]:
    """Start a resumable upload session.

    ``path`` is the destination directory (home-relative);
    ``subpath`` preserves folder-upload structure
    (``webkitRelativePath`` minus the filename) and is created
    eagerly with ``mkdir -p`` semantics. Returns
    ``{"id", "name", "size", "received": 0, "path"}``.
    """
    leaf = _sanitize_name(name, "filename")
    try:
        total = int(size)
    except (TypeError, ValueError):
        raise FileExplorerError("invalid_size", "size must be a number")
    if total < 0 or total > _RESUMABLE_MAX_BYTES:
        raise FileExplorerError(
            "too_large",
            f"upload exceeds {_RESUMABLE_MAX_BYTES // (1024 ** 3)} GiB cap",
        )
    if sha256 is not None:
        if not isinstance(sha256, str) or not all(
            c in "0123456789abcdefABCDEF" for c in sha256
        ) or len(sha256) != 64:
            raise FileExplorerError("invalid_hash", "sha256 must be 64 hex chars")
    dest_dir = _resolve(path)
    if subpath:
        # Folder uploads: each level must be a plain relative
        # component (no ``..``, no absolute, no empty).
        parts = str(subpath).replace("\\", "/").split("/")
        clean: List[str] = []
        for part in parts:
            if part in ("", "."):
                continue
            if part == ".." or len(part) > 255:
                raise FileExplorerError("outside_home", f"bad subpath: {subpath}")
            clean.append(part)
        for part in clean:
            dest_dir = os.path.join(dest_dir, part)
        if dest_dir != home_dir() and not dest_dir.startswith(home_dir() + os.sep):
            raise FileExplorerError("outside_home", f"subpath escapes $HOME: {subpath}")
        # A pre-existing intermediate symlink (``docs -> /etc``) would
        # redirect makedirs outside $HOME, so verify every level BEFORE
        # creating anything — checking after is too late.
        probe = _resolve(path)
        for part in clean:
            probe = os.path.join(probe, part)
            if os.path.lexists(probe):
                real = os.path.realpath(probe)
                if real != home_dir() and not real.startswith(home_dir() + os.sep):
                    raise FileExplorerError(
                        "outside_home",
                        f"subpath escapes $HOME via symlink: {subpath}",
                    )
        try:
            os.makedirs(dest_dir, mode=0o755, exist_ok=True)
        except OSError as e:
            raise FileExplorerError("mkdir_failed", f"could not create folders: {e}")
    if not os.path.isdir(dest_dir):
        raise FileExplorerError("not_a_directory", f"not a directory: {path}")
    dest = os.path.join(dest_dir, leaf)
    if os.path.lexists(dest):
        raise FileExplorerError("exists", f"already exists: {leaf}")
    # Free-space gate — checked against the filesystem holding
    # $HOME so a 100 GB upload fails fast instead of dying at 99%.
    try:
        free = shutil.disk_usage(home_dir()).free
    except OSError:
        free = total
    if total > free:
        raise FileExplorerError(
            "not_enough_space",
            f"need {total} bytes but only {free} free",
        )
    sweep_stale_uploads(sessions)
    session_id = secrets.token_urlsafe(24)
    staging, _ = _session_paths(session_id, sessions)
    # Preallocate: fallocate() reserves REAL blocks when the fs
    # supports it (later ENOSPC becomes impossible); sparse
    # truncate() is the instant fallback (no reservation — the
    # free-space gate above is then only advisory, and append_chunk
    # maps ENOSPC to a clean error instead of an unhandled 500).
    try:
        with open(staging, "wb") as f:
            if total:
                try:
                    os.posix_fallocate(f.fileno(), 0, total)
                except (AttributeError, OSError):
                    f.truncate(total)
    except OSError as e:
        raise FileExplorerError("staging_failed", f"could not stage upload: {e}")
    data = {
        "id": session_id,
        "name": leaf,
        "size": total,
        "received": 0,
        "dest": os.path.relpath(dest, home_dir()),
        "sha256": sha256.lower() if sha256 else None,
        "created": time.time(),
        "updated": time.time(),
    }
    _save_sidecar(data, sessions)
    return {
        "id": session_id,
        "name": leaf,
        "size": total,
        "received": 0,
        "path": data["dest"],
    }


def append_chunk(
    session_id: str,
    offset: int,
    # Any byte-stream (Werkzeug's request.stream, BytesIO, raw socket
    # file …) — only .read() is used, so no stricter type is needed.
    stream: Any,
    max_bytes: int = _RESUMABLE_PUT_MAX,
    sessions: Optional[str] = None,
) -> Dict[str, Any]:
    """Write one chunk at ``offset``, streaming in 1 MiB blocks.

    ``offset`` must equal the session's ``received`` counter —
    on mismatch a 409-style ``offset_mismatch`` error carries the
    server's ``received`` so the client can re-sync and resume
    instead of restarting. Returns ``{"received", "size"}``.
    """
    try:
        off = int(offset)
    except (TypeError, ValueError):
        raise FileExplorerError("bad_offset", "offset must be a number")
    try:
        limit = int(max_bytes)
    except (TypeError, ValueError):
        limit = _RESUMABLE_PUT_MAX
    staging, _ = _session_paths(session_id, sessions)
    # Serialize the whole read-modify-write on a per-session lock
    # file: the offset check, the byte write, AND the sidecar save
    # happen under one exclusive lock, so two concurrent chunk PUTs
    # can't interleave (stale offset accept) or clobber the shared
    # ``.tmp`` sidecar scratch file.
    lock_path = staging + ".lock"
    try:
        lock_fh = open(lock_path, "w")
    except OSError as e:
        raise FileExplorerError("staging_failed", f"lost staging file: {e}")
    with lock_fh:
        try:
            import fcntl
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass  # Windows / odd fs: best effort only
        data = _load_sidecar(session_id, sessions)
        received = int(data.get("received", 0))
        total = int(data.get("size", 0))
        if off != received:
            err = FileExplorerError(
                "offset_mismatch",
                f"server is at {received}, client sent {off}",
            )
            err.received = received  # type: ignore[attr-defined]
            raise err
        if off >= total and total > 0:
            return {"id": session_id, "received": received, "size": total}
        remaining = total - received
        budget = max(0, min(limit, remaining, _RESUMABLE_PUT_MAX))
        written = 0
        try:
            fh = open(staging, "r+b")
        except OSError as e:
            raise FileExplorerError("staging_failed", f"lost staging file: {e}")
        with fh:
            fh.seek(off)
            try:
                while written < budget:
                    block = stream.read(min(_RESUMABLE_BLOCK, budget - written))
                    if not block:
                        break
                    if isinstance(block, str):
                        block = block.encode("utf-8", errors="replace")
                    fh.write(block)
                    written += len(block)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            except OSError as e:
                import errno
                if e.errno == errno.ENOSPC:
                    raise FileExplorerError(
                        "not_enough_space",
                        "disk filled up mid-upload; free space and resume",
                    )
                raise FileExplorerError("staging_failed", f"chunk write failed: {e}")
        received += written
        data["received"] = received
        data["updated"] = time.time()
        _save_sidecar(data, sessions)
    return {"id": session_id, "received": received, "size": total}


def upload_status(
    session_id: str, sessions: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``{"id", "name", "size", "received", "path"}``."""
    data = _load_sidecar(session_id, sessions)
    return {
        "id": data["id"],
        "name": data.get("name"),
        "size": int(data.get("size", 0)),
        "received": int(data.get("received", 0)),
        "path": data.get("dest"),
    }


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(8 * 1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def complete_upload(
    session_id: str,
    sha256: Optional[str] = None,
    sessions: Optional[str] = None,
) -> Dict[str, Any]:
    """Finish a session: verify size (+ optional hash), atomic rename.

    The destination appears only here — interrupted uploads never
    leave a partial file under the real name.
    """
    data = _load_sidecar(session_id, sessions)
    received = int(data.get("received", 0))
    total = int(data.get("size", 0))
    if received != total:
        raise FileExplorerError(
            "incomplete",
            f"received {received} of {total} bytes; keep uploading",
        )
    staging, sidecar = _session_paths(session_id, sessions)
    want = (sha256 or data.get("sha256") or "").lower() or None
    if want:
        if len(want) != 64 or not all(c in "0123456789abcdef" for c in want):
            raise FileExplorerError("invalid_hash", "sha256 must be 64 hex chars")
        try:
            actual = _hash_file(staging)
        except OSError as e:
            raise FileExplorerError("staging_failed", f"lost staging file: {e}")
        if actual != want:
            for p in (staging, sidecar, staging + ".lock"):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise FileExplorerError(
                "hash_mismatch",
                "sha256 mismatch; staged bytes discarded, re-upload",
            )
    dest = _resolve(data["dest"])
    if os.path.lexists(dest):
        raise FileExplorerError("exists", "destination appeared during upload")
    try:
        os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
        try:
            os.replace(staging, dest)
        except OSError as e:
            import errno
            if e.errno != errno.EXDEV:
                raise
            # Staging dir and destination on different mounts:
            # streamed copy + unlink (constant memory).
            shutil.copyfile(staging, dest)
            shutil.copystat(staging, dest)
            os.unlink(staging)
    except FileExplorerError:
        raise
    except OSError as e:
        raise FileExplorerError("complete_failed", f"could not finalize: {e}")
    for p in (sidecar, staging + ".lock"):
        try:
            os.unlink(p)
        except OSError:
            pass
    try:
        size = os.path.getsize(dest)
    except OSError:
        size = total
    return {"path": os.path.relpath(dest, home_dir()), "size": size}


def cancel_upload(
    session_id: str, sessions: Optional[str] = None,
) -> Dict[str, Any]:
    """Discard a session's staging bytes + sidecar (idempotent)."""
    try:
        staging, sidecar = _session_paths(session_id, sessions)
    except FileExplorerError:
        return {"cancelled": True}
    for p in (staging, sidecar, staging + ".lock"):
        try:
            os.unlink(p)
        except OSError:
            pass
    return {"cancelled": True}
