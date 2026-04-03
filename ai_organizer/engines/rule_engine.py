"""Rule-based file organization engine.

Supports rules defined in YAML/DB:
  - extension mapping
  - mime-type mapping
  - regex on filename
  - date-based (year/month)
  - creator-based (from EXIF or metadata)
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .. import database as db

log = logging.getLogger(__name__)


def _match_extension(file_row, config):
    """config.extensions = ['jpg','png', ...], config.dest = 'Photos'"""
    ext = (file_row.get("extension") or "").lower()
    exts = [e.lower().lstrip(".") for e in config.get("extensions", [])]
    if ext in exts:
        return config.get("dest_folder", "Uncategorized")
    return None


def _match_mime(file_row, config):
    mime = file_row.get("mime_type") or ""
    for pattern in config.get("patterns", []):
        if mime.startswith(pattern):
            return config.get("dest_folder", "Uncategorized")
    return None


def _match_regex(file_row, config):
    name = file_row.get("file_name") or ""
    pattern = config.get("pattern", "")
    if pattern and re.search(pattern, name, re.IGNORECASE):
        return config.get("dest_folder", "Matched")
    return None


def _match_date(file_row, config):
    """Organize by date into YYYY/MM structure."""
    dt = file_row.get("modified_at") or file_row.get("created_at")
    if dt:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        fmt = config.get("format", "{year}/{month:02d}")
        return fmt.format(year=dt.year, month=dt.month, day=dt.day)
    return None


def _match_creator(file_row, config):
    meta = file_row.get("metadata_json")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    creator = meta.get("creator") or meta.get("author") or meta.get("Artist")
    if creator:
        return f"{config.get('base_folder', 'By Creator')}/{creator}"
    return None


MATCHERS = {
    "extension": _match_extension,
    "mime": _match_mime,
    "regex": _match_regex,
    "date": _match_date,
    "creator": _match_creator,
}


def evaluate_rules(file_row, rules=None):
    """Evaluate all enabled rules against a file.

    Returns list of (rule, suggested_dest) tuples.
    """
    if rules is None:
        rules = db.get_org_rules(enabled_only=True)

    suggestions = []
    for rule in rules:
        rtype = rule["rule_type"]
        config = rule["config_json"]
        if isinstance(config, str):
            config = json.loads(config)
        matcher = MATCHERS.get(rtype)
        if matcher:
            dest = matcher(file_row, config)
            if dest:
                suggestions.append((rule, dest))
    return suggestions


def propose_organization(files=None, rules=None):
    """Generate organization proposals for all indexed files.

    Returns list of dicts: {file_id, file_path, rule_name, dest_path, action}.
    """
    if files is None:
        files = db.get_files(limit=10000)
    if rules is None:
        rules = db.get_org_rules(enabled_only=True)

    proposals = []
    for f in files:
        matches = evaluate_rules(f, rules)
        if matches:
            best_rule, dest = matches[0]  # highest priority
            proposals.append({
                "file_id": f["id"],
                "file_path": f["file_path"],
                "file_name": f["file_name"],
                "rule_id": best_rule["id"],
                "rule_name": best_rule["name"],
                "dest_folder": dest,
                "action": "move",
            })
    return proposals
