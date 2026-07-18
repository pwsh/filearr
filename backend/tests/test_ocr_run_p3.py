"""P3-T6 — OCR run/orchestration tests.

The Tesseract/pdftoppm SUBPROCESS layer is MOCKED (the sandbox has no binaries);
pure logic (eligibility, the hash-gated cache, output cap, error degrade) is
exercised for real. Live verification of the actual OCR output needs an image
rebuild with tesseract-ocr + poppler-utils present (see the task doc note).
"""

from __future__ import annotations

import subprocess

import pytest

from filearr import ocr as ocr_mod
from filearr.config import Settings
from filearr.models import MediaType
from filearr.ocr import OcrError, _normalize_ocr_text
from filearr.tasks import ocr_run
from filearr.tasks.ocr_run import is_ocr_eligible, ocr_metadata


@pytest.fixture(autouse=True)
def _no_disk_guard(monkeypatch):
    """FIX-11 guard reads the REAL tmp statvfs; force deterministic 'ok' here
    (guard behaviour itself is covered by test_diskguard_fix11.py)."""
    from filearr import diskguard

    monkeypatch.setattr(diskguard, "is_critical", lambda *a, **k: False)


def _settings(**over) -> Settings:
    base = dict(
        ocr_min_text_chars=100,
        ocr_max_pages=10,
        ocr_max_pixels=40_000_000,
        ocr_timeout_s=120.0,
        ocr_max_chars=50,
        ocr_dpi=200,
        ocr_lang="eng",
        ocr_tesseract_path="tesseract",
        ocr_pdftoppm_path="pdftoppm",
    )
    base.update(over)
    return Settings(**base)


# --- eligibility -----------------------------------------------------------


def test_is_ocr_eligible_matrix():
    assert is_ocr_eligible("/a/photo.png", MediaType.image) == (True, False)
    assert is_ocr_eligible("/a/scan.pdf", MediaType.document) == (True, True)
    assert is_ocr_eligible("/a/notes.docx", MediaType.document) == (False, True)
    assert is_ocr_eligible("/a/song.mp3", MediaType.audio) == (False, False)


# --- ocr_metadata flow (subprocess mocked at run_ocr) ----------------------


def test_ocr_metadata_image_success(monkeypatch):
    called = {}

    def fake_run_ocr(path, **kw):
        called["path"] = path
        called["is_pdf"] = kw["is_pdf"]
        return "hello scanned text"

    monkeypatch.setattr(ocr_run, "run_ocr", fake_run_ocr)
    out = ocr_metadata(
        "/a/photo.png",
        media_type=MediaType.image,
        meta={"width": 1000, "height": 1000},
        prior_meta={},
        source_hash="abc123",
        settings=_settings(),
    )
    assert out["ocr_text"] == "hello scanned text"
    assert out["ocr_source_hash"] == "abc123"
    assert out["ocr_text_truncated"] is False
    assert called["is_pdf"] is False


def test_ocr_metadata_scanned_pdf_uses_raster_path(monkeypatch):
    seen = {}

    def fake_run_ocr(path, **kw):
        seen["is_pdf"] = kw["is_pdf"]
        return "ocr of a scanned pdf"

    monkeypatch.setattr(ocr_run, "run_ocr", fake_run_ocr)
    out = ocr_metadata(
        "/a/scan.pdf",
        media_type=MediaType.document,
        meta={"body_text": "", "pages": 3},
        prior_meta={},
        source_hash="h1",
        settings=_settings(),
    )
    assert out["ocr_text"] == "ocr of a scanned pdf"
    assert seen["is_pdf"] is True


def test_ocr_metadata_skips_pdf_with_text_layer(monkeypatch):
    monkeypatch.setattr(
        ocr_run, "run_ocr",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not OCR")),
    )
    out = ocr_metadata(
        "/a/text.pdf",
        media_type=MediaType.document,
        meta={"body_text": "x" * 500},
        prior_meta={},
        source_hash="h",
        settings=_settings(),
    )
    assert out == {}


def test_ocr_metadata_cache_hit_skips_tesseract(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("run_ocr must not be called on a cache hit")

    monkeypatch.setattr(ocr_run, "run_ocr", boom)
    prior = {"ocr_text": "cached", "ocr_source_hash": "same-hash"}
    out = ocr_metadata(
        "/a/photo.png",
        media_type=MediaType.image,
        meta={"width": 10, "height": 10},
        prior_meta=prior,
        source_hash="same-hash",
        settings=_settings(),
    )
    assert out == {}


def test_ocr_metadata_reocr_when_hash_changed(monkeypatch):
    monkeypatch.setattr(ocr_run, "run_ocr", lambda *a, **k: "fresh text")
    prior = {"ocr_text": "stale", "ocr_source_hash": "old-hash"}
    out = ocr_metadata(
        "/a/photo.png",
        media_type=MediaType.image,
        meta={"width": 10, "height": 10},
        prior_meta=prior,
        source_hash="new-hash",
        settings=_settings(),
    )
    assert out["ocr_text"] == "fresh text"
    assert out["ocr_source_hash"] == "new-hash"


def test_ocr_metadata_pixel_ceiling_skips(monkeypatch):
    monkeypatch.setattr(
        ocr_run, "run_ocr",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("over pixel cap")),
    )
    out = ocr_metadata(
        "/a/huge.png",
        media_type=MediaType.image,
        meta={"width": 10_000, "height": 10_000},
        prior_meta={},
        source_hash="h",
        settings=_settings(),
    )
    assert out == {}


def test_ocr_metadata_timeout_degrades(monkeypatch):
    def timeout(*a, **k):
        raise OcrError("tesseract timed out after 120s")

    monkeypatch.setattr(ocr_run, "run_ocr", timeout)
    out = ocr_metadata(
        "/a/photo.png",
        media_type=MediaType.image,
        meta={"width": 10, "height": 10},
        prior_meta={},
        source_hash="h",
        settings=_settings(),
    )
    assert "_ocr_error" in out
    assert "ocr_text" not in out


def test_ocr_metadata_output_cap(monkeypatch):
    monkeypatch.setattr(ocr_run, "run_ocr", lambda *a, **k: "y" * 50)
    out = ocr_metadata(
        "/a/photo.png",
        media_type=MediaType.image,
        meta={"width": 10, "height": 10},
        prior_meta={},
        source_hash="h",
        settings=_settings(ocr_max_chars=50),
    )
    assert out["ocr_text_truncated"] is True


def test_ocr_metadata_ineligible_returns_empty():
    out = ocr_metadata(
        "/a/song.mp3",
        media_type=MediaType.audio,
        meta={},
        prior_meta={},
        source_hash="h",
        settings=_settings(),
    )
    assert out == {}


# --- engine primitives (subprocess mocked at subprocess.run) ---------------


def test_run_tesseract_argv_and_text(monkeypatch):
    captured = {}

    class P:
        returncode = 0
        stdout = b"recognised words"
        stderr = b""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return P()

    monkeypatch.setattr(ocr_mod.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)
    text = ocr_mod.run_tesseract("/a/img.png", lang="eng", tesseract_path="tesseract")
    assert text == "recognised words"
    assert captured["argv"][1:] == ["/a/img.png", "stdout", "-l", "eng", "--psm", "3"]


def test_run_tesseract_missing_binary(monkeypatch):
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda b: None)
    with pytest.raises(OcrError):
        ocr_mod.run_tesseract("/a/img.png")


def test_run_tesseract_timeout(monkeypatch):
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda b: "/usr/bin/tesseract")

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)
    with pytest.raises(OcrError):
        ocr_mod.run_tesseract("/a/img.png", timeout_s=1.0)


def test_run_ocr_pdf_rasterizes_and_joins(monkeypatch):
    monkeypatch.setattr(
        ocr_mod, "rasterize_pdf", lambda pdf, out, **kw: ["/tmp/p1.png", "/tmp/p2.png"]
    )
    pages_ocrd = []

    def fake_tess(img, **kw):
        pages_ocrd.append(img)
        return f"text-{img[-6:-4]}"

    monkeypatch.setattr(ocr_mod, "run_tesseract", fake_tess)
    out = ocr_mod.run_ocr("/a/scan.pdf", is_pdf=True, max_chars=1000)
    assert pages_ocrd == ["/tmp/p1.png", "/tmp/p2.png"]
    assert "text-p1" in out and "text-p2" in out


def test_normalize_ocr_text_strips_controls_and_caps():
    raw = "hi\x00there\t\tworld" + "z" * 100
    out = _normalize_ocr_text(raw, 20)
    assert "\x00" not in out
    assert len(out) <= 20
    assert out.startswith("hithere world")
