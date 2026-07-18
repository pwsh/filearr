"""OCR trigger policy + engine stub (Phase 3, roadmap §5 — P3-T6).

**Inert scaffolding.** Only tests import this module. It ships the implemented,
pure trigger-threshold policy (``OcrPolicy`` + ``should_ocr``) synthesised from
the brief §3 cross-tool analysis (ocrmypdf / Paperless / Docspell / Recoll), and
a typed ``run_ocr`` stub that P3-T6 will implement against a Tesseract 5.5.2
``-stay_open`` subprocess pool.

Policy design (brief §3, R4):
- **Global default OFF** (``FILEARR_OCR_ENABLED=false``); a per-library
  ``ocr_enabled`` toggle opts in (Recoll's per-directory precedent). ``should_ocr``
  therefore short-circuits ``False`` unless the resolved policy is enabled.
- **Attempt cheap native text extraction first** (P3-T5 body-text pass), then
  **gate OCR behind an "extracted text below N chars" threshold**
  (``FILEARR_OCR_MIN_TEXT_CHARS``, default 100 — Paperless uses 50, Docspell 500;
  the brief picks ~100 and says document it).
- Two independently-tunable ceilings recur everywhere and short-circuit OCR when
  exceeded: a **page-count cap** (``FILEARR_OCR_MAX_PAGES``) and a **pixel cap**
  (``FILEARR_OCR_MAX_PIXELS``). A **timeout** (``FILEARR_OCR_TIMEOUT_S``) bounds
  the engine call itself (enforced in ``run_ocr``, not in the pure gate).

``should_ocr`` is deliberately pure over an already-extracted metadata view (a
plain dict), so it is unit-testable with no filesystem/engine and reusable by
both the extract worker and a future "Redo OCR" manual action.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

# Threshold defaults mirror the brief's chosen values / the T6 size-ceiling
# convention. These are the resolved (already library+global-merged) values the
# caller hands to ``should_ocr``; env parsing lives in config.py at P3-T6 time.
DEFAULT_MIN_TEXT_CHARS = 100

# Whitespace normaliser shared by the OCR-text sanitiser.
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class OcrPolicy:
    """Resolved OCR trigger thresholds for one item's evaluation.

    ``enabled`` is the effective per-library opt-in (global default false, R4).
    ``max_pages`` / ``max_pixels`` are ``None`` when uncapped. ``timeout_s`` is
    carried here for completeness but is enforced by ``run_ocr`` (the pure gate
    cannot time-bound an engine it does not call).
    """

    enabled: bool = False
    min_text_chars: int = DEFAULT_MIN_TEXT_CHARS
    max_pages: int | None = None
    max_pixels: int | None = None
    timeout_s: float = 180.0


def should_ocr(item_meta: dict[str, Any], policy: OcrPolicy) -> bool:
    """Decide whether an item should be OCR'd, per the brief §3 policy.

    ``item_meta`` is a resolved metadata view for the candidate file. Recognised
    keys (all optional):

    * ``text_len`` (int) or ``body_text`` (str) — how much native text a cheap
      extraction already recovered. If that meets ``min_text_chars`` the file has
      a usable text layer and OCR is skipped (all PDF-touching tools check for an
      existing text layer first — brief §3).
    * ``pages`` (int) — page count; over ``max_pages`` skips (Docspell/Paperless
      page cap).
    * ``pixels`` (int) — largest-image / rasterised-page pixel count; over
      ``max_pixels`` skips (ocrmypdf ``--skip-big`` / Paperless
      ``OCR_MAX_IMAGE_PIXELS``).

    Returns ``True`` only when OCR is both permitted (policy enabled) and worth
    running (insufficient native text, within both ceilings). Pure: no IO.
    """
    if not policy.enabled:
        return False

    # Cheap-native-text-first: skip OCR when an existing text layer is sufficient.
    text_len = item_meta.get("text_len")
    if text_len is None:
        body = item_meta.get("body_text")
        text_len = len(body) if isinstance(body, str) else 0
    if text_len >= policy.min_text_chars:
        return False

    pages = item_meta.get("pages")
    if policy.max_pages is not None and isinstance(pages, int) and pages > policy.max_pages:
        return False

    pixels = item_meta.get("pixels")
    if policy.max_pixels is not None and isinstance(pixels, int) and pixels > policy.max_pixels:
        return False

    return True


class OcrError(RuntimeError):
    """OCR could not be performed (missing binary, timeout, nonzero exit, or a
    rasterisation failure). The message is safe to store under ``_ocr_error``."""


# Default engine knobs (mirrored by config.py; kept here so the module's engine
# functions are self-contained and unit-testable without importing Settings).
DEFAULT_LANG = "eng"
DEFAULT_DPI = 200
DEFAULT_MAX_PAGES = 10
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_CHARS = 100_000
_PSM = "3"  # fully automatic page segmentation, no OSD (tesseract default)


def _resolve(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        raise OcrError(f"OCR binary not found: {binary!r}")
    return resolved


def run_tesseract(
    image_path: str,
    *,
    lang: str = DEFAULT_LANG,
    tesseract_path: str = "tesseract",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """OCR a single raster image via one Tesseract subprocess, returning its text.

    ``tesseract <img> stdout -l <lang> --psm 3`` — argv list (never a shell
    string), a hard timeout (child killed on expiry), stdout decoded
    ``errors="replace"``. Raises :class:`OcrError` on any failure. This is the
    per-file, pooled-concurrency posture the brief calls for (one subprocess per
    file — ``-stay_open`` is an exiftool feature, NOT a tesseract one).
    """
    binary = _resolve(tesseract_path)
    # Positional args (image, "stdout") then options; argv list (never a shell
    # string) so nothing in the untrusted path is interpreted by a shell.
    argv = [binary, image_path, "stdout", "-l", lang, "--psm", _PSM]
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout_s, check=False, shell=False
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"tesseract timed out after {timeout_s:g}s") from exc
    except OSError as exc:
        raise OcrError(f"tesseract could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = detail[-1] if detail else f"exit {proc.returncode}"
        raise OcrError(f"tesseract failed: {msg}")
    return proc.stdout.decode("utf-8", "replace")


def rasterize_pdf(
    pdf_path: str,
    out_dir: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    dpi: int = DEFAULT_DPI,
    pdftoppm_path: str = "pdftoppm",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[str]:
    """Rasterise the first ``max_pages`` pages of a PDF to PNGs in ``out_dir``.

    ``pdftoppm -png -r <dpi> -f 1 -l <max_pages> <pdf> <out_dir>/page`` (poppler-
    utils, shipped in the Docker runtime stage). Returns the sorted list of
    produced image paths. Raises :class:`OcrError` on failure. Bounding pages +
    DPI caps the pixel/time cost before any OCR happens.
    """
    binary = _resolve(pdftoppm_path)
    prefix = os.path.join(out_dir, "page")
    argv = [
        binary, "-png", "-r", str(int(dpi)),
        "-f", "1", "-l", str(int(max_pages)),
        pdf_path, prefix,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout_s, check=False, shell=False
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"pdftoppm timed out after {timeout_s:g}s") from exc
    except OSError as exc:
        raise OcrError(f"pdftoppm could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = detail[-1] if detail else f"exit {proc.returncode}"
        raise OcrError(f"pdftoppm failed: {msg}")
    pages = sorted(
        os.path.join(out_dir, n) for n in os.listdir(out_dir) if n.endswith(".png")
    )
    if not pages:
        raise OcrError("pdftoppm produced no page images")
    return pages


def run_ocr(
    path: str,
    *,
    is_pdf: bool = False,
    lang: str = DEFAULT_LANG,
    tesseract_path: str = "tesseract",
    pdftoppm_path: str = "pdftoppm",
    dpi: int = DEFAULT_DPI,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """OCR ``path`` and return the extracted text (char-capped).

    An image file is OCR'd directly; a (scanned) PDF is first rasterised page-by-
    page with pdftoppm (bounded by ``max_pages``/``dpi``) into a temp dir, each
    page OCR'd, and the page texts joined. The subprocess layer (tesseract /
    pdftoppm) is the ONLY external dependency — never in-process linking, matching
    the ffprobe/exiftool posture. Raises :class:`OcrError` on failure; the caller
    degrades to an ``_ocr_error`` sentinel rather than failing the extract.
    """
    if is_pdf:
        with tempfile.TemporaryDirectory(prefix="filearr-ocr-") as tmp:
            pages = rasterize_pdf(
                path, tmp,
                max_pages=max_pages, dpi=dpi,
                pdftoppm_path=pdftoppm_path, timeout_s=timeout_s,
            )
            parts: list[str] = []
            total = 0
            for page in pages:
                text = run_tesseract(
                    page, lang=lang, tesseract_path=tesseract_path, timeout_s=timeout_s
                )
                if text:
                    parts.append(text)
                    total += len(text)
                if total >= max_chars:
                    break
            joined = "\n".join(parts)
    else:
        joined = run_tesseract(
            path, lang=lang, tesseract_path=tesseract_path, timeout_s=timeout_s
        )
    return _normalize_ocr_text(joined, max_chars)


def _normalize_ocr_text(raw: str, max_chars: int) -> str:
    """Whitespace-normalise + char-cap OCR output (untrusted content). Collapses
    runs of whitespace to single spaces, strips control chars, truncates to
    ``max_chars`` — same discipline as the body-text pass."""
    out: list[str] = []
    for ch in raw:
        o = ord(ch)
        if o in (0x09, 0x0A, 0x0B, 0x0C, 0x0D):
            out.append(" ")
        elif o < 0x20 or 0x7F <= o <= 0x9F:
            continue
        else:
            out.append(ch)
    collapsed = _WS_RE.sub(" ", "".join(out)).strip()
    return collapsed[:max_chars].rstrip() if len(collapsed) > max_chars else collapsed
