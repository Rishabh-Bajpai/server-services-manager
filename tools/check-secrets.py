#!/usr/bin/env python3
"""Pre-commit guard: scan for accidentally-committed sensitive data.

Patterns live in a *user-local* file (``tools/secrets.txt`` or
``~/.config/ssm/secrets.txt``) so the actual patterns — which
include the user's home directory, conda env name, and program
names — never get committed to the public repo. See
``tools/secrets.txt.example`` for the format.

Usage:
    python3 tools/check-secrets.py [staged|all]

Exit code 0 if clean, 1 if leaks were found.

Wire as a pre-commit hook:
    ln -sf ../../tools/check-secrets.py .git/hooks/pre-commit
"""
import os
import re
import subprocess
import sys

# We always exclude the pattern-file itself and the tool's own
# source from being scanned — they contain the patterns as
# string literals, which is expected. The user-local
# secrets.txt is also excluded (it's not in git anyway).
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SELF_FILES = {
    os.path.realpath(os.path.join(THIS_DIR, p)) for p in (
        "check-secrets.py",
        "scrub.py",
        "clean-history.sh",
        "secrets.txt",
        "secrets.txt.example",
    )
}


def _load_patterns():
    """Load patterns from the user-local config file.

    Falls back to an empty list (no patterns) if the file
    doesn't exist. The example file is documentation only.
    """
    candidates = [
        os.path.join(THIS_DIR, "secrets.txt"),
        os.path.expanduser("~/.config/ssm/secrets.txt"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        out = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                out.append(line)
        return out
    return []


LEAK_PATTERNS = _load_patterns()
if not LEAK_PATTERNS:
    sys.exit("ERROR: no patterns configured. Copy tools/secrets.txt.example to "
             "tools/secrets.txt and add your specifics.")

_pattern = re.compile("|".join(re.escape(s) for s in LEAK_PATTERNS))


def scan_text(text: str, source: str) -> list:
    matches = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in _pattern.finditer(line):
            matches.append((source, i, line.strip()[:120]))
    return matches


def _scan_files(files):
    matches = []
    for fname in files:
        if not os.path.isfile(fname):
            continue
        if os.path.realpath(fname) in SELF_FILES:
            continue
        try:
            with open(fname, "rb") as f:
                data = f.read()
        except OSError:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        matches.extend(scan_text(text, fname))
    return matches


def scan_staged():
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--diff-filter=ACMRT", "--name-only"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        sys.exit(f"git diff failed: {e}")
    return _scan_files([f for f in out.splitlines() if f])


def scan_tracked():
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], text=True,
        )
    except subprocess.CalledProcessError as e:
        sys.exit(f"git ls-files failed: {e}")
    return _scan_files([f for f in out.splitlines() if f])


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "staged"
    if mode == "staged":
        matches = scan_staged()
    elif mode == "all":
        matches = scan_tracked()
    else:
        sys.exit(f"unknown mode: {mode}")
    if not matches:
        print("OK: no sensitive data detected")
        return 0
    print(f"ERROR: {len(matches)} potential leak(s) found:", file=sys.stderr)
    for source, line, text in matches:
        print(f"  {source}:{line}: {text}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
