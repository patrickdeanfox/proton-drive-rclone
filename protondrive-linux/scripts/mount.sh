#!/usr/bin/env bash
# Mount Proton Drive via rclone FUSE
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

LOG_FILE="$LOG_DIR/mount.log"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"; }

# Check if already mounted
if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo "Already mounted at $MOUNT_DIR"
    exit 0
fi

mkdir -p "$MOUNT_DIR"

log "Mounting $RCLONE_REMOTE: → $MOUNT_DIR"

# Build mount flags
FLAGS=(
    --vfs-cache-mode "$VFS_CACHE_MODE"
    --vfs-cache-max-age "$VFS_CACHE_MAX_AGE"
    --vfs-cache-max-size "${VFS_CACHE_MAX_SIZE:-10G}"
    --vfs-read-ahead "$VFS_READ_AHEAD"
    --dir-cache-time "$DIR_CACHE_TIME"
    --poll-interval "$POLL_INTERVAL"
    --buffer-size "${BUFFER_SIZE:-64M}"
    --log-file "$LOG_FILE"
    --log-level "${LOG_LEVEL:-INFO}"
    --allow-non-empty
)

# Append extra flags if set
if [[ -n "${MOUNT_EXTRA_FLAGS:-}" ]]; then
    IFS=' ' read -ra EXTRA <<< "$MOUNT_EXTRA_FLAGS"
    FLAGS+=("${EXTRA[@]}")
fi

# Foreground or daemon mode
if [[ "${1:-}" == "--foreground" || "${1:-}" == "-f" ]]; then
    log "Starting in foreground mode"
    exec rclone mount "$RCLONE_REMOTE:" "$MOUNT_DIR" "${FLAGS[@]}"
else
    FLAGS+=(--daemon)
    rclone mount "$RCLONE_REMOTE:" "$MOUNT_DIR" "${FLAGS[@]}"

    # Wait for mount to come up
    for i in $(seq 1 10); do
        if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
            log "✓ Mounted successfully"
            echo "✓ Proton Drive mounted at $MOUNT_DIR"
            exit 0
        fi
        sleep 1
    done

    log "✗ Mount did not come up in 10 seconds — check $LOG_FILE"
    echo "✗ Mount may have failed. Check: $LOG_FILE"
    exit 1
fi
