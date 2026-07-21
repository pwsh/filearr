"""P3-T5 — document body-text extraction, decompression guard, and the Meili
snippet/highlight wiring.

Pure/unit coverage (no Meili server, no DB) except where a fake Meili client
exercises the /search endpoint shape. Verifies:
  * pdf/docx/txt body text happy paths;
  * per-page partial extraction (one bad page loses only that page);
  * both caps (store char cap + Meili index cap);
  * the zip-bomb guard rejects a crafted ratio-bomb BEFORE any parser opens it
    (python-docx is monkeypatched to fail the test if it is ever called);
  * build_doc projects a capped body_text and body_text is the LAST searchable
    attribute (attribute-ranking: name matches outrank body matches);
  * /search requests highlighting/cropping and passes _formatted through as a
    SAFE snippet/highlight shape (raw body_text + _formatted stripped from hits);
  * encrypted PDFs yield no body text.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from filearr.tasks.documents import (
    DocumentError,
    _normalize_body_text,
    extract_body,
    extract_docx,
    guard_decompression,
)

BIG = 1 << 30


# --------------------------------------------------------------- happy paths
def test_pdf_body_text(sample_pdf_text):
    meta = extract_body(str(sample_pdf_text), max_chars=10_000, max_bytes=BIG)
    assert "body_text" in meta
    assert "Hello body text extraction world" in meta["body_text"]
    assert meta["body_text_truncated"] is False


def test_docx_body_text(sample_docx_text):
    meta = extract_body(str(sample_docx_text), max_chars=10_000, max_bytes=BIG)
    assert "quick brown fox" in meta["body_text"]
    assert "wombat marsupial" in meta["body_text"]
    assert meta["body_text_truncated"] is False


def test_txt_body_text(sample_txt):
    meta = extract_body(str(sample_txt), max_chars=10_000, max_bytes=BIG)
    # whitespace (incl. the tab + newlines) collapses to single spaces.
    assert meta["body_text"] == "plain text notes about aardvarks and bandicoots"
    assert meta["body_text_truncated"] is False


def test_xlsx_has_no_body_text(sample_xlsx):
    # Spreadsheets get structure only, never body text.
    assert extract_body(str(sample_xlsx), max_chars=10_000, max_bytes=BIG) == {}


def test_unsupported_ext_no_body(tmp_path):
    f = tmp_path / "book.epub"
    f.write_bytes(b"not an epub")
    assert extract_body(str(f), max_chars=100, max_bytes=BIG) == {}


# ----------------------------------------------------- partial extraction
def test_pdf_page_failure_is_partial(sample_pdf_text, tmp_path, monkeypatch):
    """A page whose text extraction raises loses ONLY that page — the rest still
    contribute (mirrors the property extractor's per-field discipline)."""
    import pypdf
    from pypdf import PdfReader, PdfWriter
    from pypdf._page import PageObject

    # Build a 2-page PDF (same text on both) by duplicating the text page.
    src = PdfReader(str(sample_pdf_text))
    w = PdfWriter()
    w.append(src)
    w.append(src)
    two = tmp_path / "two.pdf"
    with open(two, "wb") as fh:
        w.write(fh)

    orig = PageObject.extract_text
    state = {"n": 0}

    def flaky(self, *a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("bad page")  # first page unreadable
        return orig(self, *a, **k)

    monkeypatch.setattr(pypdf._page.PageObject, "extract_text", flaky)

    meta = extract_body(str(two), max_chars=10_000, max_bytes=BIG)
    # Page 1 was lost, page 2 survived — body still present, no exception.
    assert "Hello body text extraction world" in meta["body_text"]


# --------------------------------------------------------------- the two caps
def test_store_char_cap_truncates(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 5000, encoding="utf-8")
    meta = extract_body(str(big), max_chars=100, max_bytes=BIG)
    assert len(meta["body_text"]) == 100
    assert meta["body_text_truncated"] is True


def test_index_cap_in_build_doc(monkeypatch):
    """build_doc caps what it projects to Meili at body_text_index_chars, which is
    SMALLER than the stored body_text_max_chars (index-bloat control)."""
    import uuid
    from datetime import UTC, datetime

    from filearr import search as search_mod
    from filearr.config import get_settings
    from filearr.models import Item, ItemStatus

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "body_text_index_chars", 50)

    item = Item(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="document", file_group="document-text",
        path="/data/d.pdf",
        rel_path="d.pdf",
        filename="d.pdf",
        extension="pdf",
        size=1,
        mtime=datetime.now(UTC),
        metadata_={"body_text": "y" * 4000, "body_text_truncated": True},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
    )
    doc = search_mod.build_doc(item)
    assert len(doc["body_text"]) == 50
    get_settings.cache_clear()


def test_build_doc_omits_empty_body_text():
    import uuid
    from datetime import UTC, datetime

    from filearr import search as search_mod
    from filearr.models import Item, ItemStatus

    item = Item(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="video", file_group="video",
        path="/data/a.mkv",
        rel_path="a.mkv",
        filename="a.mkv",
        extension="mkv",
        size=1,
        mtime=datetime.now(UTC),
        metadata_={},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
    )
    assert search_mod.build_doc(item)["body_text"] is None


# --------------------------------------------------------- searchable ordering
def test_body_text_is_last_searchable():
    from filearr import search as search_mod
    from filearr.meili_ops import SEARCHABLE_ATTRIBUTES

    # body_text sits at the TAIL of searchableAttributes so a name/title/tags hit
    # outranks a deep body match (attribute-ranking rule). P3-T13 appended
    # ``archive_members`` after it (an even-lower-priority match), so body_text is
    # now the SECOND-to-last attribute and must still come AFTER every name field.
    assert SEARCHABLE_ATTRIBUTES[-1] == "archive_members"
    assert SEARCHABLE_ATTRIBUTES[-2] == "body_text"
    assert SEARCHABLE_ATTRIBUTES.index("body_text") > SEARCHABLE_ATTRIBUTES.index("filename")
    assert "body_text" in search_mod.SEARCHABLE
    # never a filter/sort target (it is free text, not a facet).
    assert "body_text" not in search_mod.FILTERABLE
    assert "body_text" not in search_mod.SORTABLE


# ------------------------------------------------------------ zip-bomb guard
def test_guard_rejects_ratio_bomb(zipbomb_docx):
    with pytest.raises(DocumentError, match="decompression guard"):
        guard_decompression(str(zipbomb_docx))


def test_guard_rejects_total_ceiling(zipbomb_docx):
    # A tiny total ceiling trips even below the ratio floor.
    with pytest.raises(DocumentError, match="decompression guard"):
        guard_decompression(str(zipbomb_docx), decompressed_max=1024, ratio_limit=1e9)


def test_guard_passes_normal_docx(sample_docx_text):
    # A legitimate small office file is NOT rejected.
    guard_decompression(str(sample_docx_text))


def test_body_bomb_rejected_before_parse(zipbomb_docx, monkeypatch):
    """The crafted bomb is rejected from the central directory BEFORE python-docx
    opens it: Document is monkeypatched to fail the test if it is ever called."""
    import docx

    def _boom(*a, **k):
        raise AssertionError("python-docx must NOT be called on a rejected bomb")

    monkeypatch.setattr(docx, "Document", _boom)
    with pytest.raises(DocumentError, match="decompression guard"):
        extract_body(str(zipbomb_docx), max_chars=1000, max_bytes=BIG)


def test_property_extractor_bomb_rejected_before_parse(zipbomb_docx, monkeypatch):
    import docx

    def _boom(*a, **k):
        raise AssertionError("python-docx must NOT be called on a rejected bomb")

    monkeypatch.setattr(docx, "Document", _boom)
    with pytest.raises(DocumentError, match="decompression guard"):
        extract_docx(str(zipbomb_docx), max_bytes=BIG)


# ----------------------------------------------------------- normalization
def test_normalize_strips_control_chars_and_collapses_ws():
    raw = "a\x00b\x07c\t d\n\n e\x1b[31mf"
    out, truncated = _normalize_body_text(raw, 1000)
    # NUL/BEL/ESC control bytes dropped (no space inserted); tab/newlines collapse
    # to a single space. The printable "[31m" after the stripped ESC is kept.
    assert out == "abc d e[31mf"
    assert truncated is False


def test_normalize_reports_truncation():
    out, truncated = _normalize_body_text("word " * 100, 20)
    assert len(out) <= 20
    assert truncated is True


# ------------------------------------------------------------ encrypted PDF
def test_encrypted_pdf_yields_no_body(encrypted_pdf):
    assert extract_body(str(encrypted_pdf), max_chars=1000, max_bytes=BIG) == {}


# ------------------------------------------------- /search highlight wiring
class _FakeSearchIndex:
    def __init__(self, sink, hits):
        self._sink = sink
        self._hits = hits

    async def search(self, q, **kwargs):
        self._sink["q"] = q
        self._sink.update(kwargs)
        return SimpleNamespace(
            hits=self._hits,
            estimated_total_hits=len(self._hits),
            facet_distribution={},
        )


class _FakeClient:
    def __init__(self, sink, hits):
        self._sink = sink
        self._hits = hits

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def index(self, name):
        return _FakeSearchIndex(self._sink, self._hits)


def _make_app(monkeypatch, hits):
    from filearr.config import get_settings
    from filearr.main import create_app

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    sink: dict = {}
    monkeypatch.setattr(
        "filearr.api.search.client", lambda: _FakeClient(sink, hits)
    )
    app = create_app()
    return httpx.ASGITransport(app=app), sink


@pytest.mark.asyncio
async def test_search_requests_highlight_and_crop(monkeypatch):
    transport, sink = _make_app(monkeypatch, [])
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=fox")
    assert r.status_code == 200, r.text
    assert sink["attributes_to_highlight"] == ["body_text", "title", "filename"]
    assert sink["attributes_to_crop"] == ["body_text"]
    assert sink["crop_length"] == 30
    assert sink["highlight_pre_tag"] == "<em>"
    assert sink["highlight_post_tag"] == "</em>"


@pytest.mark.asyncio
async def test_search_snippet_shape_is_safe(monkeypatch):
    hit = {
        "id": "1",
        "title": "Report",
        "filename": "report.pdf",
        "body_text": "y" * 500,  # full (index-capped) body must NOT leak
        "_formatted": {
            "title": "<em>Report</em>",
            "filename": "report.pdf",
            "body_text": "...the quick <em>fox</em> jumped...",
        },
    }
    transport, _ = _make_app(monkeypatch, [hit])
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=fox")
    assert r.status_code == 200, r.text
    h = r.json()["hits"][0]
    # snippet = cropped body; highlight carries title/filename markers.
    assert h["snippet"] == "...the quick <em>fox</em> jumped..."
    assert h["highlight"]["title"] == "<em>Report</em>"
    # raw body_text + the whole _formatted block are stripped from the hit.
    assert "body_text" not in h
    assert "_formatted" not in h


@pytest.mark.asyncio
async def test_search_hit_without_formatted_has_no_snippet(monkeypatch):
    hit = {"id": "2", "title": "Plain", "filename": "p.mkv"}
    transport, _ = _make_app(monkeypatch, [hit])
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=x")
    assert r.status_code == 200, r.text
    h = r.json()["hits"][0]
    assert "snippet" not in h
    assert "highlight" not in h
