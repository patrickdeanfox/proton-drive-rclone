#!/usr/bin/env python3
"""MCP server exposing the proton-drive-rclone file index to AI agents.

Runs over stdio (standard MCP transport) — compatible with Claude Code
and any MCP-capable client.

Tools exposed:
  search_files        — full-text / semantic / hybrid search
  get_file_metadata   — rich metadata + OCR text for a file
  find_duplicates     — query duplicate groups with similarity scores
  browse_files        — list files in a directory with metadata filters
  propose_organization — run Python-first → AI pipeline, return proposals

Usage (add to Claude Code settings or MCP client config):
  {
    "command": "python",
    "args": ["/path/to/ai_organizer/mcp_server.py"],
    "env": {
      "PG_HOST": "localhost",
      "PG_DB": "protondrive_ai",
      "PG_USER": "protondrive",
      "PG_PASSWORD": "protondrive"
    }
  }

Or run directly for testing:
  python ai_organizer/mcp_server.py
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure the project root is on the path when run as a script
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool, TextContent,
        CallToolResult, ListToolsResult,
    )
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    print("ERROR: mcp package not installed. Run: pip install mcp>=1.0.0",
          file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Database access (lazy — only connects when a tool is called)
# ---------------------------------------------------------------------------

_db = None


def _get_db():
    global _db
    if _db is not None:
        return _db
    from ai_organizer import database as database_module
    _db = database_module
    return _db


# ---------------------------------------------------------------------------
# Helper: serialize datetime objects
# ---------------------------------------------------------------------------

def _serialize(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes, dict)):
        return [_serialize(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_search_files(query: str, mode: str = "hybrid",
                       limit: int = 20) -> dict:
    """Full-text / semantic / hybrid file search."""
    from ai_organizer.engines.search_engine import search_files_as_dicts
    db = _get_db()
    results = search_files_as_dicts(query, mode=mode, limit=limit, db_module=db)
    return {
        "query": query,
        "mode": mode,
        "count": len(results),
        "results": results,
    }


def _tool_get_file_metadata(file_id: int = None,
                             file_path: str = None) -> dict:
    """Return rich metadata, OCR text, and AI analysis for a file."""
    db = _get_db()
    # Resolve file_id from path if needed
    if file_id is None and file_path:
        with db.get_conn() as conn:
            import psycopg2.extras
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT id FROM files WHERE file_path = %s", (file_path,))
            row = cur.fetchone()
            if row:
                file_id = row["id"]
            else:
                return {"error": f"File not found: {file_path}"}

    if file_id is None:
        return {"error": "Provide file_id or file_path"}

    # Gather all information
    result = {"file_id": file_id}

    with db.get_conn() as conn:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Base file record
        cur.execute("SELECT * FROM files WHERE id = %s", (file_id,))
        file_row = cur.fetchone()
        if not file_row:
            return {"error": f"File ID {file_id} not found"}
        result["file"] = _serialize(dict(file_row))

        # Rich metadata
        cur.execute("SELECT * FROM file_metadata WHERE file_id = %s", (file_id,))
        meta_row = cur.fetchone()
        result["metadata"] = _serialize(dict(meta_row)) if meta_row else None

        # OCR text
        cur.execute("SELECT text_content, language, word_count, extracted_at "
                    "FROM file_ocr_text WHERE file_id = %s", (file_id,))
        ocr_row = cur.fetchone()
        if ocr_row:
            result["ocr"] = {
                "language": ocr_row["language"],
                "word_count": ocr_row["word_count"],
                "extracted_at": _serialize(ocr_row["extracted_at"]),
                "text_preview": (ocr_row["text_content"] or "")[:500],
            }
        else:
            result["ocr"] = None

        # AI analysis
        cur.execute("""
            SELECT model_name, analysis_type, result_json, confidence, created_at
            FROM ai_analysis WHERE file_id = %s ORDER BY created_at DESC LIMIT 5
        """, (file_id,))
        analyses = [_serialize(dict(r)) for r in cur.fetchall()]
        result["ai_analyses"] = analyses

        # Embeddings presence
        cur.execute("SELECT embed_type, model_name FROM file_embeddings WHERE file_id = %s",
                    (file_id,))
        result["embeddings"] = [dict(r) for r in cur.fetchall()]

    return result


def _tool_find_duplicates(dup_type: str = "all",
                           min_certainty: float = 0.0,
                           limit: int = 20) -> dict:
    """Query duplicate groups with similarity scores and certainty filtering."""
    db = _get_db()
    detection_type = None if dup_type == "all" else dup_type

    groups = db.get_duplicate_groups(
        detection_type=detection_type,
        limit=limit,
        offset=0,
    )

    result_groups = []
    for g in groups:
        gd = _serialize(dict(g))
        # Fetch members with similarity
        members = db.get_duplicate_group_detail(gd["id"])
        members_list = []
        for m in members:
            md = _serialize(dict(m))
            sim = float(md.get("similarity") or 0)
            if sim >= min_certainty:
                members_list.append(md)
        if members_list:
            gd["members"] = members_list
            result_groups.append(gd)

    return {
        "type_filter": dup_type,
        "min_certainty": min_certainty,
        "count": len(result_groups),
        "groups": result_groups,
    }


def _tool_browse_files(path: str = "/", mime_prefix: str = None,
                        extension: str = None, limit: int = 50) -> dict:
    """List indexed files in a directory with rich metadata."""
    db = _get_db()

    with db.get_conn() as conn:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        params = [f"{path}%"]
        q = """
            SELECT f.id, f.file_path, f.file_name, f.extension, f.mime_type,
                   f.size_bytes, f.modified_at, m.python_category,
                   m.python_confidence, m.needs_ai
            FROM files f
            LEFT JOIN file_metadata m ON m.file_id = f.id
            WHERE f.file_path LIKE %s
        """
        if mime_prefix:
            q += " AND f.mime_type LIKE %s"
            params.append(mime_prefix + "%")
        if extension:
            q += " AND f.extension = %s"
            params.append(extension)
        q += " ORDER BY f.file_path LIMIT %s"
        params.append(limit)
        cur.execute(q, params)
        files = [_serialize(dict(r)) for r in cur.fetchall()]

    return {
        "path": path,
        "count": len(files),
        "files": files,
    }


def _tool_propose_organization(path: str,
                                confidence_threshold: float = 0.85) -> dict:
    """Run the Python-first → AI pipeline and return organization proposals.

    Does NOT apply any changes — read-only preview.
    """
    from ai_organizer.engines.rule_engine import propose_organization
    db = _get_db()

    try:
        proposals = propose_organization(path, db_module=db)
    except TypeError:
        # Signature may vary — try without db_module
        proposals = propose_organization(path)

    if not isinstance(proposals, list):
        proposals = list(proposals) if proposals else []

    # Filter by confidence threshold
    safe_proposals = []
    for p in proposals:
        conf = float(p.get("confidence", 1.0))
        if conf >= confidence_threshold:
            safe_proposals.append(p)

    return {
        "path": path,
        "confidence_threshold": confidence_threshold,
        "total_proposals": len(proposals),
        "safe_proposals": len(safe_proposals),
        "proposals": safe_proposals,
        "note": (f"{len(proposals) - len(safe_proposals)} proposals below "
                 f"confidence threshold ({confidence_threshold:.0%}) were filtered."),
    }


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="search_files",
        description=(
            "Search indexed files by content. Supports three modes:\n"
            "  text     — fuzzy filename + OCR text search (rapidfuzz)\n"
            "  semantic — vector similarity search (pgvector + sentence-transformers)\n"
            "  hybrid   — combines both (recommended)\n"
            "Returns ranked list of files with relevance scores and text snippets."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "mode": {
                    "type": "string",
                    "enum": ["text", "semantic", "hybrid"],
                    "default": "hybrid",
                    "description": "Search mode",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum results to return",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_file_metadata",
        description=(
            "Return rich metadata for a file: EXIF data, audio tags, PDF info, "
            "OCR text preview, AI categorization results, and embedding status. "
            "Provide either file_id (integer) or file_path (absolute string)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "Database file ID"},
                "file_path": {"type": "string", "description": "Absolute file path"},
            },
        },
    ),
    Tool(
        name="find_duplicates",
        description=(
            "Query duplicate groups detected in the file index.\n"
            "Types: exact (SHA-256), near (perceptual hash), fuzzy (text similarity), all.\n"
            "Use min_certainty to filter out low-confidence matches."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dup_type": {
                    "type": "string",
                    "enum": ["all", "exact", "near", "fuzzy"],
                    "default": "all",
                    "description": "Filter by detection method",
                },
                "min_certainty": {
                    "type": "number",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Minimum similarity score (0.0–1.0) to include",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum groups to return",
                },
            },
        },
    ),
    Tool(
        name="browse_files",
        description=(
            "List indexed files under a directory path with rich metadata. "
            "Optionally filter by MIME type prefix or file extension."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "default": "/",
                    "description": "Directory path prefix to filter by",
                },
                "mime_prefix": {
                    "type": "string",
                    "description": "MIME prefix filter, e.g. 'image/', 'audio/'",
                },
                "extension": {
                    "type": "string",
                    "description": "File extension filter, e.g. '.pdf'",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Maximum files to return",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="propose_organization",
        description=(
            "Run the Python-first → AI organization pipeline on a directory and "
            "return proposed file moves. Read-only — does NOT apply any changes. "
            "Only proposals above confidence_threshold are returned."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to analyze",
                },
                "confidence_threshold": {
                    "type": "number",
                    "default": 0.85,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Minimum confidence to include a proposal",
                },
            },
            "required": ["path"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Server main
# ---------------------------------------------------------------------------

async def _run_server():
    server = Server("proton-drive-rclone")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "search_files":
                result = _tool_search_files(
                    query=arguments["query"],
                    mode=arguments.get("mode", "hybrid"),
                    limit=int(arguments.get("limit", 20)),
                )
            elif name == "get_file_metadata":
                result = _tool_get_file_metadata(
                    file_id=arguments.get("file_id"),
                    file_path=arguments.get("file_path"),
                )
            elif name == "find_duplicates":
                result = _tool_find_duplicates(
                    dup_type=arguments.get("dup_type", "all"),
                    min_certainty=float(arguments.get("min_certainty", 0.0)),
                    limit=int(arguments.get("limit", 20)),
                )
            elif name == "browse_files":
                result = _tool_browse_files(
                    path=arguments.get("path", "/"),
                    mime_prefix=arguments.get("mime_prefix"),
                    extension=arguments.get("extension"),
                    limit=int(arguments.get("limit", 50)),
                )
            elif name == "propose_organization":
                result = _tool_propose_organization(
                    path=arguments["path"],
                    confidence_threshold=float(
                        arguments.get("confidence_threshold", 0.85)
                    ),
                )
            else:
                result = {"error": f"Unknown tool: {name}"}
        except Exception as e:
            result = {"error": str(e), "tool": name}

        return [TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(_run_server())
