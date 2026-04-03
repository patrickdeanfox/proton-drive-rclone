"""File scanner — indexes files into the database."""

import hashlib
import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import database as db

log = logging.getLogger(__name__)

BUF_SIZE = 65536  # 64 KiB chunks for hashing


def sha256_of(filepath: str) -> str:
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while True:
                data = f.read(BUF_SIZE)
                if not data:
                    break
                h.update(data)
    except OSError:
        return ""
    return h.hexdigest()


def scan_directory(root_path: str, job_id: str = None, extensions=None, skip_hidden=True):
    """Walk *root_path* and upsert every file into the DB.

    Returns (scanned, errors) counts.
    """
    root = Path(root_path)
    if not root.is_dir():
        raise FileNotFoundError(f"{root_path} is not a directory")

    if job_id is None:
        job_id = str(uuid.uuid4())

    db.create_job(job_id, "scan")
    scanned = 0
    errors = 0
    all_files = []

    for dirpath, dirnames, filenames in os.walk(root):
        if skip_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if skip_hidden and fname.startswith("."):
                continue
            fp = os.path.join(dirpath, fname)
            all_files.append(fp)

    total = len(all_files)
    log.info("Scanning %d files in %s", total, root_path)

    for i, fp in enumerate(all_files):
        try:
            p = Path(fp)
            ext = p.suffix.lower().lstrip(".") if p.suffix else None
            if extensions and ext not in extensions:
                continue

            stat = p.stat()
            mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
            h = sha256_of(fp)

            db.upsert_file({
                "file_path": str(p.resolve()),
                "file_name": p.name,
                "extension": ext,
                "mime_type": mime,
                "size_bytes": stat.st_size,
                "sha256": h,
                "created_at": datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc),
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                "metadata_json": "{}",
            })
            scanned += 1
        except Exception as e:
            log.warning("Error scanning %s: %s", fp, e)
            errors += 1

        if i % 50 == 0:
            progress = (i + 1) / total if total else 1
            db.update_job(job_id, progress=progress,
                          message=f"Scanned {i+1}/{total}")

    db.update_job(job_id, status="completed", progress=1.0,
                  message=f"Scanned {scanned} files ({errors} errors)",
                  result={"scanned": scanned, "errors": errors})
    return scanned, errors
