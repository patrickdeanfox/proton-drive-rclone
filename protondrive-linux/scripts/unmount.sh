#!/usr/bin/env bash
# Unmount Proton Drive cleanly
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

if ! mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo "Not currently mounted at $MOUNT_DIR"
    exit 0
fi

echo "Unmounting $MOUNT_DIR..."

# Try clean unmount first
if fusermount -u "$MOUNT_DIR" 2>/dev/null || fusermount3 -u "$MOUNT_DIR" 2>/dev/null; then
    echo "✓ Unmounted cleanly"
else
    echo "Clean unmount failed, trying lazy unmount..."
    fusermount -uz "$MOUNT_DIR" 2>/dev/null || fusermount3 -uz "$MOUNT_DIR" 2>/dev/null
    echo "✓ Lazy unmount complete"
fi
