#!/usr/bin/env python3
"""Proton Drive metadata inventory. Never downloads or changes anything.

Walks /my-files one folder at a time with the official Proton Drive CLI,
then lists the Photos timeline, and stores one row per item in SQLite.
Safe to stop and re-run: finished folders are not listed again.
"""

import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

DEBUG = True  # gates all console output

BASE_DIR = Path(__file__).resolve().parent
CLI_PATH = BASE_DIR / "proton-drive"
DB_PATH = BASE_DIR / "inventory.sqlite"

MY_FILES_ROOT = "/my-files"
PHOTOS_ROOT = "/photos"
SECTION_MY_FILES = "my-files"
SECTION_PHOTOS = "photos"

LIST_TIMEOUT_S = 1800
PHOTOS_TIMEOUT_S = 3600
RETRY_DELAYS_S = (30, 120, 300)  # the CLI must run one call at a time
PROGRESS_EVERY_FOLDERS = 25
ERROR_TAIL_CHARS = 300

LOGIN_NEEDED_TEXT = "You need to login first"
TYPE_FOLDER = "folder"
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

EXIT_OK = 0
EXIT_SOME_FAILED = 1
EXIT_LOGIN_NEEDED = 2

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    children INTEGER,
    error TEXT,
    listed_at TEXT
);
CREATE TABLE IF NOT EXISTS items (
    uid TEXT PRIMARY KEY,
    section TEXT NOT NULL,
    parent_path TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    name_ok INTEGER NOT NULL,
    type TEXT,
    media_type TEXT,
    size INTEGER,
    sha1 TEXT,
    sha1_verified INTEGER,
    mtime TEXT,
    capture_time TEXT,
    created TEXT,
    storage_size INTEGER,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS items_sha1 ON items(sha1);
CREATE INDEX IF NOT EXISTS items_section ON items(section);
"""


class LoginRequired(Exception):
    """The CLI has no valid session; nothing else can proceed."""


# ── Pure helpers ─────────────────────────────────────────────────────────────

def clean_error(text):
    """Strip terminal colours and keep the tail of a CLI error."""
    return ANSI_RE.sub("", text or "").strip()[-ERROR_TAIL_CHARS:]


def parse_items(stdout):
    """Parse CLI --json output into a list. Returns None when it is not JSON."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def item_name(item):
    """Return (name, ok). Undecryptable names get a uid-based placeholder."""
    name = item.get("name") or {}
    if name.get("ok") and isinstance(name.get("value"), str):
        return name["value"], True
    return f"undecryptable-{item.get('uid', 'unknown')}", False


def item_row(item, parent_path, section, keep_raw):
    """Flatten one CLI node into an items row."""
    name, name_ok = item_name(item)
    revision = item.get("activeRevision") or {}
    digests = revision.get("claimedDigests") or {}
    camera = (revision.get("claimedAdditionalMetadata") or {}).get("Camera") or {}
    photo = item.get("photo") or {}
    return (
        item.get("uid"),
        section,
        parent_path,
        f"{parent_path}/{name}",
        name,
        int(name_ok),
        item.get("type"),
        item.get("mediaType"),
        revision.get("claimedSize"),
        digests.get("sha1"),
        int(bool(digests.get("sha1Verified"))),
        revision.get("claimedModificationTime"),
        photo.get("captureTime") or camera.get("CaptureTime"),
        item.get("creationTime"),
        item.get("totalStorageSize"),
        json.dumps(item) if keep_raw else None,
    )


# ── IO ───────────────────────────────────────────────────────────────────────

def log(message):
    if DEBUG:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def run_cli(args, timeout_s):
    """Run one CLI command. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [str(CLI_PATH), *args, "--json"],
            capture_output=True, text=True, timeout=timeout_s,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout_s}s"


def fetch_list(args, timeout_s):
    """Run a listing command with retries. Returns (items, error); items is None on failure."""
    error = ""
    for delay in (0, *RETRY_DELAYS_S):
        if delay:
            log(f"retrying in {delay}s: {error}")
            time.sleep(delay)
        code, stdout, stderr = run_cli(args, timeout_s)
        if LOGIN_NEEDED_TEXT in stdout or LOGIN_NEEDED_TEXT in stderr:
            raise LoginRequired()
        items = parse_items(stdout) if code == 0 else None
        if items is not None:
            return items, ""
        error = clean_error(stderr) or clean_error(stdout) or f"exit code {code}"
    return None, error


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.execute("INSERT OR IGNORE INTO folders(path, status) VALUES (?, ?)", (MY_FILES_ROOT, STATUS_PENDING))
    db.execute("UPDATE folders SET status = ? WHERE status = ?", (STATUS_PENDING, STATUS_FAILED))
    db.commit()
    return db


def save_items(db, rows):
    db.executemany("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def walk_my_files(db):
    """List every pending folder until none remain. Returns the number that failed."""
    processed = 0
    while True:
        row = db.execute(
            "SELECT path FROM folders WHERE status = ? ORDER BY rowid LIMIT 1", (STATUS_PENDING,)
        ).fetchone()
        if row is None:
            break
        path = row[0]
        items, error = fetch_list(["filesystem", "list", path], LIST_TIMEOUT_S)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        if items is None:
            db.execute(
                "UPDATE folders SET status = ?, attempts = attempts + 1, error = ?, listed_at = ? WHERE path = ?",
                (STATUS_FAILED, error, now, path),
            )
        else:
            rows = [item_row(item, path, SECTION_MY_FILES, keep_raw=False) for item in items]
            save_items(db, rows)
            subfolders = [(r[3], STATUS_PENDING) for r in rows if r[6] == TYPE_FOLDER and r[5]]
            db.executemany("INSERT OR IGNORE INTO folders(path, status) VALUES (?, ?)", subfolders)
            db.execute(
                "UPDATE folders SET status = ?, attempts = attempts + 1, children = ?, error = NULL, listed_at = ? WHERE path = ?",
                (STATUS_DONE, len(items), now, path),
            )
        db.commit()
        processed += 1
        if processed % PROGRESS_EVERY_FOLDERS == 0:
            log_progress(db)
    return db.execute("SELECT COUNT(*) FROM folders WHERE status = ?", (STATUS_FAILED,)).fetchone()[0]


def inventory_photos(db):
    """List the Photos timeline with details. Returns True on success."""
    items, error = fetch_list(["photo", "timeline", "--load-details"], PHOTOS_TIMEOUT_S)
    if items is None:
        log(f"photos timeline failed: {error}")
        return False
    rows = [item_row(item, PHOTOS_ROOT, SECTION_PHOTOS, keep_raw=True) for item in items if "missingUid" not in item]
    db.execute("DELETE FROM items WHERE section = ?", (SECTION_PHOTOS,))
    save_items(db, rows)
    db.commit()
    log(f"photos timeline: {len(rows)} items")
    return True


def log_progress(db):
    done, pending, failed = (
        db.execute("SELECT COUNT(*) FROM folders WHERE status = ?", (s,)).fetchone()[0]
        for s in (STATUS_DONE, STATUS_PENDING, STATUS_FAILED)
    )
    files, size = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM items WHERE section = ? AND type != ?",
        (SECTION_MY_FILES, TYPE_FOLDER),
    ).fetchone()
    log(f"folders done={done} pending={pending} failed={failed} | files={files} size={size / 2**30:.1f} GiB")


# ── Init ─────────────────────────────────────────────────────────────────────

def main():
    db = open_db()
    try:
        log("inventory started (metadata only)")
        photos_ok = inventory_photos(db)
        failed = walk_my_files(db)
        log_progress(db)
    except LoginRequired:
        log("stopped: the CLI is not logged in. Run: ./proton-drive auth login")
        return EXIT_LOGIN_NEEDED
    finally:
        db.close()
    if failed or not photos_ok:
        log(f"finished with problems: failed_folders={failed} photos_ok={photos_ok}")
        return EXIT_SOME_FAILED
    log("finished: all folders listed")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
