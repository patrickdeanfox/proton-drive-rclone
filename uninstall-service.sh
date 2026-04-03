#!/usr/bin/env bash
#
# uninstall-service.sh — Remove the protondrive-webapp systemd service
#
set -euo pipefail

SERVICE_NAME="protondrive-webapp"

echo "==> Uninstalling ${SERVICE_NAME} service..."

sudo systemctl stop    "${SERVICE_NAME}.service" 2>/dev/null || true
sudo systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload

echo "✅  ${SERVICE_NAME} service removed."
echo "    Note: The Python venv at ./venv was left in place."
echo "    To remove it: rm -rf ./venv"
