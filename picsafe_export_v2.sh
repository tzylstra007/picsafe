#!/bin/bash
# =============================================================================
# picsafe_export_v2.sh  —  PicSafe Export v2 Shell Wrapper
# =============================================================================
# Activates the PicSafe venv and runs picsafe_export_v2.py.
#
# Usage:
#   ./picsafe_export_v2.sh              # normal export run
#   ./picsafe_export_v2.sh --dry-run    # scan + report, no file writes
#
# Typically called by the nightly scheduled task AFTER picsafe_bridge_v2_appsheet.py
# =============================================================================

set -euo pipefail

# ── CRON-SAFE PATH ────────────────────────────────────────────────────────────
export PATH="/Users/tomz/.local/bin:/opt/homebrew/bin:/opt/local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

PICSAFE_DIR="$HOME/PicSafe"
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

echo "========================================"
echo "  PicSafe Export v2  —  $TIMESTAMP"
echo "========================================"

# ── ACTIVATE VENV ─────────────────────────────────────────────────────────────
cd "$PICSAFE_DIR"

if [ ! -f "venv/bin/activate" ]; then
    echo "❌  venv not found at $PICSAFE_DIR/venv"
    echo "   Run: python3 -m venv venv && venv/bin/pip install osxphotos smartsheet-python-sdk requests"
    exit 1
fi

source venv/bin/activate

# ── RUN EXPORT ────────────────────────────────────────────────────────────────
python picsafe_export_v2.py "$@"
EXIT_CODE=$?

deactivate 2>/dev/null || true

echo ""
echo "========================================"
echo "  Export finished (exit $EXIT_CODE)"
echo "========================================"

exit $EXIT_CODE
