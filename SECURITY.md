# Security Model

## Overview

proton-drive-rclone is a **local-only** desktop application that manages file syncing between a Linux PC and Proton Drive via rclone. It is designed to run on a single-user workstation and is not intended to be exposed to untrusted networks.

## Architecture

```
User (browser) ──HTTP──▶ Flask webapp (127.0.0.1:5000)
                              │
                              ├──▶ rclone (subprocess, list args)
                              │       └──▶ Proton Drive API (TLS)
                              │
                              ├──▶ Shell scripts (allowlisted)
                              │
                              └──▶ Local filesystem (sandboxed paths)
```

## Data Flow & Storage

### What data is stored

| Data | Location | Permissions | Sensitivity |
|------|----------|-------------|-------------|
| rclone config (credentials) | `~/.config/rclone/rclone.conf` | 0600 (set by rclone) | HIGH — contains encrypted Proton credentials |
| App config | `~/.config/protondrive-linux/config.env` | 0644 | LOW — paths, sync preferences |
| Sync configs | `~/.local/share/protondrive-linux/webapp/sync_configs.json` | 0644 | LOW — folder paths and settings |
| Schedules | `~/.local/share/protondrive-linux/webapp/schedules.json` | 0644 | LOW — schedule timing |
| Sync history | `~/.local/share/protondrive-linux/webapp/sync_history.json` | 0644 | LOW — timestamps and status |
| Sync logs | `~/.local/share/protondrive-linux/logs/sync.log` | 0644 | MEDIUM — may contain filenames |
| Lock file | `$XDG_RUNTIME_DIR/protondrive-sync.lock` | 0644 | NONE — PID only |

### What data is NOT stored

- Proton Drive passwords or tokens (managed exclusively by rclone)
- File contents (never cached by the webapp; rclone handles transfers directly)
- Authentication tokens for the webapp (there is no authentication layer — see below)

## Security Controls

### Network Binding

The webapp binds to **127.0.0.1** (localhost) by default. It is not accessible from other machines on the network. To change this, pass `--host 0.0.0.0` explicitly — but only do so on trusted networks and behind a firewall.

### Subprocess Security

- All rclone invocations use **list-form arguments** (no shell=True), preventing shell injection.
- Only **allowlisted rclone subcommands** can be executed: `version`, `listremotes`, `lsd`, `lsjson`, `about`, `config`, `bisync`, `sync`.
- Only **allowlisted shell scripts** can be run: `mount.sh`, `unmount.sh`, `health.sh`.
- Subprocesses receive a **minimal environment** (PATH, HOME, USER, locale vars only). Secrets from the parent environment are not propagated.

### Input Validation

- **Local paths** are resolved via `os.path.realpath()` and blocked from accessing `/proc`, `/sys`, `/dev`, `/boot`, `/sbin`.
- **Remote paths** are validated to reject shell metacharacters (`;`, `|`, `$`, backticks, etc.).
- **Remote names** must match `^[A-Za-z0-9_-]+$`.
- **Config keys** are validated against an allowlist. Config values are sanitized to prevent newline injection.
- **Cron expressions** are validated for format (5 fields, safe characters only).
- **Integer parameters** (log lines, poll offsets) are bounded to prevent DoS.

### XSS Prevention

- All user-supplied data rendered in HTML templates is escaped via `escHtml()` (DOM-based text escaping).
- Dynamic onclick attributes have been replaced with `data-*` attributes and safe event binding.
- Error messages displayed in the UI are HTML-escaped.

### Encryption

- Proton Drive uses **end-to-end encryption**. Files are encrypted client-side by the Proton Drive backend in rclone before upload. The webapp never handles encryption directly.
- rclone's Proton Drive backend uses **OpenPGP encryption** (via Proton's libraries). No operation in this app bypasses or weakens this encryption.
- All Proton Drive API communication uses **TLS 1.2+** (enforced by rclone).

## Known Limitations

1. **No authentication**: The webapp has no login system. Anyone with access to localhost:5000 can manage syncs. This is acceptable for a single-user desktop application but would need to be addressed before any network exposure.

2. **No CSRF protection**: Since the app is localhost-only, CSRF risk is limited to same-origin attacks from malicious pages. A CSRF token system should be added before any network deployment.

3. **File path visibility**: The browse API can list any directory the user has read access to (except blocked system paths). This is by design for a local file manager but should be restricted if multi-user access is ever considered.

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT** open a public GitHub issue for security vulnerabilities.
2. Email the maintainer directly with a description of the vulnerability, steps to reproduce, and potential impact.
3. Allow reasonable time for a fix before public disclosure.

## Security Audit History

| Date | Scope | Findings | Status |
|------|-------|----------|--------|
| 2026-04 | Full codebase audit | Path traversal, XSS, env leakage, shell injection vectors, insecure defaults | All fixed |
