#!/bin/bash
# git-ssh.sh — portable SSH wrapper for PicSafe git operations
# Locates the deploy key relative to this script, so it works on any machine
# (Cowork VM, Mac, etc.) as long as the PicSafe folder is present.
#
# Usage: set git config core.sshCommand to the ABSOLUTE path of this file, e.g.
#   git config core.sshCommand "/path/to/PicSafe/.ssh_deploy/git-ssh.sh"
#
# On the Cowork VM, run:
#   .ssh_deploy/setup_vm_ssh.sh
# to configure git to use this wrapper automatically.

KEYS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec ssh -i "$KEYS_DIR/id_picsafe" -o StrictHostKeyChecking=no "$@"
