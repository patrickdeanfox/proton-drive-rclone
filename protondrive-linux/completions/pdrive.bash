# Bash completion for pdrive
# Source this in .bashrc: source /path/to/completions/pdrive.bash
# Or copy to /etc/bash_completion.d/pdrive

_pdrive() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    commands="mount unmount sync status duplicates organize rename backup cleanup watch logs health config help version"

    case "$prev" in
        pdrive)
            COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
            return 0
            ;;
        sync)
            COMPREPLY=( $(compgen -W "--dry-run --force --one-way --resync" -- "$cur") )
            return 0
            ;;
        duplicates|dedup)
            COMPREPLY=( $(compgen -W "--action --scope --name-only --path --help" -- "$cur") )
            return 0
            ;;
        --action)
            COMPREPLY=( $(compgen -W "report move delete" -- "$cur") )
            return 0
            ;;
        --scope)
            COMPREPLY=( $(compgen -W "local remote" -- "$cur") )
            return 0
            ;;
        organize|org)
            COMPREPLY=( $(compgen -W "--dry-run --no-dry-run --rules --path --help" -- "$cur") )
            return 0
            ;;
        rename)
            COMPREPLY=( $(compgen -W "--pattern --path --dry-run --recursive --help" -- "$cur") )
            return 0
            ;;
        --pattern)
            COMPREPLY=( $(compgen -W "slugify date-prefix strip-copy lowercase strip-spaces sequential" -- "$cur") )
            return 0
            ;;
        backup)
            COMPREPLY=( $(compgen -W "--list --restore --prune --keep --dir --help" -- "$cur") )
            return 0
            ;;
        --restore|-r)
            # Complete with snapshot names
            local backup_dir="$HOME/.local/share/protondrive-linux/backups"
            if [[ -d "$backup_dir" ]]; then
                local snaps
                snaps=$(ls -1 "$backup_dir" 2>/dev/null)
                COMPREPLY=( $(compgen -W "$snaps latest" -- "$cur") )
            fi
            return 0
            ;;
        cleanup)
            COMPREPLY=( $(compgen -W "--dry-run --all --help" -- "$cur") )
            return 0
            ;;
        mount)
            COMPREPLY=( $(compgen -W "--foreground" -- "$cur") )
            return 0
            ;;
        config|cfg)
            COMPREPLY=( $(compgen -W "edit path" -- "$cur") )
            return 0
            ;;
        --path|--rules|--dir)
            # Complete directories
            COMPREPLY=( $(compgen -d -- "$cur") )
            return 0
            ;;
    esac

    COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
    return 0
}

complete -F _pdrive pdrive
