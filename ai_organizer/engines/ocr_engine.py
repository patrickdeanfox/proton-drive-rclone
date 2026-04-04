"""OCR engine — Python-first text extraction pipeline.

Pipeline:
  1. metadata_engine identifies file type (image or PDF)
  2. Pillow preprocesses the image (grayscale, contrast, denoise)
  3. pytesseract extracts text (no AI involved)
  4. Results stored in file_ocr_text table via database module
  5. If text is extracted from a scanned PDF/image, it feeds
     the text-embedding pipeline downstream (sentence-transformers)

Requires system packages:
  apt install tesseract-ocr tesseract-ocr-eng poppler-utils
  pip install pytesseract pdf2image
"""

import logging
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Availability checks (lazy imports)
# ---------------------------------------------------------------------------

def _check_tesseract() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _check_pdf2image() -> bool:
    try:
        import pdf2image
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Image preprocessing for better OCR accuracy
# ---------------------------------------------------------------------------

def _preprocess_for_ocr(img):
    """Apply Pillow transforms to improve OCR accuracy.

    Steps: convert to greyscale → auto-contrast → slight sharpening.
    Returns the preprocessed PIL Image.
    """
    from PIL import Image, ImageEnhance, ImageFilter
    # Convert to greyscale
    img = img.convert("L")
    # Upscale small images — Tesseract works best at ~300 DPI
    min_dim = 1000
    w, h = img.size
    if w < min_dim or h < min_dim:
        scale = max(min_dim / w, min_dim / h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    # Auto-contrast
    img = ImageEnhance.Contrast(img).enhance(1.5)
    # Mild sharpening
    img = img.filter(ImageFilter.SHARPEN)
    return img


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _ocr_image_file(filepath: str, lang: str = "eng") -> Optional[str]:
    """Run Tesseract OCR on a single image file."""
    try:
        from PIL import Image
        import pytesseract
        with Image.open(filepath) as img:
            processed = _preprocess_for_ocr(img)
            text = pytesseract.image_to_string(processed, lang=lang)
        return text.strip() or None
    except Exception as e:
        log.warning("OCR error for image %s: %s", filepath, e)
        return None


def _ocr_pdf_file(filepath: str, lang: str = "eng",
                  max_pages: int = 20) -> Optional[str]:
    """Convert PDF pages to images then OCR each page.

    First tries to extract embedded text via pypdf (fast, no OCR needed).
    Falls back to pdf2image + Tesseract for scanned PDFs.
    """
    # Try pypdf text layer first
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        pages_text = []
        for page in reader.pages[:max_pages]:
            t = page.extract_text() or ""
            pages_text.append(t.strip())
        combined = "\n\n".join(p for p in pages_text if p)
        if len(combined) > 100:
            return combined  # Has a real text layer — no OCR needed
    except Exception as e:
        log.debug("pypdf text extraction failed for %s: %s", filepath, e)

    # Fall back to image-based OCR
    if not _check_pdf2image():
        log.warning("pdf2image not installed — cannot OCR scanned PDF %s", filepath)
        return None

    try:
        from pdf2image import convert_from_path
        import pytesseract
        pages = convert_from_path(filepath, dpi=200, last_page=max_pages)
        texts = []
        for page_img in pages:
            processed = _preprocess_for_ocr(page_img)
            t = pytesseract.image_to_string(processed, lang=lang)
            texts.append(t.strip())
        combined = "\n\n".join(t for t in texts if t)
        return combined or None
    except Exception as e:
        log.warning("PDF OCR error for %s: %s", filepath, e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_text(filepath: str, lang: str = "eng") -> Optional[str]:
    """Extract text from an image or PDF file.

    Returns the extracted text string, or None if extraction failed / file
    type is not supported.

    No AI is involved — this is pure Python (Pillow + pytesseract + pypdf).
    """
    path = Path(filepath)
    ext = path.suffix.lower()

    pdf_exts = {".pdf"}
    img_exts = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp",
                ".gif", ".webp", ".heic", ".heif"}

    if ext in pdf_exts:
        return _ocr_pdf_file(filepath, lang=lang)
    elif ext in img_exts:
        return _ocr_image_file(filepath, lang=lang)
    else:
        # Try to detect via MIME
        try:
            import filetype
            kind = filetype.guess(filepath)
            if kind and kind.mime.startswith("image/"):
                return _ocr_image_file(filepath, lang=lang)
            if kind and kind.mime == "application/pdf":
                return _ocr_pdf_file(filepath, lang=lang)
        except ImportError:
            pass
        return None


def batch_ocr(file_records: list, lang: str = "eng",
              job_id: str = None, db_module=None) -> dict:
    """Run OCR on a list of file dicts and optionally store results in DB.

    file_records: list of dicts with at least 'id' and 'file_path' keys.
    db_module: if provided, calls db_module.save_ocr_text(file_id, text).

    Emits real-time progress via ProgressTracker / SocketIO.

    Returns: {"processed": int, "skipped": int, "errors": int}
    """
    from ..progress import ProgressTracker, register_operation, unregister_operation

    if not _check_tesseract():
        log.error("Tesseract not found — OCR unavailable. "
                  "Install: apt install tesseract-ocr")
        return {"processed": 0, "skipped": 0, "errors": 0,
                "error": "tesseract not found"}

    if job_id is None:
        job_id = str(uuid.uuid4())

    total = len(file_records)
    tracker = ProgressTracker(
        operation_type="ocr",
        total=total,
        unit="files",
        job_id=job_id,
    )
    register_operation(tracker)

    processed = 0
    skipped = 0
    errors = 0

    for i, record in enumerate(file_records):
        fpath = record.get("file_path") or record.get("path", "")
        fid = record.get("id")
        fname = Path(fpath).name

        try:
            text = extract_text(fpath, lang=lang)
            if text is None:
                skipped += 1
            else:
                processed += 1
                if db_module and fid:
                    try:
                        db_module.save_ocr_text(fid, text, lang=lang)
                    except Exception as e:
                        log.debug("DB save_ocr_text error for %s: %s", fpath, e)
        except Exception as e:
            log.warning("OCR batch error for %s: %s", fpath, e)
            errors += 1

        tracker.update(
            current=i + 1,
            message=f"OCR: {fname}",
            extra={"ocr_processed": processed, "ocr_skipped": skipped},
        )

    summary = {"processed": processed, "skipped": skipped, "errors": errors}
    tracker.complete(
        result=summary,
        message=f"OCR complete: {processed} extracted, {skipped} skipped",
    )
    unregister_operation(tracker.operation_id)
    return summary
