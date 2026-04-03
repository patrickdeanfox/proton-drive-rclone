"""AI-powered content categorization engine.

Uses local models (CLIP / ResNet) for image analysis and categorization.
Designed with a provider abstraction so cloud APIs can be swapped in later.
"""

import json
import logging
import os
import uuid
from pathlib import Path

from .. import database as db

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class BaseAIProvider:
    """Interface for AI providers."""
    name = "base"

    def categorize_image(self, filepath: str) -> dict:
        raise NotImplementedError

    def generate_embedding(self, filepath: str) -> list:
        raise NotImplementedError

    def describe_image(self, filepath: str) -> str:
        raise NotImplementedError


class LocalCLIPProvider(BaseAIProvider):
    """Uses CLIP model locally for zero-shot image classification."""
    name = "local_clip"

    def __init__(self):
        self._model = None
        self._processor = None
        self._tokenizer = None
        self._available = None

    def _ensure_loaded(self):
        if self._model is not None:
            return True
        if self._available is False:
            return False
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
            log.info("Loading CLIP model (first time may download ~600MB)...")
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
                return_tensors="pt", padding=True
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
            }
        except Exception as e:
            log.error("CLIP categorize error for %s: %s", filepath, e)
            return {"error": str(e)}

    def generate_embedding(self, filepath: str) -> list:
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


class CloudAPIProvider(BaseAIProvider):
    """Placeholder for cloud-based AI (OpenAI Vision, Google Cloud Vision, etc)."""
    name = "cloud_api"

    def __init__(self, api_key=None, endpoint=None):
        self.api_key = api_key or os.getenv("AI_API_KEY")
        self.endpoint = endpoint or os.getenv("AI_ENDPOINT")

    def categorize_image(self, filepath: str) -> dict:
        # Placeholder — would POST image to cloud API
        return {"error": "Cloud API not configured", "provider": self.name}

    def generate_embedding(self, filepath: str) -> list:
        return []

    def describe_image(self, filepath: str) -> str:
        return "Cloud API not configured"


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------
PROVIDERS = {
    "local_clip": LocalCLIPProvider,
    "cloud_api": CloudAPIProvider,
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
# High-level functions
# ---------------------------------------------------------------------------

def analyze_file(file_id: int, provider_name=None):
    """Run AI analysis on a single file and store results."""
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM files WHERE id = %s", (file_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    if not row:
        return {"error": "File not found"}

    file_data = dict(zip(cols, row))
    mime = file_data.get("mime_type", "")
    provider = get_provider(provider_name)
    results = {}

    if mime.startswith("image/"):
        cat = provider.categorize_image(file_data["file_path"])
        if "error" not in cat:
            db.save_ai_analysis(file_id, provider.name, "categorization", cat,
                               cat.get("confidence"))
            results["categorization"] = cat
    return results


def batch_analyze(job_id=None, provider_name=None, limit=500):
    """Analyze all un-analyzed image files."""
    if job_id is None:
        job_id = str(uuid.uuid4())
    db.create_job(job_id, "ai_analysis")

    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.id FROM files f
            LEFT JOIN ai_analysis a ON a.file_id = f.id
            WHERE f.mime_type LIKE 'image/%%' AND a.id IS NULL
            LIMIT %s
        """, (limit,))
        file_ids = [r[0] for r in cur.fetchall()]

    total = len(file_ids)
    done = 0
    errors = 0

    for i, fid in enumerate(file_ids):
        try:
            analyze_file(fid, provider_name)
            done += 1
        except Exception as e:
            log.warning("AI analysis error for file %d: %s", fid, e)
            errors += 1
        if i % 10 == 0:
            db.update_job(job_id, progress=(i+1)/total if total else 1,
                          message=f"Analyzed {i+1}/{total}")

    db.update_job(job_id, status="completed", progress=1.0,
                  message=f"Analyzed {done} files ({errors} errors)",
                  result={"analyzed": done, "errors": errors})
    return {"job_id": job_id, "analyzed": done, "errors": errors}
