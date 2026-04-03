"""PostgreSQL database layer for AI file organization."""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default connection settings (overrideable via env or web UI)
# ---------------------------------------------------------------------------
DEFAULT_DB = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", 5432)),
    "dbname": os.getenv("PG_DB", "protondrive_ai"),
    "user": os.getenv("PG_USER", "protondrive"),
    "password": os.getenv("PG_PASSWORD", "protondrive"),
}

DB_SETTINGS_FILE = Path.home() / ".config" / "protondrive-linux" / "db_settings.json"


def load_db_settings():
    """Load DB connection settings from disk (or defaults)."""
    if DB_SETTINGS_FILE.exists():
        try:
            return json.loads(DB_SETTINGS_FILE.read_text())
        except Exception:
            pass
    return dict(DEFAULT_DB)


def save_db_settings(settings: dict):
    DB_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DB_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
_pool = None


def get_pool(settings=None, force_new=False):
    global _pool
    if _pool and not force_new:
        return _pool
    if settings is None:
        settings = load_db_settings()
    try:
        _pool = ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=settings["host"],
            port=settings["port"],
            dbname=settings["dbname"],
            user=settings["user"],
            password=settings["password"],
        )
        return _pool
    except Exception as e:
        log.error("Failed to create DB pool: %s", e)
        _pool = None
        raise


@contextmanager
def get_conn():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def test_connection(settings: dict) -> dict:
    """Test a DB connection and return status."""
    try:
        conn = psycopg2.connect(
            host=settings["host"],
            port=settings["port"],
            dbname=settings["dbname"],
            user=settings["user"],
            password=settings["password"],
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()[0]
        conn.close()
        return {"ok": True, "version": version}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- File metadata
CREATE TABLE IF NOT EXISTS files (
    id              BIGSERIAL PRIMARY KEY,
    file_path       TEXT NOT NULL UNIQUE,
    file_name       TEXT NOT NULL,
    extension       TEXT,
    mime_type       TEXT,
    size_bytes      BIGINT,
    sha256          TEXT,
    created_at      TIMESTAMPTZ,
    modified_at     TIMESTAMPTZ,
    indexed_at      TIMESTAMPTZ DEFAULT now(),
    metadata_json   JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_ext    ON files(extension);
CREATE INDEX IF NOT EXISTS idx_files_mime   ON files(mime_type);

-- AI analysis results
CREATE TABLE IF NOT EXISTS ai_analysis (
    id              BIGSERIAL PRIMARY KEY,
    file_id         BIGINT REFERENCES files(id) ON DELETE CASCADE,
    model_name      TEXT NOT NULL,
    analysis_type   TEXT NOT NULL,  -- 'categorization', 'embedding', 'tags', 'description'
    result_json     JSONB NOT NULL,
    confidence      REAL,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_file ON ai_analysis(file_id);

-- Duplicate detection results
CREATE TABLE IF NOT EXISTS duplicate_groups (
    id              BIGSERIAL PRIMARY KEY,
    group_hash      TEXT NOT NULL UNIQUE,
    detection_type  TEXT NOT NULL,  -- 'exact', 'near', 'perceptual'
    status          TEXT DEFAULT 'pending',  -- 'pending','reviewed','resolved'
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS duplicate_members (
    id              BIGSERIAL PRIMARY KEY,
    group_id        BIGINT REFERENCES duplicate_groups(id) ON DELETE CASCADE,
    file_id         BIGINT REFERENCES files(id) ON DELETE CASCADE,
    similarity      REAL DEFAULT 1.0,
    keep            BOOLEAN DEFAULT NULL  -- NULL=undecided, TRUE=keep, FALSE=delete
);
CREATE INDEX IF NOT EXISTS idx_dupmem_group ON duplicate_members(group_id);

-- Organization rules
CREATE TABLE IF NOT EXISTS organization_rules (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    rule_type       TEXT NOT NULL,  -- 'extension','mime','regex','ai_category','date','creator'
    config_json     JSONB NOT NULL,
    priority        INT DEFAULT 0,
    enabled         BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Organization actions log
CREATE TABLE IF NOT EXISTS organization_log (
    id              BIGSERIAL PRIMARY KEY,
    file_id         BIGINT REFERENCES files(id) ON DELETE SET NULL,
    rule_id         BIGINT REFERENCES organization_rules(id) ON DELETE SET NULL,
    action          TEXT NOT NULL,  -- 'move','copy','tag'
    source_path     TEXT,
    dest_path       TEXT,
    status          TEXT DEFAULT 'proposed',  -- 'proposed','applied','reverted'
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- User preferences
CREATE TABLE IF NOT EXISTS preferences (
    key             TEXT PRIMARY KEY,
    value           JSONB NOT NULL,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Job tracking
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    job_type        TEXT NOT NULL,  -- 'scan','organize','duplicates','ai_analysis'
    status          TEXT DEFAULT 'pending',
    progress        REAL DEFAULT 0,
    message         TEXT,
    result_json     JSONB DEFAULT '{}',
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Extension placeholders
CREATE TABLE IF NOT EXISTS tags (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    color           TEXT DEFAULT '#6366f1'
);

CREATE TABLE IF NOT EXISTS file_tags (
    file_id         BIGINT REFERENCES files(id) ON DELETE CASCADE,
    tag_id          BIGINT REFERENCES tags(id) ON DELETE CASCADE,
    auto_generated  BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (file_id, tag_id)
);

CREATE TABLE IF NOT EXISTS collections (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    smart_filter    JSONB,  -- for smart collections
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collection_files (
    collection_id   BIGINT REFERENCES collections(id) ON DELETE CASCADE,
    file_id         BIGINT REFERENCES files(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, file_id)
);
"""


def init_schema():
    """Create all tables if they don't exist."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(SCHEMA_SQL)
        log.info("Database schema initialized.")


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------
def upsert_file(data: dict) -> int:
    """Insert or update a file record. Returns file id."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO files (file_path, file_name, extension, mime_type,
                               size_bytes, sha256, created_at, modified_at, metadata_json)
            VALUES (%(file_path)s, %(file_name)s, %(extension)s, %(mime_type)s,
                    %(size_bytes)s, %(sha256)s, %(created_at)s, %(modified_at)s,
                    %(metadata_json)s)
            ON CONFLICT (file_path) DO UPDATE SET
                file_name = EXCLUDED.file_name,
                extension = EXCLUDED.extension,
                mime_type = EXCLUDED.mime_type,
                size_bytes = EXCLUDED.size_bytes,
                sha256 = EXCLUDED.sha256,
                modified_at = EXCLUDED.modified_at,
                metadata_json = EXCLUDED.metadata_json,
                indexed_at = now()
            RETURNING id
        """, data)
        return cur.fetchone()[0]


def get_files(limit=100, offset=0, extension=None, mime_prefix=None):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        q = "SELECT * FROM files WHERE 1=1"
        params = []
        if extension:
            q += " AND extension = %s"
            params.append(extension)
        if mime_prefix:
            q += " AND mime_type LIKE %s"
            params.append(mime_prefix + '%')
        q += " ORDER BY indexed_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        cur.execute(q, params)
        return cur.fetchall()


def get_file_count():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM files")
        return cur.fetchone()[0]


def save_ai_analysis(file_id, model_name, analysis_type, result, confidence=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ai_analysis (file_id, model_name, analysis_type, result_json, confidence)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (file_id, model_name, analysis_type, json.dumps(result), confidence))
        return cur.fetchone()[0]


def save_duplicate_group(group_hash, detection_type, members):
    """Save a duplicate group. members = [(file_id, similarity), ...]"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO duplicate_groups (group_hash, detection_type)
            VALUES (%s, %s)
            ON CONFLICT (group_hash) DO UPDATE SET created_at = now()
            RETURNING id
        """, (group_hash, detection_type))
        gid = cur.fetchone()[0]
        # Clear old members
        cur.execute("DELETE FROM duplicate_members WHERE group_id = %s", (gid,))
        for fid, sim in members:
            cur.execute("""
                INSERT INTO duplicate_members (group_id, file_id, similarity)
                VALUES (%s, %s, %s)
            """, (gid, fid, sim))
        return gid


def get_duplicate_groups(status=None, detection_type=None, limit=50, offset=0):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        q = """SELECT dg.*, count(dm.id) as member_count
               FROM duplicate_groups dg
               LEFT JOIN duplicate_members dm ON dm.group_id = dg.id
               WHERE 1=1"""
        params = []
        if status:
            q += " AND dg.status = %s"
            params.append(status)
        if detection_type:
            q += " AND dg.detection_type = %s"
            params.append(detection_type)
        q += " GROUP BY dg.id ORDER BY dg.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        cur.execute(q, params)
        return cur.fetchall()


def get_duplicate_group_detail(group_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT dm.*, f.file_path, f.file_name, f.size_bytes, f.sha256, f.mime_type
            FROM duplicate_members dm
            JOIN files f ON f.id = dm.file_id
            WHERE dm.group_id = %s
            ORDER BY f.size_bytes DESC
        """, (group_id,))
        return cur.fetchall()


def update_duplicate_member_keep(member_id, keep):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE duplicate_members SET keep = %s WHERE id = %s", (keep, member_id))


def update_duplicate_group_status(group_id, status):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE duplicate_groups SET status = %s WHERE id = %s", (status, group_id))


# Organization rules CRUD
def get_org_rules(enabled_only=False):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        q = "SELECT * FROM organization_rules"
        if enabled_only:
            q += " WHERE enabled = TRUE"
        q += " ORDER BY priority DESC, id"
        cur.execute(q)
        return cur.fetchall()


def save_org_rule(rule: dict) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        if rule.get("id"):
            cur.execute("""
                UPDATE organization_rules SET name=%s, description=%s,
                rule_type=%s, config_json=%s, priority=%s, enabled=%s, updated_at=now()
                WHERE id=%s RETURNING id
            """, (rule["name"], rule.get("description"), rule["rule_type"],
                  json.dumps(rule["config_json"]), rule.get("priority", 0),
                  rule.get("enabled", True), rule["id"]))
        else:
            cur.execute("""
                INSERT INTO organization_rules (name, description, rule_type, config_json, priority, enabled)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (rule["name"], rule.get("description"), rule["rule_type"],
                  json.dumps(rule["config_json"]), rule.get("priority", 0),
                  rule.get("enabled", True)))
        return cur.fetchone()[0]


def delete_org_rule(rule_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM organization_rules WHERE id = %s", (rule_id,))


# Preferences
def get_preference(key, default=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM preferences WHERE key = %s", (key,))
        row = cur.fetchone()
        if row:
            return row[0]
        return default


def set_preference(key, value):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO preferences (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """, (key, json.dumps(value)))


# Jobs
def create_job(job_id, job_type):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO jobs (id, job_type, status, started_at)
            VALUES (%s, %s, 'running', now())
        """, (job_id, job_type))


def update_job(job_id, status=None, progress=None, message=None, result=None):
    with get_conn() as conn:
        cur = conn.cursor()
        sets = []
        params = []
        if status:
            sets.append("status = %s")
            params.append(status)
            if status in ('completed', 'failed'):
                sets.append("finished_at = now()")
        if progress is not None:
            sets.append("progress = %s")
            params.append(progress)
        if message:
            sets.append("message = %s")
            params.append(message)
        if result is not None:
            sets.append("result_json = %s")
            params.append(json.dumps(result))
        if sets:
            params.append(job_id)
            cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = %s", params)


def get_job(job_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def get_recent_jobs(limit=20):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT %s", (limit,))
        return cur.fetchall()


def get_stats():
    """Get overview statistics."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        stats = {}
        cur.execute("SELECT count(*) as total FROM files")
        stats["total_files"] = cur.fetchone()["total"]
        cur.execute("SELECT count(*) as total FROM duplicate_groups WHERE status != 'resolved'")
        stats["pending_duplicates"] = cur.fetchone()["total"]
        cur.execute("SELECT count(*) as total FROM ai_analysis")
        stats["ai_analyses"] = cur.fetchone()["total"]
        cur.execute("SELECT count(*) as total FROM organization_rules WHERE enabled = TRUE")
        stats["active_rules"] = cur.fetchone()["total"]
        cur.execute("SELECT coalesce(sum(size_bytes),0) as total FROM files")
        stats["total_size_bytes"] = cur.fetchone()["total"]
        # Extension breakdown
        cur.execute("""
            SELECT extension, count(*) as cnt
            FROM files WHERE extension IS NOT NULL
            GROUP BY extension ORDER BY cnt DESC LIMIT 10
        """)
        stats["top_extensions"] = cur.fetchall()
        return stats
