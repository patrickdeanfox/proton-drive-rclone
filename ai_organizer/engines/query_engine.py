"""Natural language query engine for file system Q&A.

Accepts plain-English questions, classifies the intent (count, search,
describe, find_related, list_types, storage_usage), and dispatches to the
appropriate backend (SQL, search_engine, stats, etc.).

If Ollama is running the intent is classified by a local LLM; if not,
a lightweight keyword heuristic is used as fallback.

Usage:
    result = answer_question("how many PDFs do I have?", db_module=db)
    # {"answer": "You have 42 PDF files.", "query_type": "count", "files": [], ...}
"""

import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent labels
# ---------------------------------------------------------------------------
INTENTS = (
    "count",           # "how many X files?"
    "search",          # "find files about Y"
    "describe",        # "what files do I have?" / overview
    "find_related",    # "files related to X"
    "list_types",      # "what types of files?"
    "storage_usage",   # "how much storage?"
)

# Simple keyword fallback when Ollama is unavailable
_KEYWORD_RULES = [
    (re.compile(r'\b(how many|count|number of)\b', re.I), "count"),
    (re.compile(r'\b(storage|space|size|bytes|gigabytes?|megabytes?)\b', re.I), "storage_usage"),
    (re.compile(r'\b(types?|extensions?|kinds?|categories?)\b', re.I), "list_types"),
    (re.compile(r'\b(related|similar|about|concerning)\b', re.I), "find_related"),
    (re.compile(r'\b(find|search|look for|locate)\b', re.I), "search"),
]


def _classify_intent_heuristic(question: str) -> str:
    for pattern, intent in _KEYWORD_RULES:
        if pattern.search(question):
            return intent
    return "describe"


def _classify_intent(question: str) -> str:
    """Classify question intent via Ollama LLM, falling back to heuristic."""
    try:
        from ..llm.task_router import get_router
        router = get_router()
        result = router.run_task("intent_classification", question)
        if result:
            # Normalise: strip punctuation and lowercase
            label = result.strip().lower().split()[0].rstrip(".,;:")
            if label in INTENTS:
                return label
    except Exception as e:
        log.debug("LLM intent classification error: %s", e)
    return _classify_intent_heuristic(question)


def _extract_filter_keyword(question: str) -> Optional[str]:
    """Extract a file-type keyword from the question, e.g. 'PDF' → '.pdf'."""
    ext_map = {
        "pdf": ".pdf", "pdfs": ".pdf",
        "image": None,   # mime prefix filter
        "images": None,
        "photo": None, "photos": None,
        "video": None, "videos": None,
        "audio": None, "music": None,
        "word": ".docx", "excel": ".xlsx", "powerpoint": ".pptx",
        "jpg": ".jpg", "jpeg": ".jpg", "png": ".png",
        "mp3": ".mp3", "mp4": ".mp4",
        "zip": ".zip",
        "text": ".txt",
        "code": None,
    }
    words = re.findall(r'\b\w+\b', question.lower())
    for w in words:
        if w in ext_map:
            return ext_map[w]  # may be None for mime-based types
    return None


def _query_count(question: str, db_module, scope=None) -> dict:
    """Answer a 'how many' question with SQL."""
    ext = _extract_filter_keyword(question)
    scope_filter = ""
    scope_params: list = []

    # Scope path filtering
    if scope and scope.get("allowed_paths"):
        conds = " OR ".join("file_path LIKE %s" for _ in scope["allowed_paths"])
        scope_filter = f" AND ({conds})"
        scope_params = [p + "%" for p in scope["allowed_paths"]]

    try:
        with db_module.get_conn() as conn:
            cur = conn.cursor()
            if ext:
                cur.execute(
                    f"SELECT count(*) FROM files WHERE extension = %s{scope_filter}",
                    [ext] + scope_params
                )
                label = f"{ext} files"
            else:
                # Try mime prefix
                mime = _mime_from_question(question)
                if mime:
                    cur.execute(
                        f"SELECT count(*) FROM files WHERE mime_type LIKE %s{scope_filter}",
                        [mime + "%"] + scope_params
                    )
                    label = f"{mime} files"
                else:
                    cur.execute(
                        f"SELECT count(*) FROM files WHERE 1=1{scope_filter}",
                        scope_params
                    )
                    label = "files"
            count = cur.fetchone()[0]
        answer = f"You have {count:,} {label}."
        return {"answer": answer, "query_type": "count", "count": count, "files": []}
    except Exception as e:
        log.warning("Count query error: %s", e)
        return {"answer": "Could not count files.", "query_type": "count", "count": 0, "files": []}


def _mime_from_question(question: str) -> Optional[str]:
    q = question.lower()
    if any(w in q for w in ("image", "photo", "picture", "jpg", "jpeg", "png")):
        return "image/"
    if any(w in q for w in ("video", "movie", "film")):
        return "video/"
    if any(w in q for w in ("audio", "music", "sound", "mp3")):
        return "audio/"
    return None


def _query_storage(db_module, scope=None) -> dict:
    """Return total storage usage."""
    scope_filter = ""
    scope_params: list = []
    if scope and scope.get("allowed_paths"):
        conds = " OR ".join("file_path LIKE %s" for _ in scope["allowed_paths"])
        scope_filter = f" WHERE ({conds})"
        scope_params = [p + "%" for p in scope["allowed_paths"]]

    try:
        with db_module.get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT coalesce(sum(size_bytes),0), count(*) FROM files{scope_filter}",
                scope_params
            )
            total_bytes, total_files = cur.fetchone()
        gb = total_bytes / (1024 ** 3)
        mb = total_bytes / (1024 ** 2)
        size_str = f"{gb:.2f} GB" if gb >= 1 else f"{mb:.1f} MB"
        answer = f"You are using {size_str} across {total_files:,} files."
        return {"answer": answer, "query_type": "storage_usage",
                "total_bytes": int(total_bytes), "total_files": total_files, "files": []}
    except Exception as e:
        log.warning("Storage query error: %s", e)
        return {"answer": "Could not calculate storage.", "query_type": "storage_usage", "files": []}


def _query_list_types(db_module, scope=None) -> dict:
    """Return a breakdown of file types."""
    scope_filter = ""
    scope_params: list = []
    if scope and scope.get("allowed_paths"):
        conds = " OR ".join("file_path LIKE %s" for _ in scope["allowed_paths"])
        scope_filter = f" AND ({conds})"
        scope_params = [p + "%" for p in scope["allowed_paths"]]

    try:
        with db_module.get_conn() as conn:
            import psycopg2.extras
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(f"""
                SELECT extension, count(*) as cnt,
                       coalesce(sum(size_bytes),0) as total_bytes
                FROM files
                WHERE extension IS NOT NULL{scope_filter}
                GROUP BY extension ORDER BY cnt DESC LIMIT 20
            """, scope_params)
            rows = cur.fetchall()
        lines = [f"  {r['extension']}: {r['cnt']:,} files" for r in rows]
        answer = "File types in your collection:\n" + "\n".join(lines)
        return {"answer": answer, "query_type": "list_types",
                "breakdown": [dict(r) for r in rows], "files": []}
    except Exception as e:
        log.warning("List types error: %s", e)
        return {"answer": "Could not list file types.", "query_type": "list_types", "files": []}


def _query_describe(db_module, scope=None) -> dict:
    """Give an overview of the file collection."""
    try:
        stats = db_module.get_stats()
    except Exception as e:
        return {"answer": f"Error fetching stats: {e}", "query_type": "describe", "files": []}

    total = stats.get("total_files", 0)
    size_gb = stats.get("total_size_bytes", 0) / (1024 ** 3)
    top_exts = ", ".join(
        f"{r['extension']} ({r['cnt']})"
        for r in (stats.get("top_extensions") or [])[:5]
    )

    # Try to enrich with LLM narrative
    summary_prompt = (
        f"File collection overview: {total} files, {size_gb:.1f} GB. "
        f"Top types: {top_exts}. "
        "Write a friendly 2-sentence summary."
    )
    try:
        from ..llm.task_router import get_router
        narrative = get_router().run_task("agent_narrative", summary_prompt)
    except Exception:
        narrative = None

    if not narrative:
        narrative = (
            f"Your collection contains {total:,} files totalling {size_gb:.2f} GB. "
            f"Most common types: {top_exts or 'various'}."
        )

    return {
        "answer": narrative,
        "query_type": "describe",
        "stats": {k: v for k, v in stats.items() if k != "top_extensions"},
        "files": [],
    }


def _query_search(question: str, db_module, scope=None, limit: int = 10) -> dict:
    """Run hybrid search and return matching files."""
    try:
        from .search_engine import search_files_as_dicts
        results = search_files_as_dicts(question, mode="hybrid", limit=limit,
                                        db_module=db_module)
        # Apply scope filter
        if scope:
            results = [r for r in results
                       if db_module.path_allowed(r.get("file_path", ""), scope)]
        count = len(results)
        answer = (f"Found {count} file{'s' if count != 1 else ''} matching '{question}'."
                  if results else f"No files found matching '{question}'.")
        return {"answer": answer, "query_type": "search",
                "count": count, "files": results}
    except Exception as e:
        log.warning("Search query error: %s", e)
        return {"answer": "Search failed.", "query_type": "search", "files": []}


def answer_question(question: str, db_module,
                    scope: Optional[dict] = None,
                    limit: int = 10) -> dict:
    """Main entry point: classify intent and dispatch to the right backend.

    Returns:
        {
          "answer":     str   — human-readable answer
          "query_type": str   — intent label
          "files":      list  — supporting file records (may be empty)
          ...           additional type-specific fields
        }
    """
    intent = _classify_intent(question)
    log.debug("Q&A intent: %s — '%s'", intent, question)

    if intent == "count":
        return _query_count(question, db_module, scope)
    if intent == "storage_usage":
        return _query_storage(db_module, scope)
    if intent == "list_types":
        return _query_list_types(db_module, scope)
    if intent == "describe":
        return _query_describe(db_module, scope)
    if intent in ("search", "find_related"):
        return _query_search(question, db_module, scope, limit)
    # Fallback to search for unknown intents
    return _query_search(question, db_module, scope, limit)
