# Proton Drive rclone Web Interface

A local web-based UI for managing rclone syncs with Proton Drive.

## Features

- **Dashboard** — Real-time status overview of rclone, mount, sync, and remote connectivity
- **Sync Folders** — Configure which local and remote folders to sync with an interactive folder picker
- **Schedules** — Set up recurring sync jobs (interval, daily, or cron-based)
- **File Browser** — Side-by-side view of local and Proton Drive files for easy comparison

## Prerequisites

- Python 3.8+
- `rclone` installed and configured with a Proton Drive remote
- The `protondrive-linux` scripts (included in this repository)

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the web app
python app.py
```

Then open http://localhost:5000 in your browser.

## Architecture

```
webapp/
├── app.py                          # Flask application (routes, API, scheduler)
├── requirements.txt                # Python dependencies
├── install-service.sh              # Installs the systemd service
├── uninstall-service.sh            # Removes the systemd service
├── protondrive-webapp.service      # systemd unit file (template)
├── static/
│   ├── css/style.css               # Dark theme UI styles
│   └── js/app.js                   # Frontend logic (vanilla JS)
└── templates/
    ├── base.html                    # Layout with sidebar navigation
    ├── index.html                   # Dashboard page
    ├── folders.html                 # Sync configuration page
    ├── schedules.html               # Schedule management page
    └── browser.html                 # Dual-pane file browser
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | System status overview |
| GET | `/api/sync-configs` | List sync configurations |
| POST | `/api/sync-configs` | Create sync configuration |
| PUT | `/api/sync-configs/:id` | Update sync configuration |
| DELETE | `/api/sync-configs/:id` | Delete sync configuration |
| POST | `/api/sync-configs/:id/run` | Trigger immediate sync |
| GET | `/api/schedules` | List schedules |
| POST | `/api/schedules` | Create schedule |
| PUT | `/api/schedules/:id` | Update schedule |
| DELETE | `/api/schedules/:id` | Delete schedule |
| POST | `/api/schedules/:id/toggle` | Enable/disable schedule |
| GET | `/api/sync-history` | Recent sync history |
| GET | `/api/browse/local?path=` | Browse local files |
| GET | `/api/browse/remote?path=` | Browse Proton Drive files |
| GET | `/api/browse/local/tree?path=` | Local folder tree |
| GET | `/api/browse/remote/tree?path=` | Remote folder tree |
| GET | `/api/config` | Get configuration |
| PUT | `/api/config` | Update configuration |
| POST | `/api/actions/mount` | Mount Proton Drive |
| POST | `/api/actions/unmount` | Unmount Proton Drive |
| POST | `/api/actions/health` | Run health check |

## Running as a System Service

The web app can be installed as a **systemd service** so it runs in the background, starts on boot, and restarts automatically if it crashes.

### Install the Service

```bash
cd webapp/
./install-service.sh
```

The installer will:
1. Create a Python virtual environment (`venv/`)
2. Install all dependencies
3. Write a systemd unit file to `/etc/systemd/system/protondrive-webapp.service`
4. Enable and start the service

After installation the web UI is available at **http://localhost:5000**.

### Manage the Service

```bash
# Check status
sudo systemctl status protondrive-webapp

# Stop the service
sudo systemctl stop protondrive-webapp

# Start the service
sudo systemctl start protondrive-webapp

# Restart the service
sudo systemctl restart protondrive-webapp

# Disable auto-start on boot
sudo systemctl disable protondrive-webapp

# Re-enable auto-start on boot
sudo systemctl enable protondrive-webapp
```

### View Logs

```bash
# Follow live logs
journalctl -u protondrive-webapp -f

# Show last 100 lines
journalctl -u protondrive-webapp -n 100

# Logs since last boot
journalctl -u protondrive-webapp -b
```

### Uninstall the Service

```bash
cd webapp/
./uninstall-service.sh
```

This stops and removes the systemd service. The Python virtual environment (`venv/`) is left in place; delete it manually if desired:

```bash
rm -rf venv/
```

## Notes

- When running as a service, schedules execute automatically 24/7
- When running manually (`python app.py`), schedules only run while the app is running
- The app stores schedule and config data in `~/.local/share/protondrive-linux/webapp/`
- All sync operations use rclone under the hood
- The file browser supports navigation through both local and remote directories
