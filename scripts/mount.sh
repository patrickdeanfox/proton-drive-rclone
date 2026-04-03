#!/usr/bin/env bash
#
# mount.sh — Mount Proton Drive via rclone FUSE mount
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"

# Source config if available
CONFIG_DIR="${HOME}/.config/protondrive"
CONFIG_FILE="${CONFIG_DIR}/config.env"

RCLONE_REMOTE="protondrive"
MOUNT_DIR="${HOME}/ProtonDrive"

if [ -f "$CONFIG_FILE" ]; then
    while IFS='=' read -r key val; do
        key=$(echo "$key" | xargs)
        val=$(echo "$val" | xargs | sed 's/^["'\'']//' | sed 's/["'\'']*$//' | sed 's/#.*//' | xargs)
        val="${val/\$HOME/$HOME}"
        case "$key" in
            RCLONE_REMOTE) RCLONE_REMOTE="$val" ;;
            MOUNT_DIR) MOUNT_DIR="$val" ;;
        esac
    done < <(grep -v '^#' "$CONFIG_FILE" | grep '=')
fi

# Check rclone
if ! command -v rclone &>/dev/null; then
    echo "ERROR: rclone not found. Please install rclone first."
    exit 1
fi

# Check if already mounted
if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo "Already mounted at $MOUNT_DIR"
    exit 0
fi

# Create mount directory
mkdir -p "$MOUNT_DIR"

echo "Mounting ${RCLONE_REMOTE}: at ${MOUNT_DIR}..."

rclone mount "${RCLONE_REMOTE}:" "${MOUNT_DIR}" \
    --vfs-cache-mode writes \
    --vfs-cache-max-age 1h \
    --dir-cache-time 30s \
    --poll-interval 15s \
    --log-level INFO \
    --daemon

# Wait briefly and verify mount
sleep 2
if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo "✓ Proton Drive mounted at $MOUNT_DIR"
else
    echo "ERROR: Mount may have failed. Check rclone logs."
    exit 1
fi
