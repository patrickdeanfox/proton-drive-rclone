"""Flask blueprint with all AI-organizer API endpoints."""

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

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
    cancel_migration, get_migration_status, get_migration_logs,
    list_active_migrations,
)
from .utils.yaml_rules import import_rules_from_yaml, export_rules_to_yaml
from .progress import (
    ProgressTracker, register_operation, unregister_operation,
    get_active_operations, get_operation, emit_progress,
)

log = logging.getLogger(__name__)
bp = Blueprint("ai", __name__, url_prefix="/ai")

# Convenience: run a long task in background
_bg_threads = {}


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

    t = threading.Thread(target=_wrapper, daemon=True)
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
    path = data.get("path", "")
    if not path or not Path(path).is_dir():
        return jsonify({"ok": False, "error": "Invalid directory path"}), 400
    job_id = _run_bg("scan", scan_directory, path)
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
    yaml_path = data.get("path", "")
    if not yaml_path or not Path(yaml_path).exists():
        return jsonify({"ok": False, "error": "YAML file not found"}), 400
    try:
        count = import_rules_from_yaml(yaml_path)
        return jsonify({"ok": True, "imported": count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/rules/export", methods=["POST"])
def api_export_rules():
    data = request.get_json(force=True)
    output = data.get("path", "/tmp/organize-rules-export.yml")
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
            import psycopg2.extras
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
            import psycopg2.extras
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
