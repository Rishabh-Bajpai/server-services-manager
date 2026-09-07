#!/usr/bin/env python3
"""Scrub sensitive data from text.

Used by ``tools/clean-history.sh`` as a tree filter and as a
message filter. The REPLACEMENTS list is loaded from
``tools/secrets.txt`` (gitignored — the user's actual
home directory, conda env name, and program names live there,
not in this file). See ``tools/secrets.txt.example`` for the
file format.
"""
import os
import re
import subprocess
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_replacements():
    """Load (pattern, replacement) pairs from the user-local
    secrets.txt. Falls back to an empty list (no scrubbing) if
    the file is missing — the cleanup becomes a no-op so the
    history rewrite can still proceed.
    """
    candidates = [
        os.path.join(THIS_DIR, "secrets.txt"),
        os.path.expanduser("~/.config/ssm/secrets.txt"),
        "/etc/ssm/secrets.txt",
    ]
    out = []
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "->" in line:
                    pattern, _, replacement = line.partition("->")
                    out.append((pattern.strip(), replacement.strip()))
                else:
                    # Detect-only: replace with a generic placeholder
                    out.append((line, "<REDACTED>"))
        return out
    return []


REPLACEMENTS = _load_replacements()
if not REPLACEMENTS:
    print("WARNING: no patterns configured; cleanup is a no-op. "
          "Copy tools/secrets.txt.example to tools/secrets.txt and add your "
          "specifics.", file=sys.stderr)

pattern = re.compile("|".join(re.escape(s) for s, _ in REPLACEMENTS))
REPLACEMENT_MAP = dict(REPLACEMENTS)


def fix_text(text: str) -> str:
    def _replace(m):
        key = m.group(0)
        if key == "":
            return ""
        return REPLACEMENT_MAP.get(key, key)
    return pattern.sub(_replace, text)


def _is_gitignored(path: str, root: str) -> bool:
    """Check if ``path`` (relative to ``root``) matches a pattern
    in the repo's ``.gitignore``. Returns False if git is not
    available or the file isn't in a git repo.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--non-matching",
             path],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    # Exit 0 = ignored; exit 1 = not ignored; exit 128 = not a git repo
    return result.returncode == 0


def walk_files(root: str):
    """Yield (path, text) for every *tracked* text file under
    root. Skips ``.git/`` and any path that ``git check-ignore``
    says is ignored (``tools/secrets.txt``, ``.env``,
    ``config.yaml``, etc.) so a stray call doesn't accidentally
    rewrite your local secrets.
    """
    # Try the tracked-only path first (fastest, most correct)
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=root, capture_output=True, text=True, timeout=10,
            check=True,
        )
        tracked = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        for rel in tracked:
            path = os.path.join(root, rel)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            yield path, text
        return
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: walk the filesystem and filter via gitignore
    for dirpath, dirnames, filenames in os.walk(root):
        if "/.git/" in dirpath or dirpath == os.path.join(root, ".git"):
            continue
        for fname in filenames:
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, root)
            if _is_gitignored(rel, root):
                continue
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except (OSError, IOError):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            yield path, text


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "files"
    if mode == "files":
        root = sys.argv[2] if len(sys.argv) > 2 else "."
        for path, text in walk_files(root):
            new_text = fix_text(text)
            if new_text != text:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_text)
    elif mode == "stdin":
        sys.stdout.write(fix_text(sys.stdin.read()))
    else:
        sys.exit(f"unknown mode: {mode}")
