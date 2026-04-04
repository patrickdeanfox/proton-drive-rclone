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

_LEGACY_CFG = Path.home() / ".config" / "protondrive-linux"
_NEW_CFG    = Path.home() / ".config" / "protondrive"
_CFG_DIR = _LEGACY_CFG if _LEGACY_CFG.exists() else _NEW_CFG
DB_SETTINGS_FILE = _CFG_DIR / "db_settings.json"


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

-- ── Phase 3 additions ────────────────────────────────────────────────────

-- Rich metadata extracted by Python (EXIF, audio tags, PDF info, MIME)
-- Stored separately so it can be queried/indexed independently of files.metadata_json
CREATE TABLE IF NOT EXISTS file_metadata (
    id              BIGSERIAL PRIMARY KEY,
    file_id         BIGINT REFERENCES files(id) ON DELETE CASCADE UNIQUE,
    file_type       TEXT,                 -- 'image', 'audio', 'pdf', 'video', etc.
    python_category TEXT,                 -- category from metadata_engine
    python_confidence REAL DEFAULT 0,     -- 0.0-1.0 classification confidence
    needs_ai        BOOLEAN DEFAULT TRUE, -- False when Python is fully confident
    metadata        JSONB DEFAULT '{}',   -- full extracted metadata blob
    extracted_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_filemeta_file    ON file_metadata(file_id);
CREATE INDEX IF NOT EXISTS idx_filemeta_type    ON file_metadata(file_type);
CREATE INDEX IF NOT EXISTS idx_filemeta_cat     ON file_metadata(python_category);

-- OCR text extracted from images and PDFs (pytesseract, pypdf)
CREATE TABLE IF NOT EXISTS file_ocr_text (
    id              BIGSERIAL PRIMARY KEY,
    file_id         BIGINT REFERENCES files(id) ON DELETE CASCADE UNIQUE,
    text_content    TEXT NOT NULL,
    language        TEXT DEFAULT 'eng',
    word_count      INT,
    extracted_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ocr_file ON file_ocr_text(file_id);
-- Full-text search index on OCR content
CREATE INDEX IF NOT EXISTS idx_ocr_fts ON file_ocr_text
    USING gin(to_tsvector('english', text_content));

-- Vector embeddings for semantic search (requires pgvector extension)
-- embed_type: 'image' (CLIP 512-dim) | 'text' (sentence-transformers 384-dim)
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN others THEN
    RAISE NOTICE 'pgvector extension not available — file_embeddings will use JSONB fallback';
END $$;

CREATE TABLE IF NOT EXISTS file_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    file_id         BIGINT REFERENCES files(id) ON DELETE CASCADE,
    embed_type      TEXT NOT NULL,        -- 'image' or 'text'
    model_name      TEXT NOT NULL,        -- e.g. 'clip-vit-base-patch32'
    embedding_json  JSONB,                -- fallback when pgvector unavailable
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (file_id, embed_type)
);
CREATE INDEX IF NOT EXISTS idx_emb_file ON file_embeddings(file_id);
CREATE INDEX IF NOT EXISTS idx_emb_type ON file_embeddings(embed_type);

-- Try to add the pgvector column (safe no-op if extension missing)
DO $$ BEGIN
    ALTER TABLE file_embeddings ADD COLUMN IF NOT EXISTS
        embedding vector(512);
EXCEPTION WHEN undefined_object THEN
    RAISE NOTICE 'pgvector not available — using JSONB for embeddings';
END $$;

-- IVFFlat index for fast ANN search (created after data is loaded)
-- CREATE INDEX ON file_embeddings USING ivfflat (embedding vector_cosine_ops)
--   WITH (lists = 100);  -- uncomment after >10k embeddings are stored

-- Migration jobs tracking
CREATE TABLE IF NOT EXISTS migration_jobs (
    id              TEXT PRIMARY KEY,
    source_remote   TEXT NOT NULL,
    source_path     TEXT DEFAULT '',
    dest_remote     TEXT NOT NULL,
    dest_path       TEXT DEFAULT '',
    mode            TEXT DEFAULT 'copy',
    status          TEXT DEFAULT 'pending',
    progress        REAL DEFAULT 0,
    bytes_done      BIGINT DEFAULT 0,
    bytes_total     BIGINT DEFAULT 0,
    files_done      INT DEFAULT 0,
    files_total     INT DEFAULT 0,
    options_json    JSONB DEFAULT '{}',
    result_json     JSONB DEFAULT '{}',
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Progress snapshots for operations
CREATE TABLE IF NOT EXISTS progress_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    operation_id    TEXT NOT NULL,
    operation_type  TEXT NOT NULL,
    percent         REAL DEFAULT 0,
    current_val     BIGINT DEFAULT 0,
    total_val       BIGINT DEFAULT 0,
    message         TEXT,
    speed           REAL,
    eta_seconds     REAL,
    snapshot_json   JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_progress_op ON progress_snapshots(operation_id);
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


def create_migration_job(data: dict):
    """Create a migration job record."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO migration_jobs (id, source_remote, source_path, dest_remote, dest_path,
                                        mode, status, options_json, started_at)
            VALUES (%(id)s, %(source_remote)s, %(source_path)s, %(dest_remote)s, %(dest_path)s,
                    %(mode)s, 'running', %(options_json)s, now())
        """, data)


def update_migration_job(job_id, **kwargs):
    """Update a migration job record."""
    with get_conn() as conn:
        cur = conn.cursor()
        sets = []
        params = []
        for k, v in kwargs.items():
            if k == "result":
                sets.append("result_json = %s")
                params.append(json.dumps(v))
            elif k == "options":
                sets.append("options_json = %s")
                params.append(json.dumps(v))
            else:
                sets.append(f"{k} = %s")
                params.append(v)
        if kwargs.get("status") in ("completed", "failed", "cancelled"):
            sets.append("finished_at = now()")
        if sets:
            params.append(job_id)
            cur.execute(f"UPDATE migration_jobs SET {', '.join(sets)} WHERE id = %s", params)


def get_migration_jobs(limit=20):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM migration_jobs ORDER BY created_at DESC LIMIT %s", (limit,))
        return cur.fetchall()


def get_migration_job(job_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM migration_jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Phase 3 CRUD: file_metadata, file_ocr_text, file_embeddings
# ---------------------------------------------------------------------------

def save_file_metadata(file_id: int, file_type: str, python_category: str,
                       python_confidence: float, needs_ai: bool, metadata: dict):
    """Upsert Python-extracted metadata for a file."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO file_metadata
                (file_id, file_type, python_category, python_confidence, needs_ai, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (file_id) DO UPDATE SET
                file_type         = EXCLUDED.file_type,
                python_category   = EXCLUDED.python_category,
                python_confidence = EXCLUDED.python_confidence,
                needs_ai          = EXCLUDED.needs_ai,
                metadata          = EXCLUDED.metadata,
                extracted_at      = now()
        """, (file_id, file_type, python_category,
              python_confidence, needs_ai, json.dumps(metadata)))


def get_file_metadata(file_id: int) -> dict:
    """Return rich metadata for a file, or None."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM file_metadata WHERE file_id = %s", (file_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def save_ocr_text(file_id: int, text: str, lang: str = "eng"):
    """Upsert OCR-extracted text for a file."""
    word_count = len(text.split()) if text else 0
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO file_ocr_text (file_id, text_content, language, word_count)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (file_id) DO UPDATE SET
                text_content = EXCLUDED.text_content,
                language     = EXCLUDED.language,
                word_count   = EXCLUDED.word_count,
                extracted_at = now()
        """, (file_id, text, lang, word_count))


def get_ocr_text(file_id: int) -> dict:
    """Return OCR text record for a file, or None."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM file_ocr_text WHERE file_id = %s", (file_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def save_file_embedding(file_id: int, embedding: list, embed_type: str = "image",
                        model_name: str = None):
    """Upsert a vector embedding for a file.

    Stores in the vector column when pgvector is available, always stores
    in embedding_json as a fallback.
    """
    if model_name is None:
        model_name = ("clip-vit-base-patch32" if embed_type == "image"
                      else "all-MiniLM-L6-v2")
    emb_json = json.dumps(embedding)
    # Try pgvector column first
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO file_embeddings
                    (file_id, embed_type, model_name, embedding, embedding_json)
                VALUES (%s, %s, %s, %s::vector, %s)
                ON CONFLICT (file_id, embed_type) DO UPDATE SET
                    model_name     = EXCLUDED.model_name,
                    embedding      = EXCLUDED.embedding,
                    embedding_json = EXCLUDED.embedding_json,
                    created_at     = now()
            """, (file_id, embed_type, model_name,
                  str(embedding), emb_json))
    except Exception:
        # pgvector unavailable — fall back to JSONB only
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO file_embeddings
                    (file_id, embed_type, model_name, embedding_json)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (file_id, embed_type) DO UPDATE SET
                    model_name     = EXCLUDED.model_name,
                    embedding_json = EXCLUDED.embedding_json,
                    created_at     = now()
            """, (file_id, embed_type, model_name, emb_json))


def search_by_vector(query_embedding: list, embed_type: str = "text",
                     limit: int = 20) -> list:
    """Return (file_id, file_path, similarity) tuples sorted by cosine similarity."""
    try:
        with get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT f.id, f.file_path, f.file_name,
                       1 - (e.embedding <=> %s::vector) AS similarity
                FROM file_embeddings e
                JOIN files f ON f.id = e.file_id
                WHERE e.embed_type = %s AND e.embedding IS NOT NULL
                ORDER BY similarity DESC
                LIMIT %s
            """, (str(query_embedding), embed_type, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        # pgvector not available
        return []


# Safety settings helpers

def get_safety_settings() -> dict:
    """Return the current safety/certainty threshold settings."""
    defaults = {
        "duplicate_certainty_threshold": 0.95,
        "organize_certainty_threshold": 0.85,
        "enable_auto_delete": False,
        "require_human_confirmation_below": 1.0,
    }
    stored = get_preference("safety_settings", {})
    return {**defaults, **(stored or {})}


def save_safety_settings(settings: dict):
    """Persist safety settings to preferences."""
    current = get_safety_settings()
    current.update(settings)
    set_preference("safety_settings", current)


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
        # Phase 3 stats
        cur.execute("SELECT count(*) as total FROM file_ocr_text")
        stats["ocr_files"] = cur.fetchone()["total"]
        cur.execute("SELECT count(*) as total FROM file_embeddings")
        stats["embedded_files"] = cur.fetchone()["total"]
        cur.execute("SELECT count(*) as total FROM file_metadata")
        stats["metadata_extracted"] = cur.fetchone()["total"]
        # Extension breakdown
        cur.execute("""
            SELECT extension, count(*) as cnt
            FROM files WHERE extension IS NOT NULL
            GROUP BY extension ORDER BY cnt DESC LIMIT 10
        """)
        stats["top_extensions"] = cur.fetchall()
        return stats
