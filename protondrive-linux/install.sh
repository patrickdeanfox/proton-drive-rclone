#!/usr/bin/env bash
# protondrive-linux installer
# Sets up rclone, directories, config, and systemd services

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/protondrive-linux"
DATA_DIR="$HOME/.local/share/protondrive-linux"
LOG_DIR="$DATA_DIR/logs"
BIN_DIR="$HOME/.local/bin"
SYSTEMD_DIR="$HOME/.config/systemd/user"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

header() {
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  $*${NC}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

confirm() {
    local prompt="${1:-Continue?}"
    read -rp "$(echo -e "${YELLOW}$prompt [y/N]:${NC} ")" answer
    [[ "$answer" =~ ^[Yy]$ ]]
}

# ─── Dependency checks ────────────────────────────────────────────────

check_deps() {
    header "Checking Dependencies"

    local missing=()

    # rclone
    if command -v rclone &>/dev/null; then
        local ver
        ver=$(rclone version --check 2>/dev/null | head -1 || rclone version 2>/dev/null | head -1)
        ok "rclone found: $ver"
    else
        missing+=("rclone")
        warn "rclone not found"
    fi

    # fuse3
    if command -v fusermount3 &>/dev/null || command -v fusermount &>/dev/null; then
        ok "FUSE found"
    else
        missing+=("fuse3")
        warn "fuse3 not found"
    fi

    # jq
    if command -v jq &>/dev/null; then
        ok "jq found"
    else
        missing+=("jq")
        warn "jq not found"
    fi

    # sha1sum / shasum
    if command -v sha1sum &>/dev/null || command -v shasum &>/dev/null; then
        ok "SHA1 tool found"
    else
        warn "sha1sum/shasum not found (needed for dedup)"
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo ""
        warn "Missing dependencies: ${missing[*]}"

        if command -v apt &>/dev/null; then
            info "Install with: sudo apt install ${missing[*]}"
        elif command -v dnf &>/dev/null; then
            info "Install with: sudo dnf install ${missing[*]}"
        elif command -v pacman &>/dev/null; then
            info "Install with: sudo pacman -S ${missing[*]}"
        fi

        if confirm "Attempt to install missing packages?"; then
            install_deps "${missing[@]}"
        else
            err "Cannot continue without: ${missing[*]}"
            exit 1
        fi
    fi
}

install_deps() {
    local pkgs=("$@")
    if command -v apt &>/dev/null; then
        sudo apt update && sudo apt install -y "${pkgs[@]}"
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y "${pkgs[@]}"
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm "${pkgs[@]}"
    else
        err "Could not detect package manager. Install manually: ${pkgs[*]}"
        exit 1
    fi
}

# ─── rclone configuration ─────────────────────────────────────────────

setup_rclone() {
    header "Configuring rclone for Proton Drive"

    # Check if a protondrive remote already exists
    if rclone listremotes 2>/dev/null | grep -q "protondrive:"; then
        ok "rclone remote 'protondrive' already configured"
        if confirm "Reconfigure it?"; then
            rclone config delete protondrive
        else
            return 0
        fi
    fi

    echo ""
    info "You'll now configure rclone for Proton Drive."
    info "Make sure you've already logged into Proton Drive via a browser"
    info "(encryption keys must be generated first)."
    echo ""
    info "When prompted for storage type, choose: protondrive"
    echo ""

    read -rp "$(echo -e "${BOLD}Press Enter to start rclone config...${NC}")" _
    rclone config

    # Verify it worked
    if rclone listremotes 2>/dev/null | grep -q ":"; then
        local remote
        remote=$(rclone listremotes 2>/dev/null | head -1 | tr -d ':')
        ok "Remote configured: $remote"
        echo "$remote"
    else
        err "No rclone remote found after configuration"
        exit 1
    fi
}

# ─── Directory and config setup ───────────────────────────────────────

setup_directories() {
    header "Setting Up Directories"

    local mount_dir sync_dir

    read -rp "$(echo -e "${BLUE}Mount directory${NC} [$HOME/ProtonDrive]: ")" mount_dir
    mount_dir="${mount_dir:-$HOME/ProtonDrive}"

    read -rp "$(echo -e "${BLUE}Sync directory${NC} [$HOME/ProtonSync]: ")" sync_dir
    sync_dir="${sync_dir:-$HOME/ProtonSync}"

    mkdir -p "$mount_dir" "$sync_dir" "$CONFIG_DIR" "$LOG_DIR" "$BIN_DIR" "$SYSTEMD_DIR"

    ok "Created: $mount_dir"
    ok "Created: $sync_dir"
    ok "Created: $CONFIG_DIR"
    ok "Created: $LOG_DIR"

    # Detect the remote name
    local remote
    remote=$(rclone listremotes 2>/dev/null | grep -i proton | head -1 | tr -d ':')
    if [[ -z "$remote" ]]; then
        remote=$(rclone listremotes 2>/dev/null | head -1 | tr -d ':')
    fi
    remote="${remote:-protondrive}"

    # Write config
    cat > "$CONFIG_DIR/config.env" << EOF
# protondrive-linux configuration
# Generated $(date -Iseconds)

# ─── rclone remote ────────────────────────────────────────────
RCLONE_REMOTE="$remote"

# ─── Paths ────────────────────────────────────────────────────
MOUNT_DIR="$mount_dir"
SYNC_DIR="$sync_dir"
LOG_DIR="$LOG_DIR"

# ─── Mount options ────────────────────────────────────────────
VFS_CACHE_MODE="full"
VFS_CACHE_MAX_AGE="1h"
VFS_CACHE_MAX_SIZE="10G"
DIR_CACHE_TIME="5m"
POLL_INTERVAL="30s"
VFS_READ_AHEAD="128M"
BUFFER_SIZE="64M"
MOUNT_EXTRA_FLAGS=""

# ─── Sync options ─────────────────────────────────────────────
SYNC_INTERVAL_MIN=5
SYNC_CONFLICT_POLICY="newer"     # newer | larger | skip
SYNC_EXCLUDE_PATTERNS=".DS_Store,Thumbs.db,*.tmp,~*,.~lock.*,*.swp,*.swo"
SYNC_MAX_DELETE_PCT=50           # safety: abort if >50% of files would be deleted
SYNC_CHECKERS=8
SYNC_TRANSFERS=4

# ─── Duplicate handling ───────────────────────────────────────
DEDUP_HASH_ALGO="sha1"
DEDUP_ACTION="report"            # report | move-to-trash | delete-oldest
DEDUP_TRASH_DIR=".protondrive-trash"

# ─── File organizer ───────────────────────────────────────────
ORGANIZE_ENABLED=false
ORGANIZE_ON_SYNC=false
ORGANIZE_DRY_RUN=true

# ─── Rename ───────────────────────────────────────────────────
RENAME_DEFAULT_PATTERN="slugify"  # slugify | date-prefix | strip-copy | lowercase

# ─── Logging ──────────────────────────────────────────────────
LOG_MAX_SIZE_MB=50
LOG_RETAIN_DAYS=30
LOG_LEVEL="INFO"
EOF

    ok "Config written: $CONFIG_DIR/config.env"
}

# ─── Organization rules ───────────────────────────────────────────────

setup_organize_rules() {
    cat > "$CONFIG_DIR/organize-rules.yml" << 'EOF'
# File organization rules for pdrive organize
# Files are matched top-to-bottom; first match wins unless fallback: true

rules:
  - name: images
    match: "*.{jpg,jpeg,png,gif,webp,svg,heic,heif,bmp,tiff,raw,cr2,nef}"
    destination: "Media/Images"

  - name: screenshots
    match: "Screenshot*.{png,jpg}"
    destination: "Media/Screenshots"

  - name: photos
    match: "IMG_*.{jpg,jpeg,heic}"
    destination: "Media/Photos/{year}/{month}"

  - name: documents
    match: "*.{pdf,docx,doc,xlsx,xls,pptx,ppt,odt,ods,odp,txt,md,rtf,csv}"
    destination: "Documents"

  - name: ebooks
    match: "*.{epub,mobi,azw3}"
    destination: "Books"

  - name: videos
    match: "*.{mp4,mkv,avi,mov,webm,flv,wmv,m4v}"
    destination: "Media/Videos"

  - name: audio
    match: "*.{mp3,flac,ogg,wav,aac,m4a,opus,wma}"
    destination: "Media/Audio"

  - name: archives
    match: "*.{zip,tar,gz,bz2,xz,7z,rar,tar.gz,tar.bz2,tar.xz,tgz}"
    destination: "Archives"

  - name: code
    match: "*.{py,js,ts,jsx,tsx,rs,go,java,c,cpp,h,hpp,sh,bash,rb,php,sql}"
    destination: "Code"

  - name: config
    match: "*.{json,yml,yaml,toml,ini,conf,cfg,env}"
    destination: "Config"

  - name: fonts
    match: "*.{ttf,otf,woff,woff2}"
    destination: "Fonts"

  - name: by-date
    match: "*"
    destination: "Unsorted/{year}/{month}"
    fallback: true
EOF

    ok "Organization rules: $CONFIG_DIR/organize-rules.yml"
}

# ─── systemd units ────────────────────────────────────────────────────

install_systemd() {
    header "Installing Systemd Services"

    # Copy units
    for unit in "$SCRIPT_DIR"/systemd/*; do
        local name
        name=$(basename "$unit")
        # Replace placeholders
        sed \
            -e "s|{{SCRIPT_DIR}}|$SCRIPT_DIR/scripts|g" \
            -e "s|{{CONFIG_DIR}}|$CONFIG_DIR|g" \
            -e "s|{{USER}}|$USER|g" \
            "$unit" > "$SYSTEMD_DIR/$name"
        ok "Installed: $name"
    done

    systemctl --user daemon-reload
    ok "systemd daemon reloaded"

    echo ""
    if confirm "Enable auto-mount on login?"; then
        systemctl --user enable protondrive-mount.service
        ok "Auto-mount enabled"
    fi

    if confirm "Enable scheduled sync (every 5 min)?"; then
        systemctl --user enable --now protondrive-sync.timer
        ok "Sync timer enabled"
    fi
}

# ─── Install pdrive CLI ──────────────────────────────────────────────

install_cli() {
    header "Installing pdrive CLI"

    cp "$SCRIPT_DIR/scripts/pdrive" "$BIN_DIR/pdrive"
    chmod +x "$BIN_DIR/pdrive"
    ok "Installed: $BIN_DIR/pdrive"

    # Ensure ~/.local/bin is in PATH
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        warn "$HOME/.local/bin is not in your PATH"
        info "Add this to your ~/.bashrc or ~/.zshrc:"
        echo '  export PATH="$HOME/.local/bin:$PATH"'
    fi
}

# ─── Verify ──────────────────────────────────────────────────────────

verify_setup() {
    header "Verifying Setup"

    local remote
    # shellcheck source=/dev/null
    source "$CONFIG_DIR/config.env"
    remote="$RCLONE_REMOTE"

    info "Testing connection to $remote..."
    if rclone lsd "$remote:" --max-depth 0 &>/dev/null; then
        ok "Successfully connected to Proton Drive"
    else
        warn "Could not connect — check rclone config and credentials"
        info "You can re-run: rclone config"
    fi

    # Check fuse group
    if ! groups "$USER" | grep -qw fuse; then
        if getent group fuse &>/dev/null; then
            warn "You're not in the 'fuse' group. Mounting may fail."
            if confirm "Add yourself to the fuse group?"; then
                sudo usermod -aG fuse "$USER"
                ok "Added to fuse group — log out and back in for it to take effect"
            fi
        fi
    fi
}

# ─── Main ─────────────────────────────────────────────────────────────

main() {
    header "protondrive-linux installer"

    check_deps
    setup_rclone
    setup_directories
    setup_organize_rules
    install_systemd
    install_cli
    verify_setup

    header "Installation Complete"

    echo -e "  ${GREEN}✓${NC} rclone configured for Proton Drive"
    echo -e "  ${GREEN}✓${NC} Config: $CONFIG_DIR/config.env"
    echo -e "  ${GREEN}✓${NC} CLI:    pdrive --help"
    echo ""
    echo -e "  ${BOLD}Get started:${NC}"
    echo "    pdrive mount        # mount Proton Drive"
    echo "    pdrive sync         # run first sync"
    echo "    pdrive status       # check everything"
    echo ""
}

main "$@"
