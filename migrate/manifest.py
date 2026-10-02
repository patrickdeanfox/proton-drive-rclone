#!/usr/bin/env python3
"""Build migration.sqlite: one row per Proton file with its state and NAS destination.

Inputs: inventory.sqlite (from inventory.py) and nas-index.sqlite (NAS index with
the `placement` table from the comparison). Output: migration.sqlite, read by migrate.py.
Destination rules are the decisions of 2026-10-02.
"""

import sqlite3
import sys
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

DEBUG = True

BASE_DIR = Path(__file__).resolve().parent
INVENTORY_DB = BASE_DIR / "inventory.sqlite"
NAS_INDEX_DB = BASE_DIR / "nas-index.sqlite"
OUTPUT_DB = BASE_DIR / "migration.sqlite"

MY_FILES_PREFIX = "/my-files/"
PHOTOS_SECTION = "photos"
ADULT_PREFIX = "/my-files/data/data/"
ALL_DUPLICATE_FOLDERS = ("/my-files/dropbox/219 Rocky Run Road",)

DEST_IMMICH = "immich"
DEST_PDFOX = "pdfox"
DEST_ADULT = "adult"

MEDIA_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "tif", "tiff", "bmp", "dng", "raw", "cr2", "nef", "arw",
    "mp4", "mov", "m4v", "avi", "mkv", "mts", "m2ts", "3gp", "wmv",
}

STATE_DUPLICATE = "duplicate"
STATE_NEW = "new"
STATE_PROBABLE = "probable-duplicate"

JOB_FOLDER = "folder"
JOB_FILE = "file"
JOB_PHOTOS = "photos"

SCHEMA = """
CREATE TABLE files (
    uid TEXT PRIMARY KEY,
    section TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha1 TEXT NOT NULL,
    mtime TEXT,
    capture_time TEXT,
    state TEXT NOT NULL,
    dest TEXT NOT NULL,
    dest_rel TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    placed_path TEXT,
    checked_at TEXT
);
CREATE INDEX files_sha1 ON files(sha1);
CREATE INDEX files_status ON files(status);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    local_rel TEXT NOT NULL,
    expected_files INTEGER NOT NULL,
    expected_bytes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
"""


# ── Pure helpers ─────────────────────────────────────────────────────────────

def log(message):
    if DEBUG:
        print(message, flush=True)


def extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def destination(section, path, name):
    """Return (dest, dest_rel): where a file ends up on the NAS, relative to that destination root."""
    if section == PHOTOS_SECTION:
        return DEST_IMMICH, f"photos/{name}"
    if path.startswith(ADULT_PREFIX):
        return DEST_ADULT, path[len(ADULT_PREFIX):]
    rel = path[len(MY_FILES_PREFIX):]
    if extension(name) in MEDIA_EXTENSIONS:
        return DEST_IMMICH, rel
    return DEST_PDFOX, rel


def top_folder(path):
    """'/my-files/dropbox/X/y.jpg' -> '/my-files/dropbox/X'; loose root files -> None."""
    parts = path[len(MY_FILES_PREFIX):].split("/")
    if len(parts) == 1:
        return None
    if parts[0] == "dropbox":
        return f"{MY_FILES_PREFIX}dropbox/{parts[1]}" if len(parts) > 2 else None
    return f"{MY_FILES_PREFIX}{parts[0]}"


# ── IO ───────────────────────────────────────────────────────────────────────

def load_rows():
    db = sqlite3.connect(f"file:{NAS_INDEX_DB}?mode=ro", uri=True)
    db.execute(f"ATTACH 'file:{INVENTORY_DB}?mode=ro' AS inv")
    rows = db.execute(
        """SELECT i.uid, i.section, i.path, i.name, i.size, i.sha1, i.mtime, i.capture_time, p.state, p.note
           FROM inv.items i JOIN placement p ON p.uid = i.uid WHERE i.type != 'folder'"""
    ).fetchall()
    db.close()
    return rows


def build_jobs(files):
    """Group files to download into CLI jobs: whole folders where most content is new, single files elsewhere."""
    jobs = {}
    for uid, section, path, name, size, sha1, mtime, capture_time, state, dest, dest_rel, note in files:
        if state == STATE_DUPLICATE:
            continue
        if section == PHOTOS_SECTION:
            key = (JOB_PHOTOS, "/photos", "photos")
        elif path.startswith(ADULT_PREFIX) or top_folder(path) is None:
            parent = path.rsplit("/", 1)[0]
            key = (JOB_FILE, path, parent[1:])
        else:
            folder = top_folder(path)
            key = (JOB_FOLDER, folder, folder.rsplit("/", 1)[0][1:])
        if key[1] in ALL_DUPLICATE_FOLDERS:
            continue
        count, total = jobs.get(key, (0, 0))
        jobs[key] = (count + 1, total + size)
    return [(kind, remote, local_rel, count, total) for (kind, remote, local_rel), (count, total) in sorted(jobs.items())]


def main():
    if OUTPUT_DB.exists():
        log(f"refusing to overwrite existing {OUTPUT_DB}")
        return 1
    rows = load_rows()
    files = []
    for uid, section, path, name, size, sha1, mtime, capture_time, state, note in rows:
        dest, dest_rel = destination(section, path, name)
        files.append((uid, section, path, name, size, sha1, mtime, capture_time, state, dest, dest_rel, note))
    jobs = build_jobs(files)

    out = sqlite3.connect(OUTPUT_DB)
    out.executescript(SCHEMA)
    out.executemany("INSERT INTO files (uid, section, path, name, size, sha1, mtime, capture_time, state, dest, dest_rel, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", files)
    out.executemany("INSERT INTO jobs (kind, remote_path, local_rel, expected_files, expected_bytes) VALUES (?,?,?,?,?)", jobs)
    out.execute("UPDATE files SET status = 'skipped' WHERE state = ?", (STATE_DUPLICATE,))
    out.commit()

    for dest, state, count, gib in out.execute(
        "SELECT dest, state, COUNT(*), ROUND(SUM(size)/1073741824.0, 2) FROM files GROUP BY 1, 2 ORDER BY 1, 2"
    ):
        log(f"{dest:7} {state:19} {count:6} files {gib:8} GiB")
    log(f"jobs: {len(jobs)} (download {sum(j[3] for j in jobs)} files, {sum(j[4] for j in jobs)/2**30:.1f} GiB expected)")
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
