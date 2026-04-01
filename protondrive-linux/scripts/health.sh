#!/usr/bin/env bash
# Full health check for protondrive-linux setup
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

pass=0
warn=0
fail=0

check() {
    local desc="$1"
    shift
    if "$@" &>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $desc"
        ((pass++))
    else
        echo -e "  ${RED}✗${NC} $desc"
        ((fail++))
        return 1
    fi
}

check_warn() {
    local desc="$1"
    shift
    if "$@" &>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $desc"
        ((pass++))
    else
        echo -e "  ${YELLOW}!${NC} $desc"
        ((warn++))
    fi
}

echo ""
echo -e "${BOLD}protondrive-linux health check${NC}"
echo ""

# ─── Dependencies ──────────────────────────────────────────────────────

echo -e "${BOLD}Dependencies${NC}"
check "rclone installed" command -v rclone
check "FUSE available" bash -c "command -v fusermount3 || command -v fusermount"
check_warn "jq installed" command -v jq
check_warn "sha1sum available" bash -c "command -v sha1sum || command -v shasum"

rclone_ver=$(rclone version 2>/dev/null | head -1 | grep -oP 'v[\d.]+' || echo "unknown")
echo -e "  ${DIM}rclone version: $rclone_ver${NC}"
echo ""

# ─── Configuration ────────────────────────────────────────────────────

echo -e "${BOLD}Configuration${NC}"
check "config.env exists" test -f "$HOME/.config/protondrive-linux/config.env"
check "organize-rules.yml exists" test -f "$HOME/.config/protondrive-linux/organize-rules.yml"
check "mount directory exists" test -d "$MOUNT_DIR"
check "sync directory exists" test -d "$SYNC_DIR"
check "log directory exists" test -d "$LOG_DIR"
echo ""

# ─── rclone Remote ────────────────────────────────────────────────────

echo -e "${BOLD}rclone Remote${NC}"
check "remote '$RCLONE_REMOTE' configured" rclone listremotes | grep -q "$RCLONE_REMOTE:"

echo -e "  ${DIM}Testing connection...${NC}"
if rclone lsd "$RCLONE_REMOTE:" --max-depth 0 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} Connection to Proton Drive successful"
    ((pass++))
else
    echo -e "  ${RED}✗${NC} Cannot connect to Proton Drive"
    ((fail++))
    echo -e "    ${DIM}Check credentials: rclone config reconnect $RCLONE_REMOTE:${NC}"
fi
echo ""

# ─── Mount ────────────────────────────────────────────────────────────

echo -e "${BOLD}Mount${NC}"
if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} FUSE mount active at $MOUNT_DIR"
    ((pass++))

    # Test read
    if ls "$MOUNT_DIR" &>/dev/null; then
        echo -e "  ${GREEN}✓${NC} Mount is readable"
        ((pass++))
    else
        echo -e "  ${RED}✗${NC} Mount exists but is not readable"
        ((fail++))
    fi
else
    echo -e "  ${YELLOW}!${NC} Not currently mounted (run: pdrive mount)"
    ((warn++))
fi

# FUSE group
if groups "$USER" | grep -qw fuse 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} User in fuse group"
    ((pass++))
elif getent group fuse &>/dev/null; then
    echo -e "  ${YELLOW}!${NC} User not in fuse group (may cause mount issues)"
    ((warn++))
else
    echo -e "  ${DIM}  fuse group doesn't exist (probably fine)${NC}"
fi
echo ""

# ─── Systemd ──────────────────────────────────────────────────────────

echo -e "${BOLD}Systemd Services${NC}"
for unit in protondrive-mount.service protondrive-sync.service protondrive-sync.timer; do
    if systemctl --user cat "$unit" &>/dev/null; then
        local_state=$(systemctl --user is-enabled "$unit" 2>/dev/null || echo "disabled")
        active=$(systemctl --user is-active "$unit" 2>/dev/null || echo "inactive")
        echo -e "  ${GREEN}✓${NC} $unit (enabled=$local_state, active=$active)"
        ((pass++))
    else
        echo -e "  ${YELLOW}!${NC} $unit not installed"
        ((warn++))
    fi
done
echo ""

# ─── Logs ─────────────────────────────────────────────────────────────

echo -e "${BOLD}Logs${NC}"
for logname in sync.log mount.log organize.log; do
    local logpath="$LOG_DIR/$logname"
    if [[ -f "$logpath" ]]; then
        local size
        size=$(du -h "$logpath" | cut -f1)
        local lines
        lines=$(wc -l < "$logpath")
        echo -e "  ${GREEN}✓${NC} $logname ($size, $lines lines)"
        ((pass++))
    else
        echo -e "  ${DIM}  $logname (not created yet)${NC}"
    fi
done

# Check for recent errors
if [[ -f "$LOG_DIR/sync.log" ]]; then
    recent_errors=$(tail -100 "$LOG_DIR/sync.log" | grep -ci 'error\|fail' || true)
    if [[ "$recent_errors" -gt 0 ]]; then
        echo -e "  ${YELLOW}!${NC} $recent_errors error/fail mentions in recent sync log"
        ((warn++))
    fi
fi
echo ""

# ─── Disk space ───────────────────────────────────────────────────────

echo -e "${BOLD}Disk Space${NC}"
if [[ -d "$SYNC_DIR" ]]; then
    sync_size=$(du -sh "$SYNC_DIR" 2>/dev/null | cut -f1)
    echo -e "  Sync folder: $sync_size"
fi

vfs_cache="$HOME/.cache/rclone/vfs/$RCLONE_REMOTE"
if [[ -d "$vfs_cache" ]]; then
    cache_size=$(du -sh "$vfs_cache" 2>/dev/null | cut -f1)
    echo -e "  VFS cache:   $cache_size"
fi

avail=$(df -h "$HOME" | tail -1 | awk '{print $4}')
echo -e "  Available:   $avail"
echo ""

# ─── Summary ──────────────────────────────────────────────────────────

echo -e "${BOLD}━━━ Results ━━━${NC}"
echo -e "  ${GREEN}✓ $pass passed${NC}  ${YELLOW}! $warn warnings${NC}  ${RED}✗ $fail failed${NC}"

if [[ $fail -gt 0 ]]; then
    echo ""
    echo -e "${RED}Some checks failed — review the output above.${NC}"
    exit 1
elif [[ $warn -gt 0 ]]; then
    echo ""
    echo -e "${YELLOW}Everything works but some items need attention.${NC}"
fi
echo ""
