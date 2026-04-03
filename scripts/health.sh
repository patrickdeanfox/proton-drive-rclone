#!/usr/bin/env bash
#
# health.sh — Check health of Proton Drive rclone setup
#
set -euo pipefail

CONFIG_DIR="${HOME}/.config/protondrive"
CONFIG_FILE="${CONFIG_DIR}/config.env"

RCLONE_REMOTE="protondrive"
SYNC_DIR="${HOME}/ProtonSync"
MOUNT_DIR="${HOME}/ProtonDrive"

if [ -f "$CONFIG_FILE" ]; then
    while IFS='=' read -r key val; do
        key=$(echo "$key" | xargs)
        val=$(echo "$val" | xargs | sed 's/^["'\'']//' | sed 's/["'\'']*$//' | sed 's/#.*//' | xargs)
        val="${val/\$HOME/$HOME}"
        case "$key" in
            RCLONE_REMOTE) RCLONE_REMOTE="$val" ;;
            SYNC_DIR) SYNC_DIR="$val" ;;
            MOUNT_DIR) MOUNT_DIR="$val" ;;
        esac
    done < <(grep -v '^#' "$CONFIG_FILE" | grep '=')
fi

echo "=== Proton Drive Health Check ==="
echo ""

# Check rclone
if command -v rclone &>/dev/null; then
    echo "✓ rclone installed: $(rclone version --check 2>/dev/null | head -1 || rclone version 2>/dev/null | head -1)"
else
    echo "✗ rclone not found"
fi

# Check remote
if rclone lsd "${RCLONE_REMOTE}:" --max-depth 0 --contimeout 10s --timeout 15s &>/dev/null; then
    echo "✓ Remote '${RCLONE_REMOTE}' is reachable"
else
    echo "✗ Remote '${RCLONE_REMOTE}' is not reachable"
fi

# Check mount
if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo "✓ Proton Drive mounted at $MOUNT_DIR"
else
    echo "○ Not mounted at $MOUNT_DIR"
fi

# Check sync dir
if [ -d "$SYNC_DIR" ]; then
    local_files=$(find "$SYNC_DIR" -type f 2>/dev/null | wc -l)
    echo "✓ Sync directory exists: $SYNC_DIR ($local_files files)"
else
    echo "○ Sync directory not found: $SYNC_DIR"
fi

# Check disk space
echo ""
echo "Disk space:"
df -h "$HOME" 2>/dev/null | tail -1 | awk '{print "  Used: "$3" / "$2" ("$5" used)"}'

echo ""
echo "=== Health check complete ==="
