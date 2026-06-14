#!/usr/bin/env python3
"""Scrub sensitive data from text.

Used by ``tools/clean-history.sh`` as a tree filter and as a
message filter. The REPLACEMENTS list should be kept in sync
with ``tools/check-secrets.py`` so anything flagged as a leak
by the pre-commit guard can be cleaned from history by the
same regex.
"""
import os
import re
import sys

REPLACEMENTS = [
    ("/home/rishabh/ComfyUI", "/home/user/<app>"),
    ("/home/rishabh/Downloads", "/home/user/Downloads"),
    ("/home/rishabh/server-files", "/path/to/server-files"),
    ("/home/rishabh/github_projects/server-services-manager", "<repo-dir>"),
    ("/home/rishabh", "/home/user"),
    ("conda run -n comfyui", "conda run -n <env>"),
    ("conda activate process-manager", "conda activate <env>"),
    ('"name: comfyui"', '"name: <app>"'),
    ("name: comfyui", "name: <app>"),
    ("name: ComfyUI", "name: <app>"),
    ("name: localsend", "name: <app>"),
    ('"name: localsend"', '"name: <app>"'),
    ('"user:rishabh"', '"user:testuser"'),
    ("user:rishabh", "user:testuser"),
    ("ComfyUI", "<app>"),
    ("localsend", "<app>"),
    ("comfyui", "<app>"),
    ("process-manager", "<env>"),
    ("AAaa813@123", "<REDACTED-PASSWORD>"),
    ("813@", "<REDACTED-PASSWORD>"),
]

pattern = re.compile("|".join(re.escape(s) for s, _ in REPLACEMENTS))
REPLACEMENT_MAP = dict(REPLACEMENTS)


def fix_text(text: str) -> str:
    return pattern.sub(lambda m: REPLACEMENT_MAP[m.group(0)], text)


def walk_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        if "/.git/" in dirpath or dirpath == os.path.join(root, ".git"):
            continue
        for fname in filenames:
            path = os.path.join(dirpath, fname)
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
