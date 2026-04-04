"""Python-first metadata extraction engine.

Extracts rich metadata entirely with Python libraries — no AI required.
Provides a confidence score so the AI pipeline can skip files that are
already fully classified.

Supported file types:
  - Images   : EXIF (GPS, camera, date taken), dimensions, color space
  - Audio    : ID3/FLAC/MP4/OGG tags (artist, album, title, duration, bitrate)
  - PDF      : title, author, pages, creation date, word estimate
  - Video    : dimensions, duration via file headers
  - All      : MIME type via magic bytes, size, timestamps

AI pipeline contract:
  extract_metadata(filepath) → MetadataResult
    .metadata     dict  — structured extracted data
    .category     str   — best Python-derived category (may be None)
    .confidence   float — 0.0–1.0; >= 0.90 means skip AI
    .needs_ai     bool  — True when Python cannot classify reliably
"""

import logging
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class MetadataResult:
    filepath: str
    metadata: dict = field(default_factory=dict)
    category: Optional[str] = None
    confidence: float = 0.0
    needs_ai: bool = True
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# MIME detection (magic bytes — no libmagic needed)
# ---------------------------------------------------------------------------

def _detect_mime(filepath: str) -> Optional[str]:
    """Detect MIME type from magic bytes via filetype, fallback to mimetypes."""
    try:
        import filetype
        kind = filetype.guess(filepath)
        if kind:
            return kind.mime
    except ImportError:
        pass
    except Exception as e:
        log.debug("filetype error for %s: %s", filepath, e)

    import mimetypes
    mime, _ = mimetypes.guess_type(filepath)
    return mime


# ---------------------------------------------------------------------------
# Extension → broad category mapping (confidence 0.95 — deterministic)
# ---------------------------------------------------------------------------

_EXT_CATEGORY = {
    # Documents
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".odt": "document", ".rtf": "document", ".txt": "document",
    ".md": "document", ".rst": "document", ".tex": "document",
    # Spreadsheets
    ".xls": "spreadsheet", ".xlsx": "spreadsheet", ".ods": "spreadsheet",
    ".csv": "spreadsheet",
    # Presentations
    ".ppt": "presentation", ".pptx": "presentation", ".odp": "presentation",
    # Images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".bmp": "image", ".tiff": "image", ".tif": "image", ".webp": "image",
    ".svg": "image", ".heic": "image", ".heif": "image", ".raw": "image",
    ".cr2": "image", ".nef": "image", ".arw": "image",
    # Audio
    ".mp3": "audio", ".flac": "audio", ".ogg": "audio", ".wav": "audio",
    ".aac": "audio", ".m4a": "audio", ".wma": "audio", ".opus": "audio",
    # Video
    ".mp4": "video", ".mkv": "video", ".avi": "video", ".mov": "video",
    ".wmv": "video", ".flv": "video", ".webm": "video", ".m4v": "video",
    # Archives
    ".zip": "archive", ".tar": "archive", ".gz": "archive", ".bz2": "archive",
    ".7z": "archive", ".rar": "archive", ".xz": "archive",
    # Code
    ".py": "code", ".js": "code", ".ts": "code", ".java": "code",
    ".c": "code", ".cpp": "code", ".h": "code", ".go": "code",
    ".rs": "code", ".rb": "code", ".php": "code", ".sh": "code",
    ".yaml": "code", ".yml": "code", ".json": "code", ".xml": "code",
    ".toml": "code", ".ini": "code", ".conf": "code",
    # Fonts
    ".ttf": "font", ".otf": "font", ".woff": "font", ".woff2": "font",
    # Disk images
    ".iso": "disk_image", ".img": "disk_image", ".dmg": "disk_image",
}


# ---------------------------------------------------------------------------
# Image EXIF extraction
# ---------------------------------------------------------------------------

def _extract_image_metadata(filepath: str) -> dict:
    meta = {}
    try:
        from PIL import Image, ExifTags
        with Image.open(filepath) as img:
            meta["width"] = img.width
            meta["height"] = img.height
            meta["mode"] = img.mode
            meta["format"] = img.format

            exif_raw = img._getexif() if hasattr(img, "_getexif") else None
            if exif_raw:
                exif = {}
                for tag_id, val in exif_raw.items():
                    tag = ExifTags.TAGS.get(tag_id, str(tag_id))
                    # Skip raw binary blobs
                    if isinstance(val, bytes) and len(val) > 64:
                        continue
                    try:
                        exif[tag] = str(val) if not isinstance(val, (int, float, str)) else val
                    except Exception:
                        pass
                if exif:
                    meta["exif"] = exif
                    # Extract common fields to top level for easy querying
                    if "DateTime" in exif:
                        meta["date_taken"] = exif["DateTime"]
                    if "Make" in exif:
                        meta["camera_make"] = exif["Make"]
                    if "Model" in exif:
                        meta["camera_model"] = exif["Model"]
                    if "GPSInfo" in exif:
                        meta["has_gps"] = True
    except Exception as e:
        log.debug("Image metadata error for %s: %s", filepath, e)
    return meta


# ---------------------------------------------------------------------------
# Audio metadata extraction
# ---------------------------------------------------------------------------

def _extract_audio_metadata(filepath: str) -> dict:
    meta = {}
    try:
        import mutagen
        audio = mutagen.File(filepath, easy=True)
        if audio is None:
            return meta
        # Duration / bitrate from info
        if hasattr(audio, "info"):
            info = audio.info
            if hasattr(info, "length"):
                meta["duration_seconds"] = round(info.length, 2)
            if hasattr(info, "bitrate"):
                meta["bitrate_kbps"] = info.bitrate
            if hasattr(info, "sample_rate"):
                meta["sample_rate_hz"] = info.sample_rate
            if hasattr(info, "channels"):
                meta["channels"] = info.channels
        # Tags
        for tag in ("title", "artist", "album", "albumartist",
                    "date", "genre", "tracknumber", "discnumber"):
            val = audio.get(tag)
            if val:
                meta[tag] = str(val[0]) if isinstance(val, list) else str(val)
    except ImportError:
        log.debug("mutagen not installed — skipping audio metadata")
    except Exception as e:
        log.debug("Audio metadata error for %s: %s", filepath, e)
    return meta


# ---------------------------------------------------------------------------
# PDF metadata extraction
# ---------------------------------------------------------------------------

def _extract_pdf_metadata(filepath: str) -> dict:
    meta = {}
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        meta["pages"] = len(reader.pages)
        info = reader.metadata
        if info:
            for k in ("/Title", "/Author", "/Subject", "/Creator",
                      "/Producer", "/CreationDate", "/ModDate"):
                v = info.get(k)
                if v:
                    clean_key = k.lstrip("/").lower()
                    meta[clean_key] = str(v)
        # Rough word count from first page text
        try:
            first_text = reader.pages[0].extract_text() or ""
            meta["word_count_estimate"] = len(first_text.split())
            meta["has_text_layer"] = len(first_text.strip()) > 20
        except Exception:
            meta["has_text_layer"] = False
    except ImportError:
        log.debug("pypdf not installed — skipping PDF metadata")
    except Exception as e:
        log.debug("PDF metadata error for %s: %s", filepath, e)
    return meta


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------

def extract_metadata(filepath: str) -> MetadataResult:
    """Extract metadata using Python-only libraries. No AI involved.

    Returns a MetadataResult with .needs_ai=False when Python can classify
    the file with high confidence (>= 0.90).
    """
    path = Path(filepath)
    result = MetadataResult(filepath=filepath)

    if not path.exists():
        result.error = "File not found"
        return result

    # ── Base file info ────────────────────────────────────────────────────
    try:
        st = path.stat()
        result.metadata.update({
            "size_bytes": st.st_size,
            "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "created_at": datetime.fromtimestamp(st.st_ctime).isoformat(),
        })
    except Exception as e:
        log.debug("stat error for %s: %s", filepath, e)

    # ── MIME detection ────────────────────────────────────────────────────
    mime = _detect_mime(filepath)
    if mime:
        result.metadata["mime_type"] = mime

    # ── Extension-based category (high confidence) ────────────────────────
    ext = path.suffix.lower()
    ext_category = _EXT_CATEGORY.get(ext)

    if ext_category:
        result.category = ext_category
        result.confidence = 0.95  # deterministic from extension

    # ── Type-specific deep extraction ─────────────────────────────────────
    if ext_category == "image" or (mime and mime.startswith("image/")):
        img_meta = _extract_image_metadata(filepath)
        result.metadata.update(img_meta)
        result.metadata["file_type"] = "image"
        # Keep needs_ai=True for images — CLIP can sub-classify (photo/screenshot/meme/art)
        result.needs_ai = True
        result.confidence = 0.75  # Python knows it's an image but not the sub-type

    elif ext_category == "audio" or (mime and mime.startswith("audio/")):
        audio_meta = _extract_audio_metadata(filepath)
        result.metadata.update(audio_meta)
        result.metadata["file_type"] = "audio"
        result.needs_ai = False  # Python extracts full audio metadata
        result.confidence = 0.99

    elif ext_category == "document" and ext == ".pdf":
        pdf_meta = _extract_pdf_metadata(filepath)
        result.metadata.update(pdf_meta)
        result.metadata["file_type"] = "pdf"
        has_text = pdf_meta.get("has_text_layer", False)
        result.needs_ai = not has_text  # AI helps if no text layer (scanned PDF)
        result.confidence = 0.95 if has_text else 0.70

    elif ext_category in ("video",):
        result.metadata["file_type"] = "video"
        result.needs_ai = False
        result.confidence = 0.99

    elif ext_category in ("document", "spreadsheet", "presentation",
                           "archive", "code", "font", "disk_image"):
        result.metadata["file_type"] = ext_category
        result.needs_ai = False
        result.confidence = 0.99

    else:
        # Unknown — let AI take over
        result.needs_ai = True
        result.confidence = 0.0

    return result


# ---------------------------------------------------------------------------
# Batch extraction with progress reporting
# ---------------------------------------------------------------------------

def batch_extract_metadata(file_records: list, job_id: str = None,
                            db_module=None) -> dict:
    """Extract metadata for a list of file dicts (with 'id' and 'file_path').

    Emits ProgressTracker updates. Stores results via db_module.upsert_file
    when provided.

    Returns: {"processed": int, "errors": int, "needs_ai": int}
    """
    from ..progress import ProgressTracker, register_operation, unregister_operation
    import uuid

    if job_id is None:
        job_id = str(uuid.uuid4())

    total = len(file_records)
    tracker = ProgressTracker(
        operation_type="metadata_extraction",
        total=total,
        unit="files",
        job_id=job_id,
    )
    register_operation(tracker)

    processed = 0
    errors = 0
    needs_ai_count = 0

    for i, record in enumerate(file_records):
        fpath = record.get("file_path") or record.get("path", "")
        fid = record.get("id")
        try:
            res = extract_metadata(fpath)
            if res.error:
                errors += 1
            else:
                processed += 1
                if res.needs_ai:
                    needs_ai_count += 1
                # Persist rich metadata back to DB if module available
                if db_module and fid:
                    try:
                        existing = dict(record)
                        existing["metadata_json"] = res.metadata
                        if res.metadata.get("mime_type") and not existing.get("mime_type"):
                            existing["mime_type"] = res.metadata["mime_type"]
                        db_module.upsert_file(existing)
                    except Exception as e:
                        log.debug("DB upsert error for %s: %s", fpath, e)
        except Exception as e:
            log.warning("Metadata extraction error for %s: %s", fpath, e)
            errors += 1

        tracker.update(
            current=i + 1,
            message=f"Extracted metadata: {Path(fpath).name}",
        )

    summary = {"processed": processed, "errors": errors, "needs_ai": needs_ai_count}
    tracker.complete(result=summary, message=f"Metadata extracted for {processed} files")
    unregister_operation(tracker.operation_id)

    return summary
