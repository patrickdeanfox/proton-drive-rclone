#!/usr/bin/env bash
# Clean up caches, old logs, dedup trash, and rclone VFS cache
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

DRY_RUN=false
ALL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --all)     ALL=true ;;
        --help|-h)
            cat << 'EOF'
Usage: pdrive cleanup [--dry-run] [--all]

Cleans:
  - Old log files beyond retention period
  - Dedup trash (.protondrive-trash/)
  - rclone VFS cache
  - Empty directories in sync folder

  --all    Also clear ALL rclone cache (not just expired)
  --dry-run  Show what would be cleaned without deleting
EOF
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

freed=0

clean_item() {
    local desc="$1"
    local path="$2"
    local size=0

    if [[ -e "$path" ]]; then
        size=$(du -sb "$path" 2>/dev/null | cut -f1 || echo 0)
        if $DRY_RUN; then
            echo -e "  ${YELLOW}would remove${NC} $desc ($(numfmt --to=iec "$size" 2>/dev/null || echo "$size bytes"))"
        else
            rm -rf "$path"
            echo -e "  ${GREEN}✓${NC} $desc ($(numfmt --to=iec "$size" 2>/dev/null || echo "$size bytes"))"
        fi
        ((freed += size))
    fi
}

echo -e "${BOLD}protondrive-linux cleanup${NC}"
$DRY_RUN && echo -e "${YELLOW}DRY RUN — nothing will be deleted${NC}"
echo ""

# ─── Old logs ──────────────────────────────────────────────────────────

echo -e "${BOLD}Logs${NC} (older than ${LOG_RETAIN_DAYS} days)"
old_logs=$(find "$LOG_DIR" -name "*.log.*.bak" -mtime +"$LOG_RETAIN_DAYS" 2>/dev/null || true)
if [[ -n "$old_logs" ]]; then
    echo "$old_logs" | while IFS= read -r f; do
        clean_item "$(basename "$f")" "$f"
    done
else
    echo -e "  ${DIM}nothing to clean${NC}"
fi
echo ""

# ─── Dedup trash ──────────────────────────────────────────────────────

TRASH="$SYNC_DIR/${DEDUP_TRASH_DIR:-.protondrive-trash}"
echo -e "${BOLD}Dedup Trash${NC} ($TRASH)"
if [[ -d "$TRASH" ]] && [[ -n "$(ls -A "$TRASH" 2>/dev/null)" ]]; then
    trash_size=$(du -sb "$TRASH" 2>/dev/null | cut -f1 || echo 0)
    trash_count=$(find "$TRASH" -type f | wc -l)
    if $DRY_RUN; then
        echo -e "  ${YELLOW}would remove${NC} $trash_count files ($(numfmt --to=iec "$trash_size" 2>/dev/null))"
    else
        rm -rf "${TRASH:?}/"*
        echo -e "  ${GREEN}✓${NC} $trash_count files ($(numfmt --to=iec "$trash_size" 2>/dev/null))"
    fi
    ((freed += trash_size))
else
    echo -e "  ${DIM}empty${NC}"
fi
echo ""

# ─── rclone VFS cache ─────────────────────────────────────────────────

VFS_CACHE="$HOME/.cache/rclone/vfs/$RCLONE_REMOTE"
echo -e "${BOLD}rclone VFS Cache${NC} ($VFS_CACHE)"
if [[ -d "$VFS_CACHE" ]]; then
    cache_size=$(du -sb "$VFS_CACHE" 2>/dev/null | cut -f1 || echo 0)

    if $ALL; then
        if $DRY_RUN; then
            echo -e "  ${YELLOW}would clear all${NC} ($(numfmt --to=iec "$cache_size" 2>/dev/null))"
        else
            rm -rf "${VFS_CACHE:?}/"*
            echo -e "  ${GREEN}✓${NC} cleared all ($(numfmt --to=iec "$cache_size" 2>/dev/null))"
        fi
        ((freed += cache_size))
    else
        # Only remove files older than cache max age
        old_cache=$(find "$VFS_CACHE" -type f -mmin +60 2>/dev/null || true)
        if [[ -n "$old_cache" ]]; then
            old_size=$(echo "$old_cache" | xargs du -cb 2>/dev/null | tail -1 | cut -f1 || echo 0)
            old_count=$(echo "$old_cache" | wc -l)
            if $DRY_RUN; then
                echo -e "  ${YELLOW}would remove${NC} $old_count expired files ($(numfmt --to=iec "$old_size" 2>/dev/null))"
            else
                echo "$old_cache" | xargs rm -f
                echo -e "  ${GREEN}✓${NC} $old_count expired files ($(numfmt --to=iec "$old_size" 2>/dev/null))"
            fi
            ((freed += old_size))
        else
            echo -e "  ${DIM}no expired cache${NC}"
        fi
    fi
else
    echo -e "  ${DIM}no cache directory${NC}"
fi
echo ""

# ─── rclone bisync cache ──────────────────────────────────────────────

BISYNC_CACHE="$HOME/.cache/rclone/bisync"
echo -e "${BOLD}Bisync State${NC}"
if [[ -d "$BISYNC_CACHE" ]]; then
    bs_size=$(du -sh "$BISYNC_CACHE" 2>/dev/null | cut -f1)
    bs_files=$(find "$BISYNC_CACHE" -type f | wc -l)
    echo -e "  $bs_files state files ($bs_size) — ${DIM}keeping (needed for bisync)${NC}"
    if $ALL; then
        echo -e "  ${YELLOW}Use 'pdrive sync --resync' to reset bisync state${NC}"
    fi
else
    echo -e "  ${DIM}no bisync state${NC}"
fi
echo ""

# ─── Empty directories ────────────────────────────────────────────────

echo -e "${BOLD}Empty Directories${NC} (in $SYNC_DIR)"
empty_dirs=$(find "$SYNC_DIR" -mindepth 1 -type d -empty 2>/dev/null || true)
if [[ -n "$empty_dirs" ]]; then
    empty_count=$(echo "$empty_dirs" | wc -l)
    if $DRY_RUN; then
        echo -e "  ${YELLOW}would remove${NC} $empty_count empty directories"
        echo "$empty_dirs" | head -5 | while IFS= read -r d; do
            echo -e "    ${DIM}${d#$SYNC_DIR/}${NC}"
        done
        [[ "$empty_count" -gt 5 ]] && echo -e "    ${DIM}...and $((empty_count - 5)) more${NC}"
    else
        echo "$empty_dirs" | xargs rmdir 2>/dev/null || true
        echo -e "  ${GREEN}✓${NC} removed $empty_count empty directories"
    fi
else
    echo -e "  ${DIM}none${NC}"
fi
echo ""

# ─── Summary ──────────────────────────────────────────────────────────

echo -e "${BOLD}━━━ Summary ━━━${NC}"
if [[ $freed -gt 0 ]]; then
    echo -e "Space freed: $(numfmt --to=iec "$freed" 2>/dev/null || echo "$freed bytes")"
else
    echo "Nothing to clean up."
fi
$DRY_RUN && echo -e "${YELLOW}Run without --dry-run to apply${NC}"
