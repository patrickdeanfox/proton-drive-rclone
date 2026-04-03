"""Duplicate detection engine.

- Exact duplicates: SHA-256 hash matching
- Near duplicates: perceptual hashing (pHash) for images
- Similarity: cosine similarity on CLIP embeddings (optional)
"""

import hashlib
import logging
import uuid
from collections import defaultdict
from pathlib import Path

from .. import database as db

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exact duplicate detection
# ---------------------------------------------------------------------------

def find_exact_duplicates(job_id=None):
    """Group files with identical SHA-256 hashes."""
    if job_id is None:
        job_id = str(uuid.uuid4())
    db.create_job(job_id, "duplicates")
    db.update_job(job_id, message="Finding exact duplicates...")

    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT sha256, array_agg(id) as file_ids
            FROM files
            WHERE sha256 IS NOT NULL AND sha256 != ''
            GROUP BY sha256
            HAVING count(*) > 1
        """)
        groups = cur.fetchall()

    total_groups = 0
    for sha, file_ids in groups:
        members = [(fid, 1.0) for fid in file_ids]
        db.save_duplicate_group(f"exact:{sha}", "exact", members)
        total_groups += 1

    db.update_job(job_id, status="completed", progress=1.0,
                  message=f"Found {total_groups} exact duplicate groups",
                  result={"groups": total_groups})
    return {"job_id": job_id, "groups": total_groups}


# ---------------------------------------------------------------------------
# Perceptual hashing for near-duplicate images
# ---------------------------------------------------------------------------

def _compute_phash(filepath: str, hash_size=8):
    """Compute perceptual hash of an image."""
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(filepath).convert("L").resize(
            (hash_size + 1, hash_size), Image.LANCZOS
        )
        pixels = np.array(img, dtype=float)
        diff = pixels[:, 1:] > pixels[:, :-1]
        return "".join(str(int(b)) for row in diff for b in row)
    except Exception as e:
        log.debug("pHash error for %s: %s", filepath, e)
        return None


def _hamming_distance(h1: str, h2: str) -> int:
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))


def find_near_duplicates(threshold=5, job_id=None):
    """Find near-duplicate images using perceptual hashing.

    threshold: max Hamming distance to consider as near-duplicate.
    """
    if job_id is None:
        job_id = str(uuid.uuid4())
    db.create_job(job_id, "duplicates")
    db.update_job(job_id, message="Computing perceptual hashes...")

    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, file_path FROM files
            WHERE mime_type LIKE 'image/%%'
            ORDER BY id
        """)
        images = cur.fetchall()

    total = len(images)
    hashes = {}  # file_id -> phash string

    for i, (fid, fpath) in enumerate(images):
        h = _compute_phash(fpath)
        if h:
            hashes[fid] = h
        if i % 50 == 0:
            db.update_job(job_id, progress=0.5 * (i+1) / total if total else 0.5,
                          message=f"Hashing {i+1}/{total}")

    # Compare all pairs (for large sets this should use VP-tree / LSH)
    db.update_job(job_id, progress=0.5, message="Comparing hashes...")
    file_ids = list(hashes.keys())
    groups = []  # list of sets
    used = set()

    for i in range(len(file_ids)):
        if file_ids[i] in used:
            continue
        group = {file_ids[i]}
        for j in range(i + 1, len(file_ids)):
            if file_ids[j] in used:
                continue
            dist = _hamming_distance(hashes[file_ids[i]], hashes[file_ids[j]])
            if dist <= threshold:
                group.add(file_ids[j])
        if len(group) > 1:
            groups.append(group)
            used.update(group)

    # Save groups
    total_groups = 0
    for group in groups:
        ref = sorted(group)[0]
        ref_hash = hashes[ref]
        members = []
        for fid in group:
            dist = _hamming_distance(ref_hash, hashes[fid])
            similarity = 1.0 - (dist / len(ref_hash)) if ref_hash else 1.0
            members.append((fid, round(similarity, 4)))
        group_hash = f"near:{ref_hash[:16]}:{ref}"
        db.save_duplicate_group(group_hash, "near", members)
        total_groups += 1

    db.update_job(job_id, status="completed", progress=1.0,
                  message=f"Found {total_groups} near-duplicate groups",
                  result={"groups": total_groups})
    return {"job_id": job_id, "groups": total_groups}


def run_all_detection(job_id=None):
    """Run both exact and near-duplicate detection."""
    if job_id is None:
        job_id = str(uuid.uuid4())
    db.create_job(job_id, "duplicates")

    exact = find_exact_duplicates()
    near = find_near_duplicates()

    db.update_job(job_id, status="completed", progress=1.0,
                  message=f"Found {exact['groups']} exact + {near['groups']} near groups",
                  result={"exact_groups": exact["groups"], "near_groups": near["groups"]})
    return {"job_id": job_id, "exact": exact["groups"], "near": near["groups"]}
