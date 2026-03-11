#!/bin/bash
# picsafe_nightly.sh
# ─────────────────────────────────────────────────────────────────────────────
# Nightly PicSafe v2 pipeline orchestrator.
# Runs on the Mac via launchd (com.picsafe.nightly.plist).
#
# Sequence
#   1. picsafe_bridge_v2_appsheet.py  — Apple Photos → AppSheet sync
#   2. picsafe_gphotos_publisher_v1.py — AppSheet → Google Photos upload
#   3. picsafe_git_sync.sh            — auto-commit and push tracked file changes
#
# Logs go to:  ~/PicSafe/logs/nightly_YYYY-MM-DD.log
# Exit codes:  0 = full success, 1 = at least one stage failed
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PICSAFE_DIR="$HOME/PicSafe"
VENV_PYTHON="$PICSAFE_DIR/venv/bin/python3"
LOG_DIR="$PICSAFE_DIR/logs"
LOG_FILE="$LOG_DIR/nightly_$(date '+%Y-%m-%d').log"

mkdir -p "$LOG_DIR"

# ── Helpers ───────────────────────────────────────────────────────────────────
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE"; }

EXIT_CODE=0

log "════════════════════════════════════════════════════════════"
log "PicSafe Nightly Pipeline — $(date '+%A %B %d, %Y')"
log "════════════════════════════════════════════════════════════"

cd "$PICSAFE_DIR" || { log "ERROR: Cannot cd to $PICSAFE_DIR"; exit 1; }

# ── Stage 1: Bridge ───────────────────────────────────────────────────────────
log ""
log "▶  STAGE 1: Bridge (Apple Photos → AppSheet)"
log "─────────────────────────────────────────────"
if "$VENV_PYTHON" picsafe_bridge_v2_appsheet.py >> "$LOG_FILE" 2>&1; then
    log "✅ Bridge completed successfully."
    BRIDGE_OK=true
else
    log "❌ Bridge FAILED (exit $?). Publisher will be skipped."
    EXIT_CODE=1
    BRIDGE_OK=false
fi

# ── Stage 2: Publisher ────────────────────────────────────────────────────────
log ""
log "▶  STAGE 2: Publisher (AppSheet → Google Photos)"
log "─────────────────────────────────────────────────"
if $BRIDGE_OK; then
    if "$VENV_PYTHON" picsafe_gphotos_publisher_v1.py >> "$LOG_FILE" 2>&1; then
        log "✅ Publisher completed successfully."
    else
        log "❌ Publisher FAILED (exit $?)."
        EXIT_CODE=1
    fi
else
    log "⏭️  Skipped (bridge did not succeed)."
fi

# ── Stage 3: Git sync ─────────────────────────────────────────────────────────
log ""
log "▶  STAGE 3: Git sync (commit & push tracked changes)"
log "──────────────────────────────────────────────────────"
if bash "$PICSAFE_DIR/picsafe_git_sync.sh" >> "$LOG_FILE" 2>&1; then
    log "✅ Git sync completed."
else
    log "⚠️  Git sync returned non-zero (may be no changes — check log)."
    # Not fatal — don't change EXIT_CODE for this stage
fi

# ── Summary ───────────────────────────────────────────────────────────────────
log ""
log "════════════════════════════════════════════════════════════"
if [[ "$EXIT_CODE" -eq 0 ]]; then
    log "🎉 Nightly pipeline COMPLETE — all stages succeeded."
else
    log "⚠️  Nightly pipeline finished with ERRORS — check log for details."
fi
log "Log: $LOG_FILE"
log "════════════════════════════════════════════════════════════"

exit "$EXIT_CODE"
