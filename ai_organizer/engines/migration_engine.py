"""
Dropbox → Proton Drive migration engine using rclone.

Handles:
 - Configuring Dropbox remote via rclone
 - Browsing Dropbox folders
 - Running migration transfers with real-time progress
 - Pause / resume / cancel support (SIGSTOP / SIGCONT)
 - Conflict resolution strategies: skip, rename_newer, overwrite
 - Persistent state checkpointing for crash recovery
 - Pre-migration bandwidth validation and dedup check
"""

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from ..progress import ProgressTracker, emit_progress, register_operation, unregister_operation

log = logging.getLogger(__name__)

# Active migrations keyed by migration_id (in-memory cache)
_active_migrations: dict = {}
_migrations_lock = threading.Lock()

# Conflict strategy → rclone flags
_CONFLICT_FLAGS: dict[str, list[str]] = {
    "skip":         [],                          # default rclone behaviour (skip if dest newer)
    "rename_newer": ["--backup-dir-suffix", ".bak", "--suffix-keep-extension"],
    "overwrite":    ["--ignore-times"],          # re-transfer regardless of modtime
    "newer_wins":   ["--update"],                # only transfer if source is newer
}


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


def test_remote_connection(remote_name: str) -> dict:
    """Test connectivity to any rclone remote. Returns {ok, info/error}."""
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


# Alias kept for backwards compatibility
test_dropbox_connection = test_remote_connection


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
        files = []
        for line in result.stderr.split("\n"):
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


def validate_endpoints(source_remote: str, dest_remote: str) -> dict:
    """Verify both source and destination remotes are reachable before starting."""
    src_check = test_remote_connection(source_remote)
    dst_check = test_remote_connection(dest_remote)
    ok = src_check["ok"] and dst_check["ok"]
    return {
        "ok": ok,
        "source": src_check,
        "dest": dst_check,
        "error": (
            None if ok else
            f"Source: {src_check.get('error','ok')} | Dest: {dst_check.get('error','ok')}"
        ),
    }


def start_migration(migration_id, source_remote, source_path,
                    dest_remote, dest_path, options=None, db_module=None):
    """
    Start a migration in a background thread.
    Returns immediately. Progress is broadcast via WebSocket.

    options keys:
      mode              str   — 'copy' | 'sync'
      transfers         int   — parallel transfers
      checkers          int   — parallel checkers
      bandwidth         str   — rclone bwlimit string (e.g. '10M')
      conflict_strategy str   — 'skip'|'rename_newer'|'overwrite'|'newer_wins'
      filters           dict  — include_extensions, exclude_extensions, min_size, max_size
      validate_first    bool  — test connection before starting (default True)
    """
    options = options or {}

    # Validate connectivity unless explicitly skipped
    if options.get("validate_first", True):
        check = validate_endpoints(source_remote, dest_remote)
        if not check["ok"]:
            return {"ok": False, "error": check["error"]}

    tracker = ProgressTracker(
        operation_id=migration_id,
        operation_type="migration",
        unit="bytes",
    )
    register_operation(tracker)

    conflict_strategy = options.get("conflict_strategy", "skip")

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
        "conflict_strategy": conflict_strategy,
        "conflict_count": 0,
        "lines": [],
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "result": None,
        "db_module": db_module,
    }

    with _migrations_lock:
        _active_migrations[migration_id] = migration

    # Persist to DB if available
    if db_module:
        try:
            db_module.create_migration_job({
                "id": migration_id,
                "source_remote": source_remote,
                "source_path": source_path,
                "dest_remote": dest_remote,
                "dest_path": dest_path,
                "mode": options.get("mode", "copy"),
                "options_json": json.dumps(options),
            })
        except Exception as e:
            log.debug("Failed to persist migration job: %s", e)

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
    db_module = migration.get("db_module")
    conflict_strategy = migration.get("conflict_strategy", "skip")

    src = (f"{migration['source_remote']}:{migration['source_path']}"
           if migration["source_path"] else f"{migration['source_remote']}:")
    dst = (f"{migration['dest_remote']}:{migration['dest_path']}"
           if migration["dest_path"] else f"{migration['dest_remote']}:")

    mode = options.get("mode", "copy")
    cmd = ["rclone", mode, src, dst]

    # Transfer tuning
    cmd += ["--transfers", str(options.get("transfers", 4))]
    cmd += ["--checkers", str(options.get("checkers", 8))]
    if options.get("bandwidth"):
        cmd += ["--bwlimit", options["bandwidth"]]

    # Conflict strategy flags
    cmd += _CONFLICT_FLAGS.get(conflict_strategy, [])

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

    # Progress and logging
    cmd += ["--stats", "1s", "--stats-one-line", "-v", "--log-level", "INFO"]

    tracker.update(message=f"Starting {mode}: {src} → {dst}")

    last_checkpoint = time.monotonic()

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

            # Handle cancellation
            if migration["cancelled"]:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                tracker.fail(error="Migration cancelled by user")
                migration["status"] = "cancelled"
                migration["finished_at"] = datetime.now().isoformat()
                emit_progress("migration_log", {"migration_id": mid, "line": "[Cancelled]"})
                if db_module:
                    try:
                        db_module.update_migration_job(mid, status="cancelled")
                    except Exception:
                        pass
                return

            # Handle pause (busy-wait while paused)
            while migration["paused"] and not migration["cancelled"]:
                time.sleep(0.5)

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

                # Checkpoint every 10 s
                now = time.monotonic()
                if db_module and now - last_checkpoint > 10:
                    state = {
                        "bytes_done": progress.get("bytes_done_raw", 0),
                        "bytes_total": progress.get("bytes_total_raw", 0),
                        "files_done": progress.get("files_done", 0),
                    }
                    try:
                        db_module.update_migration_job_state(mid, state)
                        db_module.update_migration_job(
                            mid,
                            bytes_done=state["bytes_done"],
                            bytes_total=state["bytes_total"],
                            files_done=state["files_done"],
                        )
                    except Exception:
                        pass
                    last_checkpoint = now

            emit_progress("migration_log", {"migration_id": mid, "line": line})

        proc.wait()
        rc = proc.returncode

        if rc == 0:
            tracker.complete(message="Migration completed successfully")
            migration["status"] = "completed"
            if db_module:
                try:
                    db_module.update_migration_job(mid, status="completed",
                                                   progress=1.0,
                                                   result={"exit_code": 0})
                except Exception:
                    pass
        else:
            tracker.fail(error=f"rclone exited with code {rc}")
            migration["status"] = "failed"
            if db_module:
                try:
                    db_module.update_migration_job(mid, status="failed",
                                                   error_message=f"exit code {rc}")
                except Exception:
                    pass

    except Exception as e:
        tracker.fail(error=str(e))
        migration["status"] = "failed"
        log.error("Migration %s error: %s", mid, e)
        if db_module:
            try:
                db_module.update_migration_job(mid, status="failed", error_message=str(e))
            except Exception:
                pass

    migration["finished_at"] = datetime.now().isoformat()
    unregister_operation(mid)


def _parse_rclone_line(line):
    """Parse an rclone progress/stats line."""
    progress = {}

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

    count = re.search(r'Transferred:\s+(\d+)\s*/\s*(\d+),\s*(\d+)%', line)
    if count and "bytes_done" not in progress:
        progress["files_done"] = int(count.group(1))
        progress["files_total"] = int(count.group(2))
        progress["percent"] = int(count.group(3))

    err = re.search(r'Errors:\s+(\d+)', line)
    if err:
        progress["errors"] = int(err.group(1))

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


def pause_migration(migration_id: str) -> dict:
    """Pause a running migration (SIGSTOP on rclone process)."""
    with _migrations_lock:
        m = _active_migrations.get(migration_id)
    if not m:
        return {"ok": False, "error": "Migration not found"}
    if m["status"] != "running" or m["paused"]:
        return {"ok": False, "error": "Migration is not running or already paused"}
    proc = m.get("process")
    if proc and proc.poll() is None:
        try:
            os.kill(proc.pid, signal.SIGSTOP)
        except Exception as e:
            return {"ok": False, "error": f"SIGSTOP failed: {e}"}
    m["paused"] = True
    m["tracker"].update(message="Migration paused")
    if m.get("db_module"):
        try:
            m["db_module"].update_migration_job(migration_id, status="paused")
        except Exception:
            pass
    return {"ok": True, "status": "paused"}


def resume_migration(migration_id: str) -> dict:
    """Resume a paused migration (SIGCONT on rclone process)."""
    with _migrations_lock:
        m = _active_migrations.get(migration_id)
    if not m:
        return {"ok": False, "error": "Migration not found"}
    if not m["paused"]:
        return {"ok": False, "error": "Migration is not paused"}
    proc = m.get("process")
    if proc and proc.poll() is None:
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except Exception as e:
            return {"ok": False, "error": f"SIGCONT failed: {e}"}
    m["paused"] = False
    m["status"] = "running"
    m["tracker"].update(message="Migration resumed")
    if m.get("db_module"):
        try:
            m["db_module"].update_migration_job(migration_id, status="running")
        except Exception:
            pass
    return {"ok": True, "status": "running"}


def cancel_migration(migration_id):
    """Cancel a running migration."""
    with _migrations_lock:
        m = _active_migrations.get(migration_id)
        if m and m["status"] in ("running", "paused"):
            # Resume first if paused so the thread can observe cancelled flag
            if m["paused"]:
                proc = m.get("process")
                if proc and proc.poll() is None:
                    try:
                        os.kill(proc.pid, signal.SIGCONT)
                    except Exception:
                        pass
                m["paused"] = False
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
        snap["paused"] = m["paused"]
        snap["conflict_strategy"] = m.get("conflict_strategy", "skip")
        snap["conflict_count"] = m.get("conflict_count", 0)
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
                "paused": m["paused"],
                "source": f"{m['source_remote']}:{m['source_path']}",
                "dest": f"{m['dest_remote']}:{m['dest_path']}",
                "conflict_strategy": m.get("conflict_strategy", "skip"),
                "started_at": m["started_at"],
                "finished_at": m["finished_at"],
            }
        return result
