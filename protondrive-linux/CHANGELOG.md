# Changelog

## 0.1.0 — 2026-04-01

Initial release.

### Features
- Interactive installer with dependency detection
- rclone FUSE mount with VFS caching and tunable options
- Two-way bisync with conflict resolution (newer/larger/skip)
- One-way sync mode (local → remote)
- Scheduled sync via systemd timer (configurable interval)
- File watcher with inotify for instant sync on changes
- Duplicate finder by SHA1 hash or filename, with report/move/delete actions
- File organizer with YAML-based rules (type, date, glob patterns)
- Bulk renamer (slugify, date-prefix, strip-copy, lowercase, sequential)
- Incremental snapshots with hardlink-based backups and one-command restore
- Cache cleanup for VFS cache, old logs, dedup trash, empty directories
- Desktop notifications via notify-send/kdialog/zenity
- Status dashboard showing mount, sync, quota, and systemd state
- Full health check (dependencies, config, connection, services, disk)
- Bash and Zsh tab completions
- Log rotation with configurable retention
- Sync locking to prevent concurrent runs
- Safety limits on max delete percentage
- Makefile for install/uninstall/lint/test
- Uninstall script with clean service teardown
