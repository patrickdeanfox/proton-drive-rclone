"""Face recognition pipeline — Python-first, no cloud.

Pipeline:
  1. mediapipe face detection  — bounding boxes, no GPU required
  2. CLIP embedding on cropped face  — reuses existing ai_engine provider
  3. DBSCAN clustering (scikit-learn) — groups faces → person clusters
  4. Optional Ollama cluster naming  — suggests name from context

Feature flag: FEATURE_FLAGS["face_recognition"] must be True.

Usage:
    result = scan_faces_in_directory("/home/user/photos", db_module=db)
"""

import logging
import uuid
from pathlib import Path
from typing import Optional

from ..progress import ProgressTracker, register_operation, unregister_operation

log = logging.getLogger(__name__)

# Default clustering settings
DBSCAN_EPS = 0.4          # cosine distance threshold (0 = identical, 2 = opposite)
DBSCAN_MIN_SAMPLES = 2    # minimum faces to form a cluster


# ---------------------------------------------------------------------------
# Face detection (mediapipe)
# ---------------------------------------------------------------------------

def _detect_faces(filepath: str) -> list[dict]:
    """Detect faces in an image. Returns list of {x, y, w, h, confidence}."""
    try:
        import mediapipe as mp
        from PIL import Image
        import numpy as np

        mp_face = mp.solutions.face_detection
        with mp_face.FaceDetection(model_selection=1, min_detection_confidence=0.5) as detector:
            with Image.open(filepath).convert("RGB") as img:
                img_np = np.array(img)
                results = detector.process(img_np)

        if not results.detections:
            return []

        h, w = img_np.shape[:2]
        faces = []
        for det in results.detections:
            bb = det.location_data.relative_bounding_box
            faces.append({
                "x": max(0, int(bb.xmin * w)),
                "y": max(0, int(bb.ymin * h)),
                "w": int(bb.width * w),
                "h": int(bb.height * h),
                "confidence": round(det.score[0], 3) if det.score else 0,
            })
        return faces
    except ImportError:
        log.warning("mediapipe not installed — face detection unavailable")
        return []
    except Exception as e:
        log.debug("Face detection error for %s: %s", filepath, e)
        return []


# ---------------------------------------------------------------------------
# Face embedding (CLIP on cropped face)
# ---------------------------------------------------------------------------

def _embed_face(filepath: str, bbox: dict) -> list[float]:
    """Generate a CLIP embedding for a cropped face region."""
    try:
        from PIL import Image
        from ..engines.ai_engine import get_provider

        with Image.open(filepath).convert("RGB") as img:
            x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            # Expand crop slightly for context
            pad = max(w, h) // 4
            x1 = max(0, x - pad)
            y1 = max(0, y - pad)
            x2 = min(img.width, x + w + pad)
            y2 = min(img.height, y + h + pad)
            crop = img.crop((x1, y1, x2, y2))

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            crop.save(tmp.name, "JPEG")
            tmp_path = tmp.name

        try:
            provider = get_provider("local_clip")
            return provider.generate_image_embedding(tmp_path)
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        log.debug("Face embedding error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Clustering (DBSCAN on embeddings)
# ---------------------------------------------------------------------------

def _cluster_embeddings(embeddings: list[list[float]],
                         eps: float = DBSCAN_EPS,
                         min_samples: int = DBSCAN_MIN_SAMPLES) -> list[int]:
    """Cluster face embeddings with DBSCAN. Returns cluster label per embedding.
    Label -1 = noise (unclustered).
    """
    if len(embeddings) < 2:
        return [0] * len(embeddings)
    try:
        import numpy as np
        from sklearn.cluster import DBSCAN
        from sklearn.preprocessing import normalize

        X = normalize(np.array(embeddings, dtype=float))
        labels = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine").fit_predict(X)
        return labels.tolist()
    except ImportError:
        log.warning("scikit-learn not installed — each face assigned to its own cluster")
        return list(range(len(embeddings)))
    except Exception as e:
        log.warning("Clustering error: %s", e)
        return list(range(len(embeddings)))


# ---------------------------------------------------------------------------
# Cluster naming (optional Ollama)
# ---------------------------------------------------------------------------

def _suggest_cluster_name(filenames: list[str], cluster_idx: int) -> str:
    """Ask Ollama to suggest a name for a face cluster."""
    try:
        from ..llm.task_router import get_router
        router = get_router()
        files_str = ", ".join(filenames[:10])
        result = router.run_task(
            "face_cluster_naming",
            f"Files: {files_str}. Cluster index: {cluster_idx}.",
        )
        if result:
            return result.strip()[:100]
    except Exception:
        pass
    return f"Person {cluster_idx + 1}"


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def scan_faces_in_directory(directory: str, db_module=None,
                             job_id: str = None) -> dict:
    """Scan images in a directory for faces, embed and cluster them.

    Returns:
        {
          "scanned": int,
          "faces_found": int,
          "clusters": int,
          "errors": int,
        }
    """
    if job_id is None:
        job_id = str(uuid.uuid4())

    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        return {"error": f"Directory not found: {directory}"}

    image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".heic"}
    image_files = [p for p in dir_path.rglob("*") if p.suffix.lower() in image_exts]

    tracker = ProgressTracker(
        operation_type="face_scan",
        total=len(image_files),
        unit="images",
        job_id=job_id,
    )
    register_operation(tracker)

    scanned = 0
    errors = 0
    all_embeddings: list[list[float]] = []
    face_records: list[dict] = []   # {file_id, face_index, bbox, emb_idx}

    for i, img_path in enumerate(image_files):
        try:
            faces = _detect_faces(str(img_path))
            file_id = None

            if db_module and faces:
                # Look up file_id from DB
                try:
                    with db_module.get_conn() as conn:
                        cur = conn.cursor()
                        cur.execute("SELECT id FROM files WHERE file_path = %s", (str(img_path),))
                        row = cur.fetchone()
                        if row:
                            file_id = row[0]
                except Exception:
                    pass

            for fi, bbox in enumerate(faces):
                emb = _embed_face(str(img_path), bbox)
                if emb:
                    emb_idx = len(all_embeddings)
                    all_embeddings.append(emb)
                    face_records.append({
                        "file_id": file_id,
                        "face_index": fi,
                        "bbox": bbox,
                        "emb_idx": emb_idx,
                        "filepath": str(img_path),
                    })

            scanned += 1
        except Exception as e:
            log.warning("Face scan error for %s: %s", img_path, e)
            errors += 1

        tracker.update(current=i + 1, message=f"Scanned {i+1}/{len(image_files)}")

    # Cluster all embeddings
    labels: list[int] = []
    if all_embeddings:
        tracker.update(message="Clustering faces…")
        labels = _cluster_embeddings(all_embeddings)

    # Persist to DB and build clusters
    n_clusters = 0
    if db_module and face_records and labels:
        cluster_id_map: dict[int, int] = {}  # sklearn_label → db_cluster_id

        for rec, label in zip(face_records, labels):
            if label == -1:
                # Unclustered noise — create solo cluster
                label_key = -(rec["emb_idx"] + 1000)
            else:
                label_key = label

            if label_key not in cluster_id_map:
                name = _suggest_cluster_name(
                    [rec["filepath"]], label if label >= 0 else len(cluster_id_map)
                )
                cluster_db_id = db_module.save_face_cluster(
                    suggested_name=name,
                    representative_file_id=rec.get("file_id"),
                )
                cluster_id_map[label_key] = cluster_db_id
                n_clusters += 1

            cluster_db_id = cluster_id_map[label_key]
            emb = all_embeddings[rec["emb_idx"]]

            if rec.get("file_id"):
                try:
                    db_module.save_face_detection(
                        file_id=rec["file_id"],
                        face_index=rec["face_index"],
                        bounding_box=rec["bbox"],
                        embedding=emb,
                        cluster_id=cluster_db_id,
                    )
                except Exception as e:
                    log.debug("save_face_detection error: %s", e)

        # Update face_count on each cluster
        for label_key, cluster_db_id in cluster_id_map.items():
            count = sum(1 for lbl in labels
                        if (lbl == label_key or (label_key < 0 and lbl == -1)))
            try:
                db_module.update_face_cluster(cluster_db_id, face_count=count)
            except Exception:
                pass

    summary = {
        "scanned": scanned,
        "faces_found": len(face_records),
        "clusters": n_clusters,
        "errors": errors,
    }
    tracker.complete(result=summary)
    unregister_operation(tracker.operation_id)
    return summary
