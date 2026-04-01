#!/usr/bin/env bash
# Create timestamped local snapshots of your sync folder
# Uses hardlinks for space efficiency (only changed files take extra space)
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

BACKUP_DIR="${BACKUP_DIR:-$HOME/.local/share/protondrive-linux/backups}"
MAX_BACKUPS="${BACKUP_MAX_KEEP:-10}"
LOG_FILE="$LOG_DIR/backup.log"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

ACTION="create"

# ─── Parse args ────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --list|-l)    ACTION="list" ;;
        --restore|-r) ACTION="restore"; RESTORE_TARGET="${2:-}"; shift ;;
        --prune)      ACTION="prune" ;;
        --dir)        BACKUP_DIR="$2"; shift ;;
        --keep)       MAX_BACKUPS="$2"; shift ;;
        --help|-h)
            cat << 'EOF'
Usage: pdrive backup [options]

Options:
  (default)          Create a new snapshot
  --list, -l         List existing snapshots
  --restore, -r ID   Restore a snapshot (by name or "latest")
  --prune            Delete old snapshots beyond --keep limit
  --keep N           Keep N most recent snapshots (default: 10)
  --dir PATH         Custom backup directory
EOF
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"; }

# ─── List ──────────────────────────────────────────────────────────────

do_list() {
    echo -e "${BOLD}Snapshots${NC} ($BACKUP_DIR)"
    echo ""

    if [[ ! -d "$BACKUP_DIR" ]] || [[ -z "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ]]; then
        echo "  No snapshots yet. Run: pdrive backup"
        return
    fi

    local count=0
    for snap in "$BACKUP_DIR"/*/; do
        [[ -d "$snap" ]] || continue
        local name
        name=$(basename "$snap")
        local size
        size=$(du -sh "$snap" 2>/dev/null | cut -f1)
        local files
        files=$(find "$snap" -type f | wc -l)
        echo -e "  ${GREEN}$name${NC}  ($files files, $size)"
        ((count++))
    done

    echo ""
    echo "Total: $count snapshots"
}

# ─── Create ────────────────────────────────────────────────────────────

do_create() {
    if [[ ! -d "$SYNC_DIR" ]]; then
        echo "Sync directory does not exist: $SYNC_DIR"
        exit 1
    fi

    mkdir -p "$BACKUP_DIR"

    local timestamp
    timestamp=$(date +%Y%m%d-%H%M%S)
    local snap_dir="$BACKUP_DIR/$timestamp"

    echo -e "${BOLD}Creating snapshot: $timestamp${NC}"

    # Find latest existing snapshot for hardlink reference
    local latest_snap=""
    if [[ -d "$BACKUP_DIR" ]]; then
        latest_snap=$(ls -1d "$BACKUP_DIR"/*/ 2>/dev/null | sort | tail -1 || true)
    fi

    if [[ -n "$latest_snap" && -d "$latest_snap" ]]; then
        # Use rsync with --link-dest for space-efficient incremental backup
        log "Creating incremental snapshot from $(basename "$latest_snap")"
        rsync -a --link-dest="$latest_snap" "$SYNC_DIR/" "$snap_dir/"
    else
        # First snapshot — full copy
        log "Creating first full snapshot"
        rsync -a "$SYNC_DIR/" "$snap_dir/"
    fi

    local snap_size
    snap_size=$(du -sh "$snap_dir" 2>/dev/null | cut -f1)
    local snap_files
    snap_files=$(find "$snap_dir" -type f | wc -l)

    log "✓ Snapshot created: $timestamp ($snap_files files, $snap_size)"
    echo -e "${GREEN}✓${NC} Snapshot: $timestamp ($snap_files files, $snap_size)"

    # Auto-prune
    do_prune_silent
}

# ─── Restore ───────────────────────────────────────────────────────────

do_restore() {
    local target="$RESTORE_TARGET"

    if [[ -z "$target" ]]; then
        echo "Specify a snapshot to restore: pdrive backup --restore <ID>"
        echo "Use 'pdrive backup --list' to see available snapshots."
        echo "Use 'latest' to restore the most recent."
        exit 1
    fi

    if [[ "$target" == "latest" ]]; then
        target=$(ls -1d "$BACKUP_DIR"/*/ 2>/dev/null | sort | tail -1 || true)
        target=$(basename "$target")
    fi

    local snap_dir="$BACKUP_DIR/$target"

    if [[ ! -d "$snap_dir" ]]; then
        echo -e "${RED}Snapshot not found:${NC} $target"
        do_list
        exit 1
    fi

    local snap_files
    snap_files=$(find "$snap_dir" -type f | wc -l)

    echo -e "${BOLD}Restore snapshot: $target${NC} ($snap_files files)"
    echo -e "${YELLOW}This will replace the contents of:${NC} $SYNC_DIR"
    echo ""

    read -rp "$(echo -e "${YELLOW}Are you sure? [y/N]:${NC} ")" answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi

    # Create a safety backup of current state first
    local safety="$BACKUP_DIR/pre-restore-$(date +%Y%m%d-%H%M%S)"
    echo "Creating safety backup of current state..."
    rsync -a "$SYNC_DIR/" "$safety/"
    echo -e "${DIM}Safety backup: $(basename "$safety")${NC}"

    # Restore
    rsync -a --delete "$snap_dir/" "$SYNC_DIR/"

    log "✓ Restored snapshot: $target → $SYNC_DIR"
    echo -e "${GREEN}✓${NC} Restored: $target"
    echo ""
    echo "Run 'pdrive sync --force' to push the restored state to Proton Drive."
}

# ─── Prune ─────────────────────────────────────────────────────────────

do_prune_silent() {
    local snaps
    snaps=$(ls -1d "$BACKUP_DIR"/*/ 2>/dev/null | sort || true)
    local count
    count=$(echo "$snaps" | grep -c . || echo 0)

    if [[ "$count" -gt "$MAX_BACKUPS" ]]; then
        local to_delete=$((count - MAX_BACKUPS))
        echo "$snaps" | head -"$to_delete" | while IFS= read -r old; do
            rm -rf "$old"
            log "Pruned old snapshot: $(basename "$old")"
        done
    fi
}

do_prune() {
    echo -e "${BOLD}Pruning snapshots${NC} (keeping $MAX_BACKUPS most recent)"
    echo ""

    local snaps
    snaps=$(ls -1d "$BACKUP_DIR"/*/ 2>/dev/null | sort || true)
    local count
    count=$(echo "$snaps" | grep -c . || echo 0)

    if [[ "$count" -le "$MAX_BACKUPS" ]]; then
        echo "Nothing to prune ($count snapshots, limit is $MAX_BACKUPS)"
        return
    fi

    local to_delete=$((count - MAX_BACKUPS))
    echo "Removing $to_delete old snapshot(s)..."
    echo ""

    echo "$snaps" | head -"$to_delete" | while IFS= read -r old; do
        local name
        name=$(basename "$old")
        rm -rf "$old"
        echo -e "  ${RED}✗${NC} Deleted: $name"
        log "Pruned: $name"
    done

    echo ""
    echo -e "${GREEN}✓${NC} Done. $MAX_BACKUPS snapshots remaining."
}

# ─── Dispatch ──────────────────────────────────────────────────────────

case "$ACTION" in
    create)  do_create ;;
    list)    do_list ;;
    restore) do_restore ;;
    prune)   do_prune ;;
esac
