"""P3-T6 — OCR extraction pass (per-library opt-in, hash-gated cache).

Runs INSIDE ``extract_item`` only when the owning ``Library.ocr_enabled`` is true
(resolved per-run from the library row, like the T7 hash policy), so a library
with the global default (OFF, R4) and no opt-in pays ZERO OCR cost.

Eligibility (v1 scope): image items always; ``document`` items that are PDFs with
NO usable native text layer (scanned PDFs — body-text below the threshold). docx/
xlsx/txt already carry a real text layer and are never OCR'd. The trigger gate is
the pure ``ocr.should_ocr`` policy (native-text-below-threshold + page/pixel
ceilings). The engine (``ocr.run_ocr``) is one Tesseract subprocess per image
(``-stay_open`` is an exiftool feature, not tesseract); a scanned PDF is first
rasterised with pdftoppm.

Hash-gated cache (Recoll/Docspell precedent, brief §3): the OCR text is cached in
``metadata_.ocr_text`` alongside ``metadata_.ocr_source_hash`` (the file's current
content/quick hash). A re-extract whose hash still matches SKIPS Tesseract entirely
and reuses the cached text (invariant-1-safe: fully Postgres-resident, so
``rebuild_index`` re-projects OCR text with zero re-OCR cost). The projection joins
``ocr_text`` into the same searchable ``body_text`` field (``search.build_doc``),
under the same index cap.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Any

from filearr.config import Settings
from filearr.errors import sanitize_error
from filearr.models import MediaType
from filearr.ocr import OcrError, OcrPolicy, run_ocr, should_ocr


def _native_text_len(meta: dict[str, Any]) -> int:
    body = meta.get("body_text")
    return len(body) if isinstance(body, str) else 0


def _pixels(meta: dict[str, Any]) -> int | None:
    w = meta.get("width") or meta.get("exif.width")
    h = meta.get("height") or meta.get("exif.height")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return w * h
    return None


def is_ocr_eligible(path: str, media_type: MediaType) -> tuple[bool, bool]:
    """Return ``(eligible, is_pdf)`` for the OCR pass.

    Images are eligible (``is_pdf=False``); ``document`` items are eligible ONLY
    when the file is a PDF (``is_pdf=True``) — the scanned-vs-text decision is left
    to ``should_ocr`` (native text threshold). Everything else is ineligible.
    """
    if media_type == MediaType.image:
        return True, False
    if media_type == MediaType.document:
        return PurePath(path).suffix.lower() == ".pdf", True
    return False, False


def ocr_metadata(
    path: str,
    *,
    media_type: MediaType,
    meta: dict[str, Any],
    prior_meta: dict[str, Any],
    source_hash: str | None,
    settings: Settings,
) -> dict[str, Any]:
    """Compute the OCR metadata delta to merge into ``metadata_``.

    ``meta`` is the current-run extractor output (native ``body_text``/dimensions
    already present); ``prior_meta`` is the item's existing ``metadata_`` (the cache
    source). Returns ``{}`` when OCR is skipped (ineligible, sufficient native text,
    a page/pixel ceiling, or a hash-cache hit), a ``{ocr_text, ocr_source_hash,
    ocr_text_truncated}`` delta on success, or ``{"_ocr_error": ...}`` on engine
    failure (degrade — never fail the extract). Never raises.
    """
    eligible, is_pdf = is_ocr_eligible(path, media_type)
    if not eligible:
        return {}

    policy = OcrPolicy(
        enabled=True,  # caller only invokes when library.ocr_enabled is true
        min_text_chars=settings.ocr_min_text_chars,
        max_pages=settings.ocr_max_pages,
        max_pixels=settings.ocr_max_pixels,
        timeout_s=settings.ocr_timeout_s,
    )
    view = {
        "text_len": _native_text_len(meta),
        "pages": meta.get("pages"),
        "pixels": _pixels(meta),
    }
    if not should_ocr(view, policy):
        return {}

    # FIX-11: OCR rasterises PDF pages into the tmp dir (pdftoppm) before running
    # Tesseract — a real, potentially large disk write. When tmp is at the
    # critical low-space floor, SKIP OCR (record a sentinel and continue) rather
    # than fail the extract: OCR text is disposable enrichment, and the extract's
    # DB row (invariant 2) must still land. Degrades exactly like an engine error.
    import tempfile as _tf

    from filearr import diskguard

    if diskguard.is_critical(_tf.gettempdir(), settings):
        return {"_ocr_error": "disk_full_guard: OCR skipped (low disk)"}

    # Hash-gated cache: skip Tesseract when the cached OCR text is for the same
    # bytes (honours T7 — source_hash is content_hash when computed, else quick_hash).
    if (
        source_hash is not None
        and prior_meta.get("ocr_source_hash") == source_hash
        and "ocr_text" in prior_meta
    ):
        return {}

    try:
        text = run_ocr(
            path,
            is_pdf=is_pdf,
            lang=settings.ocr_lang,
            tesseract_path=settings.ocr_tesseract_path,
            pdftoppm_path=settings.ocr_pdftoppm_path,
            dpi=settings.ocr_dpi,
            max_pages=settings.ocr_max_pages,
            timeout_s=settings.ocr_timeout_s,
            max_chars=settings.ocr_max_chars,
        )
    except OcrError as exc:
        return {"_ocr_error": sanitize_error(exc)}
    except Exception as exc:  # defence in depth — OCR must never kill an extract
        return {"_ocr_error": sanitize_error(exc)}

    return {
        "ocr_text": text,
        "ocr_source_hash": source_hash,
        "ocr_text_truncated": len(text) >= settings.ocr_max_chars,
    }
