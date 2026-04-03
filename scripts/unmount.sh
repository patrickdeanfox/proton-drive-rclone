#!/usr/bin/env bash
#
# unmount.sh — Unmount Proton Drive FUSE mount
#
set -euo pipefail

CONFIG_DIR="${HOME}/.config/protondrive"
CONFIG_FILE="${CONFIG_DIR}/config.env"

MOUNT_DIR="${HOME}/ProtonDrive"

if [ -f "$CONFIG_FILE" ]; then
    while IFS='=' read -r key val; do
        key=$(echo "$key" | xargs)
        val=$(echo "$val" | xargs | sed 's/^["'\'']//' | sed 's/["'\'']*$//' | sed 's/#.*//' | xargs)
        val="${val/\$HOME/$HOME}"
        case "$key" in
            MOUNT_DIR) MOUNT_DIR="$val" ;;
        esac
    done < <(grep -v '^#' "$CONFIG_FILE" | grep '=')
fi

if ! mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo "Not mounted at $MOUNT_DIR"
    exit 0
fi

echo "Unmounting ${MOUNT_DIR}..."
fusermount -u "$MOUNT_DIR" 2>/dev/null || umount "$MOUNT_DIR"
echo "✓ Unmounted $MOUNT_DIR"
