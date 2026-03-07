#!/bin/bash
# picsafe_git_sync.sh
# ─────────────────────────────────────────────────────────────────────────────
# Commits any changes to tracked PicSafe v2 files and pushes to GitHub.
# Safe to run automatically (nightly) or manually.
#
# Behaviour
#   • Only stages changes to ALREADY-TRACKED files (git add -u)
#     → new scripts won't accidentally end up in the public repo
#   • Skips silently if nothing changed or no remote is configured
#   • Exits non-zero on push failure so the caller can log the error
#
# Usage
#   ./picsafe_git_sync.sh              # auto-detect changes
#   ./picsafe_git_sync.sh --dry-run    # show what would happen, don't push
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PICSAFE_DIR="$HOME/PicSafe"
BRANCH="main"
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# ── Helpers ───────────────────────────────────────────────────────────────────
log()  { echo "[picsafe_git_sync] $*"; }
warn() { echo "[picsafe_git_sync] WARNING: $*" >&2; }
die()  { echo "[picsafe_git_sync] ERROR: $*" >&2; exit 1; }

# ── Pre-flight ────────────────────────────────────────────────────────────────
cd "$PICSAFE_DIR" || die "Cannot cd to $PICSAFE_DIR"

# Must be a git repo
git rev-parse --git-dir > /dev/null 2>&1 || die "Not a git repository: $PICSAFE_DIR"

# Need a remote to push to
REMOTE_URL=$(git remote get-url origin 2>/dev/null || true)
if [[ -z "$REMOTE_URL" ]]; then
    log "No git remote configured — skipping push (run one-time setup first)"
    exit 0
fi

# ── Check for changes to tracked files ───────────────────────────────────────
# git add -u stages modifications/deletions to TRACKED files only
git add -u

STAGED_COUNT=$(git diff --cached --name-only | wc -l | tr -d ' ')

if [[ "$STAGED_COUNT" -eq 0 ]]; then
    log "No changes to tracked files — nothing to commit"
    exit 0
fi

# Build a compact summary of changed files for the commit message
CHANGED_FILES=$(git diff --cached --name-only | tr '\n' ' ' | sed 's/ $//')
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

COMMIT_MSG="chore: auto-sync PicSafe v2 scripts [${TIMESTAMP}]

Changed files (${STAGED_COUNT}): ${CHANGED_FILES}"

# ── Commit ────────────────────────────────────────────────────────────────────
if $DRY_RUN; then
    log "DRY-RUN — would commit ${STAGED_COUNT} file(s):"
    git diff --cached --name-status
    log "DRY-RUN — would push to: $REMOTE_URL ($BRANCH)"
    # Unstage so we leave repo clean after dry-run
    git reset HEAD > /dev/null 2>&1
    exit 0
fi

git commit -m "$COMMIT_MSG" \
    --author "PicSafe Automation <picsafe@$(hostname)>"

log "Committed ${STAGED_COUNT} file(s): $CHANGED_FILES"

# ── Push ──────────────────────────────────────────────────────────────────────
log "Pushing to origin/$BRANCH …"
if git push origin "$BRANCH"; then
    log "Push successful → $REMOTE_URL"
else
    die "Push failed — check SSH key / network and retry manually"
fi
