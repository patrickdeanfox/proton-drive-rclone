"""AI-powered content categorization engine — Python-first pipeline.

Pipeline order (per file):
  1. metadata_engine.extract_metadata()   — Python only, always runs
     • If confidence >= PYTHON_CONFIDENCE_THRESHOLD (0.90) AND needs_ai=False
       → skip AI entirely; use Python result
  2. rule_engine (existing, called externally for organization proposals)
  3. LocalCLIPProvider.categorize_image() — only for images where Python
     cannot determine the sub-type (photo vs screenshot vs meme, etc.)
  4. TextEmbeddingProvider              — generates sentence-transformer
     embeddings from OCR text for semantic search (stored in DB)
  5. CLIPEmbeddingProvider              — stores CLIP image embeddings in DB

All embeddings are persisted in file_embeddings (pgvector) so AI runs
only ONCE per file — subsequent searches are pure SQL vector queries.

Provider abstraction is kept for easy cloud-API swap-in later.
"""

import json
import logging
import os
import uuid
from pathlib import Path

from .. import database as db
from ..progress import ProgressTracker, register_operation, unregister_operation

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PYTHON_CONFIDENCE_THRESHOLD = 0.90   # skip AI when Python is this confident
TEXT_EMBEDDING_DIM = 384             # all-MiniLM-L6-v2
CLIP_EMBEDDING_DIM = 512             # CLIP ViT-B/32


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class BaseAIProvider:
    """Interface for AI providers."""
    name = "base"

    def categorize_image(self, filepath: str) -> dict:
        raise NotImplementedError

    def generate_image_embedding(self, filepath: str) -> list:
        raise NotImplementedError

    def generate_text_embedding(self, text: str) -> list:
        raise NotImplementedError

    def describe_image(self, filepath: str) -> str:
        raise NotImplementedError


class LocalCLIPProvider(BaseAIProvider):
    """Uses CLIP model locally for zero-shot image classification.

    Only invoked AFTER metadata_engine has had a chance to classify the file.
    Called when metadata_engine.needs_ai=True (mainly for images).
    """
    name = "local_clip"

    def __init__(self):
        self._model = None
        self._processor = None
        self._available = None

    def _ensure_loaded(self):
        if self._model is not None:
            return True
        if self._available is False:
            return False
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
            log.info("Loading CLIP model (first time may download ~600 MB)…")
            model_name = "openai/clip-vit-base-patch32"
            self._model = CLIPModel.from_pretrained(model_name)
            self._processor = CLIPProcessor.from_pretrained(model_name)
            self._available = True
            log.info("CLIP model loaded.")
            return True
        except ImportError:
            log.warning("transformers/torch not installed — CLIP unavailable")
            self._available = False
            return False
        except Exception as e:
            log.error("Failed to load CLIP: %s", e)
            self._available = False
            return False

    def categorize_image(self, filepath: str, categories=None) -> dict:
        if not self._ensure_loaded():
            return {"error": "CLIP not available"}
        if categories is None:
            categories = [
                "photograph", "screenshot", "document", "meme",
                "art", "diagram", "receipt", "nature", "people",
                "food", "animal", "building", "vehicle", "text",
            ]
        try:
            from PIL import Image
            import torch
            image = Image.open(filepath).convert("RGB")
            inputs = self._processor(
                text=categories, images=image,
                return_tensors="pt", padding=True,
            )
            with torch.no_grad():
                outputs = self._model(**inputs)
            logits = outputs.logits_per_image[0]
            probs = logits.softmax(dim=-1).tolist()
            results = sorted(
                zip(categories, probs), key=lambda x: x[1], reverse=True
            )
            return {
                "top_category": results[0][0],
                "confidence": round(results[0][1], 4),
                "all_scores": {c: round(s, 4) for c, s in results},
                "source": "clip",
            }
        except Exception as e:
            log.error("CLIP categorize error for %s: %s", filepath, e)
            return {"error": str(e)}

    def generate_image_embedding(self, filepath: str) -> list:
        if not self._ensure_loaded():
            return []
        try:
            from PIL import Image
            import torch
            image = Image.open(filepath).convert("RGB")
            inputs = self._processor(images=image, return_tensors="pt")
            with torch.no_grad():
                emb = self._model.get_image_features(**inputs)
            return emb[0].tolist()
        except Exception as e:
            log.error("CLIP embedding error: %s", e)
            return []

    def generate_text_embedding(self, text: str) -> list:
        """CLIP also supports text embeddings for cross-modal search."""
        if not self._ensure_loaded():
            return []
        try:
            import torch
            inputs = self._processor(text=[text], return_tensors="pt", padding=True)
            with torch.no_grad():
                emb = self._model.get_text_features(**inputs)
            return emb[0].tolist()
        except Exception as e:
            log.error("CLIP text embedding error: %s", e)
            return []


class SentenceTransformerProvider(BaseAIProvider):
    """Local sentence-transformers for text embeddings (OCR, documents)."""
    name = "sentence_transformers"

    def __init__(self):
        self._model = None
        self._available = None

    def _ensure_loaded(self):
        if self._model is not None:
            return True
        if self._available is False:
            return False
        try:
            from sentence_transformers import SentenceTransformer
            log.info("Loading sentence-transformers model (all-MiniLM-L6-v2, ~80 MB)…")
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            self._available = True
            log.info("Sentence-transformers loaded.")
            return True
        except ImportError:
            log.debug("sentence-transformers not installed")
            self._available = False
            return False
        except Exception as e:
            log.warning("Failed to load sentence-transformers: %s", e)
            self._available = False
            return False

    def categorize_image(self, filepath: str) -> dict:
        return {"error": "SentenceTransformer does not categorize images"}

    def generate_image_embedding(self, filepath: str) -> list:
        return []

    def generate_text_embedding(self, text: str) -> list:
        if not self._ensure_loaded():
            return []
        try:
            vec = self._model.encode(text, normalize_embeddings=True)
            return vec.tolist()
        except Exception as e:
            log.error("Sentence embedding error: %s", e)
            return []


class CloudAPIProvider(BaseAIProvider):
    """Placeholder for cloud-based AI (OpenAI Vision, Google Cloud Vision, etc)."""
    name = "cloud_api"

    def __init__(self, api_key=None, endpoint=None):
        self.api_key = api_key or os.getenv("AI_API_KEY")
        self.endpoint = endpoint or os.getenv("AI_ENDPOINT")

    def categorize_image(self, filepath: str) -> dict:
        return {"error": "Cloud API not configured", "provider": self.name}

    def generate_image_embedding(self, filepath: str) -> list:
        return []

    def generate_text_embedding(self, text: str) -> list:
        return []

    def describe_image(self, filepath: str) -> str:
        return "Cloud API not configured"


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS = {
    "local_clip": LocalCLIPProvider,
    "cloud_api": CloudAPIProvider,
    "sentence_transformers": SentenceTransformerProvider,
}

_active_provider = None


def get_provider(name=None) -> BaseAIProvider:
    global _active_provider
    if name is None:
        name = os.getenv("AI_PROVIDER", "local_clip")
    if _active_provider is None or _active_provider.name != name:
        cls = PROVIDERS.get(name, LocalCLIPProvider)
        _active_provider = cls()
    return _active_provider


def get_available_providers():
    return list(PROVIDERS.keys())


# ---------------------------------------------------------------------------
# Python-first analysis pipeline
# ---------------------------------------------------------------------------

def analyze_file(file_id: int, provider_name: str = None) -> dict:
    """Analyze a single file using the Python-first pipeline.

    Steps:
      1. Always run metadata_engine (Python-only, fast)
      2. If file needs AI (images, scanned PDFs): run CLIP categorization
      3. Store image embedding (CLIP) in file_embeddings (pgvector)
      4. If OCR text exists, store text embedding (sentence-transformers)

    Returns a dict with keys: metadata, categorization (optional),
                               image_embedding (bool), text_embedding (bool)
    """
    from .metadata_engine import extract_metadata

    with db.get_conn() as conn:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM files WHERE id = %s", (file_id,))
        file_data = cur.fetchone()
    if not file_data:
        return {"error": "File not found"}

    filepath = file_data["file_path"]
    results = {}

    # ── Step 1: Python metadata extraction ────────────────────────────────
    meta_result = extract_metadata(filepath)
    results["metadata"] = meta_result.metadata
    results["python_category"] = meta_result.category
    results["python_confidence"] = meta_result.confidence

    # Persist enriched metadata to the files table
    try:
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE files SET metadata_json = %s
                WHERE id = %s
            """, (json.dumps(meta_result.metadata), file_id))
    except Exception as e:
        log.debug("metadata persist error for file %d: %s", file_id, e)

    # ── Step 2: AI categorization (images only, when Python is uncertain) ──
    if meta_result.needs_ai:
        provider = get_provider(provider_name)
        mime = file_data.get("mime_type", "")
        if mime.startswith("image/") or meta_result.category == "image":
            cat = provider.categorize_image(filepath)
            if "error" not in cat:
                db.save_ai_analysis(file_id, provider.name, "categorization",
                                    cat, cat.get("confidence"))
                results["categorization"] = cat
    else:
        log.debug("Skipping AI for %s — Python confidence %.0f%%",
                  filepath, meta_result.confidence * 100)
        results["skipped_ai"] = True

    # ── Step 3: image embedding (CLIP) ────────────────────────────────────
    mime = file_data.get("mime_type", "")
    if mime.startswith("image/"):
        provider = get_provider(provider_name)
        if hasattr(provider, "generate_image_embedding"):
            emb = provider.generate_image_embedding(filepath)
            if emb:
                try:
                    db.save_file_embedding(file_id, emb, embed_type="image")
                    results["image_embedding"] = True
                except Exception as e:
                    log.debug("image embedding save error: %s", e)

    # ── Step 4: text embedding (sentence-transformers from OCR text) ───────
    try:
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT text_content FROM file_ocr_text WHERE file_id = %s",
                (file_id,)
            )
            row = cur.fetchone()
            ocr_text = row[0] if row else None
    except Exception:
        ocr_text = None

    if ocr_text and len(ocr_text) > 50:
        st_provider = get_provider("sentence_transformers")
        text_emb = st_provider.generate_text_embedding(ocr_text[:2000])
        if text_emb:
            try:
                db.save_file_embedding(file_id, text_emb, embed_type="text")
                results["text_embedding"] = True
            except Exception as e:
                log.debug("text embedding save error: %s", e)

    return results


def batch_analyze(job_id: str = None, provider_name: str = None,
                  limit: int = 500) -> dict:
    """Analyze files through the Python-first pipeline in batch.

    Only processes files that haven't been analyzed yet (no ai_analysis row
    for categorization, OR no metadata_json, OR missing embeddings).
    """
    if job_id is None:
        job_id = str(uuid.uuid4())
    db.create_job(job_id, "ai_analysis")

    # Files needing analysis: images without categorization
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.id FROM files f
            LEFT JOIN ai_analysis a
              ON a.file_id = f.id AND a.analysis_type = 'categorization'
            WHERE a.id IS NULL
            LIMIT %s
        """, (limit,))
        file_ids = [r[0] for r in cur.fetchall()]

    total = len(file_ids)
    tracker = ProgressTracker(
        operation_type="ai_analysis",
        total=total,
        unit="files",
        job_id=job_id,
    )
    register_operation(tracker)

    done = 0
    errors = 0
    skipped_ai = 0

    for i, fid in enumerate(file_ids):
        try:
            result = analyze_file(fid, provider_name)
            if result.get("skipped_ai"):
                skipped_ai += 1
            done += 1
        except Exception as e:
            log.warning("AI analysis error for file %d: %s", fid, e)
            errors += 1

        tracker.update(
            current=i + 1,
            message=f"Analyzed {i+1}/{total} (skipped AI: {skipped_ai})",
        )

    summary = {"analyzed": done, "errors": errors, "skipped_ai": skipped_ai}
    db.update_job(
        job_id, status="completed", progress=1.0,
        message=f"Analyzed {done} files ({skipped_ai} used Python-only, {errors} errors)",
        result=summary,
    )
    tracker.complete(result=summary)
    unregister_operation(tracker.operation_id)
    return {"job_id": job_id, **summary}
