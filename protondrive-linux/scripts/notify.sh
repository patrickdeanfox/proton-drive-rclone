#!/usr/bin/env bash
# Desktop notification wrapper for protondrive events
# Supports notify-send (freedesktop), kdialog, osascript
set -euo pipefail

TITLE="${1:-Proton Drive}"
MSG="${2:-}"
URGENCY="${3:-normal}"  # low | normal | critical
ICON="${4:-sync}"

if [[ -z "$MSG" ]]; then
    echo "Usage: notify.sh <title> <message> [urgency] [icon]"
    exit 1
fi

# Try notify-send (GNOME, XFCE, etc.)
if command -v notify-send &>/dev/null; then
    notify-send -u "$URGENCY" -i "$ICON" "$TITLE" "$MSG"
    exit 0
fi

# Try kdialog (KDE)
if command -v kdialog &>/dev/null; then
    kdialog --passivepopup "$MSG" 5 --title "$TITLE"
    exit 0
fi

# Try zenity
if command -v zenity &>/dev/null; then
    zenity --notification --text="$TITLE: $MSG" 2>/dev/null &
    exit 0
fi

# Fallback: terminal bell + stderr
echo -e "\a[$TITLE] $MSG" >&2
