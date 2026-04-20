#!/usr/bin/env python3
"""
rclone Web Interface — simple GUI for pull/push syncs between any two remotes.
"""

import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

# ─── Configuration ─────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path.home() / ".config" / "protondrive-linux"
CONFIG_FILE = CONFIG_DIR / "config.env"
DATA_DIR = Path.home() / ".local" / "share" / "protondrive-linux"
WEBAPP_DATA = DATA_DIR / "webapp"
SYNC_CONFIGS_FILE = WEBAPP_DATA / "sync_configs.json"
LOG_DIR = DATA_DIR / "logs"
SYNC_LOG_FILE = LOG_DIR / "sync.log"
HISTORY_FILE = WEBAPP_DATA / "sync_history.json"

WEBAPP_DATA.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# ─── Helpers ───────────────────────────────────────────────────────────


def strip_inline_comment(value):
    if not value:
        return value
    idx = value.find("#")
    if idx != -1:
        value = value[:idx]
    return value.strip()


def load_config_env():
    config = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                val = strip_inline_comment(val)
                val = val.replace("$HOME", str(Path.home()))
                config[key.strip()] = val
    config.setdefault("RCLONE_REMOTE", "protondrive")
    config.setdefault("SYNC_DIR", str(Path.home() / "ProtonSync"))
    config.setdefault("LOG_DIR", str(LOG_DIR))
    return config


def load_json(path, default=None):
    if default is None:
        default = []
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def run_rclone_cmd(args, timeout=60):
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


# ─── Sync History ──────────────────────────────────────────────────────

MAX_HISTORY = 100
sync_history_lock = threading.Lock()

try:
    sync_history = load_json(HISTORY_FILE, [])
except Exception:
    sync_history = []


def record_sync(job_id, job_name, success, message=""):
    entry = {
        "id": str(uuid.uuid4())[:8],
        "job_id": job_id,
        "job_name": job_name,
        "timestamp": datetime.now().isoformat(),
        "success": success,
        "message": message[:500],
    }
    with sync_history_lock:
        sync_history.insert(0, entry)
        if len(sync_history) > MAX_HISTORY:
            sync_history.pop()
        try:
            save_json(HISTORY_FILE, sync_history)
        except Exception:
            pass


# ─── Live Progress Tracking ────────────────────────────────────────────

active_syncs = {}
active_syncs_lock = threading.Lock()


def _parse_rclone_progress(line):
    """Extract progress info from a rclone stats line."""
    progress = {}

    xfer_match = re.search(
        r'Transferred:\s+([\d.]+\s*\S+)\s*/\s*([\d.]+\s*\S+),\s*(\d+)%'
        r'(?:,\s*([\d.]+\s*\S+/s))?'
        r'(?:,\s*ETA\s+([\w:]+))?',
        line
    )
    if xfer_match:
        progress["bytes_done"] = xfer_match.group(1).strip()
        progress["bytes_total"] = xfer_match.group(2).strip()
        progress["percent"] = int(xfer_match.group(3))
        if xfer_match.group(4):
            progress["speed"] = xfer_match.group(4).strip()
        if xfer_match.group(5):
            progress["eta"] = xfer_match.group(5).strip()

    count_match = re.search(r'Transferred:\s+(\d+)\s*/\s*(\d+),\s*(\d+)%', line)
    if count_match and "bytes_done" not in progress:
        progress["files_done"] = int(count_match.group(1))
        progress["files_total"] = int(count_match.group(2))
        progress["percent"] = int(count_match.group(3))

    checks_match = re.search(r'Checks:\s+(\d+)\s*/\s*(\d+)', line)
    if checks_match:
        progress["checks_done"] = int(checks_match.group(1))
        progress["checks_total"] = int(checks_match.group(2))

    err_match = re.search(r'Errors:\s+(\d+)', line)
    if err_match:
        progress["errors"] = int(err_match.group(1))

    file_match = re.search(r'\*\s+(.+?):\s+(\d+)%\s*/\s*([\d.]+\s*\S+)', line)
    if file_match:
        progress["current_file"] = file_match.group(1).strip()
        progress["current_file_percent"] = int(file_match.group(2))

    return progress if progress else None


def _build_rclone_bisync_args(local_path, remote_full, env_config, resync=False):
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

    max_del = env_config.get("SYNC_MAX_DELETE_PCT", "50")
    args += ["--max-delete", str(max_del)]

    if resync:
        args.append("--resync")

    args += ["--stats", "1s", "--stats-one-line"]
    return args


def _run_rclone_streaming(config_id, rclone_args):
    """Run rclone in background, stream output to active_syncs and sync.log."""
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config = next((c for c in configs if c["id"] == config_id), None)
    job_name = config.get("name", config_id) if config else config_id

    def _append(line):
        with active_syncs_lock:
            entry = active_syncs[config_id]
            entry["lines"].append(line)
            progress = _parse_rclone_progress(line)
            if progress:
                entry["progress"].update(progress)
        try:
            with open(SYNC_LOG_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

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

    # Bisync first-run retry
    if rc != 0 and "--resync" not in rclone_args:
        if any(kw in output.lower() for kw in ("bisync requires", "requires --resync")):
            _append("[auto-retry] Bisync requires --resync — retrying...")
            rc, output = _run(rclone_args + ["--resync"])

    success = rc == 0
    _append(f"[{datetime.now().strftime('%H:%M:%S')}] {'Sync completed.' if success else f'Sync failed (exit {rc}).'}")

    with active_syncs_lock:
        active_syncs[config_id]["done"] = True
        active_syncs[config_id]["success"] = success
        if success:
            active_syncs[config_id]["progress"]["percent"] = 100

    record_sync(config_id, job_name, success, output[-500:] if output else "")


def execute_sync_job(job_id):
    """Execute a sync job in the calling thread."""
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config = next((c for c in configs if c["id"] == job_id), None)
    if not config:
        record_sync(job_id, "Unknown", False, "Config not found")
        return

    env_config = load_config_env()
    # Per-config remote takes priority over global default
    remote = config.get("remote") or env_config.get("RCLONE_REMOTE", "protondrive")
    local_path = config.get("local_path", "")
    remote_path = config.get("remote_path", "")
    direction = config.get("direction", "bisync")

    if not local_path:
        record_sync(job_id, config.get("name", ""), False, "No local path configured")
        return

    remote_full = f"{remote}:{remote_path}" if remote_path else f"{remote}:"

    with active_syncs_lock:
        if job_id not in active_syncs or active_syncs[job_id].get("done", True):
            active_syncs[job_id] = {
                "lines": [], "done": False, "success": None,
                "started_at": datetime.now().isoformat(),
                "name": config.get("name", job_id),
                "progress": {},
                "direction": direction,
                "local_path": local_path,
                "remote_path": remote_path,
                "remote": remote,
            }

    if direction == "bisync":
        state_dir = Path.home() / ".cache" / "rclone" / "bisync"
        needs_resync = not state_dir.exists() or not any(
            remote in str(f) for f in state_dir.glob("*.lst")
        )
        args = _build_rclone_bisync_args(local_path, remote_full, env_config, resync=needs_resync)
    elif direction == "push":
        args = ["sync", local_path, remote_full,
                "--log-level", env_config.get("LOG_LEVEL", "INFO"),
                "--transfers", str(env_config.get("SYNC_TRANSFERS", "4")),
                "--stats", "1s", "--stats-one-line"]
    else:  # pull
        args = ["sync", remote_full, local_path,
                "--log-level", env_config.get("LOG_LEVEL", "INFO"),
                "--transfers", str(env_config.get("SYNC_TRANSFERS", "4")),
                "--stats", "1s", "--stats-one-line"]

    _run_rclone_streaming(job_id, args)


# ─── Routes: Pages ────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sync")
def sync_page():
    return render_template("sync.html")


@app.route("/connection")
def connection_page():
    return render_template("connection.html")


@app.route("/logs")
def logs_page():
    return render_template("logs.html")


# ─── Routes: API — Status ─────────────────────────────────────────────


@app.route("/api/status")
def api_status():
    rclone_ok, rclone_out, _ = run_rclone_cmd(["version"], timeout=10)
    rclone_version = ""
    if rclone_ok:
        for line in rclone_out.splitlines():
            if "rclone v" in line:
                rclone_version = line.strip()
                break

    with active_syncs_lock:
        running_syncs = sum(1 for s in active_syncs.values() if not s.get("done", True))

    return jsonify({
        "rclone_installed": rclone_ok,
        "rclone_version": rclone_version,
        "running_syncs": running_syncs,
    })


@app.route("/api/active-syncs")
def api_active_syncs():
    with active_syncs_lock:
        result = {}
        for cid, info in active_syncs.items():
            result[cid] = {
                "running": not info["done"],
                "success": info["success"],
                "started_at": info.get("started_at"),
                "name": info.get("name", cid),
                "progress": info.get("progress", {}),
                "line_count": len(info["lines"]),
                "direction": info.get("direction", ""),
                "remote": info.get("remote", ""),
            }
    return jsonify(result)


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
        "remote": data.get("remote", ""),
        "local_path": data.get("local_path", ""),
        "remote_path": data.get("remote_path", ""),
        "direction": data.get("direction", "push"),
        "exclude_patterns": data.get("exclude_patterns", ""),
        "created_at": datetime.now().isoformat(),
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
            cfg.update({
                "name": data.get("name", cfg["name"]),
                "remote": data.get("remote", cfg.get("remote", "")),
                "local_path": data.get("local_path", cfg["local_path"]),
                "remote_path": data.get("remote_path", cfg["remote_path"]),
                "direction": data.get("direction", cfg["direction"]),
                "exclude_patterns": data.get("exclude_patterns", cfg.get("exclude_patterns", "")),
            })
            save_json(SYNC_CONFIGS_FILE, configs)
            return jsonify(cfg)
    return jsonify({"error": "Config not found"}), 404


@app.route("/api/sync-configs/<config_id>", methods=["DELETE"])
def api_delete_sync_config(config_id):
    configs = load_json(SYNC_CONFIGS_FILE, [])
    configs = [c for c in configs if c["id"] != config_id]
    save_json(SYNC_CONFIGS_FILE, configs)
    return jsonify({"success": True})


@app.route("/api/sync-configs/<config_id>/run", methods=["POST"])
def api_run_sync(config_id):
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config = next((c for c in configs if c["id"] == config_id), None)
    if not config:
        return jsonify({"success": False, "message": "Config not found"}), 404

    with active_syncs_lock:
        if config_id in active_syncs and not active_syncs[config_id].get("done", True):
            return jsonify({"success": False, "message": "Sync already running"})
        active_syncs[config_id] = {
            "lines": [], "done": False, "success": None,
            "started_at": datetime.now().isoformat(),
            "name": config.get("name", config_id),
            "progress": {},
            "direction": config.get("direction", "push"),
            "local_path": config.get("local_path", ""),
            "remote_path": config.get("remote_path", ""),
            "remote": config.get("remote", ""),
        }

    thread = threading.Thread(target=execute_sync_job, args=(config_id,), daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Sync started"})


@app.route("/api/sync-configs/<config_id>/status")
def api_sync_status(config_id):
    since = int(request.args.get("since", 0))
    with active_syncs_lock:
        if config_id not in active_syncs:
            return jsonify({"running": False, "lines": [], "total": 0, "success": None, "progress": {}})
        info = active_syncs[config_id]
        return jsonify({
            "running": not info["done"],
            "lines": info["lines"][since:],
            "total": len(info["lines"]),
            "success": info["success"],
            "started_at": info.get("started_at"),
            "name": info.get("name", config_id),
            "progress": info.get("progress", {}),
        })


# ─── Routes: API — Sync History ───────────────────────────────────────


@app.route("/api/sync-history")
def api_sync_history():
    with sync_history_lock:
        return jsonify(sync_history[:50])


# ─── Routes: API — File Browser (tree only, for path picker modal) ────


@app.route("/api/browse/local/tree")
def api_local_tree():
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
    """Browse a specific remote's directory tree (uses ?remote=name&path=...)."""
    path = request.args.get("path", "")
    remote_name = request.args.get("remote", "")
    if not remote_name:
        env_config = load_config_env()
        remote_name = env_config.get("RCLONE_REMOTE", "protondrive")

    remote_path = f"{remote_name}:{path}" if path else f"{remote_name}:"
    success, stdout, stderr = run_rclone_cmd(
        ["lsjson", remote_path, "--dirs-only", "--no-modtime", "--no-mimetype"],
        timeout=30,
    )
    dirs = []
    if success and stdout.strip():
        try:
            items = json.loads(stdout)
            for item in sorted(items, key=lambda x: x.get("Name", "").lower()):
                dirs.append({
                    "name": item["Name"],
                    "path": os.path.join(path, item["Name"]) if path else item["Name"],
                })
        except json.JSONDecodeError:
            pass

    parent = str(Path(path).parent) if path and path != "." else None
    if parent == ".":
        parent = ""
    return jsonify({"path": path or "/", "parent": parent, "dirs": dirs})


# ─── Routes: API — Remote / Connection Management ─────────────────────


def _parse_rclone_config_show(output):
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
            fields[k] = "[hidden]" if k.lower() in SECRET_KEYS else v.strip()
    return fields


@app.route("/api/remotes")
def api_list_remotes():
    success, stdout, stderr = run_rclone_cmd(["listremotes"], timeout=10)
    if not success:
        return jsonify({"error": stderr or "rclone not available", "remotes": []})

    remotes = []
    for line in stdout.strip().splitlines():
        name = line.rstrip(":")
        if not name:
            continue
        ok, cfg_out, _ = run_rclone_cmd(["config", "show", name], timeout=10)
        fields = _parse_rclone_config_show(cfg_out) if ok else {}
        remotes.append({
            "name": name,
            "type": fields.get("type", "unknown"),
        })
    return jsonify({"remotes": remotes})


@app.route("/api/remotes/<name>/test", methods=["POST"])
def api_test_remote(name):
    success, _, stderr = run_rclone_cmd(
        ["lsd", f"{name}:", "--max-depth", "0", "--contimeout", "15s", "--timeout", "30s"],
        timeout=60,
    )
    return jsonify({"success": success, "error": stderr if not success else ""})


@app.route("/api/remotes/<name>/about")
def api_remote_about(name):
    success, stdout, stderr = run_rclone_cmd(["about", f"{name}:", "--json"], timeout=20)
    if success and stdout.strip():
        try:
            return jsonify({"success": True, "data": json.loads(stdout)})
        except json.JSONDecodeError:
            pass
    return jsonify({"success": False, "error": stderr or "Unable to retrieve quota"})


# ─── Routes: API — Logs ───────────────────────────────────────────────


@app.route("/api/logs")
def api_get_logs():
    lines = int(request.args.get("lines", 200))
    if not SYNC_LOG_FILE.exists():
        return jsonify({"lines": [], "path": str(SYNC_LOG_FILE), "exists": False})
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(SYNC_LOG_FILE)],
            capture_output=True, text=True, timeout=5
        )
        content = result.stdout.splitlines()
    except Exception:
        content = SYNC_LOG_FILE.read_text().splitlines()[-lines:]
    return jsonify({"lines": content, "path": str(SYNC_LOG_FILE), "exists": True})


@app.route("/api/logs/download")
def api_download_logs():
    if not SYNC_LOG_FILE.exists():
        return jsonify({"error": "No log file yet"}), 404
    return send_file(str(SYNC_LOG_FILE), mimetype="text/plain",
                     as_attachment=True, download_name="sync.log")


@app.route("/api/logs/clear", methods=["POST"])
def api_clear_logs():
    if SYNC_LOG_FILE.exists():
        SYNC_LOG_FILE.write_text("")
    return jsonify({"success": True})


# ─── Startup ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print("=" * 60)
    print("  rclone Web Interface")
    print(f"  http://localhost:{args.port}")
    print("=" * 60)
    app.run(host=args.host, port=args.port, debug=False)
