#!/usr/bin/env bash
#
# install-service.sh — Install protondrive-webapp as a systemd user service
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="protondrive-webapp"
VENV_DIR="${SCRIPT_DIR}/venv"
USER_NAME="$(whoami)"

echo "==> Proton Drive Web App — Service Installer"
echo ""

# ── 1. Create / update Python virtual environment ──────────────────────────
echo "[1/4] Setting up Python virtual environment..."
if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    echo "      Created venv at ${VENV_DIR}"
else
    echo "      venv already exists at ${VENV_DIR}"
fi

# ── 2. Install dependencies ────────────────────────────────────────────────
echo "[2/4] Installing Python dependencies..."
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt" -q
echo "      Dependencies installed."

# ── 3. Generate and install systemd service file ───────────────────────────
echo "[3/4] Installing systemd service..."

# Generate a concrete service file from the template (replace %I with user)
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "${SERVICE_FILE}" > /dev/null <<EOF
[Unit]
Description=Proton Drive rclone Web Interface
After=network.target
Wants=network.target

[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
WorkingDirectory=${SCRIPT_DIR}
Environment=PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/${USER_NAME}
ExecStart=${VENV_DIR}/bin/python app.py
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

echo "      Service file written to ${SERVICE_FILE}"

# ── 4. Enable and start the service ────────────────────────────────────────
echo "[4/4] Enabling and starting the service..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl start  "${SERVICE_NAME}.service"

echo ""
echo "✅  ${SERVICE_NAME} installed and running!"
echo ""
echo "    Web UI:  http://localhost:5000"
echo ""
echo "    Useful commands:"
echo "      sudo systemctl status  ${SERVICE_NAME}"
echo "      sudo systemctl restart ${SERVICE_NAME}"
echo "      sudo systemctl stop    ${SERVICE_NAME}"
echo "      journalctl -u ${SERVICE_NAME} -f"
echo ""
