"""Duplicate detection engine.

Detection methods:
  1. Exact duplicates    — SHA-256 hash matching (always available)
  2. Near-duplicate images — imagehash (pHash + dHash combined) via BK-tree
                            O(n log n) search instead of the old O(n²) loop
  3. Fuzzy document dupes — rapidfuzz on extracted OCR/PDF text content

Safety enforcement:
  Every resolve action that marks a file for deletion is checked against
  a configurable certainty threshold (default 0.95).  Actions with similarity
  below the threshold are rejected at the engine level — not just the UI.

  get_certainty_threshold(db_module) → float   (reads from preferences)
  check_deletion_certainty(similarity, threshold) → bool
"""

import logging
import uuid
from pathlib import Path
from typing import Optional

import psycopg2.extras

from .. import database as db
from ..progress import ProgressTracker, register_operation, unregister_operation

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety: certainty threshold
# ---------------------------------------------------------------------------

DEFAULT_CERTAINTY_THRESHOLD = 0.95   # 95% similarity required for auto-delete
DEFAULT_ORGANIZE_THRESHOLD  = 0.85   # 85% confidence required for auto-organize


def get_certainty_threshold(db_module=None) -> float:
    """Read the duplicate deletion certainty threshold from preferences."""
    _db = db_module or db
    try:
        val = _db.get_preference("duplicate_certainty_threshold",
                                 DEFAULT_CERTAINTY_THRESHOLD)
        return float(val)
    except Exception:
        return DEFAULT_CERTAINTY_THRESHOLD


def check_deletion_certainty(similarity: float, threshold: float = None) -> bool:
    """Return True only if similarity meets or exceeds the safety threshold."""
    if threshold is None:
        threshold = DEFAULT_CERTAINTY_THRESHOLD
    return similarity >= threshold


# ---------------------------------------------------------------------------
# imagehash — multi-hash strategy for better accuracy
# ---------------------------------------------------------------------------

def _compute_imagehash(filepath: str):
    """Compute a combined pHash+dHash for the image using imagehash library.

    Returns an imagehash object (supports XOR hamming distance via '-').
    Falls back to None if imagehash/Pillow unavailable.
    """
    try:
        import imagehash
        from PIL import Image
        with Image.open(filepath) as img:
            ph = imagehash.phash(img, hash_size=8)
            dh = imagehash.dhash(img, hash_size=8)
        # Combine: store as tuple for BK-tree (we use phash as primary key)
        return ph, dh
    except ImportError:
        log.debug("imagehash not installed — falling back to custom pHash")
        return _compute_phash_fallback(filepath), None
    except Exception as e:
        log.debug("imagehash error for %s: %s", filepath, e)
        return None, None


def _compute_phash_fallback(filepath: str) -> Optional[str]:
    """Original pHash implementation as fallback when imagehash unavailable."""
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(filepath).convert("L").resize(
            (9, 8), Image.LANCZOS
        )
        pixels = np.array(img, dtype=float)
        diff = pixels[:, 1:] > pixels[:, :-1]
        return "".join(str(int(b)) for row in diff for b in row)
    except Exception as e:
        log.debug("pHash fallback error for %s: %s", filepath, e)
        return None


def _hamming_distance(h1, h2) -> int:
    """Hamming distance between two imagehash objects or binary strings."""
    if h1 is None or h2 is None:
        return 999
    # imagehash objects support '-' operator
    if hasattr(h1, "__sub__"):
        return h1 - h2
    # Plain string fallback
    return sum(c1 != c2 for c1, c2 in zip(str(h1), str(h2)))


# ---------------------------------------------------------------------------
# BK-tree for O(n log n) near-duplicate search
# ---------------------------------------------------------------------------

class _BKTree:
    """Simple BK-tree using Hamming distance for perceptual hash search."""

    def __init__(self):
        self._root = None  # (hash, file_id, children_dict)

    def insert(self, h, file_id):
        if self._root is None:
            self._root = [h, file_id, {}]
            return
        node = self._root
        while True:
            dist = _hamming_distance(h, node[0])
            if dist == 0:
                return  # duplicate hash — skip
            children = node[2]
            if dist not in children:
                children[dist] = [h, file_id, {}]
                return
            node = children[dist]

    def search(self, query, max_dist: int) -> list:
        """Return list of (file_id, distance) within max_dist."""
        if self._root is None:
            return []
        results = []
        stack = [self._root]
        while stack:
            node = stack.pop()
            dist = _hamming_distance(query, node[0])
            if dist <= max_dist:
                results.append((node[1], dist))
            # Only recurse into children where distance could be ≤ max_dist
            for d, child in node[2].items():
                if abs(d - dist) <= max_dist:
                    stack.append(child)
        return results


# ---------------------------------------------------------------------------
# Fuzzy document duplicate detection (rapidfuzz)
# ---------------------------------------------------------------------------

def find_fuzzy_document_duplicates(threshold: float = 0.85, job_id: str = None,
                                    db_module=None) -> dict:
    """Find near-duplicate documents by comparing OCR/extracted text content.

    Uses rapidfuzz token_set_ratio — much faster than edit distance for
    long documents and resilient to word-order changes.

    threshold: 0.0–1.0 similarity required to be considered near-duplicate.
    """
    _db = db_module or db
    if job_id is None:
        job_id = str(uuid.uuid4())
    _db.create_job(job_id, "duplicates")

    tracker = ProgressTracker(
        operation_type="duplicate_fuzzy_docs",
        total=0,
        unit="files",
        job_id=job_id,
    )
    register_operation(tracker)

    try:
        from rapidfuzz import fuzz
    except ImportError:
        msg = "rapidfuzz not installed — document duplicate detection unavailable"
        log.warning(msg)
        tracker.fail(error=msg)
        unregister_operation(tracker.operation_id)
        _db.update_job(job_id, status="failed", message=msg)
        return {"job_id": job_id, "groups": 0, "error": msg}

    # Fetch files that have OCR text
    with _db.get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT f.id, f.file_path, o.text_content
            FROM files f
            JOIN file_ocr_text o ON o.file_id = f.id
            WHERE length(o.text_content) > 100
            ORDER BY f.id
        """)
        docs = cur.fetchall()

    _TEXT_LIMIT = 5000  # cap text comparison at 5KB for speed
    total = len(docs)
    tracker.total = total
    tracker.update(current=0, message=f"Comparing {total} documents for fuzzy duplicates…")

    groups = []
    used = set()

    for i, doc_a in enumerate(docs):
        if doc_a["id"] in used:
            tracker.update(current=i + 1, message=f"Fuzzy compare {i+1}/{total}")
            continue
        group = [(doc_a["id"], 1.0)]
        text_a = (doc_a["text_content"] or "")[:_TEXT_LIMIT]

        for doc_b in docs[i + 1:]:
            if doc_b["id"] in used:
                continue
            text_b = (doc_b["text_content"] or "")[:_TEXT_LIMIT]
            ratio = fuzz.token_set_ratio(text_a, text_b) / 100.0
            if ratio >= threshold:
                group.append((doc_b["id"], round(ratio, 4)))
                used.add(doc_b["id"])

        if len(group) > 1:
            used.add(doc_a["id"])
            groups.append(group)

        tracker.update(current=i + 1, message=f"Fuzzy compare {i+1}/{total}")

    total_groups = 0
    for group in groups:
        ref_id = group[0][0]
        _db.save_duplicate_group(f"fuzzy:{ref_id}", "fuzzy_text", group)
        total_groups += 1

    summary = {"job_id": job_id, "groups": total_groups}
    _db.update_job(job_id, status="completed", progress=1.0,
                   message=f"Found {total_groups} fuzzy document duplicate groups",
                   result=summary)
    tracker.complete(result=summary)
    unregister_operation(tracker.operation_id)
    return summary


# ---------------------------------------------------------------------------
# Exact duplicate detection
# ---------------------------------------------------------------------------

def find_exact_duplicates(job_id=None) -> dict:
    """Group files with identical SHA-256 hashes."""
    if job_id is None:
        job_id = str(uuid.uuid4())
    db.create_job(job_id, "duplicates")

    tracker = ProgressTracker(
        operation_type="duplicate_exact",
        total=0,
        unit="groups",
        job_id=job_id,
    )
    register_operation(tracker)
    tracker.update(message="Querying for exact duplicates (SHA-256)…")

    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT sha256, array_agg(id ORDER BY id) AS file_ids
            FROM files
            WHERE sha256 IS NOT NULL AND sha256 != ''
            GROUP BY sha256
            HAVING count(*) > 1
        """)
        groups = cur.fetchall()

    tracker.total = len(groups)
    total_groups = 0
    for i, (sha, file_ids) in enumerate(groups):
        members = [(fid, 1.0) for fid in file_ids]
        db.save_duplicate_group(f"exact:{sha}", "exact", members)
        total_groups += 1
        tracker.update(current=i + 1, message=f"Saved exact group {i+1}/{len(groups)}")

    summary = {"job_id": job_id, "groups": total_groups}
    db.update_job(job_id, status="completed", progress=1.0,
                  message=f"Found {total_groups} exact duplicate groups",
                  result=summary)
    tracker.complete(result=summary, message=f"Found {total_groups} exact duplicate groups")
    unregister_operation(tracker.operation_id)
    return summary


# ---------------------------------------------------------------------------
# Near-duplicate images (imagehash + BK-tree)
# ---------------------------------------------------------------------------

def find_near_duplicates(threshold: int = 10, job_id: str = None) -> dict:
    """Find near-duplicate images using imagehash + BK-tree.

    threshold: max Hamming distance out of 64 bits (default 10 ≈ 85% similar).
    Uses BK-tree for O(n log n) search instead of the old O(n²) approach.
    """
    if job_id is None:
        job_id = str(uuid.uuid4())
    db.create_job(job_id, "duplicates")

    tracker = ProgressTracker(
        operation_type="duplicate_near",
        total=0,
        unit="images",
        job_id=job_id,
    )
    register_operation(tracker)

    # ── Phase 1: load image files ──────────────────────────────────────────
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, file_path FROM files
            WHERE mime_type LIKE 'image/%%'
            ORDER BY id
        """)
        images = cur.fetchall()

    total = len(images)
    tracker.total = total
    tracker.update(current=0, message=f"Hashing {total} images…")

    # ── Phase 2: compute hashes + build BK-tree ────────────────────────────
    tree = _BKTree()
    hashes = {}   # file_id → (phash, dhash)
    hash_size = 64  # imagehash pHash at hash_size=8 produces 64 bits

    for i, (fid, fpath) in enumerate(images):
        ph, dh = _compute_imagehash(fpath)
        if ph is not None:
            hashes[fid] = (ph, dh)
            tree.insert(ph, fid)
        tracker.update(
            current=i + 1,
            message=f"Hashing {i+1}/{total}: {Path(fpath).name}",
        )

    # ── Phase 3: BK-tree search for near-duplicates ───────────────────────
    tracker.update(current=0, total=len(hashes),
                   message=f"Searching for near-duplicates (threshold={threshold})…")

    groups = []
    used = set()
    file_id_list = list(hashes.keys())

    for i, fid in enumerate(file_id_list):
        if fid in used:
            continue
        ph, _ = hashes[fid]
        # BK-tree returns (file_id, dist) pairs
        matches = tree.search(ph, threshold)
        # Filter to unprocessed matches only
        group_members = [
            (mid, dist)
            for mid, dist in matches
            if mid != fid and mid not in used
        ]
        if group_members:
            # Add self with distance 0
            all_members = [(fid, 0)] + group_members
            groups.append(all_members)
            used.update(m[0] for m in all_members)

        tracker.update(current=i + 1)

    # ── Phase 4: save groups ──────────────────────────────────────────────
    tracker.update(total=len(groups), current=0,
                   message=f"Saving {len(groups)} near-duplicate groups…")

    total_groups = 0
    for i, group in enumerate(groups):
        ref_id = group[0][0]
        ref_hash = hashes[ref_id][0]
        members = []
        for fid, dist in group:
            similarity = round(1.0 - (dist / hash_size), 4)
            members.append((fid, similarity))
        group_key = f"near:{str(ref_hash)[:16]}:{ref_id}"
        db.save_duplicate_group(group_key, "near", members)
        total_groups += 1
        tracker.update(current=i + 1)

    summary = {"job_id": job_id, "groups": total_groups}
    db.update_job(job_id, status="completed", progress=1.0,
                  message=f"Found {total_groups} near-duplicate image groups",
                  result=summary)
    tracker.complete(result=summary,
                     message=f"Found {total_groups} near-duplicate image groups")
    unregister_operation(tracker.operation_id)
    return summary


# ---------------------------------------------------------------------------
# Combined run
# ---------------------------------------------------------------------------

def run_all_detection(job_id: str = None) -> dict:
    """Run exact + near-duplicate + fuzzy document detection in sequence."""
    if job_id is None:
        job_id = str(uuid.uuid4())
    db.create_job(job_id, "duplicates")

    exact  = find_exact_duplicates()
    near   = find_near_duplicates()
    fuzzy  = find_fuzzy_document_duplicates()

    total_groups = exact["groups"] + near["groups"] + fuzzy["groups"]
    db.update_job(
        job_id, status="completed", progress=1.0,
        message=(f"Found {exact['groups']} exact + {near['groups']} near "
                 f"+ {fuzzy['groups']} fuzzy document groups"),
        result={"exact": exact["groups"], "near": near["groups"],
                "fuzzy": fuzzy["groups"], "total": total_groups},
    )
    return {"job_id": job_id, "exact": exact["groups"],
            "near": near["groups"], "fuzzy": fuzzy["groups"]}


# ---------------------------------------------------------------------------
# Safety-checked resolve
# ---------------------------------------------------------------------------

def resolve_duplicates(group_id: int, actions: dict,
                       db_module=None) -> dict:
    """Apply keep/delete decisions for a duplicate group.

    actions: {member_id: True (keep) | False (delete)}

    Raises ValueError if any delete action has similarity below the
    configured certainty threshold.

    Returns: {"applied": int, "blocked": list}
    """
    _db = db_module or db
    threshold = get_certainty_threshold(_db)

    # Fetch ALL member details including similarity scores and current keep state
    with _db.get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT dm.id, dm.file_id, dm.similarity, dm.keep, f.file_path
            FROM duplicate_members dm
            JOIN files f ON f.id = dm.file_id
            WHERE dm.group_id = %s
        """, (group_id,))
        members = {row["id"]: row for row in cur.fetchall()}

    blocked = []
    applied = 0
    # Track keep state locally to avoid a second DB round-trip
    pending_keep: dict[int, bool | None] = {mid: dict(m)["keep"] for mid, m in members.items()}

    for member_id_str, keep in actions.items():
        member_id = int(member_id_str)
        member = members.get(member_id)
        if not member:
            continue

        # Safety check: block deletes below certainty threshold
        if not keep:
            similarity = float(member.get("similarity") or 0.0)
            if not check_deletion_certainty(similarity, threshold):
                blocked.append({
                    "member_id": member_id,
                    "file_path": member["file_path"],
                    "similarity": similarity,
                    "threshold": threshold,
                    "reason": (
                        f"Certainty {similarity:.1%} is below the "
                        f"required threshold of {threshold:.1%}. "
                        "Increase similarity or raise the threshold in Settings."
                    ),
                })
                continue

        _db.update_duplicate_member_keep(member_id, keep)
        pending_keep[member_id] = keep
        applied += 1

    # Compute undecided count from in-memory state — no second DB query needed
    undecided = sum(1 for k in pending_keep.values() if k is None)
    if undecided == 0:
        _db.update_duplicate_group_status(group_id, "resolved")

    return {"applied": applied, "blocked": blocked}
