#!/usr/bin/env python3
"""Pre-commit guard: scan for accidentally-committed sensitive data.

The patterns here are kept in sync with ``scrub.py`` (the
history-cleanup script) so any string that was once sensitive
stays that way. Run ``make check-secrets`` or wire this up as
a pre-commit hook:

    ln -sf ../../tools/check-secrets.py .git/hooks/pre-commit

Exit code 0 if clean, 1 if leaks were found.
"""
import os
import re
import subprocess
import sys

REPLACEMENTS = [
    "/home/user/<app>",
    "/home/user/Downloads",
    "/path/to/server-files",
    "<repo-dir>",
    "/home/user",
    "conda run -n <env>",
    "conda activate <env>",
    '"name: <app>"',
    "name: <app>",
    "name: <app>",
    "name: <app>",
    '"name: <app>"',
    '"user:testuser"',
    "user:testuser",
    "<app>",
    "<app>",
    "<app>",
    "<env>",
    "<REDACTED-PASSWORD>",
    "<REDACTED-PASSWORD>",
]

pattern = re.compile("|".join(re.escape(s) for s in REPLACEMENTS))


def scan_text(text: str, source: str) -> list:
    matches = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in pattern.finditer(line):
            matches.append((source, i, line.strip()[:120]))
    return matches


def scan_staged():
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--diff-filter=ACMRT", "--name-only"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        sys.exit(f"git diff failed: {e}")
    files = [f for f in out.splitlines() if f]
    matches = []
    for fname in files:
        if not os.path.isfile(fname):
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


def scan_tracked():
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], text=True,
        )
    except subprocess.CalledProcessError as e:
        sys.exit(f"git ls-files failed: {e}")
    files = [f for f in out.splitlines() if f]
    matches = []
    for fname in files:
        if not os.path.isfile(fname):
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
