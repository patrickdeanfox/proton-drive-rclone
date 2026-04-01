#!/usr/bin/env bash
# Find duplicate files by hash or name
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

ACTION="${DEDUP_ACTION:-report}"
SCOPE="local"
NAME_ONLY=false
TARGET_DIR="$SYNC_DIR"
TRASH_DIR="$SYNC_DIR/${DEDUP_TRASH_DIR:-.protondrive-trash}"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ─── Parse args ────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --action)    ACTION="$2"; shift ;;
        --scope)     SCOPE="$2"; shift ;;
        --name-only) NAME_ONLY=true ;;
        --path)      TARGET_DIR="$2"; shift ;;
        --help|-h)
            echo "Usage: pdrive duplicates [--action report|move|delete] [--scope local|remote] [--name-only] [--path DIR]"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ─── Remote scan ───────────────────────────────────────────────────────

if [[ "$SCOPE" == "remote" ]]; then
    echo -e "${BOLD}Scanning remote for duplicates...${NC}"
    echo -e "${DIM}(this may take a while depending on file count)${NC}"
    echo ""

    # Use rclone dedupe in dry-run mode for reporting
    case "$ACTION" in
        report)
            rclone dedupe --dedupe-mode list "$RCLONE_REMOTE:" 2>&1
            ;;
        move)
            echo -e "${YELLOW}Remote dedup will rename duplicates (rclone dedupe --dedupe-mode rename)${NC}"
            rclone dedupe --dedupe-mode rename "$RCLONE_REMOTE:" 2>&1
            ;;
        delete)
            echo -e "${RED}Deleting oldest duplicates on remote...${NC}"
            rclone dedupe --dedupe-mode oldest "$RCLONE_REMOTE:" 2>&1
            ;;
    esac
    exit 0
fi

# ─── Local scan ────────────────────────────────────────────────────────

if [[ ! -d "$TARGET_DIR" ]]; then
    echo "Target directory does not exist: $TARGET_DIR"
    exit 1
fi

echo -e "${BOLD}Scanning for duplicates in $TARGET_DIR...${NC}"
echo ""

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

if $NAME_ONLY; then
    # Group by filename only
    find "$TARGET_DIR" -type f -not -path "*/$DEDUP_TRASH_DIR/*" -printf '%f\t%p\n' \
        | sort | uniq -d -w 1000 > "$TMPFILE"

    # Better approach: group by basename
    declare -A name_map
    while IFS= read -r -d '' filepath; do
        basename=$(basename "$filepath")
        name_map["$basename"]+="$filepath"$'\n'
    done < <(find "$TARGET_DIR" -type f -not -path "*/$DEDUP_TRASH_DIR/*" -print0)

    dup_count=0
    dup_size=0

    for name in "${!name_map[@]}"; do
        files="${name_map[$name]}"
        count=$(echo -n "$files" | grep -c '^')
        if [[ $count -gt 1 ]]; then
            ((dup_count += count - 1))
            echo -e "${YELLOW}Duplicate name:${NC} $name ($count copies)"
            while IFS= read -r f; do
                [[ -z "$f" ]] && continue
                size=$(stat --format=%s "$f" 2>/dev/null || echo 0)
                mod=$(stat --format=%y "$f" 2>/dev/null | cut -d. -f1)
                echo -e "  ${DIM}$f${NC}  (${size} bytes, $mod)"
                ((dup_size += size))
            done <<< "$files"
            echo ""
        fi
    done

else
    # Group by SHA1 hash
    echo -e "${DIM}Computing hashes...${NC}"

    declare -A hash_map

    while IFS= read -r -d '' filepath; do
        hash=$(sha1sum "$filepath" 2>/dev/null | cut -d' ' -f1)
        [[ -z "$hash" ]] && continue
        hash_map["$hash"]+="$filepath"$'\n'
    done < <(find "$TARGET_DIR" -type f -not -path "*/$DEDUP_TRASH_DIR/*" -print0)

    dup_count=0
    dup_size=0
    group_count=0

    for hash in "${!hash_map[@]}"; do
        files="${hash_map[$hash]}"
        count=$(echo -n "$files" | grep -c '^')
        if [[ $count -gt 1 ]]; then
            ((group_count++))
            ((dup_count += count - 1))
            echo -e "${YELLOW}Duplicate set${NC} [${hash:0:12}...] — $count copies:"

            first=true
            while IFS= read -r f; do
                [[ -z "$f" ]] && continue
                size=$(stat --format=%s "$f" 2>/dev/null || echo 0)
                mod=$(stat --format=%y "$f" 2>/dev/null | cut -d. -f1)

                if $first; then
                    echo -e "  ${GREEN}KEEP${NC}  $f  (${size} bytes, $mod)"
                    first=false
                else
                    echo -e "  ${RED}DUP${NC}   $f  (${size} bytes, $mod)"
                    ((dup_size += size))

                    # Handle action
                    case "$ACTION" in
                        move)
                            rel="${f#$TARGET_DIR/}"
                            dest="$TRASH_DIR/$rel"
                            mkdir -p "$(dirname "$dest")"
                            mv "$f" "$dest"
                            echo -e "        ${DIM}→ moved to $DEDUP_TRASH_DIR/${NC}"
                            ;;
                        delete)
                            rm "$f"
                            echo -e "        ${DIM}→ deleted${NC}"
                            ;;
                    esac
                fi
            done <<< "$files"
            echo ""
        fi
    done
fi

# ─── Summary ──────────────────────────────────────────────────────────

echo -e "${BOLD}━━━ Summary ━━━${NC}"
echo -e "Duplicate files:  $dup_count"
if [[ $dup_size -gt 0 ]]; then
    human_size=$(numfmt --to=iec "$dup_size" 2>/dev/null || echo "${dup_size} bytes")
    echo -e "Wasted space:     $human_size"
fi
echo -e "Action taken:     $ACTION"
