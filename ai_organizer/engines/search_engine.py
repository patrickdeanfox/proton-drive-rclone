"""Search engine — full-text fuzzy search + pgvector semantic search.

Three search modes:
  text     — rapidfuzz fuzzy matching against file names, OCR text, and metadata
  semantic — pgvector cosine similarity on stored CLIP / sentence-transformer embeddings
  hybrid   — combines text score and semantic score with configurable weights

All search is done via Python and SQL — no AI calls at query time.
AI runs once at index time to generate embeddings (see ai_engine.py).

Public API:
  search_files(query, mode="hybrid", limit=20, db_module=None) → list[SearchResult]
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    file_id: int
    file_path: str
    file_name: str
    score: float                        # 0.0–1.0 combined relevance
    match_type: str                     # "text", "semantic", "hybrid"
    snippet: Optional[str] = None       # OCR excerpt or metadata snippet
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Text search (rapidfuzz)
# ---------------------------------------------------------------------------

def _rapidfuzz_score(query: str, target: str) -> float:
    """Return a 0.0–1.0 relevance score using rapidfuzz token_set_ratio."""
    if not target:
        return 0.0
    try:
        from rapidfuzz import fuzz
        return fuzz.token_set_ratio(query.lower(), target.lower()) / 100.0
    except ImportError:
        # Fallback: simple substring check
        return 1.0 if query.lower() in target.lower() else 0.0


def _text_search(query: str, limit: int, db_module) -> list:
    """Search file names, OCR text, and metadata using fuzzy matching.

    Fetches candidates from DB (PostgreSQL full-text pre-filter), then
    re-ranks with rapidfuzz for better fuzzy matching quality.
    """
    results = []
    try:
        with db_module.get_conn() as conn:
            import psycopg2.extras
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # Broad candidate set: use PG full-text on file_name + ocr text
            cur.execute("""
                SELECT
                    f.id, f.file_path, f.file_name, f.mime_type, f.metadata_json,
                    o.text_content AS ocr_text
                FROM files f
                LEFT JOIN file_ocr_text o ON o.file_id = f.id
                WHERE
                    f.file_name ILIKE %s
                    OR o.text_content ILIKE %s
                ORDER BY f.indexed_at DESC
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", limit * 5))
            candidates = cur.fetchall()
    except Exception as e:
        log.warning("Text search DB error: %s", e)
        return results

    scored = []
    for row in candidates:
        name_score = _rapidfuzz_score(query, row["file_name"] or "")
        ocr_score = _rapidfuzz_score(query, (row["ocr_text"] or "")[:2000])
        combined = max(name_score, ocr_score * 0.9)
        if combined < 0.2:
            continue

        snippet = None
        if row["ocr_text"] and ocr_score > name_score:
            # Extract a short snippet around the first match
            text = row["ocr_text"]
            idx = text.lower().find(query.lower())
            if idx >= 0:
                start = max(0, idx - 60)
                end = min(len(text), idx + 120)
                snippet = "…" + text[start:end].strip() + "…"

        scored.append(SearchResult(
            file_id=row["id"],
            file_path=row["file_path"],
            file_name=row["file_name"],
            score=round(combined, 4),
            match_type="text",
            snippet=snippet,
            metadata=dict(row["metadata_json"] or {}),
        ))

    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:limit]


# ---------------------------------------------------------------------------
# Semantic search (pgvector)
# ---------------------------------------------------------------------------

def _generate_query_embedding(query: str, embed_type: str = "text") -> Optional[list]:
    """Generate a query embedding for semantic search.

    For text queries: uses sentence-transformers (local, ~80MB model).
    For image queries: not applicable via search — use file embedding directly.
    """
    if embed_type == "text":
        try:
            from sentence_transformers import SentenceTransformer
            model = _get_text_model()
            if model is None:
                return None
            vec = model.encode(query, normalize_embeddings=True)
            return vec.tolist()
        except ImportError:
            log.debug("sentence-transformers not installed — semantic search unavailable")
            return None
        except Exception as e:
            log.warning("Query embedding error: %s", e)
            return None
    return None


_text_model_cache = None


def _get_text_model():
    """Lazy-load the sentence-transformers model (cached)."""
    global _text_model_cache
    if _text_model_cache is not None:
        return _text_model_cache
    try:
        from sentence_transformers import SentenceTransformer
        log.info("Loading sentence-transformers model (first time ~80MB)…")
        _text_model_cache = SentenceTransformer("all-MiniLM-L6-v2")
        log.info("Sentence-transformers model loaded.")
        return _text_model_cache
    except ImportError:
        log.debug("sentence-transformers not installed")
        return None
    except Exception as e:
        log.warning("Failed to load sentence-transformers: %s", e)
        return None


def _semantic_search(query: str, limit: int, db_module,
                     embed_type: str = "text") -> list:
    """Vector similarity search using pgvector cosine distance."""
    results = []
    query_vec = _generate_query_embedding(query, embed_type)
    if query_vec is None:
        log.debug("Semantic search skipped — no query embedding available")
        return results

    try:
        with db_module.get_conn() as conn:
            import psycopg2.extras
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # pgvector cosine distance operator: <=>
            cur.execute("""
                SELECT
                    f.id, f.file_path, f.file_name, f.mime_type, f.metadata_json,
                    1 - (e.embedding <=> %s::vector) AS similarity
                FROM file_embeddings e
                JOIN files f ON f.id = e.file_id
                WHERE e.embed_type = %s
                ORDER BY similarity DESC
                LIMIT %s
            """, (str(query_vec), embed_type, limit))
            rows = cur.fetchall()
    except Exception as e:
        log.warning("Semantic search DB error: %s", e)
        return results

    for row in rows:
        sim = float(row["similarity"] or 0)
        if sim < 0.3:
            continue
        results.append(SearchResult(
            file_id=row["id"],
            file_path=row["file_path"],
            file_name=row["file_name"],
            score=round(sim, 4),
            match_type="semantic",
            metadata=dict(row["metadata_json"] or {}),
        ))

    return results


# ---------------------------------------------------------------------------
# Hybrid merge
# ---------------------------------------------------------------------------

def _merge_results(text_results: list, semantic_results: list,
                   text_weight: float = 0.4,
                   semantic_weight: float = 0.6,
                   limit: int = 20) -> list:
    """Merge and re-rank text + semantic results."""
    by_id: dict[int, SearchResult] = {}

    for r in text_results:
        by_id[r.file_id] = SearchResult(
            file_id=r.file_id,
            file_path=r.file_path,
            file_name=r.file_name,
            score=r.score * text_weight,
            match_type="hybrid",
            snippet=r.snippet,
            metadata=r.metadata,
        )

    for r in semantic_results:
        if r.file_id in by_id:
            by_id[r.file_id].score += r.score * semantic_weight
        else:
            by_id[r.file_id] = SearchResult(
                file_id=r.file_id,
                file_path=r.file_path,
                file_name=r.file_name,
                score=r.score * semantic_weight,
                match_type="hybrid",
                metadata=r.metadata,
            )

    merged = sorted(by_id.values(), key=lambda r: r.score, reverse=True)
    return merged[:limit]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def search_files(query: str, mode: str = "hybrid", limit: int = 20,
                 db_module=None) -> list:
    """Search indexed files.

    mode:
      "text"     — fuzzy filename + OCR text search (rapidfuzz)
      "semantic" — vector similarity search (pgvector + sentence-transformers)
      "hybrid"   — combines both (recommended)

    Returns a list of SearchResult sorted by descending relevance.
    """
    if not query or not query.strip():
        return []

    query = query.strip()

    if mode == "text":
        return _text_search(query, limit, db_module)
    elif mode == "semantic":
        return _semantic_search(query, limit, db_module)
    else:  # hybrid
        text_r = _text_search(query, limit, db_module)
        sem_r = _semantic_search(query, limit, db_module)
        return _merge_results(text_r, sem_r, limit=limit)


def search_files_as_dicts(query: str, mode: str = "hybrid",
                          limit: int = 20, db_module=None) -> list:
    """Same as search_files but returns plain dicts (JSON-serialisable)."""
    results = search_files(query, mode=mode, limit=limit, db_module=db_module)
    return [
        {
            "file_id": r.file_id,
            "file_path": r.file_path,
            "file_name": r.file_name,
            "score": r.score,
            "match_type": r.match_type,
            "snippet": r.snippet,
            "metadata": r.metadata,
        }
        for r in results
    ]
