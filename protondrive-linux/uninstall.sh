#!/usr/bin/env bash
# Uninstall protondrive-linux
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

CONFIG_DIR="$HOME/.config/protondrive-linux"
DATA_DIR="$HOME/.local/share/protondrive-linux"
BIN="$HOME/.local/bin/pdrive"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo -e "${BOLD}protondrive-linux uninstaller${NC}"
echo ""
echo "This will remove:"
echo "  - Systemd services and timers"
echo "  - pdrive CLI ($BIN)"
echo "  - Config ($CONFIG_DIR)"
echo "  - Logs and data ($DATA_DIR)"
echo ""
echo -e "${YELLOW}This will NOT remove:${NC}"
echo "  - rclone or its config"
echo "  - Your sync folder contents"
echo "  - Your mount folder"
echo ""

read -rp "$(echo -e "${YELLOW}Proceed with uninstall? [y/N]:${NC} ")" answer
if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# Stop and disable services
echo ""
echo "Stopping services..."
for unit in protondrive-mount.service protondrive-sync.timer protondrive-sync.service protondrive-organize.service; do
    if systemctl --user is-active "$unit" &>/dev/null; then
        systemctl --user stop "$unit" 2>/dev/null || true
        echo -e "  ${GREEN}✓${NC} Stopped $unit"
    fi
    if systemctl --user is-enabled "$unit" &>/dev/null; then
        systemctl --user disable "$unit" 2>/dev/null || true
        echo -e "  ${GREEN}✓${NC} Disabled $unit"
    fi
    rm -f "$SYSTEMD_DIR/$unit"
done
systemctl --user daemon-reload 2>/dev/null || true

# Unmount if mounted
source "$CONFIG_DIR/config.env" 2>/dev/null || true
if [[ -n "${MOUNT_DIR:-}" ]] && mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo "Unmounting $MOUNT_DIR..."
    fusermount -u "$MOUNT_DIR" 2>/dev/null || fusermount3 -u "$MOUNT_DIR" 2>/dev/null || true
    echo -e "  ${GREEN}✓${NC} Unmounted"
fi

# Remove CLI
if [[ -f "$BIN" ]]; then
    rm -f "$BIN"
    echo -e "${GREEN}✓${NC} Removed pdrive CLI"
fi

# Remove completions
rm -f "$HOME/.local/share/bash-completion/completions/pdrive" 2>/dev/null || true
rm -f /etc/bash_completion.d/pdrive 2>/dev/null || true

# Config and data
read -rp "$(echo -e "${YELLOW}Delete config and logs? [y/N]:${NC} ")" del_data
if [[ "$del_data" =~ ^[Yy]$ ]]; then
    rm -rf "$CONFIG_DIR"
    rm -rf "$DATA_DIR"
    echo -e "${GREEN}✓${NC} Config and data removed"
else
    echo -e "  Kept: $CONFIG_DIR"
    echo -e "  Kept: $DATA_DIR"
fi

echo ""
echo -e "${GREEN}Uninstall complete.${NC}"
echo ""
echo "Your rclone config and sync/mount directories were preserved."
echo "To remove rclone config: rclone config delete protondrive"
