#!/bin/bash
# setup_vm_ssh.sh — configure SSH for git push in a Cowork VM session
#
# Run once at the start of any Cowork session that needs git push:
#   bash /path/to/PicSafe/.ssh_deploy/setup_vm_ssh.sh
#
# What it does:
#   1. Copies the deploy private key into the VM's ~/.ssh/ (VM-only, not persistent)
#   2. Writes ~/.ssh/config to route github.com through that key
#   3. Does NOT touch .git/config — safe to run even when publisher git push
#      is in use on your Mac (Mac uses its own ~/.ssh/config, unaffected)
#
# This is safe to re-run. It does NOT commit or push anything.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY_SRC="$SCRIPT_DIR/id_picsafe"

# Sanity check
if [ ! -f "$KEY_SRC" ]; then
  echo "❌  Deploy key not found: $KEY_SRC"
  echo "    Is the PicSafe folder fully mounted?"
  exit 1
fi

# Set up ~/.ssh/
mkdir -p ~/.ssh
chmod 700 ~/.ssh

# Copy key to VM's ~/.ssh/ (VM filesystem, not the mounted PicSafe repo)
cp "$KEY_SRC" ~/.ssh/id_picsafe
chmod 600 ~/.ssh/id_picsafe

# Write SSH config — routes github.com through the deploy key
cat > ~/.ssh/config << 'EOF'
Host github.com
  IdentityFile ~/.ssh/id_picsafe
  StrictHostKeyChecking no
  User git
EOF
chmod 600 ~/.ssh/config

echo "✅  VM SSH configured for git push."
echo "    Key installed at: ~/.ssh/id_picsafe"
echo ""

# Quick connectivity test
echo "🔍  Testing GitHub SSH auth..."
SSH_OUT=$(ssh -T git@github.com 2>&1 || true)
echo "    $SSH_OUT"
if echo "$SSH_OUT" | grep -q "successfully authenticated"; then
  echo "✅  GitHub auth: OK — ready for git push"
else
  echo "⚠️   Auth failed. Make sure the deploy key public key is added to GitHub:"
  echo "    https://github.com/tzylstra007/picsafe/settings/keys"
  echo "    Public key is at: $SCRIPT_DIR/id_picsafe.pub"
fi
