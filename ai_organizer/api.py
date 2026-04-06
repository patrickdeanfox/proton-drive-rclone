"""Flask blueprint with all AI-organizer API endpoints."""

import json
import logging
import os
import threading
import uuid
import weakref
from datetime import datetime
from pathlib import Path

import psycopg2.extras

from flask import Blueprint, jsonify, render_template, request

from . import database as db
from .scanner import scan_directory
from .engines.rule_engine import propose_organization, evaluate_rules
from .engines.ai_engine import (
    analyze_file, batch_analyze, get_provider,
    get_available_providers, PROVIDERS,
)
from .engines.duplicate_engine import (
    find_exact_duplicates, find_near_duplicates, run_all_detection,
    find_fuzzy_document_duplicates, resolve_duplicates,
)
from .engines.metadata_engine import batch_extract_metadata
from .engines.ocr_engine import batch_ocr
from .engines.search_engine import search_files_as_dicts
from .engines.rclone_engine import (
    check_rclone, list_remotes, get_remote_about,
    get_transfer_stats, DEFAULT_SETTINGS as RCLONE_DEFAULTS,
)
from .engines.migration_engine import (
    check_dropbox_remote, configure_dropbox_remote, test_dropbox_connection,
    browse_remote, get_folder_size, dry_run_migration, start_migration,
    cancel_migration, pause_migration, resume_migration,
    get_migration_status, get_migration_logs, list_active_migrations,
    validate_endpoints,
)
from .utils.yaml_rules import import_rules_from_yaml, export_rules_to_yaml
from .progress import (
    ProgressTracker, register_operation, unregister_operation,
    get_active_operations, get_operation, emit_progress,
)

log = logging.getLogger(__name__)
bp = Blueprint("ai", __name__, url_prefix="/ai")

# Directories that must never be scanned or read
_BLOCKED_ROOTS = {"/proc", "/sys", "/dev", "/boot", "/sbin", "/bin",
                  "/run", "/snap"}


def _safe_path(raw: str, must_exist: bool = True) -> Path:
    """Resolve and validate a user-supplied path.

    Raises ValueError for empty paths, non-existent paths (when must_exist),
    or paths that point into dangerous system directories.
    """
    if not raw or not raw.strip():
        raise ValueError("Path must not be empty")
    resolved = Path(raw).resolve()
    for blocked in _BLOCKED_ROOTS:
        if str(resolved).startswith(blocked):
            raise ValueError(f"Access to {blocked} is not permitted")
    if must_exist and not resolved.exists():
        raise ValueError(f"Path does not exist: {resolved}")
    return resolved

# Background job tracking — WeakValueDictionary releases threads automatically
# after they finish so dead threads don't accumulate in memory.
_bg_threads: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_bg_threads_lock = threading.Lock()


def _run_bg(name, fn, *args, **kwargs):
    job_id = str(uuid.uuid4())
    kwargs["job_id"] = job_id

    def _wrapper():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            log.error("Background job %s error: %s", job_id, e)
            try:
                db.update_job(job_id, status="failed", message=str(e))
            except Exception:
                pass

    t = threading.Thread(target=_wrapper, name=f"bg-{name}-{job_id[:8]}", daemon=True)
    with _bg_threads_lock:
        _bg_threads[job_id] = t
    t.start()
    return job_id


# ── Pages ─────────────────────────────────────────────────────────────

@bp.route("/")
@bp.route("/dashboard")
def ai_dashboard():
    return render_template("ai/dashboard.html", active="ai_dashboard")


@bp.route("/organize")
def ai_organize_page():
    return render_template("ai/organize.html", active="ai_organize")


@bp.route("/duplicates")
def ai_duplicates_page():
    return render_template("ai/duplicates.html", active="ai_duplicates")


@bp.route("/rules")
def ai_rules_page():
    return render_template("ai/rules.html", active="ai_rules")


@bp.route("/ai-settings")
def ai_settings_page():
    return render_template("ai/ai_settings.html", active="ai_settings")


@bp.route("/db-settings")
def db_settings_page():
    return render_template("ai/db_settings.html", active="ai_db")


@bp.route("/rclone-settings")
def rclone_settings_page():
    return render_template("ai/rclone_settings.html", active="ai_rclone")


@bp.route("/extensions")
def extensions_page():
    return render_template("ai/extensions.html", active="ai_extensions")


@bp.route("/migration")
def migration_page():
    return render_template("ai/migration.html", active="ai_migration")


# ── API: Stats ────────────────────────────────────────────────────────

@bp.route("/api/stats")
def api_stats():
    try:
        stats = db.get_stats()
        # Serialize datetimes
        for k, v in stats.items():
            if isinstance(v, list):
                stats[k] = [dict(r) for r in v]
        return jsonify({"ok": True, **stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/jobs")
def api_jobs():
    try:
        jobs = db.get_recent_jobs(limit=30)
        result = []
        for j in jobs:
            d = dict(j)
            for k in ("started_at", "finished_at", "created_at"):
                if d.get(k) and isinstance(d[k], datetime):
                    d[k] = d[k].isoformat()
            result.append(d)
        return jsonify({"ok": True, "jobs": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/job/<job_id>")
def api_job_status(job_id):
    try:
        j = db.get_job(job_id)
        if not j:
            return jsonify({"ok": False, "error": "Not found"}), 404
        d = dict(j)
        for k in ("started_at", "finished_at", "created_at"):
            if d.get(k) and isinstance(d[k], datetime):
                d[k] = d[k].isoformat()
        return jsonify({"ok": True, **d})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Progress (HTTP fallback for WebSocket) ────────────────────────

@bp.route("/api/progress")
def api_progress():
    """Get all active operation progress (fallback for non-WebSocket clients)."""
    return jsonify({"ok": True, "operations": get_active_operations()})


@bp.route("/api/progress/<operation_id>")
def api_progress_detail(operation_id):
    op = get_operation(operation_id)
    if not op:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, **op})


# ── API: Scan ─────────────────────────────────────────────────────────

@bp.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(force=True)
    raw_path = data.get("path", "")
    try:
        safe = _safe_path(raw_path)
        if not safe.is_dir():
            return jsonify({"ok": False, "error": "Path is not a directory"}), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    job_id = _run_bg("scan", scan_directory, str(safe))
    return jsonify({"ok": True, "job_id": job_id})


# ── API: Organization ─────────────────────────────────────────────────

@bp.route("/api/organize/propose", methods=["POST"])
def api_organize_propose():
    """Generate organization proposals without executing them."""
    try:
        proposals = propose_organization()
        return jsonify({"ok": True, "proposals": proposals, "count": len(proposals)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/organize/apply", methods=["POST"])
def api_organize_apply():
    """Apply selected organization proposals (move/copy files)."""
    data = request.get_json(force=True)
    proposals = data.get("proposals", [])
    base_dest = data.get("base_dest", "")
    dry_run = data.get("dry_run", True)

    # Create progress tracker
    tracker = ProgressTracker(
        operation_type="organize",
        total=len(proposals),
        unit="files",
    )
    register_operation(tracker)

    results = []
    for i, p in enumerate(proposals):
        src = Path(p["file_path"])
        dest_folder = Path(base_dest) / p["dest_folder"] if base_dest else Path(p["dest_folder"])
        dest = dest_folder / src.name

        tracker.update(current=i, message=f"Processing {src.name}")

        if dry_run:
            results.append({"file": str(src), "dest": str(dest), "status": "dry_run"})
        else:
            try:
                dest_folder.mkdir(parents=True, exist_ok=True)
                src.rename(dest)
                with db.get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO organization_log (file_id, rule_id, action, source_path, dest_path, status)
                        VALUES (%s, %s, 'move', %s, %s, 'applied')
                    """, (p.get("file_id"), p.get("rule_id"), str(src), str(dest)))
                results.append({"file": str(src), "dest": str(dest), "status": "moved"})
            except Exception as e:
                results.append({"file": str(src), "dest": str(dest), "status": "error", "error": str(e)})

    tracker.complete(result={"count": len(results)})
    unregister_operation(tracker.operation_id)

    return jsonify({"ok": True, "results": results})


# ── API: Rules ────────────────────────────────────────────────────────

@bp.route("/api/rules", methods=["GET"])
def api_get_rules():
    try:
        rules = db.get_org_rules()
        result = []
        for r in rules:
            d = dict(r)
            for k in ("created_at", "updated_at"):
                if d.get(k) and isinstance(d[k], datetime):
                    d[k] = d[k].isoformat()
            result.append(d)
        return jsonify({"ok": True, "rules": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/rules", methods=["POST"])
def api_create_rule():
    data = request.get_json(force=True)
    try:
        rule_id = db.save_org_rule(data)
        return jsonify({"ok": True, "id": rule_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/rules/<int:rule_id>", methods=["PUT"])
def api_update_rule(rule_id):
    data = request.get_json(force=True)
    data["id"] = rule_id
    try:
        db.save_org_rule(data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/rules/<int:rule_id>", methods=["DELETE"])
def api_delete_rule(rule_id):
    try:
        db.delete_org_rule(rule_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/rules/import", methods=["POST"])
def api_import_rules():
    data = request.get_json(force=True)
    raw_path = data.get("path", "")
    try:
        safe = _safe_path(raw_path)
        if safe.suffix.lower() not in (".yml", ".yaml"):
            return jsonify({"ok": False, "error": "File must be a .yml or .yaml file"}), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        count = import_rules_from_yaml(str(safe))
        return jsonify({"ok": True, "imported": count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/rules/export", methods=["POST"])
def api_export_rules():
    import tempfile
    data = request.get_json(force=True)
    # Use caller-supplied path only if explicitly provided and safe; otherwise temp file
    raw_path = data.get("path", "")
    if raw_path:
        try:
            output = str(_safe_path(raw_path, must_exist=False))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    else:
        fd, output = tempfile.mkstemp(suffix=".yml", prefix="organize-rules-export-")
        os.close(fd)
    try:
        content = export_rules_to_yaml(output)
        return jsonify({"ok": True, "path": output, "content": content})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Duplicates ───────────────────────────────────────────────────

@bp.route("/api/duplicates/detect", methods=["POST"])
def api_detect_duplicates():
    data = request.get_json(force=True) if request.is_json else {}
    mode = data.get("mode", "all")
    try:
        if mode == "exact":
            job_id = _run_bg("dup_exact", find_exact_duplicates)
        elif mode == "near":
            threshold = int(data.get("threshold", 10))
            job_id = _run_bg("dup_near", find_near_duplicates, threshold=threshold)
        elif mode == "fuzzy":
            threshold = float(data.get("threshold", 0.85))
            job_id = _run_bg("dup_fuzzy", find_fuzzy_document_duplicates,
                             threshold=threshold)
        else:
            job_id = _run_bg("dup_all", run_all_detection)
        return jsonify({"ok": True, "job_id": job_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/duplicates/groups")
def api_dup_groups():
    try:
        status = request.args.get("status")
        dtype = request.args.get("type")
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        groups = db.get_duplicate_groups(status=status, detection_type=dtype,
                                         limit=limit, offset=offset)
        result = []
        for g in groups:
            d = dict(g)
            if d.get("created_at") and isinstance(d["created_at"], datetime):
                d["created_at"] = d["created_at"].isoformat()
            result.append(d)
        return jsonify({"ok": True, "groups": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/duplicates/groups/<int:group_id>")
def api_dup_group_detail(group_id):
    try:
        members = db.get_duplicate_group_detail(group_id)
        result = [dict(m) for m in members]
        return jsonify({"ok": True, "members": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/duplicates/resolve", methods=["POST"])
def api_resolve_duplicate():
    """Apply keep/delete decisions for a duplicate group.

    Server-side certainty enforcement: any delete action with similarity
    below the configured threshold is blocked and returned in 'blocked'.
    """
    data = request.get_json(force=True)
    group_id = data.get("group_id")
    actions = data.get("actions", {})

    try:
        result = resolve_duplicates(group_id, actions)
        status_code = 200
        if result.get("blocked"):
            status_code = 422  # Unprocessable — some actions blocked
        return jsonify({"ok": True, **result}), status_code
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/duplicates/settings", methods=["GET"])
def api_dup_settings_get():
    try:
        settings = db.get_safety_settings()
        return jsonify({"ok": True, "settings": settings})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: AI Analysis ──────────────────────────────────────────────────

@bp.route("/api/ai/analyze", methods=["POST"])
def api_ai_analyze():
    data = request.get_json(force=True) if request.is_json else {}
    provider = data.get("provider")
    try:
        job_id = _run_bg("ai_batch", batch_analyze, provider_name=provider)
        return jsonify({"ok": True, "job_id": job_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/ai/providers")
def api_ai_providers():
    return jsonify({
        "ok": True,
        "providers": get_available_providers(),
        "active": get_provider().name,
    })


@bp.route("/api/ai/settings", methods=["GET"])
def api_ai_settings_get():
    try:
        settings = db.get_preference("ai_settings", {
            "provider": "local_clip",
            "categories": [
                "photograph", "screenshot", "document", "meme",
                "art", "diagram", "receipt", "nature", "people",
                "food", "animal", "building", "vehicle", "text",
            ],
            "auto_analyze": False,
            "batch_limit": 500,
        })
        return jsonify({"ok": True, "settings": settings})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/ai/settings", methods=["PUT"])
def api_ai_settings_put():
    data = request.get_json(force=True)
    try:
        db.set_preference("ai_settings", data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Database ─────────────────────────────────────────────────────

@bp.route("/api/db/test", methods=["POST"])
def api_db_test():
    data = request.get_json(force=True)
    result = db.test_connection(data)
    return jsonify(result)


@bp.route("/api/db/settings", methods=["GET"])
def api_db_settings_get():
    settings = db.load_db_settings()
    safe = dict(settings)
    if safe.get("password"):
        safe["password"] = "••••••••"
    return jsonify({"ok": True, "settings": safe})


@bp.route("/api/db/settings", methods=["PUT"])
def api_db_settings_put():
    data = request.get_json(force=True)
    try:
        db.save_db_settings(data)
        db.get_pool(settings=data, force_new=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/db/init", methods=["POST"])
def api_db_init():
    try:
        db.init_schema()
        return jsonify({"ok": True, "message": "Schema initialized"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Rclone ───────────────────────────────────────────────────────

@bp.route("/api/rclone/status")
def api_rclone_status():
    return jsonify(check_rclone())


@bp.route("/api/rclone/remotes")
def api_rclone_remotes():
    return jsonify(list_remotes())


@bp.route("/api/rclone/about/<remote>")
def api_rclone_about(remote):
    return jsonify(get_remote_about(remote))


@bp.route("/api/rclone/stats/<remote>")
def api_rclone_transfer_stats(remote):
    path = request.args.get("path", "")
    return jsonify(get_transfer_stats(remote, path))


@bp.route("/api/rclone/settings", methods=["GET"])
def api_rclone_settings_get():
    try:
        settings = db.get_preference("rclone_settings", dict(RCLONE_DEFAULTS))
        return jsonify({"ok": True, "settings": settings})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/rclone/settings", methods=["PUT"])
def api_rclone_settings_put():
    data = request.get_json(force=True)
    try:
        db.set_preference("rclone_settings", data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Migration Wizard ─────────────────────────────────────────────

@bp.route("/api/migration/check-dropbox")
def api_migration_check_dropbox():
    """Check if Dropbox remote exists."""
    return jsonify(check_dropbox_remote())


@bp.route("/api/migration/configure-dropbox", methods=["POST"])
def api_migration_configure_dropbox():
    """Configure a new Dropbox remote."""
    data = request.get_json(force=True) if request.is_json else {}
    name = data.get("remote_name", "dropbox")
    return jsonify(configure_dropbox_remote(name))


@bp.route("/api/migration/test-connection", methods=["POST"])
def api_migration_test_connection():
    """Test Dropbox connection."""
    data = request.get_json(force=True) if request.is_json else {}
    remote = data.get("remote_name", "dropbox")
    return jsonify(test_dropbox_connection(remote))


@bp.route("/api/migration/browse")
def api_migration_browse():
    """Browse a remote directory (Dropbox or Proton)."""
    remote = request.args.get("remote", "dropbox")
    path = request.args.get("path", "")
    return jsonify(browse_remote(remote, path))


@bp.route("/api/migration/folder-size")
def api_migration_folder_size():
    """Get folder size info."""
    remote = request.args.get("remote", "dropbox")
    path = request.args.get("path", "")
    return jsonify(get_folder_size(remote, path))


@bp.route("/api/migration/dry-run", methods=["POST"])
def api_migration_dry_run():
    """Preview migration."""
    data = request.get_json(force=True)
    return jsonify(dry_run_migration(
        source_remote=data.get("source_remote", "dropbox"),
        source_path=data.get("source_path", ""),
        dest_remote=data.get("dest_remote", "protondrive"),
        dest_path=data.get("dest_path", ""),
        filters=data.get("filters"),
        preserve_structure=data.get("preserve_structure", True),
    ))


@bp.route("/api/migration/start", methods=["POST"])
def api_migration_start():
    """Start a migration."""
    data = request.get_json(force=True)
    migration_id = str(uuid.uuid4())[:12]

    # Save to DB
    try:
        db.create_migration_job({
            "id": migration_id,
            "source_remote": data.get("source_remote", "dropbox"),
            "source_path": data.get("source_path", ""),
            "dest_remote": data.get("dest_remote", "protondrive"),
            "dest_path": data.get("dest_path", ""),
            "mode": data.get("mode", "copy"),
            "options_json": json.dumps(data.get("options", {})),
        })
    except Exception as e:
        log.warning("Could not save migration to DB: %s", e)

    result = start_migration(
        migration_id=migration_id,
        source_remote=data.get("source_remote", "dropbox"),
        source_path=data.get("source_path", ""),
        dest_remote=data.get("dest_remote", "protondrive"),
        dest_path=data.get("dest_path", ""),
        options=data.get("options", {}),
    )
    return jsonify(result)


@bp.route("/api/migration/cancel", methods=["POST"])
def api_migration_cancel():
    """Cancel a running migration."""
    data = request.get_json(force=True)
    mid = data.get("migration_id", "")
    return jsonify(cancel_migration(mid))


@bp.route("/api/migration/status/<migration_id>")
def api_migration_status(migration_id):
    """Get migration status."""
    status = get_migration_status(migration_id)
    if not status:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, **status})


@bp.route("/api/migration/logs/<migration_id>")
def api_migration_logs(migration_id):
    """Get migration log lines."""
    since = int(request.args.get("since", 0))
    lines = get_migration_logs(migration_id, since)
    return jsonify({"ok": True, "lines": lines, "total": since + len(lines)})


@bp.route("/api/migration/list")
def api_migration_list():
    """List active/recent migrations."""
    return jsonify({"ok": True, "migrations": list_active_migrations()})


@bp.route("/api/migration/history")
def api_migration_history():
    """Get migration history from database."""
    try:
        jobs = db.get_migration_jobs(limit=50)
        result = []
        for j in jobs:
            d = dict(j)
            for k in ("started_at", "finished_at", "created_at"):
                if d.get(k) and isinstance(d[k], datetime):
                    d[k] = d[k].isoformat()
            result.append(d)
        return jsonify({"ok": True, "jobs": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Files ────────────────────────────────────────────────────────

@bp.route("/api/files")
def api_files():
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        ext = request.args.get("extension")
        mime = request.args.get("mime_prefix")
        files = db.get_files(limit=limit, offset=offset, extension=ext, mime_prefix=mime)
        result = []
        for f in files:
            d = dict(f)
            for k in ("created_at", "modified_at", "indexed_at"):
                if d.get(k) and isinstance(d[k], datetime):
                    d[k] = d[k].isoformat()
            result.append(d)
        return jsonify({"ok": True, "files": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Phase 3 — Metadata ───────────────────────────────────────────

@bp.route("/api/files/<int:file_id>/metadata")
def api_file_metadata(file_id):
    """Return rich Python-extracted metadata for a single file."""
    try:
        meta = db.get_file_metadata(file_id)
        ocr = db.get_ocr_text(file_id)
        result = {
            "file_id": file_id,
            "metadata": meta,
            "ocr": {"word_count": ocr["word_count"], "language": ocr["language"]}
                   if ocr else None,
        }
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/metadata/extract", methods=["POST"])
def api_metadata_extract():
    """Trigger Python-first metadata extraction on all un-extracted files."""
    try:
        with db.get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT f.id, f.file_path FROM files f
                LEFT JOIN file_metadata m ON m.file_id = f.id
                WHERE m.id IS NULL
                LIMIT 2000
            """)
            records = [dict(r) for r in cur.fetchall()]

        job_id = _run_bg("metadata_extract", batch_extract_metadata,
                         file_records=records, db_module=db)
        return jsonify({"ok": True, "job_id": job_id, "queued": len(records)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Phase 3 — OCR ────────────────────────────────────────────────

@bp.route("/api/ocr/scan", methods=["POST"])
def api_ocr_scan():
    """Trigger OCR on all un-OCR'd images and PDFs."""
    data = request.get_json(force=True) if request.is_json else {}
    lang = data.get("lang", "eng")
    try:
        with db.get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT f.id, f.file_path FROM files f
                LEFT JOIN file_ocr_text o ON o.file_id = f.id
                WHERE o.id IS NULL
                  AND (f.mime_type LIKE 'image/%%' OR f.mime_type = 'application/pdf'
                       OR f.extension IN ('.png','.jpg','.jpeg','.tiff','.tif',
                                          '.bmp','.gif','.webp','.pdf'))
                LIMIT 1000
            """)
            records = [dict(r) for r in cur.fetchall()]

        job_id = _run_bg("ocr_scan", batch_ocr,
                         file_records=records, lang=lang, db_module=db)
        return jsonify({"ok": True, "job_id": job_id, "queued": len(records)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/ocr/results/<int:file_id>")
def api_ocr_results(file_id):
    """Return OCR text for a specific file."""
    try:
        result = db.get_ocr_text(file_id)
        if result:
            d = dict(result)
            if d.get("extracted_at") and isinstance(d["extracted_at"], datetime):
                d["extracted_at"] = d["extracted_at"].isoformat()
            return jsonify({"ok": True, "ocr": d})
        return jsonify({"ok": False, "error": "No OCR data found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/ocr/search")
def api_ocr_search():
    """Fuzzy full-text search across OCR'd file content (rapidfuzz)."""
    query = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 20))
    if not query:
        return jsonify({"ok": False, "error": "query required"}), 400
    try:
        results = search_files_as_dicts(query, mode="text", limit=limit, db_module=db)
        return jsonify({"ok": True, "results": results, "query": query})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Phase 3 — Semantic Search ────────────────────────────────────

@bp.route("/api/search")
def api_search():
    """Search files by content.

    ?q=...        — search query (required)
    ?mode=hybrid  — text | semantic | hybrid (default: hybrid)
    ?limit=20     — max results
    """
    query = request.args.get("q", "").strip()
    mode = request.args.get("mode", "hybrid")
    limit = int(request.args.get("limit", 20))
    if not query:
        return jsonify({"ok": False, "error": "query required"}), 400
    try:
        results = search_files_as_dicts(query, mode=mode, limit=limit, db_module=db)
        return jsonify({"ok": True, "results": results, "query": query, "mode": mode})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Phase 3 — Safety Settings ────────────────────────────────────

@bp.route("/api/safety/settings", methods=["GET"])
def api_safety_settings_get():
    """Return the current safety/certainty threshold configuration."""
    try:
        settings = db.get_safety_settings()
        return jsonify({"ok": True, "settings": settings})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/safety/settings", methods=["PUT"])
def api_safety_settings_put():
    """Update safety/certainty threshold settings.

    Accepted keys:
      duplicate_certainty_threshold  float  0.0–1.0  (default 0.95)
      organize_certainty_threshold   float  0.0–1.0  (default 0.85)
      enable_auto_delete             bool   (default false)
      require_human_confirmation_below float (default 1.0)
    """
    data = request.get_json(force=True)
    allowed_keys = {
        "duplicate_certainty_threshold",
        "organize_certainty_threshold",
        "enable_auto_delete",
        "require_human_confirmation_below",
    }
    filtered = {k: v for k, v in data.items() if k in allowed_keys}
    if not filtered:
        return jsonify({"ok": False, "error": "No valid settings keys provided"}), 400
    try:
        db.save_safety_settings(filtered)
        return jsonify({"ok": True, "saved": filtered})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Extensions (placeholders) ────────────────────────────────────

@bp.route("/api/extensions")
def api_extensions():
    """List available extension modules and their status."""
    extensions = [
        {
            "id": "facial_recognition",
            "name": "Facial Recognition",
            "description": "Detect and cluster faces in photos for people-based organization.",
            "status": "planned",
            "icon": "👤",
        },
        {
            "id": "auto_tagging",
            "name": "Auto-Tagging",
            "description": "Automatically tag files with keywords based on content analysis.",
            "status": "planned",
            "icon": "🏷️",
        },
        {
            "id": "smart_collections",
            "name": "Smart Collections",
            "description": "Auto-updating collections based on rules and AI analysis.",
            "status": "planned",
            "icon": "📚",
        },
        {
            "id": "search",
            "name": "Semantic Search",
            "description": "Search files by content, description, or visual similarity.",
            "status": "active",
            "icon": "🔎",
        },
        {
            "id": "google_photos",
            "name": "Google Photos Integration",
            "description": "Import and sync with Google Photos library.",
            "status": "planned",
            "icon": "📸",
        },
        {
            "id": "google_drive",
            "name": "Google Drive Integration",
            "description": "Import and sync with Google Drive.",
            "status": "planned",
            "icon": "☁️",
        },
        {
            "id": "dropbox",
            "name": "Dropbox Migration",
            "description": "Migrate files from Dropbox to Proton Drive.",
            "status": "active",
            "icon": "📦",
        },
    ]
    return jsonify({"ok": True, "extensions": extensions})


# ── Phase 4 pages ─────────────────────────────────────────────────────────

@bp.route("/chat")
def chat_page():
    return render_template("ai/chat.html", active="ai_chat")


@bp.route("/agents")
def agents_page():
    return render_template("ai/agents.html", active="ai_agents")


@bp.route("/access-control")
def access_control_page():
    return render_template("ai/access_control.html", active="ai_access")


# ── API: Migration pause/resume ───────────────────────────────────────────

@bp.route("/api/migration/<migration_id>/pause", methods=["POST"])
def api_migration_pause(migration_id):
    result = pause_migration(migration_id)
    if result["ok"]:
        return jsonify(result)
    return jsonify(result), 400


@bp.route("/api/migration/<migration_id>/resume", methods=["POST"])
def api_migration_resume(migration_id):
    result = resume_migration(migration_id)
    if result["ok"]:
        return jsonify(result)
    return jsonify(result), 400


@bp.route("/api/migration/<migration_id>/status")
def api_migration_status_detail(migration_id):
    status = get_migration_status(migration_id)
    if status is None:
        # Fall back to DB record
        try:
            record = db.get_migration_job(migration_id)
            if record:
                d = dict(record)
                for k in ("started_at", "finished_at", "created_at"):
                    if d.get(k) and isinstance(d[k], datetime):
                        d[k] = d[k].isoformat()
                return jsonify({"ok": True, "source": "db", **d})
        except Exception:
            pass
        return jsonify({"ok": False, "error": "Migration not found"}), 404
    return jsonify({"ok": True, **status})


@bp.route("/api/migration/validate", methods=["POST"])
def api_migration_validate():
    data = request.get_json(force=True)
    source = data.get("source_remote")
    dest = data.get("dest_remote")
    if not source or not dest:
        return jsonify({"ok": False, "error": "source_remote and dest_remote required"}), 400
    try:
        result = validate_endpoints(source, dest)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Chat / NL Q&A ────────────────────────────────────────────────────

@bp.route("/api/chat/message", methods=["POST"])
def api_chat_message():
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    session_id = data.get("session_id") or str(uuid.uuid4())
    scope_filter = data.get("scope", "all")  # 'local' | 'proton' | 'all'

    # Load active access scope
    scope = None
    try:
        scope = db.get_active_scope()
        if scope and scope_filter == "local":
            # Restrict to local paths only (no remotes)
            scope = dict(scope)
            scope["allowed_remotes"] = []
    except Exception:
        pass

    try:
        from .engines.query_engine import answer_question
        result = answer_question(message, db_module=db, scope=scope)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Persist to chat history
    try:
        db.save_chat_message(session_id, "user", message)
        db.save_chat_message(
            session_id, "assistant", result.get("answer", ""),
            query_type=result.get("query_type"),
            result_count=len(result.get("files", []))
        )
    except Exception:
        pass

    return jsonify({"ok": True, "session_id": session_id, **result})


@bp.route("/api/chat/history")
def api_chat_history():
    session_id = request.args.get("session_id", "")
    if not session_id:
        return jsonify({"ok": False, "error": "session_id required"}), 400
    try:
        history = db.get_chat_history(session_id)
        return jsonify({"ok": True, "session_id": session_id, "history": history})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Agents / Workflows ───────────────────────────────────────────────

@bp.route("/api/agents/workflows")
def api_agents_workflows():
    from .agents.agent_runner import WORKFLOW_SCHEMAS
    return jsonify({"ok": True, "workflows": WORKFLOW_SCHEMAS})


@bp.route("/api/agents/run", methods=["POST"])
def api_agents_run():
    data = request.get_json(force=True)
    workflow = data.get("workflow")
    params = data.get("params", {})
    if not workflow:
        return jsonify({"ok": False, "error": "workflow is required"}), 400

    from .agents.agent_runner import AgentRunner, WORKFLOW_SCHEMAS
    if workflow not in WORKFLOW_SCHEMAS:
        return jsonify({"ok": False, "error": f"Unknown workflow: {workflow}",
                        "available": list(WORKFLOW_SCHEMAS.keys())}), 400

    def _run_agent(job_id=None, **kw):
        runner = AgentRunner(db_module=db)
        result = runner.run_workflow(workflow, params, job_id=job_id)
        try:
            db.update_job(job_id, status="completed", result=result)
        except Exception:
            pass

    job_id = _run_bg(f"agent_{workflow}", _run_agent)
    db.create_job(job_id, f"agent_{workflow}")
    return jsonify({"ok": True, "job_id": job_id, "workflow": workflow})


# ── API: Access Control ───────────────────────────────────────────────────

@bp.route("/api/access/scope", methods=["GET"])
def api_get_scope():
    try:
        scope = db.get_active_scope()
        return jsonify({"ok": True, "scope": scope})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/access/scope", methods=["PUT"])
def api_put_scope():
    data = request.get_json(force=True)
    allowed_keys = {"id", "scope_name", "allowed_paths", "allowed_remotes",
                    "blocked_patterns", "enabled"}
    scope = {k: v for k, v in data.items() if k in allowed_keys}

    # Validate paths
    validated_paths = []
    for p in scope.get("allowed_paths") or []:
        try:
            validated_paths.append(str(_safe_path(p, must_exist=False)))
        except ValueError as e:
            return jsonify({"ok": False, "error": f"Invalid path: {e}"}), 400
    if validated_paths:
        scope["allowed_paths"] = validated_paths

    try:
        scope_id = db.save_scope(scope)
        return jsonify({"ok": True, "scope_id": scope_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/access/check")
def api_access_check():
    filepath = request.args.get("path", "")
    if not filepath:
        return jsonify({"ok": False, "error": "path parameter required"}), 400
    try:
        scope = db.get_active_scope()
        allowed = db.path_allowed(filepath, scope)
        return jsonify({"ok": True, "path": filepath, "allowed": allowed,
                        "scope": scope})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Local LLM ────────────────────────────────────────────────────────

@bp.route("/api/llm/status")
def api_llm_status():
    try:
        from .llm.task_router import get_router
        status = get_router().get_status()
        return jsonify({"ok": True, **status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/llm/models")
def api_llm_models():
    try:
        from .llm.ollama_client import get_client
        models = get_client().list_models()
        return jsonify({"ok": True, "models": models})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/llm/pull", methods=["POST"])
def api_llm_pull():
    data = request.get_json(force=True)
    model = data.get("model")
    if not model:
        return jsonify({"ok": False, "error": "model is required"}), 400

    def _do_pull(job_id=None, **kw):
        from .llm.ollama_client import get_client
        ok = get_client().pull_model(model)
        try:
            db.update_job(job_id, status="completed" if ok else "failed",
                          message=f"Pull {'succeeded' if ok else 'failed'} for {model}")
        except Exception:
            pass

    job_id = _run_bg(f"llm_pull_{model}", _do_pull)
    try:
        db.create_job(job_id, "llm_pull")
    except Exception:
        pass
    return jsonify({"ok": True, "job_id": job_id, "model": model})


# ── API: Face Recognition ─────────────────────────────────────────────────

@bp.route("/api/faces/scan", methods=["POST"])
def api_faces_scan():
    data = request.get_json(force=True)
    raw_path = data.get("path", "")
    try:
        safe = _safe_path(raw_path)
        if not safe.is_dir():
            return jsonify({"ok": False, "error": "Path is not a directory"}), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    def _do_face_scan(job_id=None, **kw):
        from .engines.face_engine import scan_faces_in_directory
        result = scan_faces_in_directory(str(safe), db_module=db, job_id=job_id)
        try:
            db.update_job(job_id, status="completed", result=result)
        except Exception:
            pass

    job_id = _run_bg("face_scan", _do_face_scan)
    try:
        db.create_job(job_id, "face_scan")
    except Exception:
        pass
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/faces/clusters")
def api_faces_clusters():
    try:
        clusters = db.get_face_clusters()
        return jsonify({"ok": True, "clusters": clusters, "count": len(clusters)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/faces/clusters/<int:cluster_id>", methods=["PUT"])
def api_faces_rename_cluster(cluster_id):
    data = request.get_json(force=True)
    name = (data.get("suggested_name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "suggested_name is required"}), 400
    try:
        db.update_face_cluster(cluster_id, suggested_name=name)
        return jsonify({"ok": True, "cluster_id": cluster_id, "suggested_name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
