"""Rclone integration — transfer analysis, retry logic, verification."""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "rclone_binary": "rclone",
    "default_remote": "protondrive",
    "max_retries": 3,
    "retry_delay": 5,
    "checkers": 4,
    "transfers": 2,
    "verify_after_transfer": True,
    "bandwidth_limit": "",  # e.g. "10M"
    "log_level": "INFO",
}


def _run(args, timeout=120):
    """Run an rclone command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout"
    except FileNotFoundError:
        return -2, "", "rclone binary not found"


def check_rclone():
    """Check if rclone is installed and return version."""
    rc, out, err = _run(["rclone", "version"], timeout=10)
    if rc == 0:
        version = out.strip().split("\n")[0] if out else "unknown"
        return {"ok": True, "version": version}
    return {"ok": False, "error": err or "rclone not found"}


def list_remotes():
    """List configured rclone remotes."""
    rc, out, err = _run(["rclone", "listremotes"], timeout=15)
    if rc == 0:
        remotes = [r.rstrip(":") for r in out.strip().split("\n") if r.strip()]
        return {"ok": True, "remotes": remotes}
    return {"ok": False, "error": err}


def get_remote_about(remote_name):
    """Get storage usage info."""
    rc, out, err = _run(["rclone", "about", f"{remote_name}:", "--json"], timeout=30)
    if rc == 0:
        try:
            return {"ok": True, "data": json.loads(out)}
        except json.JSONDecodeError:
            return {"ok": True, "data": {"raw": out}}
    return {"ok": False, "error": err}


def transfer_file(local_path, remote_path, settings=None, direction="upload"):
    """Transfer a single file with retry logic.

    direction: 'upload' (local→remote) or 'download' (remote→local)
    """
    if settings is None:
        settings = dict(DEFAULT_SETTINGS)

    binary = settings.get("rclone_binary", "rclone")
    max_retries = settings.get("max_retries", 3)
    retry_delay = settings.get("retry_delay", 5)

    if direction == "upload":
        src, dst = local_path, remote_path
        cmd = "copyto"
    else:
        src, dst = remote_path, local_path
        cmd = "copyto"

    base_args = [binary, cmd, src, dst, "--log-level", settings.get("log_level", "INFO")]
    if settings.get("bandwidth_limit"):
        base_args += ["--bwlimit", settings["bandwidth_limit"]]

    for attempt in range(1, max_retries + 1):
        rc, out, err = _run(base_args, timeout=300)
        if rc == 0:
            result = {"ok": True, "attempt": attempt}
            # Verification
            if settings.get("verify_after_transfer") and direction == "upload":
                vrc, vout, verr = _run(
                    [binary, "check", local_path, remote_path, "--one-way"],
                    timeout=60
                )
                result["verified"] = vrc == 0
            return result
        log.warning("Transfer attempt %d failed: %s", attempt, err)
        if attempt < max_retries:
            time.sleep(retry_delay)

    return {"ok": False, "error": err, "attempts": max_retries}


def get_transfer_stats(remote_name, path=""):
    """Get stats about files on remote."""
    args = ["rclone", "size", f"{remote_name}:{path}", "--json"]
    rc, out, err = _run(args, timeout=60)
    if rc == 0:
        try:
            return {"ok": True, "data": json.loads(out)}
        except json.JSONDecodeError:
            return {"ok": True, "data": {"raw": out}}
    return {"ok": False, "error": err}


def sync_folder(local_path, remote_path, settings=None, dry_run=False):
    """Sync a folder with configurable settings."""
    if settings is None:
        settings = dict(DEFAULT_SETTINGS)

    binary = settings.get("rclone_binary", "rclone")
    args = [
        binary, "bisync", local_path, remote_path,
        "--checkers", str(settings.get("checkers", 4)),
        "--transfers", str(settings.get("transfers", 2)),
        "--log-level", settings.get("log_level", "INFO"),
    ]
    if settings.get("bandwidth_limit"):
        args += ["--bwlimit", settings["bandwidth_limit"]]
    if dry_run:
        args.append("--dry-run")

    max_retries = settings.get("max_retries", 3)
    retry_delay = settings.get("retry_delay", 5)

    for attempt in range(1, max_retries + 1):
        rc, out, err = _run(args, timeout=600)
        if rc == 0:
            return {"ok": True, "attempt": attempt, "output": out}
        log.warning("Sync attempt %d failed: %s", attempt, err)
        if attempt < max_retries:
            time.sleep(retry_delay)

    return {"ok": False, "error": err, "attempts": max_retries}
