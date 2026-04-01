.PHONY: install uninstall check help

PREFIX ?= $(HOME)/.local
SHELL := /bin/bash

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[32m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Run the interactive installer
	@chmod +x install.sh
	@./install.sh

uninstall: ## Uninstall everything
	@chmod +x uninstall.sh
	@./uninstall.sh

check: ## Verify dependencies and config
	@chmod +x scripts/health.sh
	@bash scripts/health.sh

completions: ## Install shell completions
	@mkdir -p $(HOME)/.local/share/bash-completion/completions
	@cp completions/pdrive.bash $(HOME)/.local/share/bash-completion/completions/pdrive
	@echo "Bash completions installed."
	@if [ -d "$(HOME)/.zsh/completions" ]; then \
		cp completions/_pdrive $(HOME)/.zsh/completions/_pdrive; \
		echo "Zsh completions installed."; \
	else \
		echo "Zsh: copy completions/_pdrive to a directory in your \$$fpath"; \
	fi

lint: ## Check scripts with shellcheck
	@if command -v shellcheck &>/dev/null; then \
		shellcheck scripts/*.sh install.sh uninstall.sh; \
		echo "All scripts passed shellcheck."; \
	else \
		echo "shellcheck not installed"; \
	fi

test-dry: ## Dry-run sync and organize to verify config
	@bash scripts/pdrive sync --dry-run
	@bash scripts/pdrive organize --dry-run

logs: ## Tail sync log
	@bash scripts/pdrive logs
