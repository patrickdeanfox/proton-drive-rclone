#!/usr/bin/env bash
# Organize files into folders by type/date based on rules
set -euo pipefail

source "$HOME/.config/protondrive-linux/config.env"

RULES_FILE="${1:-$HOME/.config/protondrive-linux/organize-rules.yml}"
DRY_RUN="${ORGANIZE_DRY_RUN:-true}"
TARGET_DIR="$SYNC_DIR"
LOG_FILE="$LOG_DIR/organize.log"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ─── Parse args ────────────────────────────────────────────────────────

shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true ;;
        --no-dry-run) DRY_RUN=false ;;
        --rules)      RULES_FILE="$2"; shift ;;
        --path)       TARGET_DIR="$2"; shift ;;
        --help|-h)
            echo "Usage: pdrive organize [--dry-run] [--rules FILE] [--path DIR]"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

log() { echo "[$(date -Iseconds)] $*" >> "$LOG_FILE"; }

# ─── Parse rules (simple YAML subset) ─────────────────────────────────

declare -a rule_names rule_patterns rule_destinations rule_fallbacks

parse_rules() {
    if [[ ! -f "$RULES_FILE" ]]; then
        echo "Rules file not found: $RULES_FILE"
        exit 1
    fi

    local idx=0
    local in_rule=false
    local name="" match="" dest="" fallback="false"

    while IFS= read -r line; do
        # Skip comments and blank lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// /}" ]] && continue

        if [[ "$line" =~ "- name:" ]]; then
            # Save previous rule
            if $in_rule && [[ -n "$match" && -n "$dest" ]]; then
                rule_names[$idx]="$name"
                rule_patterns[$idx]="$match"
                rule_destinations[$idx]="$dest"
                rule_fallbacks[$idx]="$fallback"
                ((idx++))
            fi
            name=$(echo "$line" | sed 's/.*- name:[[:space:]]*//' | tr -d '"' | tr -d "'")
            match="" dest="" fallback="false"
            in_rule=true
        elif [[ "$line" =~ "match:" ]]; then
            match=$(echo "$line" | sed 's/.*match:[[:space:]]*//' | tr -d '"' | tr -d "'")
        elif [[ "$line" =~ "destination:" ]]; then
            dest=$(echo "$line" | sed 's/.*destination:[[:space:]]*//' | tr -d '"' | tr -d "'")
        elif [[ "$line" =~ "fallback:" ]]; then
            fallback=$(echo "$line" | sed 's/.*fallback:[[:space:]]*//' | tr -d '"' | tr -d "'")
        fi
    done < "$RULES_FILE"

    # Save last rule
    if $in_rule && [[ -n "$match" && -n "$dest" ]]; then
        rule_names[$idx]="$name"
        rule_patterns[$idx]="$match"
        rule_destinations[$idx]="$dest"
        rule_fallbacks[$idx]="$fallback"
    fi
}

# ─── Match file against glob pattern ──────────────────────────────────

# Expand {a,b,c} brace patterns into separate globs
match_file() {
    local filename="$1"
    local pattern="$2"

    # Handle brace expansion: *.{jpg,png} → check each
    if [[ "$pattern" == *"{"*"}"* ]]; then
        local prefix="${pattern%%\{*}"
        local suffix="${pattern#*\}}"
        local braces="${pattern#*\{}"
        braces="${braces%%\}*}"

        IFS=',' read -ra exts <<< "$braces"
        for ext in "${exts[@]}"; do
            local expanded="${prefix}${ext}${suffix}"
            # shellcheck disable=SC2254
            case "${filename,,}" in
                ${expanded,,}) return 0 ;;
            esac
        done
        return 1
    else
        # Simple glob
        # shellcheck disable=SC2254
        case "${filename,,}" in
            ${pattern,,}) return 0 ;;
        esac
        return 1
    fi
}

# ─── Resolve destination with date placeholders ──────────────────────

resolve_dest() {
    local filepath="$1"
    local template="$2"

    local mod_date
    mod_date=$(stat --format=%y "$filepath" 2>/dev/null | cut -d' ' -f1)
    local year="${mod_date%%-*}"
    local month=$(echo "$mod_date" | cut -d- -f2)

    template="${template//\{year\}/$year}"
    template="${template//\{month\}/$month}"

    echo "$template"
}

# ─── Main ──────────────────────────────────────────────────────────────

main() {
    parse_rules

    echo -e "${BOLD}Organizing files in $TARGET_DIR${NC}"
    echo -e "Rules: ${#rule_names[@]} loaded from $RULES_FILE"
    $DRY_RUN && echo -e "${YELLOW}DRY RUN — no files will be moved${NC}"
    echo ""

    local moved=0 skipped=0

    while IFS= read -r -d '' filepath; do
        local filename
        filename=$(basename "$filepath")
        local relpath="${filepath#$TARGET_DIR/}"

        # Skip already-organized files (in known subdirs from rules)
        # Skip hidden files and trash
        [[ "$filename" == .* ]] && continue
        [[ "$relpath" == *".protondrive-trash"* ]] && continue

        # Only organize files in root or shallow dirs (skip deep nesting)
        local depth
        depth=$(echo "$relpath" | tr -cd '/' | wc -c)

        local matched=false

        for i in "${!rule_names[@]}"; do
            local pattern="${rule_patterns[$i]}"
            local dest="${rule_destinations[$i]}"
            local fallback="${rule_fallbacks[$i]}"

            # Skip fallback rules on first pass
            [[ "$fallback" == "true" ]] && continue

            if match_file "$filename" "$pattern"; then
                dest=$(resolve_dest "$filepath" "$dest")
                local target="$TARGET_DIR/$dest/$filename"

                if [[ "$filepath" == "$target" ]]; then
                    skipped=$((skipped + 1))
                    matched=true
                    break
                fi

                echo -e "  ${GREEN}${rule_names[$i]}${NC}: $relpath → $dest/"

                if ! $DRY_RUN; then
                    mkdir -p "$TARGET_DIR/$dest"
                    # Handle name collision
                    if [[ -f "$target" ]]; then
                        local base="${filename%.*}"
                        local ext="${filename##*.}"
                        local n=1
                        while [[ -f "$TARGET_DIR/$dest/${base}_${n}.${ext}" ]]; do
                            ((n++))
                        done
                        target="$TARGET_DIR/$dest/${base}_${n}.${ext}"
                    fi
                    mv "$filepath" "$target"
                    log "Moved: $relpath → $dest/$(basename "$target")"
                fi

                moved=$((moved + 1))
                matched=true
                break
            fi
        done

        # Try fallback rules if no match
        if ! $matched; then
            for i in "${!rule_names[@]}"; do
                [[ "${rule_fallbacks[$i]}" != "true" ]] && continue
                local dest="${rule_destinations[$i]}"
                dest=$(resolve_dest "$filepath" "$dest")
                local target="$TARGET_DIR/$dest/$filename"

                if [[ "$filepath" != "$target" ]]; then
                    echo -e "  ${DIM}${rule_names[$i]}${NC}: $relpath → $dest/"
                    if ! $DRY_RUN; then
                        mkdir -p "$TARGET_DIR/$dest"
                        mv "$filepath" "$target"
                        log "Moved (fallback): $relpath → $dest/$filename"
                    fi
                    moved=$((moved + 1))
                fi
                break
            done
        fi

    done < <(find "$TARGET_DIR" -maxdepth 1 -type f -print0)

    echo ""
    echo -e "${BOLD}━━━ Summary ━━━${NC}"
    echo "Files organized: $moved"
    echo "Files skipped:   $skipped"
    $DRY_RUN && echo -e "${YELLOW}Run with --no-dry-run to apply changes${NC}"
}

main
