#!/bin/sh
# One-time setup inside the container: a passphrase-less GPG key for `pass`,
# so the Proton Drive CLI can store its session without a desktop keyring.
# Run: docker compose run --rm proton-migrate sh /app/setup.sh
set -eu

KEY_NAME="proton-migrate"
mkdir -p "$HOME/.gnupg"
chmod 700 "$HOME/.gnupg"

if ! gpg --batch --list-secret-keys "$KEY_NAME" >/dev/null 2>&1; then
    gpg --batch --passphrase '' --quick-generate-key "$KEY_NAME" default default never
fi
if [ ! -d "$HOME/.password-store" ]; then
    pass init "$KEY_NAME"
fi
echo "pass store ready. Next: docker compose run --rm proton-migrate proton-drive auth login"
