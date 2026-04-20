#!/usr/bin/env python3
"""Proton Drive Sync — simple rclone web interface."""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file

logger = logging.getLogger(__name__)

# ── Security ──────────────────────────────────────────────────────────────────

_SAFE_RCLONE_CMDS = frozenset({
    "version", "listremotes", "lsd", "lsjson", "about",
    "config", "bisync", "sync", "check", "ls", "size",
})

_SAFE_SCRIPTS = frozenset({"mount.sh", "unmount.sh", "health.sh"})

_ALLOWED_CONFIG_KEYS = frozenset({
    "RCLONE_REMOTE", "SYNC_EXCLUDE_PATTERNS", "SYNC_CONFLICT_POLICY",
    "SYNC_MAX_DELETE_PCT", "SYNC_CHECKERS", "SYNC_TRANSFERS",
    "SYNC_BANDWIDTH_LIMIT", "LOG_LEVEL",
})


def _safe_env():
    keep = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "XDG_CONFIG_HOME",
            "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR", "TZ"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def _sanitize_path(path_str):
    if not path_str:
        return None
    expanded = os.path.expanduser(path_str)
    resolved = os.path.realpath(expanded)
    for prefix in ("/proc", "/sys", "/dev", "/boot", "/sbin"):
        if resolved == prefix or resolved.startswith(prefix + "/"):
            return None
    return resolved


def _validate_remote_name(name):
    return bool(re.match(r'^[A-Za-z0-9_-]+$', name))


def _validate_path_component(path_str):
    if not path_str:
        return True
    if '\0' in path_str:
        return False
    return not any(c in ';&|`$(){}[]!#~' for c in path_str)


def _validate_filename(filename):
    if not filename:
        return False
    return '/' not in filename and '\\' not in filename and '..' not in filename and '\0' not in filename


# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "scripts"

_LEGACY_CONFIG = Path.home() / ".config" / "protondrive-linux"
_NEW_CONFIG    = Path.home() / ".config" / "protondrive"
CONFIG_DIR  = _LEGACY_CONFIG if _LEGACY_CONFIG.exists() else _NEW_CONFIG
CONFIG_FILE = CONFIG_DIR / "config.env"

_LEGACY_DATA = Path.home() / ".local" / "share" / "protondrive-linux"
_NEW_DATA    = Path.home() / ".local" / "share" / "protondrive"
DATA_DIR    = _LEGACY_DATA if _LEGACY_DATA.exists() else _NEW_DATA
WEBAPP_DATA = DATA_DIR / "webapp"
LOG_DIR     = DATA_DIR / "logs"
SYNC_CONFIGS_FILE = WEBAPP_DATA / "sync_configs.json"

WEBAPP_DATA.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()

# ── Helpers ───────────────────────────────────────────────────────────────────

_config_file_lock = threading.Lock()


def load_config_env():
    config = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                config[k.strip()] = v.strip().strip('"')
    return config


def save_config_env(updates: dict):
    with _config_file_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = CONFIG_FILE.read_text().splitlines() if CONFIG_FILE.exists() else []
        existing = {}
        for i, line in enumerate(lines):
            if "=" in line and not line.strip().startswith("#"):
                k = line.split("=", 1)[0].strip()
                existing[k] = i
        for k, v in updates.items():
            if k not in _ALLOWED_CONFIG_KEYS:
                continue
            entry = f'{k}="{v}"'
            if k in existing:
                lines[existing[k]] = entry
            else:
                lines.append(entry)
        CONFIG_FILE.write_text("\n".join(lines) + "\n")


def load_json(path, default=None):
    if default is None:
        default = []
    try:
        if Path(path).exists():
            return json.loads(Path(path).read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str))


def run_rclone_cmd(args, timeout=60):
    if args and args[0] not in _SAFE_RCLONE_CMDS:
        return False, "", f"Disallowed rclone subcommand: {args[0]}"
    try:
        result = subprocess.run(
            ["rclone"] + args,
            capture_output=True, text=True, timeout=timeout, env=_safe_env(),
        )
        return result.returncode == 0, result.stdout, result.stderr
    except FileNotFoundError:
        return False, "", "rclone not found — please install rclone."
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout}s"


# ── Active syncs (in-memory) ──────────────────────────────────────────────────

active_syncs: dict = {}
active_syncs_lock = threading.Lock()

DEFAULT_EXCLUDES = ".DS_Store,Thumbs.db,*.tmp,~*,.~lock.*,*.swp,*.swo,desktop.ini"


def _parse_rclone_progress(line):
    """Extract progress info from a rclone stats line."""
    progress = {}
    stripped = re.sub(
        r'^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+'
        r'(?:DEBUG|INFO|NOTICE|WARN|ERROR)\s*:\s*', '', line)

    m = re.search(
        r'Transferred:\s+([\d.]+\s*\S+)\s*/\s*([\d.]+\s*\S+),\s*(\d+)%'
        r'(?:,\s*([\d.]+\s*\S+/s))?(?:,\s*ETA\s+([\w:]+))?', stripped)
    if m:
        progress.update(bytes_done=m.group(1).strip(), bytes_total=m.group(2).strip(),
                        percent=int(m.group(3)))
        if m.group(4): progress["speed"] = m.group(4).strip()
        if m.group(5): progress["eta"]   = m.group(5).strip()

    if not progress:
        m = re.search(
            r'^\s*([\d.]+\s*[KMGTP]?i?B)\s*/\s*([\d.]+\s*[KMGTP]?i?B),\s*(\d+)%'
            r'(?:,\s*([\d.]+\s*[KMGTP]?i?B/s))?(?:,\s*ETA\s*([\w:]+))?', stripped)
        if m:
            progress.update(bytes_done=m.group(1).strip(), bytes_total=m.group(2).strip(),
                            percent=int(m.group(3)))
            if m.group(4): progress["speed"] = m.group(4).strip()
            if m.group(5): progress["eta"]   = m.group(5).strip()

    m = re.search(r'Transferred:\s+(\d+)\s*/\s*(\d+),\s*(\d+)%', stripped)
    if m and "bytes_done" not in progress:
        progress.update(files_done=int(m.group(1)), files_total=int(m.group(2)),
                        percent=int(m.group(3)))

    m = re.search(r'Errors:\s+(\d+)', stripped)
    if m: progress["errors"] = int(m.group(1))

    return progress if progress else None


def _build_sync_args(config, env_config):
    """Build rclone args for a sync config dict."""
    remote   = env_config.get("RCLONE_REMOTE", "protondrive")
    local    = config.get("local_path", "")
    rpath    = config.get("remote_path", "")
    remote_full = f"{remote}:{rpath}" if rpath else f"{remote}:"
    direction   = config.get("direction", "push")
    log_level   = env_config.get("LOG_LEVEL", "INFO")
    checkers    = str(env_config.get("SYNC_CHECKERS", "8"))
    transfers   = str(env_config.get("SYNC_TRANSFERS", "4"))
    max_del     = str(env_config.get("SYNC_MAX_DELETE_PCT", "50"))

    excludes = env_config.get("SYNC_EXCLUDE_PATTERNS", DEFAULT_EXCLUDES)
    excl_flags = []
    for pat in excludes.split(","):
        pat = pat.strip()
        if pat:
            excl_flags += ["--exclude", pat]

    common = (["--log-level", log_level, "--checkers", checkers,
               "--transfers", transfers, "--stats", "1s", "--stats-one-line"]
              + excl_flags)

    if direction == "bisync":
        state_dir = Path.home() / ".cache" / "rclone" / "bisync"
        needs_resync = not state_dir.exists() or not any(
            remote in str(f) for f in state_dir.glob("*.lst"))
        policy = env_config.get("SYNC_CONFLICT_POLICY", "newer")
        args = (["bisync", local, remote_full]
                + ["--max-delete", max_del]
                + (["--conflict-resolve", policy] if policy in ("newer", "larger") else [])
                + ["--resilient",
                   "--drive-pacer-min-sleep", "10ms",
                   "--drive-pacer-burst", "200"]
                + common)
        if needs_resync:
            args.append("--resync")
        return args, remote_full
    elif direction == "pull":
        return ["sync", remote_full, local] + common, remote_full
    else:  # push (default)
        return (["sync", local, remote_full, "--max-delete", max_del]
                + common), remote_full


def _run_sync_thread(config_id):
    """Background thread: run rclone and stream output into active_syncs."""
    configs   = load_json(SYNC_CONFIGS_FILE, [])
    config    = next((c for c in configs if c["id"] == config_id), {})
    env_cfg   = load_config_env()
    job_name  = config.get("name", config_id)

    args, remote_full = _build_sync_args(config, env_cfg)

    def _append(line):
        with active_syncs_lock:
            s = active_syncs.get(config_id)
            if s:
                s["lines"].append(line)
                p = _parse_rclone_progress(line)
                if p:
                    s["progress"].update(p)

    def _run(run_args):
        consecutive_errors = 0
        already_exists = 0
        reached_100_at = None
        BENIGN_422 = ("already exists", "Code=2500")
        try:
            # Use stdbuf to force line-buffered output from rclone.
            # Without this, rclone buffers stdout when writing to a pipe
            # (not a terminal), so lines only appear after ~4-8 KB accumulates.
            _stdbuf = shutil.which("stdbuf")
            _cmd = ([_stdbuf, "-oL", "-eL"] if _stdbuf else []) + ["rclone"] + run_args
            proc = subprocess.Popen(
                _cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=_safe_env(),
            )
            while True:
                line = proc.stdout.readline()
                if line == '' and proc.poll() is not None:
                    break
                if not line:
                    continue
                line = line.rstrip()
                _append(line)
                if "RESTY 422" in line and any(p in line for p in BENIGN_422):
                    already_exists += 1
                    if already_exists % 10 == 0:
                        _append(f"[info] {already_exists} 'already exists' skips — file already on Proton Drive")
                    consecutive_errors = 0
                elif "RESTY 429" in line:
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0
                if consecutive_errors >= 15:
                    _append("[auto-kill] Repeated API errors — stopping rclone")
                    proc.kill()
                    proc.wait()
                    with active_syncs_lock:
                        pct = active_syncs.get(config_id, {}).get("progress", {}).get("percent", 0)
                    return 0 if pct == 100 else 1
                p = _parse_rclone_progress(line)
                if p and p.get("percent") == 100:
                    if reached_100_at is None:
                        reached_100_at = time.time()
                elif p:
                    reached_100_at = None
                if reached_100_at and (time.time() - reached_100_at) > 120:
                    _append("[auto-kill] Stuck at 100% — done")
                    proc.kill(); proc.wait()
                    return 0
            proc.wait()
            return proc.returncode
        except FileNotFoundError:
            _append("ERROR: rclone not found — please install rclone.")
            return 1
        except Exception as e:
            _append(f"ERROR: {e}")
            return 1

    ts = datetime.now().strftime("%H:%M:%S")
    _append(f"[{ts}] Starting {config.get('direction','push')} sync: {config.get('local_path','')} → {remote_full}")
    rc = _run(args)

    # Bisync first-run auto-retry
    if rc != 0 and "bisync" in args and "--resync" not in args:
        with active_syncs_lock:
            out = "\n".join(active_syncs.get(config_id, {}).get("lines", []))
        if any(kw in out.lower() for kw in ("bisync requires", "requires --resync")):
            _append("[auto-retry] Adding --resync and retrying…")
            rc = _run(args + ["--resync"])

    success = rc == 0
    ts = datetime.now().strftime("%H:%M:%S")
    _append(f"[{ts}] {'✓ Sync complete' if success else f'✗ Sync failed (exit {rc})'}")

    with active_syncs_lock:
        s = active_syncs.get(config_id)
        if s:
            s["done"]    = True
            s["success"] = success
            if success:
                s["progress"]["percent"] = 100


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/logs")
def logs_page():
    return render_template("logs.html")


@app.route("/browser")
def browser_page():
    return render_template("browser.html")


# ── API: connection & remotes ─────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    ok, out, err = run_rclone_cmd(["version"])
    version = ""
    if ok:
        m = re.search(r"rclone v([\d.]+)", out)
        version = m.group(1) if m else out.splitlines()[0]
    env = load_config_env()
    remote = env.get("RCLONE_REMOTE", "protondrive")
    conn_ok, _, conn_err = run_rclone_cmd(["lsd", f"{remote}:", "--max-depth", "0"], timeout=15)
    return jsonify({
        "rclone_ok": ok,
        "rclone_version": version,
        "connected": conn_ok,
        "remote": remote,
        "error": conn_err if not conn_ok else "",
    })


@app.route("/api/remotes")
def api_list_remotes():
    ok, out, _ = run_rclone_cmd(["listremotes"])
    remotes = [r.rstrip(":") for r in out.splitlines() if r.strip()] if ok else []
    env = load_config_env()
    active = env.get("RCLONE_REMOTE", "protondrive")
    return jsonify({"remotes": remotes, "active": active})


@app.route("/api/remotes/<name>/test", methods=["POST"])
def api_test_remote(name):
    if not _validate_remote_name(name):
        return jsonify({"ok": False, "error": "Invalid remote name"}), 400
    ok, out, err = run_rclone_cmd(["lsd", f"{name}:", "--max-depth", "0"], timeout=20)
    return jsonify({"ok": ok, "output": out[:500], "error": err[:300]})


@app.route("/api/remotes/<name>/about")
def api_remote_about(name):
    if not _validate_remote_name(name):
        return jsonify({"error": "Invalid remote name"}), 400
    ok, out, err = run_rclone_cmd(["about", f"{name}:"], timeout=20)
    total = used = free = None
    if ok:
        for line in out.splitlines():
            lo = line.lower()
            m = re.search(r"([\d.]+)\s*([KMGTP]?i?B)", line)
            if not m:
                continue
            val_str, unit = m.group(1), m.group(2)
            val = float(val_str)
            for u, mult in [("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20),
                             ("KiB", 2**10), ("TB", 10**12), ("GB", 10**9),
                             ("MB", 10**6), ("KB", 10**3)]:
                if unit.upper() == u.upper():
                    val = int(val * mult); break
            if "total" in lo:   total = val
            elif "used" in lo:  used  = val
            elif "free" in lo:  free  = val
    return jsonify({"ok": ok, "total": total, "used": used, "free": free, "error": err[:200]})


@app.route("/api/remotes/<name>/set-active", methods=["POST"])
def api_set_active_remote(name):
    if not _validate_remote_name(name):
        return jsonify({"ok": False, "error": "Invalid remote name"}), 400
    save_config_env({"RCLONE_REMOTE": name})
    return jsonify({"ok": True})


# ── API: config ───────────────────────────────────────────────────────────────

@app.route("/api/config")
def api_get_config():
    return jsonify(load_config_env())


@app.route("/api/config", methods=["PUT"])
def api_update_config():
    data = request.get_json(force=True) or {}
    safe = {k: str(v) for k, v in data.items() if k in _ALLOWED_CONFIG_KEYS}
    if not safe:
        return jsonify({"ok": False, "error": "No valid keys"}), 400
    save_config_env(safe)
    return jsonify({"ok": True})


# ── API: sync configs ─────────────────────────────────────────────────────────

@app.route("/api/sync-configs")
def api_get_sync_configs():
    configs = load_json(SYNC_CONFIGS_FILE, [])
    # Annotate with running status
    with active_syncs_lock:
        running = set(cid for cid, s in active_syncs.items() if not s.get("done", True))
    for c in configs:
        c["running"] = c["id"] in running
    return jsonify(configs)


@app.route("/api/sync-configs", methods=["POST"])
def api_create_sync_config():
    data = request.get_json(force=True) or {}
    local = _sanitize_path(data.get("local_path", ""))
    if not local:
        return jsonify({"error": "Invalid local path"}), 400
    rpath = data.get("remote_path", "").strip("/")
    if not _validate_path_component(rpath):
        return jsonify({"error": "Invalid remote path"}), 400
    config = {
        "id":          str(uuid.uuid4())[:8],
        "name":        data.get("name", "").strip() or Path(local).name,
        "local_path":  local,
        "remote_path": rpath,
        "direction":   data.get("direction", "push"),
        "created_at":  datetime.now().isoformat(),
    }
    configs = load_json(SYNC_CONFIGS_FILE, [])
    configs.append(config)
    save_json(SYNC_CONFIGS_FILE, configs)
    return jsonify(config), 201


@app.route("/api/sync-configs/<config_id>", methods=["PUT"])
def api_update_sync_config(config_id):
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config  = next((c for c in configs if c["id"] == config_id), None)
    if not config:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True) or {}
    if "name" in data:
        config["name"] = data["name"].strip()
    if "local_path" in data:
        safe = _sanitize_path(data["local_path"])
        if not safe:
            return jsonify({"error": "Invalid local path"}), 400
        config["local_path"] = safe
    if "remote_path" in data:
        rpath = data["remote_path"].strip("/")
        if not _validate_path_component(rpath):
            return jsonify({"error": "Invalid remote path"}), 400
        config["remote_path"] = rpath
    if "direction" in data and data["direction"] in ("push", "pull", "bisync"):
        config["direction"] = data["direction"]
    save_json(SYNC_CONFIGS_FILE, configs)
    return jsonify(config)


@app.route("/api/sync-configs/<config_id>", methods=["DELETE"])
def api_delete_sync_config(config_id):
    configs = load_json(SYNC_CONFIGS_FILE, [])
    configs = [c for c in configs if c["id"] != config_id]
    save_json(SYNC_CONFIGS_FILE, configs)
    return jsonify({"ok": True})


# ── API: run & status ─────────────────────────────────────────────────────────

@app.route("/api/sync-configs/<config_id>/run", methods=["POST"])
def api_run_sync(config_id):
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config  = next((c for c in configs if c["id"] == config_id), None)
    if not config:
        return jsonify({"error": "Not found"}), 404
    with active_syncs_lock:
        if config_id in active_syncs and not active_syncs[config_id].get("done", True):
            return jsonify({"error": "Sync already running"}), 409
        active_syncs[config_id] = {
            "lines":      [],
            "done":       False,
            "success":    None,
            "started_at": datetime.now().isoformat(),
            "name":       config.get("name", config_id),
            "progress":   {},
            "direction":  config.get("direction", "push"),
        }
    t = threading.Thread(target=_run_sync_thread, args=(config_id,), daemon=True)
    t.start()
    return jsonify({"ok": True, "config_id": config_id})


@app.route("/api/sync-configs/<config_id>/stop", methods=["POST"])
def api_stop_sync(config_id):
    with active_syncs_lock:
        s = active_syncs.get(config_id)
        if not s or s.get("done"):
            return jsonify({"error": "No active sync"}), 404
        s["done"]    = True
        s["success"] = False
        s["lines"].append("[stopped] Sync stopped by user.")
    return jsonify({"ok": True})


@app.route("/api/sync-configs/<config_id>/status")
def api_sync_status(config_id):
    since = request.args.get("since", 0, type=int)
    with active_syncs_lock:
        s = active_syncs.get(config_id)
    if not s:
        return jsonify({"running": False, "lines": [], "progress": {}, "done": True})
    lines = s["lines"][since:]
    return jsonify({
        "running":    not s.get("done", True),
        "done":       s.get("done", False),
        "success":    s.get("success"),
        "lines":      lines,
        "total_lines": len(s["lines"]),
        "progress":   s.get("progress", {}),
        "started_at": s.get("started_at"),
    })


@app.route("/api/active-syncs")
def api_active_syncs():
    with active_syncs_lock:
        result = [
            {"config_id": cid, "name": s["name"], "started_at": s["started_at"],
             "progress": s.get("progress", {})}
            for cid, s in active_syncs.items() if not s.get("done", True)
        ]
    return jsonify(result)


# ── API: compare folders ──────────────────────────────────────────────────────

def _lsjson_remote(remote_path, timeout=900):
    ok, out, err = run_rclone_cmd(
        ["lsjson", remote_path, "--recursive", "--fast-list", "--no-modtime", "--no-mimetype"],
        timeout=timeout)
    if not ok:
        return False, [], err
    try:
        return True, json.loads(out) if out.strip() else [], ""
    except json.JSONDecodeError as e:
        return False, [], f"Invalid JSON: {e}"


@app.route("/api/sync-configs/<config_id>/compare", methods=["POST"])
def api_compare_folders(config_id):
    configs = load_json(SYNC_CONFIGS_FILE, [])
    config  = next((c for c in configs if c["id"] == config_id), None)
    if not config:
        return jsonify({"error": "Not found"}), 404

    env    = load_config_env()
    remote = env.get("RCLONE_REMOTE", "protondrive")
    local  = config.get("local_path", "")
    rpath  = config.get("remote_path", "")
    remote_full = f"{remote}:{rpath}" if rpath else f"{remote}:"

    if not local or not Path(local).is_dir():
        return jsonify({"error": f"Local path not found: {local}"}), 400

    # Count local
    lf = lfolders = ltotal = 0
    local_files: set = set()
    base = Path(local)
    for entry in base.rglob("*"):
        if entry.is_dir():
            lfolders += 1
        elif entry.is_file():
            lf += 1
            try: ltotal += entry.stat().st_size
            except OSError: pass
            local_files.add(str(entry.relative_to(base)))

    ok, items, err = _lsjson_remote(remote_full, timeout=900)
    if not ok:
        return jsonify({"error": f"Could not list remote: {err}"}), 500

    rfolders = rf = rtotal = 0
    remote_files: set = set()
    for item in items:
        if item.get("IsDir"):
            rfolders += 1
        else:
            rf += 1
            rtotal += item.get("Size", 0)
            remote_files.add(item.get("Path", item.get("Name", "")))

    missing_remote = sorted(local_files - remote_files)
    missing_local  = sorted(remote_files - local_files)

    return jsonify({
        "config_name":           config.get("name", ""),
        "local_path":            local,
        "remote_path":           rpath or "/",
        "local":                 {"files": lf, "folders": lfolders, "total_size": ltotal},
        "remote":                {"files": rf, "folders": rfolders, "total_size": rtotal},
        "missing_on_remote":     missing_remote[:500],
        "missing_on_remote_count": len(missing_remote),
        "missing_on_local":      missing_local[:500],
        "missing_on_local_count": len(missing_local),
        "in_sync":               len(missing_remote) == 0 and len(missing_local) == 0,
    })


# ── API: browse ───────────────────────────────────────────────────────────────

@app.route("/api/browse/local")
def api_browse_local():
    path = request.args.get("path", str(Path.home()))
    safe = _sanitize_path(path)
    if not safe or not Path(safe).is_dir():
        return jsonify({"error": "Invalid path"}), 400
    items = []
    try:
        for entry in sorted(Path(safe).iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat()
                items.append({
                    "name":    entry.name,
                    "path":    str(entry),
                    "is_dir":  entry.is_dir(),
                    "size":    stat.st_size if entry.is_file() else None,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
            except OSError:
                pass
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    parent = str(Path(safe).parent) if str(safe) != "/" else None
    return jsonify({"path": safe, "parent": parent, "items": items})


@app.route("/api/browse/remote")
def api_browse_remote():
    path = request.args.get("path", "")
    env  = load_config_env()
    remote = env.get("RCLONE_REMOTE", "protondrive")
    if not _validate_path_component(path):
        return jsonify({"error": "Invalid path"}), 400
    remote_path = f"{remote}:{path}"
    ok, out, err = run_rclone_cmd(
        ["lsjson", remote_path, "--no-modtime", "--no-mimetype"], timeout=30)
    if not ok:
        return jsonify({"error": err[:300]}), 500
    try:
        items = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        items = []
    formatted = [{
        "name":   item.get("Name", ""),
        "path":   (path.rstrip("/") + "/" + item["Name"]).lstrip("/") if path else item["Name"],
        "is_dir": item.get("IsDir", False),
        "size":   item.get("Size"),
    } for item in sorted(items, key=lambda i: (not i.get("IsDir"), i.get("Name", "").lower()))]
    parent = "/".join(path.split("/")[:-1]) if "/" in path else ""
    return jsonify({"path": path, "remote": remote, "parent": parent, "items": formatted})


# ── API: logs ─────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_get_logs():
    log_file = LOG_DIR / "protondrive.log"
    if not log_file.exists():
        return jsonify({"lines": []})
    try:
        lines = log_file.read_text(errors="replace").splitlines()[-500:]
        return jsonify({"lines": lines})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print("=" * 50)
    print("  Proton Drive Sync")
    print(f"  http://localhost:{args.port}")
    print("=" * 50)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
