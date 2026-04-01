# protondrive-linux

A complete toolkit for managing Proton Drive on Linux via rclone — mounting, syncing, deduplication, file organization, and scheduled automation.

## Why?

Proton Drive has no official Linux desktop client. This project wraps `rclone` with quality-of-life scripts and systemd services to give you a proper sync-and-mount workflow without babysitting anything.

## Features

- **One-command setup** — interactive installer configures rclone, directories, and systemd units
- **FUSE mount** — browse Proton Drive as a local folder in your file manager
- **Scheduled bisync** — two-way sync on a timer (default: every 5 minutes)
- **File watcher** — instant sync on local changes via inotify
- **Duplicate finder** — detect and handle duplicate files by hash or name
- **File organizer** — auto-sort files into folders by type, date, or custom rules
- **Bulk renamer** — normalize filenames (slugify, date-prefix, strip junk)
- **Snapshots & restore** — incremental backups with hardlinks, one-command restore
- **Cache cleanup** — reclaim space from VFS cache, old logs, and dedup trash
- **Desktop notifications** — notify-send alerts on sync events
- **Health checks** — connection test, quota usage, mount status dashboard
- **Shell completions** — tab-complete for bash and zsh
- **Logging** — all operations logged with rotation

## Requirements

- Linux (tested on Ubuntu 22.04+, Fedora 38+, Arch)
- `rclone` ≥ 1.64.0 (Proton Drive backend)
- `fuse3` (for mounting)
- A Proton account with Drive enabled and **encryption keys already generated** (log in via browser first)

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/protondrive-linux.git
cd protondrive-linux
chmod +x install.sh
./install.sh
```

The installer will:
1. Check/install dependencies (rclone, fuse3, jq)
2. Walk you through `rclone config` for Proton Drive
3. Create local sync and mount directories
4. Install systemd user services and timers
5. Optionally enable auto-mount on login and scheduled sync

## Usage

All commands are available via the main `pdrive` CLI wrapper:

```bash
pdrive mount          # Mount Proton Drive to ~/ProtonDrive
pdrive unmount        # Unmount cleanly
pdrive sync           # Run a one-shot bisync
pdrive sync --dry-run # Preview what would change
pdrive watch          # Watch for changes, sync instantly
pdrive status         # Show mount, sync, quota info
pdrive duplicates     # Scan for duplicate files
pdrive organize       # Sort files by type/date rules
pdrive rename         # Bulk rename with pattern
pdrive backup         # Create a snapshot
pdrive backup --list  # List snapshots
pdrive backup --restore latest  # Restore most recent snapshot
pdrive cleanup        # Clean caches, logs, trash
pdrive logs           # Tail the sync log
pdrive health         # Full health check
pdrive config edit    # Open config in $EDITOR
```

## Directory Layout

```
~/.config/protondrive-linux/
├── config.env            # All user settings
├── organize-rules.yml    # File organization rules
└── rename-patterns.yml   # Bulk rename patterns

~/ProtonDrive/            # FUSE mount point (read-through to cloud)
~/ProtonSync/             # Local bisync folder (offline copy)

~/.local/share/protondrive-linux/
└── logs/                 # Operation logs
```

## Configuration

All settings live in `~/.config/protondrive-linux/config.env`:

```bash
# rclone remote name (set during install)
RCLONE_REMOTE="protondrive"

# Paths
MOUNT_DIR="$HOME/ProtonDrive"
SYNC_DIR="$HOME/ProtonSync"
LOG_DIR="$HOME/.local/share/protondrive-linux/logs"

# Mount options
VFS_CACHE_MODE="full"
VFS_CACHE_MAX_AGE="1h"
DIR_CACHE_TIME="5m"
POLL_INTERVAL="30s"
VFS_READ_AHEAD="128M"
MOUNT_EXTRA_FLAGS=""

# Sync options
SYNC_INTERVAL_MIN=5
SYNC_CONFLICT_POLICY="newer"   # newer | larger | skip
SYNC_EXCLUDE_PATTERNS=".DS_Store,Thumbs.db,*.tmp,~*"

# Duplicate handling
DEDUP_HASH_ALGO="sha1"
DEDUP_ACTION="report"          # report | move-to-trash | delete-oldest

# Organize rules
ORGANIZE_ENABLED=false
ORGANIZE_ON_SYNC=false

# Logging
LOG_MAX_SIZE_MB=50
LOG_RETAIN_DAYS=30
```

## Systemd Services

The installer creates user-level systemd units:

| Unit | Purpose |
|------|---------|
| `protondrive-mount.service` | FUSE mount on login |
| `protondrive-sync.service` | One-shot bisync |
| `protondrive-sync.timer` | Triggers sync on schedule |
| `protondrive-organize.service` | File organization pass |

```bash
# Manual control
systemctl --user start protondrive-mount
systemctl --user enable protondrive-sync.timer
journalctl --user -u protondrive-sync -f
```

## Duplicate Finder

Scans your sync directory (or remote) for duplicates by SHA1 hash:

```bash
pdrive duplicates                    # Report only
pdrive duplicates --action move      # Move dupes to .protondrive-trash/
pdrive duplicates --scope remote     # Scan remote directly
pdrive duplicates --name-only        # Match by filename, not hash
```

## File Organizer

Sorts files into subfolders based on rules in `organize-rules.yml`:

```yaml
rules:
  - name: images
    match: "*.{jpg,jpeg,png,gif,webp,svg,heic}"
    destination: "Media/Images"
  - name: documents
    match: "*.{pdf,docx,doc,xlsx,odt,txt,md}"
    destination: "Documents"
  - name: videos
    match: "*.{mp4,mkv,avi,mov,webm}"
    destination: "Media/Videos"
  - name: audio
    match: "*.{mp3,flac,ogg,wav,aac,m4a}"
    destination: "Media/Audio"
  - name: archives
    match: "*.{zip,tar,gz,bz2,xz,7z,rar}"
    destination: "Archives"
  - name: code
    match: "*.{py,js,ts,rs,go,java,c,cpp,h,sh}"
    destination: "Code"
  - name: by-date
    match: "*"
    destination: "Unsorted/{year}/{month}"
    fallback: true
```

## Bulk Renamer

```bash
pdrive rename --pattern slugify       # my File (1).PDF → my-file-1.pdf
pdrive rename --pattern date-prefix   # report.pdf → 2026-04-01_report.pdf
pdrive rename --pattern strip-copy    # file (copy).txt → file.txt
pdrive rename --dry-run               # Preview changes
pdrive rename --path Documents/       # Target specific folder
```

## File Watcher

Watches your sync folder for changes and triggers a sync after a short debounce. Requires `inotify-tools`.

```bash
pdrive watch       # Foreground, Ctrl+C to stop
```

Set `WATCH_DEBOUNCE_SEC` in config to control how long to wait after a change before syncing (default: 10s). The watcher sends desktop notifications on sync completion if `notify-send` is available.

## Snapshots & Backup

Space-efficient incremental backups using rsync hardlinks — only changed files consume extra disk space.

```bash
pdrive backup              # Create a new snapshot
pdrive backup --list       # List all snapshots with sizes
pdrive backup --restore latest    # Restore most recent
pdrive backup --restore 20260401-153022   # Restore specific
pdrive backup --prune      # Delete old snapshots beyond limit
pdrive backup --keep 5     # Keep only 5 most recent
```

Before any restore, a safety backup of the current state is automatically created. Set `BACKUP_MAX_KEEP` in config (default: 10).

## Cache Cleanup

Reclaim disk space from VFS cache, rotated logs, dedup trash, and empty directories.

```bash
pdrive cleanup             # Clean expired items
pdrive cleanup --dry-run   # Preview what would be cleaned
pdrive cleanup --all       # Also clear ALL rclone VFS cache
```

## Shell Completions

Tab-complete all commands and options:

```bash
# Bash — add to .bashrc
source /path/to/protondrive-linux/completions/pdrive.bash

# Or install globally
make completions

# Zsh — copy to fpath
cp completions/_pdrive ~/.zsh/completions/
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `didn't find section in config file` | Run `rclone config` and verify remote name matches `config.env` |
| Mount hangs or is empty | Ensure encryption keys exist — log into Proton Drive in a browser first |
| 2FA errors | Use `--protondrive-2fa` or set the OTP secret key in rclone config |
| Stale files after external edit | rclone's Proton backend doesn't support the event API yet; restart mount or wait for cache expiry |
| `fusermount: permission denied` | Add yourself to the `fuse` group: `sudo usermod -aG fuse $USER` then re-login |

## Contributing

PRs welcome. Please open an issue first for major changes.

## License

MIT
