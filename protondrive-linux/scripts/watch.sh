#!/usr/bin/env bash
# Watch sync directory for changes and trigger sync after a debounce period
# Requires: inotify-tools (inotifywait)
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

DEBOUNCE_SEC="${WATCH_DEBOUNCE_SEC:-10}"
LOG_FILE="$LOG_DIR/watch.log"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"; }

# ─── Dependency check ─────────────────────────────────────────────────

if ! command -v inotifywait &>/dev/null; then
    echo -e "${YELLOW}inotifywait not found.${NC}"
    echo ""
    if command -v apt &>/dev/null; then
        echo "  sudo apt install inotify-tools"
    elif command -v dnf &>/dev/null; then
        echo "  sudo dnf install inotify-tools"
    elif command -v pacman &>/dev/null; then
        echo "  sudo pacman -S inotify-tools"
    fi
    exit 1
fi

# ─── Build exclude regex from config ──────────────────────────────────

build_exclude_regex() {
    local regex=""
    IFS=',' read -ra patterns <<< "$SYNC_EXCLUDE_PATTERNS"
    for p in "${patterns[@]}"; do
        p=$(echo "$p" | xargs | sed 's/\./\\./g' | sed 's/\*/.*/g')
        [[ -n "$p" ]] && regex="${regex:+$regex|}$p"
    done
    # Always exclude hidden files and trash
    regex="${regex:+$regex|}^\\..*|protondrive-trash"
    echo "$regex"
}

# ─── Notify helper ─────────────────────────────────────────────────────

notify() {
    local msg="$1"
    if command -v notify-send &>/dev/null; then
        notify-send -i sync "Proton Drive" "$msg" 2>/dev/null || true
    fi
    log "$msg"
}

# ─── Main watch loop ──────────────────────────────────────────────────

main() {
    if [[ ! -d "$SYNC_DIR" ]]; then
        echo "Sync directory does not exist: $SYNC_DIR"
        exit 1
    fi

    local exclude_regex
    exclude_regex=$(build_exclude_regex)

    echo -e "${BOLD}Watching $SYNC_DIR for changes${NC}"
    echo -e "Debounce: ${DEBOUNCE_SEC}s"
    echo -e "Excludes: $SYNC_EXCLUDE_PATTERNS"
    echo -e "${DIM}Press Ctrl+C to stop${NC}"
    echo ""

    log "Watch started on $SYNC_DIR (debounce: ${DEBOUNCE_SEC}s)"

    local last_sync=0

    while true; do
        # Wait for filesystem events
        changed_file=$(inotifywait -r -q \
            --exclude "$exclude_regex" \
            -e modify,create,delete,move \
            --format '%w%f' \
            "$SYNC_DIR" 2>/dev/null) || continue

        local now
        now=$(date +%s)
        local elapsed=$((now - last_sync))

        log "Change detected: $changed_file"

        # Debounce: skip if we synced recently
        if [[ $elapsed -lt $DEBOUNCE_SEC ]]; then
            log "Debouncing (${elapsed}s < ${DEBOUNCE_SEC}s), skipping"
            continue
        fi

        # Batch: wait a moment for burst writes to settle
        sleep 2

        echo -e "${GREEN}→${NC} Change detected, syncing..."
        notify "Syncing changes..."

        local script_dir
        script_dir="$(dirname "$0")"
        if bash "$script_dir/sync.sh" 2>&1; then
            notify "Sync complete"
            echo -e "${GREEN}✓${NC} Sync complete"
        else
            notify "Sync failed — check logs"
            echo -e "${YELLOW}!${NC} Sync had issues — check pdrive logs"
        fi

        last_sync=$(date +%s)
    done
}

main
