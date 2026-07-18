"""Unit tests for the T6 extractors: model3d (trimesh), document/spreadsheet
(pypdf/python-docx/openpyxl) and audiobook chapters (mutagen).

Every parser must (a) populate expected metadata on a good file and (b) degrade
to a raised *typed* error (which the dispatch turns into ``_extract_error``) on a
corrupt/oversized file — never let an arbitrary exception escape.
"""

from __future__ import annotations

import pytest

from filearr.tasks.audiobook import AudiobookError, extract_chapters
from filearr.tasks.documents import (
    DocumentError,
    extract_docx,
    extract_pdf,
    extract_xlsx,
)
from filearr.tasks.documents import extract_document as doc_dispatch
from filearr.tasks.model3d import Model3DError
from filearr.tasks.model3d import extract_model3d as model_extract

# trimesh/pypdf/python-docx/openpyxl are pinned runtime deps (pyproject), so they
# are always importable in a properly provisioned env — no importorskip needed.

BIG = 1 << 30  # 1 GiB — effectively unbounded for the tiny fixtures


# --------------------------------------------------------------------- model3d

def test_model3d_stl_cube(stl_cube):
    meta = model_extract(str(stl_cube), max_bytes=BIG)
    assert meta["triangles"] == 12
    assert meta["mesh_count"] == 1
    assert meta["file_format"] == "stl"
    # box was 2x3x4 — extents come back as the bbox dims (order-independent)
    assert sorted(meta["bbox"]) == [2.0, 3.0, 4.0]
    assert meta["bbox_volume"] == pytest.approx(24.0)
    assert isinstance(meta["watertight"], bool)


def test_model3d_glb_scene_aggregates(glb_scene):
    meta = model_extract(str(glb_scene), max_bytes=BIG)
    assert meta["mesh_count"] == 2
    assert meta["triangles"] > 12  # box + sphere
    assert meta["vertices"] > 0
    assert "bbox" in meta


def test_model3d_size_ceiling(stl_cube):
    with pytest.raises(Model3DError, match="too large"):
        model_extract(str(stl_cube), max_bytes=10)


def test_model3d_corrupt_raises(corrupt_stl):
    with pytest.raises(Model3DError):
        model_extract(str(corrupt_stl), max_bytes=BIG)


def test_model3d_unsupported_ext_is_marker(tmp_path):
    f = tmp_path / "part.step"
    f.write_bytes(b"ISO-10303-21;")
    meta = model_extract(str(f), max_bytes=BIG)
    assert meta == {"unsupported": True}


# --------------------------------------------------------------------- PDF

def test_pdf_properties(sample_pdf):
    meta = extract_pdf(str(sample_pdf), max_bytes=BIG)
    assert meta["pages"] == 2
    assert meta["title"] == "The Title"
    assert meta["author"] == "The Author"
    assert meta["encrypted"] is False


def test_pdf_encrypted_flagged_without_password(encrypted_pdf):
    meta = extract_pdf(str(encrypted_pdf), max_bytes=BIG)
    assert meta["encrypted"] is True
    # locked with a real password: no page/metadata leaks, but no crash
    assert "pages" not in meta


def test_pdf_corrupt_raises(corrupt_pdf):
    with pytest.raises(DocumentError):
        extract_pdf(str(corrupt_pdf), max_bytes=BIG)


def test_pdf_size_ceiling(sample_pdf):
    with pytest.raises(DocumentError, match="too large"):
        extract_pdf(str(sample_pdf), max_bytes=5)


# --------------------------------------------------------------------- DOCX

def test_docx_properties(sample_docx):
    meta = extract_docx(str(sample_docx), max_bytes=BIG)
    assert meta["title"] == "Docx Title"
    assert meta["author"] == "Docx Author"
    assert meta["paragraphs"] == 2


def test_docx_corrupt_raises(corrupt_docx):
    with pytest.raises(DocumentError):
        extract_docx(str(corrupt_docx), max_bytes=BIG)


# --------------------------------------------------------------------- XLSX

def test_xlsx_properties(sample_xlsx):
    meta = extract_xlsx(str(sample_xlsx), max_bytes=BIG)
    assert meta["sheets"] == ["Alpha", "Beta"]
    assert meta["sheet_count"] == 2
    assert meta["title"] == "Xlsx Title"
    assert meta["author"] == "Xlsx Author"


def test_xlsx_corrupt_raises(corrupt_xlsx):
    with pytest.raises(DocumentError):
        extract_xlsx(str(corrupt_xlsx), max_bytes=BIG)


def test_document_dispatch_unsupported(tmp_path):
    f = tmp_path / "book.epub"
    f.write_bytes(b"not really an epub")
    assert doc_dispatch(str(f), max_bytes=BIG) == {"unsupported": True}


# --------------------------------------------------------------------- m4b

@pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None, reason="ffmpeg not on PATH"
)
def test_m4b_chapters(sample_m4b):
    meta = extract_chapters(str(sample_m4b))
    assert meta["chapter_count"] == 2
    titles = [c["title"] for c in meta["chapters"]]
    assert titles == ["Chapter One", "Chapter Two"]
    assert meta["chapters"][0]["start"] == pytest.approx(0.0)
    assert meta["chapters"][1]["start"] == pytest.approx(1.0, abs=0.05)


def test_m4b_corrupt_raises(corrupt_m4b):
    with pytest.raises(AudiobookError):
        extract_chapters(str(corrupt_m4b))
