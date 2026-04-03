"""
Dropbox → Proton Drive migration engine using rclone.

Handles:
 - Configuring Dropbox remote via rclone
 - Browsing Dropbox folders
 - Running migration transfers with real-time progress
 - Pause / resume / cancel support
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from ..progress import ProgressTracker, emit_progress, register_operation, unregister_operation

log = logging.getLogger(__name__)

# Active migrations keyed by migration_id
_active_migrations = {}
_migrations_lock = threading.Lock()


def check_dropbox_remote():
    """Check if a Dropbox remote is configured in rclone."""
    try:
        result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            remotes = [r.strip().rstrip(":") for r in result.stdout.strip().split("\n") if r.strip()]
            dropbox_remotes = []
            for r in remotes:
                try:
                    cfg = subprocess.run(
                        ["rclone", "config", "show", r],
                        capture_output=True, text=True, timeout=10
                    )
                    if "type = dropbox" in cfg.stdout.lower():
                        dropbox_remotes.append(r)
                except Exception:
                    pass
            return {"ok": True, "remotes": dropbox_remotes, "all_remotes": remotes}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "remotes": [], "all_remotes": []}


def configure_dropbox_remote(remote_name="dropbox"):
    """Configure a Dropbox remote using rclone config create."""
    try:
        result = subprocess.run(
            ["rclone", "config", "create", remote_name, "dropbox"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "RCLONE_CONFIG_PASS": ""}
        )
        if result.returncode == 0:
            return {"ok": True, "remote": remote_name, "output": result.stdout}
        return {"ok": False, "error": result.stderr or result.stdout}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def test_dropbox_connection(remote_name="dropbox"):
    """Test connectivity to a Dropbox remote."""
    try:
        result = subprocess.run(
            ["rclone", "about", f"{remote_name}:"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            info = {}
            for line in result.stdout.strip().split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    info[k.strip().lower()] = v.strip()
            return {"ok": True, "info": info}
        return {"ok": False, "error": result.stderr or "Connection failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def browse_remote(remote_name, path=""):
    """Browse a remote directory."""
    remote_path = f"{remote_name}:{path}" if path else f"{remote_name}:"
    try:
        result = subprocess.run(
            ["rclone", "lsjson", remote_path, "--no-modtime", "--no-mimetype"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            items = json.loads(result.stdout)
            entries = []
            for item in sorted(items, key=lambda x: (not x.get("IsDir", False), x.get("Name", "").lower())):
                entries.append({
                    "name": item.get("Name", ""),
                    "path": os.path.join(path, item["Name"]) if path else item["Name"],
                    "is_dir": item.get("IsDir", False),
                    "size": item.get("Size", 0),
                })
            return {"ok": True, "entries": entries, "path": path or "/"}
        elif result.returncode == 0:
            return {"ok": True, "entries": [], "path": path or "/"}
        return {"ok": False, "error": result.stderr}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_folder_size(remote_name, path=""):
    """Get total size and file count for a remote folder."""
    remote_path = f"{remote_name}:{path}" if path else f"{remote_name}:"
    try:
        result = subprocess.run(
            ["rclone", "size", remote_path, "--json"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                "ok": True,
                "count": data.get("count", 0),
                "bytes": data.get("bytes", 0),
            }
        return {"ok": False, "error": result.stderr}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def dry_run_migration(source_remote, source_path, dest_remote, dest_path,
                      filters=None, preserve_structure=True):
    """Perform a dry-run to preview what will be transferred."""
    src = f"{source_remote}:{source_path}" if source_path else f"{source_remote}:"
    dst = f"{dest_remote}:{dest_path}" if dest_path else f"{dest_remote}:"

    args = ["rclone", "copy", src, dst, "--dry-run", "-v", "--stats", "0"]

    if filters:
        for ext in filters.get("include_extensions", []):
            args += ["--include", f"*.{ext}"]
        for ext in filters.get("exclude_extensions", []):
            args += ["--exclude", f"*.{ext}"]
        if filters.get("min_size"):
            args += ["--min-size", filters["min_size"]]
        if filters.get("max_size"):
            args += ["--max-size", filters["max_size"]]

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=300,
            env={**os.environ, "HOME": str(Path.home())}
        )
        # Parse dry-run output
        files = []
        for line in result.stderr.split("\n"):
            # Match: NOTICE: file.txt: Skipped copy as --dry-run is set (size 1.234k)
            m = re.search(r'NOTICE:\s+(.+?):\s+Skipped\s+copy', line)
            if m:
                files.append(m.group(1).strip())

        return {
            "ok": True,
            "files": files,
            "count": len(files),
            "output": result.stderr[-2000:],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def start_migration(migration_id, source_remote, source_path,
                    dest_remote, dest_path, options=None):
    """
    Start a migration in a background thread.
    Returns immediately. Progress is broadcast via WebSocket.
    """
    options = options or {}

    tracker = ProgressTracker(
        operation_id=migration_id,
        operation_type="migration",
        unit="bytes",
    )
    register_operation(tracker)

    migration = {
        "id": migration_id,
        "source_remote": source_remote,
        "source_path": source_path,
        "dest_remote": dest_remote,
        "dest_path": dest_path,
        "options": options,
        "tracker": tracker,
        "status": "running",
        "process": None,
        "paused": False,
        "cancelled": False,
        "lines": [],
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "result": None,
    }

    with _migrations_lock:
        _active_migrations[migration_id] = migration

    thread = threading.Thread(
        target=_run_migration, args=(migration,), daemon=True
    )
    thread.start()

    return {"ok": True, "migration_id": migration_id}


def _run_migration(migration):
    """Execute the rclone migration command with streaming progress."""
    mid = migration["id"]
    tracker = migration["tracker"]
    options = migration["options"]

    src = f"{migration['source_remote']}:{migration['source_path']}" \
        if migration["source_path"] else f"{migration['source_remote']}:"
    dst = f"{migration['dest_remote']}:{migration['dest_path']}" \
        if migration["dest_path"] else f"{migration['dest_remote']}:"

    mode = options.get("mode", "copy")  # copy or sync
    cmd = ["rclone", mode, src, dst]

    # Transfer options
    transfers = str(options.get("transfers", 4))
    cmd += ["--transfers", transfers]
    cmd += ["--checkers", str(options.get("checkers", 8))]

    if options.get("bandwidth"):
        cmd += ["--bwlimit", options["bandwidth"]]

    # Filters
    filters = options.get("filters", {})
    for ext in filters.get("include_extensions", []):
        cmd += ["--include", f"*.{ext}"]
    for ext in filters.get("exclude_extensions", []):
        cmd += ["--exclude", f"*.{ext}"]
    if filters.get("min_size"):
        cmd += ["--min-size", filters["min_size"]]
    if filters.get("max_size"):
        cmd += ["--max-size", filters["max_size"]]

    # Progress stats
    cmd += ["--stats", "1s", "--stats-one-line", "-v"]
    cmd += ["--log-level", "INFO"]

    tracker.update(message=f"Starting {mode}: {src} → {dst}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "HOME": str(Path.home())},
        )
        migration["process"] = proc

        for line in proc.stdout:
            line = line.rstrip()
            migration["lines"].append(line)

            # Check cancellation
            if migration["cancelled"]:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                tracker.fail(error="Migration cancelled by user")
                migration["status"] = "cancelled"
                migration["finished_at"] = datetime.now().isoformat()
                emit_progress("migration_log", {
                    "migration_id": mid, "line": "[Cancelled by user]"
                })
                return

            # Parse progress
            progress = _parse_rclone_line(line)
            if progress:
                extra = {"migration_id": mid}
                extra.update(progress)
                if "bytes_total_raw" in progress:
                    tracker.update(
                        current=progress.get("bytes_done_raw", 0),
                        total=progress.get("bytes_total_raw", 0),
                        message=progress.get("status_line", ""),
                        extra=extra,
                    )
                elif "percent" in progress:
                    tracker.update(
                        message=progress.get("status_line", line),
                        extra=extra,
                    )

            # Emit log line
            emit_progress("migration_log", {
                "migration_id": mid, "line": line
            })

        proc.wait()
        rc = proc.returncode

        if rc == 0:
            tracker.complete(message="Migration completed successfully")
            migration["status"] = "completed"
        else:
            tracker.fail(error=f"rclone exited with code {rc}")
            migration["status"] = "failed"

    except Exception as e:
        tracker.fail(error=str(e))
        migration["status"] = "failed"
        log.error("Migration %s error: %s", mid, e)

    migration["finished_at"] = datetime.now().isoformat()
    unregister_operation(mid)


def _parse_rclone_line(line):
    """Parse an rclone progress/stats line."""
    progress = {}

    # Transferred bytes: "Transferred:   5.000 MiB / 23.000 MiB, 22%, 512 KiB/s, ETA 1m30s"
    xfer = re.search(
        r'Transferred:\s+([\d.]+\s*\S+)\s*/\s*([\d.]+\s*\S+),\s*(\d+)%'
        r'(?:,\s*([\d.]+\s*\S+/s))?'
        r'(?:,\s*ETA\s+([\w:]+))?',
        line
    )
    if xfer:
        progress["bytes_done"] = xfer.group(1).strip()
        progress["bytes_total"] = xfer.group(2).strip()
        progress["percent"] = int(xfer.group(3))
        progress["bytes_done_raw"] = _parse_size(xfer.group(1).strip())
        progress["bytes_total_raw"] = _parse_size(xfer.group(2).strip())
        if xfer.group(4):
            progress["speed"] = xfer.group(4).strip()
        if xfer.group(5):
            progress["eta"] = xfer.group(5).strip()
        progress["status_line"] = line.strip()

    # File count: "Transferred:            5 / 23, 22%"
    count = re.search(r'Transferred:\s+(\d+)\s*/\s*(\d+),\s*(\d+)%', line)
    if count and "bytes_done" not in progress:
        progress["files_done"] = int(count.group(1))
        progress["files_total"] = int(count.group(2))
        progress["percent"] = int(count.group(3))

    # Errors
    err = re.search(r'Errors:\s+(\d+)', line)
    if err:
        progress["errors"] = int(err.group(1))

    # Current file
    cf = re.search(r'\*\s+(.+?):\s+(\d+)%\s*/\s*([\d.]+\s*\S+)', line)
    if cf:
        progress["current_file"] = cf.group(1).strip()
        progress["current_file_percent"] = int(cf.group(2))

    return progress if progress else None


def _parse_size(size_str):
    """Parse rclone size string like '5.000 MiB' to bytes."""
    try:
        parts = size_str.strip().split()
        num = float(parts[0])
        unit = parts[1].lower() if len(parts) > 1 else "b"
        multipliers = {
            "b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
            "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
        }
        return int(num * multipliers.get(unit, 1))
    except Exception:
        return 0


def cancel_migration(migration_id):
    """Cancel a running migration."""
    with _migrations_lock:
        m = _active_migrations.get(migration_id)
        if m and m["status"] == "running":
            m["cancelled"] = True
            return {"ok": True}
    return {"ok": False, "error": "Migration not found or not running"}


def get_migration_status(migration_id):
    """Get current status of a migration."""
    with _migrations_lock:
        m = _active_migrations.get(migration_id)
        if not m:
            return None
        tracker = m["tracker"]
        snap = tracker._snapshot()
        snap["migration_status"] = m["status"]
        snap["started_at"] = m["started_at"]
        snap["finished_at"] = m["finished_at"]
        snap["line_count"] = len(m["lines"])
        return snap


def get_migration_logs(migration_id, since=0):
    """Get log lines from a migration."""
    with _migrations_lock:
        m = _active_migrations.get(migration_id)
        if not m:
            return []
        return m["lines"][since:]


def list_active_migrations():
    """List all active/recent migrations."""
    with _migrations_lock:
        result = {}
        for mid, m in _active_migrations.items():
            result[mid] = {
                "id": mid,
                "status": m["status"],
                "source": f"{m['source_remote']}:{m['source_path']}",
                "dest": f"{m['dest_remote']}:{m['dest_path']}",
                "started_at": m["started_at"],
                "finished_at": m["finished_at"],
            }
        return result
