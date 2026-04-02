#!/usr/bin/env bash
# Bisync (or one-way sync) between local folder and Proton Drive
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

LOG_FILE="$LOG_DIR/sync.log"
LOCK_FILE="/tmp/protondrive-sync.lock"

DRY_RUN=false
FORCE=false
ONE_WAY=false
RESYNC=false

# ─── Parse args ────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  DRY_RUN=true ;;
        --force)    FORCE=true ;;
        --one-way)  ONE_WAY=true ;;
        --resync)   RESYNC=true ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ─── Logging ───────────────────────────────────────────────────────────

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"
}

# ─── Locking ───────────────────────────────────────────────────────────

acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local pid
        pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            log "Sync already running (PID $pid), skipping"
            exit 0
        else
            log "Stale lock file found, removing"
            rm -f "$LOCK_FILE"
        fi
    fi
    echo $$ > "$LOCK_FILE"
    trap 'rm -f "$LOCK_FILE"' EXIT
}

# ─── Build exclude list ───────────────────────────────────────────────

build_excludes() {
    local excludes=()
    IFS=',' read -ra patterns <<< "$SYNC_EXCLUDE_PATTERNS"
    for p in "${patterns[@]}"; do
        p=$(echo "$p" | xargs)  # trim whitespace
        [[ -n "$p" ]] && excludes+=(--exclude "$p")
    done
    echo "${excludes[@]}"
}

# ─── Log rotation ─────────────────────────────────────────────────────

rotate_logs() {
    if [[ -f "$LOG_FILE" ]]; then
        local size_mb
        size_mb=$(du -m "$LOG_FILE" 2>/dev/null | cut -f1)
        if [[ "$size_mb" -gt "$LOG_MAX_SIZE_MB" ]]; then
            mv "$LOG_FILE" "$LOG_FILE.$(date +%Y%m%d-%H%M%S).bak"
            log "Log rotated"
        fi
    fi

    # Clean old logs
    find "$LOG_DIR" -name "sync.log.*.bak" -mtime +"$LOG_RETAIN_DAYS" -delete 2>/dev/null || true
}

# ─── Main sync ─────────────────────────────────────────────────────────

main() {
    acquire_lock
    rotate_logs
    mkdir -p "$SYNC_DIR"

    log "━━━ Sync started ━━━"
    log "Mode: $(if $ONE_WAY; then echo 'one-way (local→remote)'; else echo 'bisync'; fi)"
    log "Dry run: $DRY_RUN"

    # Build flags
    local -a flags=()
    flags+=(--log-file "$LOG_FILE")
    flags+=(--log-level "${LOG_LEVEL:-INFO}")
    flags+=(--checkers "${SYNC_CHECKERS:-8}")
    flags+=(--transfers "${SYNC_TRANSFERS:-4}")

    # Excludes
    IFS=',' read -ra patterns <<< "$SYNC_EXCLUDE_PATTERNS"
    for p in "${patterns[@]}"; do
        p=$(echo "$p" | xargs)
        [[ -n "$p" ]] && flags+=(--exclude "$p")
    done

    $DRY_RUN && flags+=(--dry-run)

    local rc=0

    if $ONE_WAY; then
        # One-way: local → remote
        log "Running: rclone sync $SYNC_DIR → $RCLONE_REMOTE:"
        if ! $FORCE; then
            flags+=(--max-delete "${SYNC_MAX_DELETE_PCT:-50}")
        fi
        rclone sync "$SYNC_DIR" "$RCLONE_REMOTE:" "${flags[@]}" 2>&1 | tee -a "$LOG_FILE" || rc=$?
    else
        # Bisync: two-way
        $RESYNC && flags+=(--resync)

        # Conflict resolution
        case "${SYNC_CONFLICT_POLICY:-newer}" in
            newer)  flags+=(--conflict-resolve newer) ;;
            larger) flags+=(--conflict-resolve larger) ;;
            skip)   ;; # omit flag: rclone creates conflict copies by default
        esac

        if ! $FORCE; then
            flags+=(--max-delete "${SYNC_MAX_DELETE_PCT:-50}")
        fi

        log "Running: rclone bisync $SYNC_DIR ↔ $RCLONE_REMOTE:"

        # bisync needs --resync on first run; detect via state files for this
        # specific path pair (not just whether the cache dir exists at all)
        local bisync_state_dir="$HOME/.cache/rclone/bisync"
        if ! $RESYNC && [[ -d "$bisync_state_dir" ]]; then
            # State files are named with a hash that encodes both paths; check
            # whether any listing file references the current remote
            if ! ls "$bisync_state_dir"/*.lst 2>/dev/null | xargs grep -ql "$RCLONE_REMOTE" 2>/dev/null; then
                log "No bisync state found for $RCLONE_REMOTE — adding --resync"
                flags+=(--resync)
            fi
        elif ! $RESYNC && [[ ! -d "$bisync_state_dir" ]]; then
            log "First bisync run detected — adding --resync"
            flags+=(--resync)
        fi

        rclone bisync "$SYNC_DIR" "$RCLONE_REMOTE:" "${flags[@]}" 2>&1 | tee -a "$LOG_FILE" || rc=$?

        # If bisync fails with "must resync", auto-resync once
        if [[ $rc -ne 0 ]] && ! $RESYNC; then
            if grep -qi "Bisync requires\|requires.*--resync\|must.*resync" "$LOG_FILE" 2>/dev/null; then
                log "Bisync requires resync — retrying with --resync"
                flags+=(--resync)
                rclone bisync "$SYNC_DIR" "$RCLONE_REMOTE:" "${flags[@]}" 2>&1 | tee -a "$LOG_FILE" || rc=$?
            fi
        fi
    fi

    if [[ $rc -eq 0 ]]; then
        log "✓ Sync completed successfully"
    else
        log "✗ Sync finished with errors (exit code: $rc)"
    fi

    # Run organizer if enabled
    if [[ "${ORGANIZE_ON_SYNC:-false}" == "true" && "${ORGANIZE_ENABLED:-false}" == "true" ]]; then
        log "Running post-sync file organization..."
        bash "$(dirname "$0")/organize.sh"
    fi

    log "━━━ Sync ended ━━━"
    return $rc
}

main
