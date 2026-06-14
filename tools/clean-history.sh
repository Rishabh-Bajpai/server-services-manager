#!/bin/bash
# Clean sensitive data from git history. Run from the repo root.
#
# Usage:
#   git tag history-backup-before-cleanup HEAD   # safety tag
#   bash tools/clean-history.sh                  # do the rewrite
#   git push --force-with-lease origin development  # push to remote
#
# By default this rewrites the current branch only (so you can
# clean `development` without touching the public `main`).
# Pass --all to rewrite every branch.
#
# The script uses scrub.py as both a tree filter (rewrites file
# content) and a message filter (rewrites commit messages), then
# removes the historical config.yaml / config_example.yaml /
# start_process_manager.sh files entirely. Garbage-collects
# orphaned blobs at the end.

set -e
cd "$(git rev-parse --show-toplevel)"

if ! git tag -l | grep -q "history-backup-before-cleanup"; then
    echo "ERROR: backup tag not found. Create it first with:"
    echo "  git tag history-backup-before-cleanup HEAD"
    exit 1
fi

REWRITE_TARGET="HEAD"
if [ "${1:-}" = "--all" ]; then
    REWRITE_TARGET="-- --all"
    echo "Rewriting ALL branches..."
else
    echo "Rewriting current branch ($(git rev-parse --abbrev-ref HEAD)) only."
    echo "Pass --all to rewrite every branch."
fi

SCRUB_SCRIPT="/tmp/scrub-$$.py"
cp tools/scrub.py "$SCRUB_SCRIPT"

export FILTER_BRANCH_SQUELCH_WARNING=1

echo "[1/4] Sanitizing blob content in commits..."
git filter-branch --force --tree-filter "python3 $SCRUB_SCRIPT files ." $REWRITE_TARGET

echo "[2/4] Sanitizing commit messages..."
git filter-branch --force --msg-filter "python3 $SCRUB_SCRIPT stdin" $REWRITE_TARGET

echo "[3/4] Removing config.yaml, config_example.yaml, and start_process_manager.sh from history..."
git filter-branch --force --index-filter '
git rm --cached --ignore-unmatch config.yaml config_example.yaml start_process_manager.sh
' $REWRITE_TARGET

echo "[4/4] Expiring reflog and garbage collecting..."
rm -rf .git/refs/original/
git reflog expire --expire=now --all
git gc --prune=now --aggressive

rm -f "$SCRUB_SCRIPT"

echo ""
echo "Done. Verify with:"
echo "  python3 tools/check-secrets.py all"
