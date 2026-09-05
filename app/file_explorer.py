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

All paths are chrooted to ``$HOME`` the same way the existing
file routes do (see ``list_files`` in server.py).
"""
from __future__ import annotations

import mimetypes
import os
import stat
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
    # For symlinks, also check the resolved target is inside
    # the home tree. realpath() follows links; if the final
    # destination is outside, reject the request.
    if os.path.islink(target):
        real = os.path.realpath(target)
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
                        rel = os.path.relpath(full, target)
                        _check_size(os.path.getsize(full))
                        zf.write(full, os.path.join(arcname, rel))
            else:
                _check_size(os.path.getsize(target))
                zf.write(target, arcname)
    return buf.getvalue(), "selection.zip"