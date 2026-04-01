#!/usr/bin/env bash
# Show status of mount, sync, and quota
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

echo ""
echo -e "${BOLD}protondrive-linux status${NC}"
echo -e "${DIM}$(date)${NC}"
echo ""

# ─── Mount status ──────────────────────────────────────────────────────

echo -e "${BOLD}Mount${NC}"
if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo -e "  Status:   ${GREEN}● mounted${NC}"
    echo -e "  Path:     $MOUNT_DIR"
    # Count files visible
    local_count=$(find "$MOUNT_DIR" -maxdepth 2 -type f 2>/dev/null | wc -l)
    echo -e "  Files:    ~$local_count (top 2 levels)"
else
    echo -e "  Status:   ${RED}● not mounted${NC}"
    echo -e "  Path:     $MOUNT_DIR"
fi
echo ""

# ─── Sync status ──────────────────────────────────────────────────────

echo -e "${BOLD}Sync${NC}"
echo -e "  Folder:   $SYNC_DIR"
if [[ -d "$SYNC_DIR" ]]; then
    sync_count=$(find "$SYNC_DIR" -type f 2>/dev/null | wc -l)
    sync_size=$(du -sh "$SYNC_DIR" 2>/dev/null | cut -f1)
    echo -e "  Files:    $sync_count"
    echo -e "  Size:     $sync_size"
else
    echo -e "  ${DIM}(directory does not exist yet)${NC}"
fi

# Last sync
LOG_FILE="$LOG_DIR/sync.log"
if [[ -f "$LOG_FILE" ]]; then
    last_sync=$(grep -oP '^\[\K[^]]+' "$LOG_FILE" | tail -1)
    last_result=$(grep -E '(Sync completed|Sync finished with errors)' "$LOG_FILE" | tail -1)
    echo -e "  Last run: ${last_sync:-unknown}"
    if echo "$last_result" | grep -q "successfully"; then
        echo -e "  Result:   ${GREEN}✓ success${NC}"
    elif [[ -n "$last_result" ]]; then
        echo -e "  Result:   ${RED}✗ errors${NC}"
    fi
fi

# Timer status
if systemctl --user is-active protondrive-sync.timer &>/dev/null; then
    next=$(systemctl --user show protondrive-sync.timer --property=NextElapseUSecRealtime --value 2>/dev/null || echo "unknown")
    echo -e "  Timer:    ${GREEN}● active${NC} (next: $next)"
else
    echo -e "  Timer:    ${YELLOW}● inactive${NC}"
fi
echo ""

# ─── Remote info ──────────────────────────────────────────────────────

echo -e "${BOLD}Remote${NC}"
echo -e "  Name:     $RCLONE_REMOTE"

# Quota / about
if about_out=$(rclone about "$RCLONE_REMOTE:" 2>/dev/null); then
    echo "$about_out" | while IFS= read -r line; do
        echo "  $line"
    done
else
    echo -e "  ${DIM}(could not fetch remote info — may need auth)${NC}"
fi
echo ""

# ─── Systemd services ─────────────────────────────────────────────────

echo -e "${BOLD}Services${NC}"
for svc in protondrive-mount.service protondrive-sync.service protondrive-sync.timer; do
    if systemctl --user is-enabled "$svc" &>/dev/null; then
        state=$(systemctl --user is-active "$svc" 2>/dev/null || echo "inactive")
        case "$state" in
            active)   icon="${GREEN}●${NC}" ;;
            inactive) icon="${YELLOW}●${NC}" ;;
            *)        icon="${RED}●${NC}" ;;
        esac
        echo -e "  $icon $svc ($state)"
    else
        echo -e "  ${DIM}○ $svc (not installed)${NC}"
    fi
done
echo ""
