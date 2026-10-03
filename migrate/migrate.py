#!/usr/bin/env python3
"""Proton Drive to NAS migration runner with a one-page status UI.

Reads migration.sqlite (built by manifest.py), downloads the pending jobs with the
official Proton Drive CLI inside a time window, verifies every file by SHA1 against
the inventory, restores modification times and moves files to their destination.
It never deletes anything and never changes anything in Proton Drive.
"""

import hashlib
import http.server
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

DEBUG = True

STATE_DIR = Path(os.environ.get("MIGRATE_STATE", "/state"))
DB_PATH = STATE_DIR / "migration.sqlite"
LOG_DIR = STATE_DIR / "logs"
CLI_PATH = Path(os.environ.get("MIGRATE_CLI", "/usr/local/bin/proton-drive"))
STAGING_DIR = Path(os.environ.get("MIGRATE_STAGING", "/data/.proton-staging"))
DEST_ROOTS = {
    "immich": Path(os.environ.get("MIGRATE_DEST_IMMICH", "/data/photos/proton-import")),
    "adult": Path(os.environ.get("MIGRATE_DEST_ADULT", "/data/adult-archive")),
    "pdfox": Path(os.environ.get("MIGRATE_DEST_PDFOX", "/home-pdfox/Proton Drive")),
}
BAD_DIR_NAME = "_bad"
UI_HOST = os.environ.get("MIGRATE_UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("MIGRATE_UI_PORT", "8104"))
INDEX_HTML = Path(__file__).resolve().parent / "index.html"

LOOP_SLEEP_S = 10
CHILD_POLL_S = 2
CHILD_TERMINATE_GRACE_S = 30
PROGRESS_SAMPLE_S = 30
ERROR_TAIL_CHARS = 400
LOG_TAIL_LINES = 60
HASH_CHUNK = 1024 * 1024
LOGIN_NEEDED_TEXT = "You need to login first"
CONFLICT_SUFFIX = " (proton)"

DEFAULT_CONFIG = {
    "enabled": "0",
    "weekend_only": "1",
    "window_start": "fri 20:00",
    "window_end": "mon 06:00",
    "quiet_start": "03:25",
    "quiet_end": "05:30",
    "phase": "download",
}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MINUTES_PER_DAY = 1440
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY

PHASE_DOWNLOAD = "download"
PHASE_VERIFY = "verify"
PHASE_PLACE = "place"
PHASE_DONE = "done"

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"

FILE_PENDING = "pending"
FILE_VERIFIED = "verified"
FILE_PLACED = "placed"
FILE_SKIPPED = "skipped"

CLI_ARGS = {
    "folder": ["filesystem", "download", "--file-conflict-strategy", "skip", "--folder-conflict-strategy", "merge"],
    "file": ["filesystem", "download", "--file-conflict-strategy", "skip", "--folder-conflict-strategy", "merge"],
    "photos": ["photo", "download", "--conflict-strategy", "rename"],
}

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS staged (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha1 TEXT NOT NULL
);
"""

LOGIN_IDLE = "idle"
LOGIN_WAITING = "waiting"
LOGIN_OK = "ok"
LOGIN_FAILED = "failed"

stop_current = threading.Event()  # set to interrupt the running CLI job
shutdown = threading.Event()
runtime = {"job": None, "child": None, "started": None, "staged_bytes": 0, "sampled_at": 0.0, "speed": 0.0,
           "login": {"status": LOGIN_IDLE, "url": None, "message": ""}}
runtime_lock = threading.Lock()


# ── Pure helpers ─────────────────────────────────────────────────────────────

def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def parse_week_point(text):
    """'fri 20:00' -> minutes since Monday 00:00. Returns None on bad input."""
    try:
        day, clock = text.strip().lower().split()
        hours, minutes = clock.split(":")
        return WEEKDAYS.index(day[:3]) * MINUTES_PER_DAY + int(hours) * 60 + int(minutes)
    except (ValueError, IndexError):
        return None


def parse_day_point(text):
    """'03:25' -> minutes since midnight. Returns None on bad input."""
    try:
        hours, minutes = text.strip().split(":")
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return None


def in_range(point, start, end, modulus):
    """True when point lies in [start, end) on a circle of the given size."""
    if start == end:
        return False
    if start < end:
        return start <= point < end
    return point >= start or point < end


def window_allows(cfg, moment):
    """Decide whether downloads may run at this moment under the config."""
    day_minute = moment.hour * 60 + moment.minute
    quiet_start, quiet_end = parse_day_point(cfg["quiet_start"]), parse_day_point(cfg["quiet_end"])
    if quiet_start is not None and quiet_end is not None and in_range(day_minute, quiet_start, quiet_end, MINUTES_PER_DAY):
        return False
    if cfg["weekend_only"] != "1":
        return True
    start, end = parse_week_point(cfg["window_start"]), parse_week_point(cfg["window_end"])
    if start is None or end is None:
        return False
    return in_range(moment.weekday() * MINUTES_PER_DAY + day_minute, start, end, MINUTES_PER_WEEK)


def iso_to_epoch(text):
    """Proton ISO timestamp -> epoch seconds, or None."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def conflict_name(path):
    """'/x/a.jpg' -> '/x/a (proton).jpg'."""
    return path.with_name(f"{path.stem}{CONFLICT_SUFFIX}{path.suffix}")


def tail_text(text):
    return (text or "").strip()[-ERROR_TAIL_CHARS:]


# ── IO: database ─────────────────────────────────────────────────────────────

def connect_db():
    """One connection per thread; SQLite connections must not be shared across threads."""
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = connect_db()
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(EXTRA_SCHEMA)
    for key, value in DEFAULT_CONFIG.items():
        db.execute("INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (key, value))
    db.execute("UPDATE jobs SET status = ? WHERE status = ?", (JOB_PENDING, JOB_RUNNING))
    db.commit()
    return db


def get_config(db):
    return {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM config")}


def set_config(db, key, value):
    db.execute("INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)", (key, str(value)))
    db.commit()


def log(db, message, level="INFO"):
    if DEBUG:
        print(f"{now_iso()} {level} {message}", flush=True)
    db.execute("INSERT INTO log(ts, level, message) VALUES (?, ?, ?)", (now_iso(), level, message))
    db.commit()


# ── IO: downloading ──────────────────────────────────────────────────────────

def job_local_dir(job):
    """Folder the CLI is pointed at; it creates the remote item's own name inside it."""
    return STAGING_DIR / job["local_rel"]


def job_target(job):
    """The staged path the job actually fills, used for progress measurement."""
    if job["kind"] == "photos":
        return job_local_dir(job)
    return job_local_dir(job) / job["remote_path"].rsplit("/", 1)[-1]


def measure_bytes(path):
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else 0


def run_job(db, job):
    """Run one CLI download job. Returns True when the job finished without failures."""
    local_dir = job_local_dir(job)
    local_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    job_log = LOG_DIR / f"job-{job['id']}.log"
    args = [str(CLI_PATH), *CLI_ARGS[job["kind"]], job["remote_path"], str(local_dir), "--json"]
    db.execute("UPDATE jobs SET status = ?, attempts = attempts + 1, started_at = ?, error = NULL WHERE id = ?",
               (JOB_RUNNING, now_iso(), job["id"]))
    db.commit()
    log(db, f"job {job['id']} start: {job['kind']} {job['remote_path']}")

    with open(job_log, "ab") as handle:
        child = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT)
        with runtime_lock:
            runtime.update(job=dict(job), child=child, started=time.time(), staged_bytes=0, sampled_at=0.0, speed=0.0)
        while child.poll() is None:
            if stop_current.is_set():
                child.terminate()
                try:
                    child.wait(CHILD_TERMINATE_GRACE_S)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                break
            sample_progress(job_target(job))
            time.sleep(CHILD_POLL_S)
    with runtime_lock:
        runtime.update(job=None, child=None, staged_bytes=0, speed=0.0)

    output = job_log.read_text(errors="replace")
    if stop_current.is_set():
        db.execute("UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?", (JOB_PENDING, now_iso(), job["id"]))
        db.commit()
        log(db, f"job {job['id']} interrupted; will resume")
        return False
    if LOGIN_NEEDED_TEXT in output:
        db.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                   (JOB_FAILED, now_iso(), LOGIN_NEEDED_TEXT, job["id"]))
        set_config(db, "enabled", "0")
        log(db, "CLI session missing: run auth login, then press Start", "ERROR")
        return False
    if child.returncode == 0:
        db.execute("UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?", (JOB_DONE, now_iso(), job["id"]))
        db.commit()
        log(db, f"job {job['id']} done")
        return True
    db.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
               (JOB_FAILED, now_iso(), tail_text(output), job["id"]))
    db.commit()
    log(db, f"job {job['id']} failed (exit {child.returncode}); see logs/job-{job['id']}.log", "ERROR")
    return False


def sample_progress(target):
    """Measure bytes staged for the running job, at most every PROGRESS_SAMPLE_S."""
    now = time.time()
    with runtime_lock:
        if now - runtime["sampled_at"] < PROGRESS_SAMPLE_S:
            return
        previous_bytes, previous_at = runtime["staged_bytes"], runtime["sampled_at"]
    total = measure_bytes(target)
    with runtime_lock:
        runtime["staged_bytes"] = total
        runtime["sampled_at"] = now
        if previous_at:
            runtime["speed"] = max(0.0, (total - previous_bytes) / (now - previous_at))


def set_login(status, url=None, message=""):
    with runtime_lock:
        runtime["login"] = {"status": status, "url": url, "message": message}


def login_in_progress():
    with runtime_lock:
        return runtime["login"]["status"] == LOGIN_WAITING


def run_login():
    """Run `auth login`, publish the sign-in link for the UI and wait for the browser side."""
    db = connect_db()
    set_login(LOGIN_WAITING, message="starting")
    log(db, "sign-in started; open the link shown in the UI")
    output = []
    try:
        child = subprocess.Popen([str(CLI_PATH), "auth", "login", "--json"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        for line in child.stdout:
            output.append(line)
            if '"signInUrl"' in line:
                try:
                    set_login(LOGIN_WAITING, url=json.loads(line)["signInUrl"], message="waiting for you to sign in")
                except (json.JSONDecodeError, KeyError):
                    pass
        child.wait()
        returncode = child.returncode
    except OSError as error:
        output.append(str(error))
        returncode = -1
    if returncode == 0:
        set_login(LOGIN_OK, message="signed in")
        log(db, "sign-in successful")
    else:
        set_login(LOGIN_FAILED, message=tail_text("".join(output)))
        log(db, "sign-in did not complete (link expired or was rejected); request a new one", "WARN")


# ── IO: verification and placement ───────────────────────────────────────────

def sha1_of(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def staged_sha1(db, path):
    """SHA1 of a staged file, cached by size and mtime."""
    stat = path.stat()
    row = db.execute("SELECT size, mtime, sha1 FROM staged WHERE path = ?", (str(path),)).fetchone()
    if row and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
        return row["sha1"]
    digest = sha1_of(path)
    db.execute("INSERT OR REPLACE INTO staged(path, size, mtime, sha1) VALUES (?, ?, ?, ?)",
               (str(path), stat.st_size, stat.st_mtime, digest))
    db.commit()
    return digest


def verify_staging(db):
    """Match every staged file to the inventory by SHA1; quarantine files that match nothing."""
    expected = {row["sha1"]: row["size"] for row in db.execute("SELECT sha1, size FROM files")}
    found = {}
    bad_dir = STAGING_DIR / BAD_DIR_NAME
    for path in sorted(p for p in STAGING_DIR.rglob("*") if p.is_file()):
        if bad_dir in path.parents:
            continue
        digest = staged_sha1(db, path)
        if digest in expected and expected[digest] == path.stat().st_size:
            found.setdefault(digest, str(path))
            continue
        target = bad_dir / path.relative_to(STAGING_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        log(db, f"quarantined unexpected or incomplete file: {path.relative_to(STAGING_DIR)}", "WARN")
    verified = 0
    for row in db.execute("SELECT uid, sha1 FROM files WHERE status = ?", (FILE_PENDING,)).fetchall():
        if row["sha1"] in found:
            db.execute("UPDATE files SET status = ?, checked_at = ? WHERE uid = ?", (FILE_VERIFIED, now_iso(), row["uid"]))
            verified += 1
    db.commit()
    missing = db.execute("SELECT COUNT(*) FROM files WHERE status = ?", (FILE_PENDING,)).fetchone()[0]
    log(db, f"verify: {verified} newly verified, {missing} still missing")
    return found


def place_files(db, found):
    """Move verified files to their destinations; extra copies of the same content become hard links."""
    placed_by_sha1 = {row["sha1"]: row["placed_path"] for row in db.execute(
        "SELECT sha1, placed_path FROM files WHERE status = ? AND placed_path IS NOT NULL", (FILE_PLACED,))}
    for row in db.execute("SELECT * FROM files WHERE status = ? ORDER BY path", (FILE_VERIFIED,)).fetchall():
        target = DEST_ROOTS[row["dest"]] / row["dest_rel"]
        if target.exists():
            log(db, f"destination exists, placing beside it: {row['dest']}/{row['dest_rel']}", "WARN")
            target = conflict_name(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = placed_by_sha1.get(row["sha1"]) or found.get(row["sha1"])
        if source is None:
            continue
        if row["sha1"] in placed_by_sha1:
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
        else:
            shutil.move(source, str(target))
            placed_by_sha1[row["sha1"]] = str(target)
        epoch = iso_to_epoch(row["mtime"])
        if epoch:
            os.utime(target, (epoch, epoch))
        db.execute("UPDATE files SET status = ?, placed_path = ? WHERE uid = ?", (FILE_PLACED, str(target), row["uid"]))
        db.commit()
    log(db, "place: finished")


# ── Runner loop ──────────────────────────────────────────────────────────────

def next_job(db):
    return db.execute("SELECT * FROM jobs WHERE status = ? ORDER BY expected_bytes, id LIMIT 1", (JOB_PENDING,)).fetchone()


def runner(db):
    while not shutdown.is_set():
        cfg = get_config(db)
        phase = cfg["phase"]
        if phase == PHASE_DOWNLOAD:
            may_run = cfg["enabled"] == "1" and window_allows(cfg, datetime.now()) and not login_in_progress()
            job = next_job(db) if may_run else None
            if job is not None:
                stop_current.clear()
                run_job(db, job)
                continue
            if cfg["enabled"] == "1" and next_job(db) is None:
                set_config(db, "phase", PHASE_VERIFY)
                continue
        elif phase == PHASE_VERIFY:
            found = verify_staging(db)
            set_config(db, "phase", PHASE_PLACE)
            place_files(db, found)
            set_config(db, "phase", PHASE_DONE)
            continue
        shutdown.wait(LOOP_SLEEP_S)


def window_watchdog():
    """Interrupt the running job when the window closes or the run is paused."""
    db = connect_db()
    while not shutdown.is_set():
        cfg = get_config(db)
        with runtime_lock:
            running = runtime["child"] is not None
        if running and (cfg["enabled"] != "1" or not window_allows(cfg, datetime.now())):
            stop_current.set()
        shutdown.wait(LOOP_SLEEP_S)


# ── HTTP UI ──────────────────────────────────────────────────────────────────

def state_snapshot(db):
    cfg = get_config(db)
    with runtime_lock:
        current = dict(runtime)
    child = current.pop("child")
    current["running"] = child is not None
    totals = db.execute("""
        SELECT
          SUM(status != 'skipped') AS to_migrate,
          SUM(CASE WHEN status != 'skipped' THEN size ELSE 0 END) AS bytes_to_migrate,
          SUM(status IN ('verified','placed')) AS verified,
          SUM(CASE WHEN status IN ('verified','placed') THEN size ELSE 0 END) AS bytes_verified,
          SUM(status = 'placed') AS placed,
          SUM(status = 'skipped') AS already_on_nas
        FROM files""").fetchone()
    jobs = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY status = 'running' DESC, status, expected_bytes DESC")]
    job_bytes = db.execute("SELECT status, SUM(expected_bytes) AS b, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    logs = [dict(row) for row in db.execute("SELECT ts, level, message FROM log ORDER BY id DESC LIMIT ?", (LOG_TAIL_LINES,))]
    done_bytes = sum(r["b"] for r in job_bytes if r["status"] == JOB_DONE) + current["staged_bytes"]
    total_bytes = sum(r["b"] for r in job_bytes)
    remaining = max(0, total_bytes - done_bytes)
    return {
        "now": now_iso(),
        "config": cfg,
        "window_open": window_allows(cfg, datetime.now()),
        "current": current,
        "totals": dict(totals),
        "jobs": jobs,
        "job_counts": {r["status"]: r["n"] for r in job_bytes},
        "bytes_total": total_bytes,
        "bytes_done": done_bytes,
        "eta_s": remaining / current["speed"] if current["speed"] > 0 else None,
        "log": logs,
        "paths": {"staging": str(STAGING_DIR), **{k: str(v) for k, v in DEST_ROOTS.items()}},
    }


def apply_control(db, body):
    action = body.get("action")
    if action == "start":
        set_config(db, "enabled", "1")
        log(db, "start requested")
    elif action == "pause":
        set_config(db, "enabled", "0")
        stop_current.set()
        log(db, "pause requested")
    elif action == "retry_failed":
        db.execute("UPDATE jobs SET status = ?, error = NULL WHERE status = ?", (JOB_PENDING, JOB_FAILED))
        set_config(db, "phase", PHASE_DOWNLOAD)
        log(db, "failed jobs queued again")
    elif action == "verify_now":
        set_config(db, "phase", PHASE_VERIFY)
        log(db, "verify and place requested")
    elif action == "login":
        with runtime_lock:
            busy = runtime["child"] is not None or runtime["login"]["status"] == LOGIN_WAITING
        if busy:
            return False
        threading.Thread(target=run_login, daemon=True).start()
    elif action == "set_window":
        parsers = {"window_start": parse_week_point, "window_end": parse_week_point,
                   "quiet_start": parse_day_point, "quiet_end": parse_day_point}
        if any(parsers[key](body[key]) is None for key in parsers if key in body):
            return False
        for key in ("weekend_only", *parsers):
            if key in body:
                set_config(db, key, body[key])
        log(db, "window updated")
    else:
        return False
    return True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def db(self):
        if not hasattr(self, "_db"):
            self._db = connect_db()
        return self._db

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/state":
            self.send_json(state_snapshot(self.db))
        elif self.path == "/":
            data = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/control":
            self.send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json({"error": "bad json"}, 400)
            return
        self.send_json({"ok": apply_control(self.db, body)})


# ── Init ─────────────────────────────────────────────────────────────────────

def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    db = init_db()
    log(db, f"runner started; staging={STAGING_DIR}")

    def on_signal(*_):
        shutdown.set()
        stop_current.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    server = http.server.ThreadingHTTPServer((UI_HOST, UI_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=window_watchdog, daemon=True).start()
    runner(db)
    server.shutdown()
    log(db, "runner stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
