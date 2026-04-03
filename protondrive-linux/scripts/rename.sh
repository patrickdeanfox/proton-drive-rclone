#!/usr/bin/env bash
# Bulk rename files with various patterns
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

PATTERN="${RENAME_DEFAULT_PATTERN:-slugify}"
TARGET_DIR="$SYNC_DIR"
DRY_RUN=false
RECURSIVE=false

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ─── Parse args ────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pattern)   PATTERN="$2"; shift ;;
        --path)
            # Sanitize: resolve and ensure it stays under SYNC_DIR
            local subpath
            subpath="$(realpath -m "$SYNC_DIR/$2")"
            if [[ "$subpath" != "$SYNC_DIR"* ]]; then
                echo "Error: path must be within $SYNC_DIR" >&2
                exit 1
            fi
            TARGET_DIR="$subpath"
            shift ;;
        --dry-run)   DRY_RUN=true ;;
        --recursive) RECURSIVE=true ;;
        --help|-h)
            cat << 'EOF'
Usage: pdrive rename [--pattern PATTERN] [--path DIR] [--dry-run] [--recursive]

Patterns:
  slugify       my File (1).PDF   → my-file-1.pdf
  date-prefix   report.pdf        → 2026-04-01_report.pdf
  strip-copy    file (copy).txt   → file.txt
  lowercase     MyFile.TXT        → myfile.txt
  strip-spaces  my file name.txt  → my_file_name.txt
  sequential    *.jpg             → 001.jpg, 002.jpg, ...
EOF
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ─── Transform functions ──────────────────────────────────────────────

transform_slugify() {
    local name="$1"
    local base="${name%.*}"
    local ext="${name##*.}"

    # If no extension, treat whole thing as base
    [[ "$base" == "$ext" ]] && ext=""

    base=$(echo "$base" | tr '[:upper:]' '[:lower:]')  # lowercase
    base=$(echo "$base" | sed 's/[()]//g')              # remove parens
    base=$(echo "$base" | sed 's/[^a-z0-9._-]/-/g')    # non-alnum → dash
    base=$(echo "$base" | sed 's/--*/-/g')              # collapse dashes
    base=$(echo "$base" | sed 's/^-//;s/-$//')          # trim dashes

    if [[ -n "$ext" ]]; then
        ext=$(echo "$ext" | tr '[:upper:]' '[:lower:]')
        echo "${base}.${ext}"
    else
        echo "$base"
    fi
}

transform_date_prefix() {
    local filepath="$1"
    local name="$2"

    local mod_date
    mod_date=$(stat --format=%y "$filepath" 2>/dev/null | cut -d' ' -f1)

    # Skip if already date-prefixed
    if [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_ ]]; then
        echo "$name"
        return
    fi

    echo "${mod_date}_${name}"
}

transform_strip_copy() {
    local name="$1"
    # Remove common copy suffixes: " (copy)", " (1)", " - Copy", " copy 2", etc.
    name=$(echo "$name" | sed -E 's/ \(copy\)//gi')
    name=$(echo "$name" | sed -E 's/ \([0-9]+\)//g')
    name=$(echo "$name" | sed -E 's/ - Copy//gi')
    name=$(echo "$name" | sed -E 's/ copy [0-9]*//gi')
    name=$(echo "$name" | sed -E 's/ \(copy [0-9]+\)//gi')
    echo "$name"
}

transform_lowercase() {
    echo "$1" | tr '[:upper:]' '[:lower:]'
}

transform_strip_spaces() {
    echo "$1" | sed 's/ /_/g'
}

# ─── Main ──────────────────────────────────────────────────────────────

main() {
    if [[ ! -d "$TARGET_DIR" ]]; then
        echo "Directory not found: $TARGET_DIR"
        exit 1
    fi

    echo -e "${BOLD}Bulk rename — pattern: $PATTERN${NC}"
    echo -e "Target: $TARGET_DIR"
    $DRY_RUN && echo -e "${YELLOW}DRY RUN — no files will be renamed${NC}"
    echo ""

    local find_args=(-type f)
    if ! $RECURSIVE; then
        find_args=(-maxdepth 1 -type f)
    fi

    local renamed=0
    local skipped=0
    local seq=1

    while IFS= read -r -d '' filepath; do
        local dir
        dir=$(dirname "$filepath")
        local name
        name=$(basename "$filepath")
        local new_name

        # Skip hidden files
        [[ "$name" == .* ]] && continue

        case "$PATTERN" in
            slugify)       new_name=$(transform_slugify "$name") ;;
            date-prefix)   new_name=$(transform_date_prefix "$filepath" "$name") ;;
            strip-copy)    new_name=$(transform_strip_copy "$name") ;;
            lowercase)     new_name=$(transform_lowercase "$name") ;;
            strip-spaces)  new_name=$(transform_strip_spaces "$name") ;;
            sequential)
                local ext="${name##*.}"
                [[ "$ext" == "$name" ]] && ext=""
                if [[ -n "$ext" ]]; then
                    new_name=$(printf "%03d.%s" "$seq" "$ext")
                else
                    new_name=$(printf "%03d" "$seq")
                fi
                ((seq++))
                ;;
            *)
                echo "Unknown pattern: $PATTERN"
                exit 1 ;;
        esac

        # Skip if no change
        if [[ "$name" == "$new_name" ]]; then
            ((skipped++))
            continue
        fi

        # Handle collision
        local target="$dir/$new_name"
        if [[ -f "$target" && "$filepath" != "$target" ]]; then
            local base="${new_name%.*}"
            local ext="${new_name##*.}"
            local n=1
            while [[ -f "$dir/${base}_${n}.${ext}" ]]; do
                ((n++))
            done
            new_name="${base}_${n}.${ext}"
            target="$dir/$new_name"
        fi

        echo -e "  ${DIM}$name${NC} → ${GREEN}$new_name${NC}"

        if ! $DRY_RUN; then
            mv "$filepath" "$target"
        fi

        ((renamed++))

    done < <(find "$TARGET_DIR" "${find_args[@]}" -print0 | sort -z)

    echo ""
    echo -e "${BOLD}━━━ Summary ━━━${NC}"
    echo "Renamed:  $renamed"
    echo "Skipped:  $skipped (already correct)"
    $DRY_RUN && echo -e "${YELLOW}Run without --dry-run to apply changes${NC}"
}

main
