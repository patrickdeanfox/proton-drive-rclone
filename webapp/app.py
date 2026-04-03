#!/usr/bin/env python3
"""
Proton Drive rclone Web Interface
A local web-based UI for managing rclone syncs with Proton Drive.
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify, render_template, request

# ─── Configuration ─────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "protondrive-linux" / "scripts"
CONFIG_DIR = Path.home() / ".config" / "protondrive-linux"
CONFIG_FILE = CONFIG_DIR / "config.env"
DATA_DIR = Path.home() / ".local" / "share" / "protondrive-linux"
WEBAPP_DATA = DATA_DIR / "webapp"
SCHEDULES_FILE = WEBAPP_DATA / "schedules.json"
SYNC_CONFIGS_FILE = WEBAPP_DATA / "sync_configs.json"
LOG_DIR = DATA_DIR / "logs"

# Ensure directories exist
WEBAPP_DATA.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

# ─── Helpers ───────────────────────────────────────────────────────────


def load_config_env():
    """Parse config.env into a dict."""
    config = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                # Expand $HOME
                val = val.replace("$HOME", str(Path.home()))
                config[key.strip()] = val
    # Defaults
    config.setdefault("RCLONE_REMOTE", "protondrive")
    config.setdefault("SYNC_DIR", str(Path.home() / "ProtonSync"))
    config.setdefault("MOUNT_DIR", str(Path.home() / "ProtonDrive"))
    config.setdefault("LOG_DIR", str(LOG_DIR))
    return config


def load_json(path, default=None):
    """Load a JSON file or return default."""
    if default is None:
        default = []
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return default


def save_json(path, data):
    """Save data as JSON."""
    path.write_text(json.dumps(data, indent=2, default=str))


def run_rclone_cmd(args, timeout=60):
    """Run an rclone command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["rclone"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except FileNotFoundError:
        return False, "", "rclone not found. Please install rclone first."
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout}s"


def run_script(script_name, args=None, timeout=120):
    """Run one of the existing bash scripts."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        return False, "", f"Script not found: {script_path}"
    cmd = ["bash", str(script_path)] + (args or [])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HOME": str(Path.home())},
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Script timed out after {timeout}s"


# ─── Sync Job History ─────────────────────────────────────────────────

sync_history = []
sync_history_lock = threading.Lock()
MAX_HISTORY = 100


def record_sync(job_id, job_name, success, message=""):
    """Record a sync operation in history."""
    with sync_history_lock:
        sync_history.insert(
            0,
            {
                "id": str(uuid.uuid4())[:8],
                "job_id": job_id,
                "job_name": job_name,
                "timestamp": datetime.now().isoformat(),
                "success": success,
                "message": message[:500],
            },
        )
        if len(sync_history) > MAX_HISTORY:
            sync_history.pop()


# ─── Live Sync Progress Tracking ──────────────────────────────────────

active_syncs = {}          # config_id → {lines, done, success, started_at}
active_syncs_lock = threading.Lock()


def _build_rclone_bisync_args(local_path, remote_full, env_config, resync=False):
    """Build rclone bisync args applying the same logic as sync.sh."""
    args = ["bisync", local_path, remote_full]
    args += ["--log-level", env_config.get("LOG_LEVEL", "INFO")]
    args += ["--checkers", str(env_config.get("SYNC_CHECKERS", "8"))]
    args += ["--transfers", str(env_config.get("SYNC_TRANSFERS", "4"))]

    for pat in env_config.get("SYNC_EXCLUDE_PATTERNS", "").split(","):
        pat = pat.strip()
        if pat:
            args += ["--exclude", pat]

    policy = env_config.get("SYNC_CONFLICT_POLICY", "newer")
    if policy in ("newer", "larger"):
        args += ["--conflict-resolve", policy]
    # skip → omit flag (rclone default: create conflict copies)

    max_del = env_config.get("SYNC_MAX_DELETE_PCT", "50")
    args += ["--max-delete", str(max_del)]

    if resync:
        args.append("--resync")

    return args


def _run_rclone_streaming(config_id, rclone_args):
    """Run rclone, stream output line-by-line into active_syncs, retry on resync error."""
    env_config = load_config_env()
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config = next((c for c in configs if c["id"] == config_id), None)
    job_name = config.get("name", config_id) if config else config_id

    def _append(line):
        with active_syncs_lock:
            active_syncs[config_id]["lines"].append(line)

    def _run(args):
        rc = 0
        output_lines = []
        try:
            proc = subprocess.Popen(
                ["rclone"] + args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "HOME": str(Path.home())},
            )
            for line in proc.stdout:
                line = line.rstrip()
                _append(line)
                output_lines.append(line)
            proc.wait()
            rc = proc.returncode
        except FileNotFoundError:
            _append("ERROR: rclone not found. Please install rclone.")
            rc = 1
        except Exception as e:
            _append(f"ERROR: {e}")
            rc = 1
        return rc, "\n".join(output_lines)

    _append(f"[{datetime.now().strftime('%H:%M:%S')}] Starting sync...")
    rc, output = _run(rclone_args)

    # Bisync first-run retry: if it failed asking for --resync, retry once
    if rc != 0 and "--resync" not in rclone_args:
        needs_resync = any(
            kw in output.lower()
            for kw in ("bisync requires", "requires --resync", "must.*resync")
        )
        if needs_resync:
            _append("[auto-retry] Bisync requires --resync — retrying...")
            rc, output = _run(rclone_args + ["--resync"])

    success = rc == 0
    _append(f"[{datetime.now().strftime('%H:%M:%S')}] {'Sync completed.' if success else f'Sync failed (exit {rc}).'}")

    with active_syncs_lock:
        active_syncs[config_id]["done"] = True
        active_syncs[config_id]["success"] = success

    record_sync(config_id, job_name, success, output[-500:] if output else "")


def execute_sync_job(job_id):
    """Execute a sync job (used by scheduler). Runs in the calling thread."""
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config = next((c for c in configs if c["id"] == job_id), None)
    if not config:
        record_sync(job_id, "Unknown", False, "Config not found")
        return

    env_config = load_config_env()
    remote = env_config.get("RCLONE_REMOTE", "protondrive")
    local_path = config.get("local_path", "")
    remote_path = config.get("remote_path", "")
    direction = config.get("direction", "bisync")

    if not local_path:
        record_sync(job_id, config.get("name", ""), False, "No local path configured")
        return

    remote_full = f"{remote}:{remote_path}" if remote_path else f"{remote}:"

    # Slot may already be reserved by api_run_sync; only init if not present
    # (scheduler-triggered jobs won't have a pre-reserved slot)
    with active_syncs_lock:
        if job_id not in active_syncs or active_syncs[job_id].get("done", True):
            active_syncs[job_id] = {
                "lines": [], "done": False, "success": None,
                "started_at": datetime.now().isoformat(),
                "name": config.get("name", job_id),
            }

    if direction == "bisync":
        # First-run detection: check bisync state dir for this remote
        state_dir = Path.home() / ".cache" / "rclone" / "bisync"
        needs_resync = False
        if not state_dir.exists():
            needs_resync = True
        elif not any(
            remote in str(f) for f in state_dir.glob("*.lst")
        ):
            needs_resync = True
        args = _build_rclone_bisync_args(local_path, remote_full, env_config, resync=needs_resync)
    elif direction == "push":
        args = ["sync", local_path, remote_full,
                "--log-level", env_config.get("LOG_LEVEL", "INFO"),
                "--transfers", str(env_config.get("SYNC_TRANSFERS", "4"))]
    else:  # pull
        args = ["sync", remote_full, local_path,
                "--log-level", env_config.get("LOG_LEVEL", "INFO"),
                "--transfers", str(env_config.get("SYNC_TRANSFERS", "4"))]

    _run_rclone_streaming(job_id, args)


# ─── Schedule Management ──────────────────────────────────────────────


def load_and_restore_schedules():
    """Restore saved schedules on startup."""
    schedules = load_json(SCHEDULES_FILE, [])
    for sched in schedules:
        if sched.get("enabled", True):
            _add_scheduler_job(sched)
    return schedules


def _add_scheduler_job(sched):
    """Add a job to APScheduler from a schedule dict."""
    job_id = f"sync_{sched['id']}"

    # Remove existing if any
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    stype = sched.get("schedule_type", "interval")
    if stype == "interval":
        minutes = int(sched.get("interval_minutes", 30))
        trigger = IntervalTrigger(minutes=minutes)
    elif stype == "cron":
        cron_expr = sched.get("cron_expression", "0 * * * *")
        parts = cron_expr.split()
        trigger = CronTrigger(
            minute=parts[0] if len(parts) > 0 else "0",
            hour=parts[1] if len(parts) > 1 else "*",
            day=parts[2] if len(parts) > 2 else "*",
            month=parts[3] if len(parts) > 3 else "*",
            day_of_week=parts[4] if len(parts) > 4 else "*",
        )
    elif stype == "daily":
        time_str = sched.get("daily_time", "02:00")
        hour, minute = time_str.split(":")
        trigger = CronTrigger(hour=int(hour), minute=int(minute))
    else:
        return

    scheduler.add_job(
        execute_sync_job,
        trigger=trigger,
        id=job_id,
        args=[sched.get("config_id", "")],
        replace_existing=True,
        name=sched.get("name", "Unnamed Schedule"),
    )


# ─── Routes: Pages ────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/schedules")
def schedules_page():
    return render_template("schedules.html")


@app.route("/folders")
def folders_page():
    return render_template("folders.html")


@app.route("/browser")
def browser_page():
    return render_template("browser.html")


@app.route("/connection")
def connection_page():
    return render_template("connection.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/logs")
def logs_page():
    return render_template("logs.html")


# ─── Routes: API — Status ─────────────────────────────────────────────


@app.route("/api/status")
def api_status():
    """Get overall system status."""
    config = load_config_env()
    sync_dir = config.get("SYNC_DIR", "")
    mount_dir = config.get("MOUNT_DIR", "")
    remote = config.get("RCLONE_REMOTE", "protondrive")

    # Check rclone availability
    rclone_ok, rclone_out, _ = run_rclone_cmd(["version"], timeout=10)
    rclone_version = ""
    if rclone_ok:
        for line in rclone_out.splitlines():
            if "rclone v" in line:
                rclone_version = line.strip()
                break

    # Check remote connectivity
    remote_ok, _, _ = run_rclone_cmd(
        ["lsd", f"{remote}:", "--max-depth", "0"], timeout=15
    )

    # Check mount status
    mount_active = False
    try:
        result = subprocess.run(
            ["mountpoint", "-q", mount_dir], capture_output=True, timeout=5
        )
        mount_active = result.returncode == 0
    except Exception:
        pass

    # Count local files
    local_files = 0
    local_size = "N/A"
    if os.path.isdir(sync_dir):
        try:
            local_files = sum(1 for _ in Path(sync_dir).rglob("*") if _.is_file())
            result = subprocess.run(
                ["du", "-sh", sync_dir], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                local_size = result.stdout.split()[0]
        except Exception:
            pass

    # Active schedules
    active_schedules = len(scheduler.get_jobs())

    return jsonify(
        {
            "rclone_installed": rclone_ok,
            "rclone_version": rclone_version,
            "remote_connected": remote_ok,
            "remote_name": remote,
            "mount_active": mount_active,
            "mount_dir": mount_dir,
            "sync_dir": sync_dir,
            "local_files": local_files,
            "local_size": local_size,
            "active_schedules": active_schedules,
            "config": config,
        }
    )


# ─── Routes: API — Sync Configs ───────────────────────────────────────


@app.route("/api/sync-configs", methods=["GET"])
def api_get_sync_configs():
    return jsonify(load_json(SYNC_CONFIGS_FILE, []))


@app.route("/api/sync-configs", methods=["POST"])
def api_create_sync_config():
    data = request.json
    configs = load_json(SYNC_CONFIGS_FILE, [])
    new_config = {
        "id": str(uuid.uuid4())[:8],
        "name": data.get("name", "Untitled"),
        "local_path": data.get("local_path", ""),
        "remote_path": data.get("remote_path", ""),
        "direction": data.get("direction", "bisync"),
        "exclude_patterns": data.get("exclude_patterns", ""),
        "created_at": datetime.now().isoformat(),
        "enabled": True,
    }
    configs.append(new_config)
    save_json(SYNC_CONFIGS_FILE, configs)
    return jsonify(new_config), 201


@app.route("/api/sync-configs/<config_id>", methods=["PUT"])
def api_update_sync_config(config_id):
    data = request.json
    configs = load_json(SYNC_CONFIGS_FILE, [])
    for cfg in configs:
        if cfg["id"] == config_id:
            cfg.update(
                {
                    "name": data.get("name", cfg["name"]),
                    "local_path": data.get("local_path", cfg["local_path"]),
                    "remote_path": data.get("remote_path", cfg["remote_path"]),
                    "direction": data.get("direction", cfg["direction"]),
                    "exclude_patterns": data.get(
                        "exclude_patterns", cfg.get("exclude_patterns", "")
                    ),
                    "enabled": data.get("enabled", cfg.get("enabled", True)),
                }
            )
            save_json(SYNC_CONFIGS_FILE, configs)
            return jsonify(cfg)
    return jsonify({"error": "Config not found"}), 404


@app.route("/api/sync-configs/<config_id>", methods=["DELETE"])
def api_delete_sync_config(config_id):
    configs = load_json(SYNC_CONFIGS_FILE, [])
    configs = [c for c in configs if c["id"] != config_id]
    save_json(SYNC_CONFIGS_FILE, configs)
    # Also remove related schedules
    schedules = load_json(SCHEDULES_FILE, [])
    for s in schedules:
        if s.get("config_id") == config_id:
            try:
                scheduler.remove_job(f"sync_{s['id']}")
            except Exception:
                pass
    schedules = [s for s in schedules if s.get("config_id") != config_id]
    save_json(SCHEDULES_FILE, schedules)
    return jsonify({"success": True})


@app.route("/api/sync-configs/<config_id>/run", methods=["POST"])
def api_run_sync(config_id):
    """Trigger an immediate sync for a config."""
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config = next((c for c in configs if c["id"] == config_id), None)
    if not config:
        return jsonify({"success": False, "message": "Config not found"}), 404

    # Claim the slot inside the lock to prevent race conditions on rapid clicks
    with active_syncs_lock:
        if config_id in active_syncs and not active_syncs[config_id].get("done", True):
            return jsonify({"success": False, "message": "Sync already running"})
        # Reserve the slot before the thread starts so no second request can slip through
        active_syncs[config_id] = {
            "lines": [], "done": False, "success": None,
            "started_at": datetime.now().isoformat(),
            "name": config.get("name", config_id),
        }

    thread = threading.Thread(target=execute_sync_job, args=(config_id,), daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Sync started"})


@app.route("/api/sync-configs/<config_id>/status")
def api_sync_status(config_id):
    """Poll live sync progress. Pass ?since=N to get only lines after index N."""
    since = int(request.args.get("since", 0))
    with active_syncs_lock:
        if config_id not in active_syncs:
            return jsonify({"running": False, "lines": [], "total": 0, "success": None})
        info = active_syncs[config_id]
        return jsonify({
            "running": not info["done"],
            "lines": info["lines"][since:],
            "total": len(info["lines"]),
            "success": info["success"],
            "started_at": info.get("started_at"),
            "name": info.get("name", config_id),
        })


# ─── Routes: API — Schedules ──────────────────────────────────────────


@app.route("/api/schedules", methods=["GET"])
def api_get_schedules():
    schedules = load_json(SCHEDULES_FILE, [])
    # Annotate with next run time
    for s in schedules:
        job = scheduler.get_job(f"sync_{s['id']}")
        if job and job.next_run_time:
            s["next_run"] = job.next_run_time.isoformat()
        else:
            s["next_run"] = None
    return jsonify(schedules)


@app.route("/api/schedules", methods=["POST"])
def api_create_schedule():
    data = request.json
    schedules = load_json(SCHEDULES_FILE, [])
    new_sched = {
        "id": str(uuid.uuid4())[:8],
        "name": data.get("name", "Untitled Schedule"),
        "config_id": data.get("config_id", ""),
        "schedule_type": data.get("schedule_type", "interval"),
        "interval_minutes": data.get("interval_minutes", 30),
        "cron_expression": data.get("cron_expression", "0 * * * *"),
        "daily_time": data.get("daily_time", "02:00"),
        "enabled": data.get("enabled", True),
        "created_at": datetime.now().isoformat(),
    }
    schedules.append(new_sched)
    save_json(SCHEDULES_FILE, schedules)
    if new_sched["enabled"]:
        _add_scheduler_job(new_sched)
    return jsonify(new_sched), 201


@app.route("/api/schedules/<sched_id>", methods=["PUT"])
def api_update_schedule(sched_id):
    data = request.json
    schedules = load_json(SCHEDULES_FILE, [])
    for s in schedules:
        if s["id"] == sched_id:
            s.update(
                {
                    "name": data.get("name", s["name"]),
                    "config_id": data.get("config_id", s["config_id"]),
                    "schedule_type": data.get("schedule_type", s["schedule_type"]),
                    "interval_minutes": data.get(
                        "interval_minutes", s.get("interval_minutes", 30)
                    ),
                    "cron_expression": data.get(
                        "cron_expression", s.get("cron_expression", "0 * * * *")
                    ),
                    "daily_time": data.get("daily_time", s.get("daily_time", "02:00")),
                    "enabled": data.get("enabled", s.get("enabled", True)),
                }
            )
            # Update scheduler
            job_id = f"sync_{s['id']}"
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass
            if s["enabled"]:
                _add_scheduler_job(s)
            save_json(SCHEDULES_FILE, schedules)
            return jsonify(s)
    return jsonify({"error": "Schedule not found"}), 404


@app.route("/api/schedules/<sched_id>", methods=["DELETE"])
def api_delete_schedule(sched_id):
    schedules = load_json(SCHEDULES_FILE, [])
    for s in schedules:
        if s["id"] == sched_id:
            try:
                scheduler.remove_job(f"sync_{s['id']}")
            except Exception:
                pass
    schedules = [s for s in schedules if s["id"] != sched_id]
    save_json(SCHEDULES_FILE, schedules)
    return jsonify({"success": True})


@app.route("/api/schedules/<sched_id>/toggle", methods=["POST"])
def api_toggle_schedule(sched_id):
    schedules = load_json(SCHEDULES_FILE, [])
    for s in schedules:
        if s["id"] == sched_id:
            s["enabled"] = not s.get("enabled", True)
            job_id = f"sync_{s['id']}"
            if s["enabled"]:
                _add_scheduler_job(s)
            else:
                try:
                    scheduler.remove_job(job_id)
                except Exception:
                    pass
            save_json(SCHEDULES_FILE, schedules)
            return jsonify(s)
    return jsonify({"error": "Schedule not found"}), 404


# ─── Routes: API — Sync History ───────────────────────────────────────


@app.route("/api/sync-history")
def api_sync_history():
    with sync_history_lock:
        return jsonify(sync_history[:50])


# ─── Routes: API — File Browser ───────────────────────────────────────


@app.route("/api/browse/local")
def api_browse_local():
    """Browse local filesystem directory."""
    path = request.args.get("path", "")
    config = load_config_env()

    if not path:
        path = config.get("SYNC_DIR", str(Path.home()))

    path = os.path.expanduser(path)

    if not os.path.exists(path):
        return jsonify({"error": f"Path does not exist: {path}"}), 404

    if not os.path.isdir(path):
        return jsonify({"error": "Not a directory"}), 400

    entries = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat()
                entries.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "is_dir": entry.is_dir(),
                        "size": stat.st_size if not entry.is_dir() else 0,
                        "modified": datetime.fromtimestamp(
                            stat.st_mtime
                        ).isoformat(),
                    }
                )
            except (PermissionError, OSError):
                continue
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    parent = str(Path(path).parent) if path != "/" else None

    return jsonify(
        {"path": path, "parent": parent, "entries": entries}
    )


@app.route("/api/browse/remote")
def api_browse_remote():
    """Browse Proton Drive remote directory via rclone."""
    path = request.args.get("path", "")
    config = load_config_env()
    remote = config.get("RCLONE_REMOTE", "protondrive")
    remote_path = f"{remote}:{path}" if path else f"{remote}:"

    success, stdout, stderr = run_rclone_cmd(
        ["lsjson", remote_path, "--no-modtime", "--no-mimetype"], timeout=30
    )

    if not success:
        return jsonify(
            {"error": stderr or "Failed to list remote directory", "path": path}
        ), 500

    entries = []
    try:
        items = json.loads(stdout) if stdout.strip() else []
        for item in sorted(items, key=lambda x: (not x.get("IsDir", False), x.get("Name", "").lower())):
            entries.append(
                {
                    "name": item.get("Name", ""),
                    "path": os.path.join(path, item["Name"]) if path else item["Name"],
                    "is_dir": item.get("IsDir", False),
                    "size": item.get("Size", 0),
                    "modified": item.get("ModTime", ""),
                }
            )
    except json.JSONDecodeError:
        return jsonify({"error": "Failed to parse remote listing"}), 500

    parent = str(Path(path).parent) if path and path != "." else None
    if parent == ".":
        parent = ""

    return jsonify(
        {"path": path or "/", "parent": parent, "entries": entries}
    )


@app.route("/api/browse/local/tree")
def api_local_tree():
    """Get directory tree for folder selection."""
    path = request.args.get("path", str(Path.home()))
    path = os.path.expanduser(path)

    if not os.path.isdir(path):
        return jsonify({"error": "Not a directory"}), 400

    dirs = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({"name": entry.name, "path": entry.path})
    except PermissionError:
        pass

    return jsonify({"path": path, "parent": str(Path(path).parent), "dirs": dirs})


@app.route("/api/browse/remote/tree")
def api_remote_tree():
    """Get remote directory tree for folder selection."""
    path = request.args.get("path", "")
    config = load_config_env()
    remote = config.get("RCLONE_REMOTE", "protondrive")
    remote_path = f"{remote}:{path}" if path else f"{remote}:"

    success, stdout, stderr = run_rclone_cmd(
        ["lsjson", remote_path, "--dirs-only", "--no-modtime", "--no-mimetype"],
        timeout=30,
    )

    dirs = []
    if success and stdout.strip():
        try:
            items = json.loads(stdout)
            for item in sorted(items, key=lambda x: x.get("Name", "").lower()):
                dirs.append(
                    {
                        "name": item["Name"],
                        "path": os.path.join(path, item["Name"]) if path else item["Name"],
                    }
                )
        except json.JSONDecodeError:
            pass

    parent = str(Path(path).parent) if path and path != "." else None
    if parent == ".":
        parent = ""

    return jsonify({"path": path or "/", "parent": parent, "dirs": dirs})


# ─── Routes: API — Config ─────────────────────────────────────────────


@app.route("/api/config")
def api_get_config():
    return jsonify(load_config_env())


@app.route("/api/config", methods=["PUT"])
def api_update_config():
    """Update specific config values."""
    data = request.json
    if not CONFIG_FILE.exists():
        return jsonify({"error": "Config file not found"}), 404

    content = CONFIG_FILE.read_text()
    for key, val in data.items():
        pattern = rf'^{re.escape(key)}=.*$'
        replacement = f'{key}="{val}"'
        new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
        if count > 0:
            content = new_content
        else:
            content += f'\n{key}="{val}"'

    CONFIG_FILE.write_text(content)
    return jsonify({"success": True})


# ─── Routes: API — Remote / Connection Management ─────────────────


def _parse_rclone_config_show(output):
    """Parse `rclone config show <remote>` into a dict, hiding secrets."""
    SECRET_KEYS = {"password", "token", "client_secret", "pass",
                   "refresh_token", "access_token", "private_key"}
    fields = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("["):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if k.lower() in SECRET_KEYS:
                fields[k] = "[hidden]"
            else:
                fields[k] = v
    return fields


@app.route("/api/remotes")
def api_list_remotes():
    """List all rclone remotes with type and active status."""
    success, stdout, stderr = run_rclone_cmd(["listremotes"], timeout=10)
    if not success:
        return jsonify({"error": stderr or "rclone not available", "remotes": [], "active": ""})

    config = load_config_env()
    active = config.get("RCLONE_REMOTE", "")

    remotes = []
    for line in stdout.strip().splitlines():
        name = line.rstrip(":")
        if not name:
            continue
        # Get remote details
        ok, cfg_out, _ = run_rclone_cmd(["config", "show", name], timeout=10)
        fields = _parse_rclone_config_show(cfg_out) if ok else {}
        remotes.append({
            "name": name,
            "type": fields.get("type", "unknown"),
            "username": fields.get("username", fields.get("user", "")),
            "is_active": name == active,
        })

    return jsonify({"remotes": remotes, "active": active})


@app.route("/api/remotes/<name>/test", methods=["POST"])
def api_test_remote(name):
    """Test connectivity to a remote."""
    success, _, stderr = run_rclone_cmd(
        ["lsd", f"{name}:", "--max-depth", "0"], timeout=20
    )
    return jsonify({"success": success, "error": stderr if not success else ""})


@app.route("/api/remotes/<name>/about")
def api_remote_about(name):
    """Get quota and usage info for a remote via `rclone about --json`."""
    success, stdout, stderr = run_rclone_cmd(
        ["about", f"{name}:", "--json"], timeout=20
    )
    if success and stdout.strip():
        try:
            return jsonify({"success": True, "data": json.loads(stdout)})
        except json.JSONDecodeError:
            pass
    return jsonify({"success": False, "error": stderr or "Unable to retrieve quota"})


@app.route("/api/remotes/<name>/config")
def api_remote_config(name):
    """Return sanitized config fields for a remote (secrets hidden)."""
    success, stdout, stderr = run_rclone_cmd(["config", "show", name], timeout=10)
    if not success:
        return jsonify({"error": stderr or "Remote not found"}), 404
    return jsonify({"fields": _parse_rclone_config_show(stdout)})


@app.route("/api/remotes/<name>/set-active", methods=["POST"])
def api_set_active_remote(name):
    """Set RCLONE_REMOTE in config.env to the given remote name."""
    if not CONFIG_FILE.exists():
        return jsonify({"error": "Config file not found. Run the installer first."}), 404
    content = CONFIG_FILE.read_text()
    pattern = r'^RCLONE_REMOTE=.*$'
    replacement = f'RCLONE_REMOTE="{name}"'
    new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
    CONFIG_FILE.write_text(new_content if count > 0 else content + f'\n{replacement}')
    return jsonify({"success": True})


# ─── Routes: API — Quick Actions ──────────────────────────────────────


@app.route("/api/actions/mount", methods=["POST"])
def api_mount():
    success, stdout, stderr = run_script("mount.sh", timeout=30)
    return jsonify({"success": success, "output": stdout, "error": stderr})


@app.route("/api/actions/unmount", methods=["POST"])
def api_unmount():
    success, stdout, stderr = run_script("unmount.sh", timeout=15)
    return jsonify({"success": success, "output": stdout, "error": stderr})


@app.route("/api/actions/health", methods=["POST"])
def api_health_check():
    success, stdout, stderr = run_script("health.sh", timeout=30)
    return jsonify({"success": success, "output": stdout + stderr})


# ─── Routes: API — Logs ───────────────────────────────────────────────


@app.route("/api/logs")
def api_get_logs():
    """Return the last N lines of sync.log."""
    lines = int(request.args.get("lines", 200))
    log_file = LOG_DIR / "sync.log"
    if not log_file.exists():
        return jsonify({"lines": [], "path": str(log_file), "exists": False})
    try:
        # Efficient tail without reading the whole file
        result = subprocess.run(
            ["tail", "-n", str(lines), str(log_file)],
            capture_output=True, text=True, timeout=5
        )
        content = result.stdout.splitlines()
    except Exception:
        content = log_file.read_text().splitlines()[-lines:]
    return jsonify({"lines": content, "path": str(log_file), "exists": True})


@app.route("/api/logs/clear", methods=["POST"])
def api_clear_logs():
    """Truncate sync.log."""
    log_file = LOG_DIR / "sync.log"
    if log_file.exists():
        log_file.write_text("")
    return jsonify({"success": True})


# ─── Startup ───────────────────────────────────────────────────────────

# Restore saved schedules on startup
load_and_restore_schedules()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print("=" * 60)
    print("  Proton Drive rclone Web Interface")
    print(f"  http://localhost:{args.port}")
    print("=" * 60)
    app.run(host=args.host, port=args.port, debug=False)
