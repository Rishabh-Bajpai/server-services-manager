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

# Patterns that, if found, indicate a leak. The values are the
# strings to match (the original, *un-redacted* form). The
# replacement target (the sanitized form) is defined in
# ``scrub.py``; both must stay in sync.
LEAK_PATTERNS = [
    "/home/rishabh/ComfyUI",
    "/home/rishabh/Downloads",
    "/home/rishabh/server-files",
    "/home/rishabh/github_projects/server-services-manager",
    "/home/rishabh",
    "conda run -n comfyui",
    "conda activate process-manager",
    '"name: comfyui"',
    "name: comfyui",
    "name: ComfyUI",
    "name: localsend",
    '"name: localsend"',
    '"user:rishabh"',
    "user:rishabh",
    "ComfyUI",
    "localsend",
    "comfyui",
    "process-manager",
    "AAaa813@123",
    "813@",
]

pattern = re.compile("|".join(re.escape(s) for s in LEAK_PATTERNS))


def scan_text(text: str, source: str) -> list:
    matches = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in pattern.finditer(line):
            matches.append((source, i, line.strip()[:120]))
    return matches


# The pattern-list files themselves contain the patterns as string
# literals — that's expected, not a leak. They also reference the
# generic placeholders (``/home/user/<app>`` etc.) that the cleanup
# script uses as replacements, which is also expected.
SELF_FILES = {os.path.realpath(p) for p in [
    __file__,
    os.path.join(os.path.dirname(__file__), "scrub.py"),
    os.path.join(os.path.dirname(__file__), "clean-history.sh"),
]}


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
