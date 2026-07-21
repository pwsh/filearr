"""file_group taxonomy: the additive, extension-derived similarity layer finer
than MediaType.

Layers exercised:
* **pure taxonomy** — ``detect_group`` (single ext, compound ``tar.*``, case, unknown
  -> other, dotfiles), registry integrity (no dup exts, every group id real, every
  ``media_types.EXT_MAP`` ext lands in a real group), and helper functions.
* **projection** — ``search.build_doc`` emits ``file_group``; a sidecar inherits its
  OWN extension's group (not the parent's).
* **settings wiring** — ``file_group`` is filterable AND facet-searchable (a
  low-cardinality controlled vocab), the opposite of the hash attributes.
* **search filter** — ``build_filters`` turns repeatable ``file_group`` into an OR
  clause, validated against the controlled vocabulary (injection-safe).
* **reference endpoint** — ``GET /system/file-groups`` returns the FROZEN shape.
* **doc drift** — the committed reference markdown equals the generator output.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from filearr import file_groups as fg
from filearr.file_groups import (
    EXT_GROUP_MAP,
    FILE_GROUPS,
    category_for_group,
    detect_group,
    extensions_for_group,
    reference_doc_path,
    registry_payload,
    render_reference_markdown,
)
from filearr.meili_ops import (
    FACET_SEARCH_CANDIDATES,
    FACET_SEARCH_DISABLED,
    FILTERABLE_ATTRIBUTES,
)
from filearr.models import Item, ItemStatus


# --------------------------------------------------------------------------- #
# Pure taxonomy                                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,expected",
    [
        ("holiday.jpg", "raster-photo"),
        ("IMG_0001.CR2", "raw-photo"),          # case-insensitive, Canon RAW
        ("song.flac", "audio-lossless"),
        ("song.mp3", "audio-lossy"),
        ("book.m4b", "audiobook"),
        ("track.wav", "audio-lossless"),        # PCM sample -> lossless group
        ("movie.mkv", "video"),
        ("stream.ts", "video"),                 # transport stream, not TypeScript
        ("app.tsx", "source-code"),             # TS still source
        ("notes.md", "markup"),
        ("main.py", "source-code"),
        ("deploy.sh", "script"),
        ("data.json", "config-data"),
        ("model.stl", "3d-model"),
        ("part.step", "cad"),
        ("subs.srt", "subtitle"),
        ("book.epub", "ebook"),
        ("issue1.cbz", "comic"),
        ("slides.pptx", "presentation"),
        ("sheet.xlsx", "spreadsheet"),
        ("private.key", "certificate-key"),     # key -> cert, not Keynote
        ("archive.zip", "archive"),
        ("ubuntu.iso", "disk-image"),
        ("pkg.deb", "package-installer"),
        ("lib.so", "executable-binary"),
        ("mail.eml", "email"),
        ("server.log", "log"),
        ("font.ttf", "font"),
        ("data.sqlite", "database"),
        ("style.css", "web-asset"),
        ("analysis.ipynb", "notebook"),
        ("session.flp", "audio-project"),
        ("list.m3u", "playlist"),
    ],
)
def test_detect_group_known(path, expected):
    assert detect_group(path) == expected


def test_detect_group_compound_tar():
    # A recognised compound ending classifies the whole file as archive.
    assert detect_group("backup.tar.gz") == "archive"
    assert detect_group("src.tar.zst") == "archive"
    assert detect_group("a.b.c.tar.xz") == "archive"
    # A non-compound double extension falls through to the last suffix.
    assert detect_group("movie.1080p.mkv") == "video"


def test_detect_group_unknown_and_dotfiles():
    assert detect_group("mystery.zzzzz") == "other"
    assert detect_group("noextension") == "other"
    assert detect_group(".bashrc") == "other"       # leading-dot, no stem
    assert detect_group(".gitignore") == "other"
    assert detect_group("") == "other"


def test_detect_group_full_paths_and_backslashes():
    assert detect_group("/data/media/a.mp4") == "video"
    assert detect_group("relative/dir/report.PDF") == "pdf"


def test_registry_shape_and_order():
    # 37 groups, 'other' last, ids match the FileGroup.id.
    ids = list(FILE_GROUPS)
    assert ids[-1] == fg.GROUP_OTHER == "other"
    assert len(ids) == len(set(ids))
    for gid, g in FILE_GROUPS.items():
        assert g.id == gid


def test_no_duplicate_extension_across_groups():
    # EXT_GROUP_MAP is a dict, but assert the AUTHORING source has no dup either
    # (the same guarantee _invert enforces at import, re-checked here explicitly).
    seen: dict[str, str] = {}
    for gid, exts in fg._GROUP_EXTENSIONS.items():
        for e in exts:
            assert e not in seen, f"{e!r} in both {seen.get(e)!r} and {gid!r}"
            seen[e] = gid
    assert seen.keys() == EXT_GROUP_MAP.keys()


def test_every_group_id_referenced_is_real():
    assert set(EXT_GROUP_MAP.values()) <= set(FILE_GROUPS)


def test_every_mapped_extension_lands_in_a_real_non_other_group():
    # Every extension the taxonomy knows classifies into a real, non-catch-all group.
    for ext, gid in EXT_GROUP_MAP.items():
        assert gid in FILE_GROUPS, f"ext {ext!r} -> unknown group {gid!r}"
        assert gid != "other"


def test_breadth_is_substantial():
    # The whole point is BREADTH — shrink 'other'. Guard against accidental gutting.
    assert len(EXT_GROUP_MAP) > 800


def test_helpers():
    # W8-B: the group's parent is its file_category (the removed media_type rollup).
    assert category_for_group("raw-photo") == "image"
    assert category_for_group("archive") == "archive"
    assert category_for_group("subtitle") == "video"
    # extensions_for_group is sorted + reflects collision resolution.
    raw = extensions_for_group("raw-photo")
    assert raw == sorted(raw)
    assert "cr2" in raw and "ptx" not in raw   # ptx went to audio-project
    assert extensions_for_group("other") == []


# --------------------------------------------------------------------------- #
# Projection (build_doc)                                                       #
# --------------------------------------------------------------------------- #
def _make_item(*, ext: str, sidecar_of=None, file_category="other", file_group="other") -> Item:
    return Item(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category=file_category, file_group=file_group,
        path=f"/data/a.{ext}",
        rel_path=f"a.{ext}",
        filename=f"a.{ext}",
        extension=ext,
        size=1,
        mtime=datetime.now(UTC),
        metadata_={},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
        sidecar_of=sidecar_of,
    )


def test_build_doc_projects_file_group():
    from filearr import search as search_mod

    doc = search_mod.build_doc(_make_item(ext="mkv", file_category="video", file_group="video"))
    assert doc["file_group"] == "video"
    assert doc["file_category"] == "video"

    # When the stored taxonomy columns are NULL (an unclassified / pre-scan row),
    # build_doc DERIVES (file_category, file_group) from the extension via the seed
    # classifier — a .zip is file_category 'archive' / file_group 'archive'.
    zip_doc = search_mod.build_doc(_make_item(ext="zip", file_category=None, file_group=None))
    assert zip_doc["file_category"] == "archive"
    assert zip_doc["file_group"] == "archive"


def test_build_doc_sidecar_gets_own_group_not_parent():
    from filearr import search as search_mod

    parent = _make_item(ext="mkv", file_category="video", file_group="video")
    sidecar = _make_item(
        ext="jpg", sidecar_of=parent.id, file_category="image", file_group="raster-photo"
    )
    sdoc = search_mod.build_doc(sidecar)
    # The sidecar's own extension decides its group (a poster.jpg is raster-photo),
    # NOT the parent video's group.
    assert sdoc["is_sidecar"] is True
    assert sdoc["file_group"] == "raster-photo"


# --------------------------------------------------------------------------- #
# Settings wiring (meili_ops)                                                  #
# --------------------------------------------------------------------------- #
def test_file_group_is_filterable_and_facet_searchable():
    assert "file_group" in FILTERABLE_ATTRIBUTES
    # low-cardinality controlled vocab => a genuine facet-search candidate,
    # and explicitly NOT in the facet-search-disabled (hash/numeric) set.
    assert "file_group" in FACET_SEARCH_CANDIDATES
    assert "file_group" not in FACET_SEARCH_DISABLED


# --------------------------------------------------------------------------- #
# Search filter construction (build_filters)                                   #
# --------------------------------------------------------------------------- #
def test_build_filters_file_group_single_and_multi():
    from filearr.api.search import build_filters

    f = build_filters(file_group=["raw-photo"])
    assert "(file_group = 'raw-photo')" in f

    # repeatable => OR
    f2 = build_filters(file_group=["pdf", "document-office"])
    assert "(file_group = 'pdf' OR file_group = 'document-office')" in f2


def test_build_filters_file_group_unknown_dropped():
    from filearr.api.search import build_filters

    # unknown / hostile values are validated out (injection-safe controlled vocab)
    f = build_filters(file_group=["raw-photo", "not-a-group", "x' OR '1'='1"])
    clause = next(c for c in f if c.startswith("(file_group"))
    assert clause == "(file_group = 'raw-photo')"

    # all-unknown => no file_group clause at all
    f2 = build_filters(file_group=["nope", "zzz"])
    assert not any("file_group" in c for c in f2)


def test_build_filters_no_file_group_is_noop():
    from filearr.api.search import build_filters

    assert not any("file_group" in c for c in build_filters())


def test_file_group_joins_saved_search_vocabulary():
    # SEARCH_PARAM_NAMES is derived from the endpoint signature; the new param
    # must auto-extend the saved-search vocabulary.
    from filearr.api.search import SEARCH_PARAM_NAMES

    assert "file_group" in SEARCH_PARAM_NAMES


# --------------------------------------------------------------------------- #
# Reference endpoint — FROZEN contract                                         #
# --------------------------------------------------------------------------- #
def test_registry_payload_frozen_shape():
    payload = registry_payload()
    assert isinstance(payload, list) and payload
    for entry in payload:
        assert set(entry) == {"id", "label", "file_category", "description", "extensions"}
        assert isinstance(entry["extensions"], list)
        assert isinstance(entry["file_category"], str)
    # order == registry order; ids unique
    assert [e["id"] for e in payload] == list(FILE_GROUPS)


@pytest.mark.asyncio
async def test_file_groups_endpoint(monkeypatch):
    # No DB needed: file_groups() body touches no session, and require_scope
    # short-circuits with auth off. Override get_session so the injected dep
    # resolves without a real Postgres.
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    async def _no_session():
        yield None

    app = create_app()
    app.dependency_overrides[get_session] = _no_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/system/file-groups")
    app.dependency_overrides.clear()

    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list) and len(data) == len(FILE_GROUPS)
    first = data[0]
    assert set(first) == {"id", "label", "file_category", "description", "extensions"}
    # matches the pure registry payload exactly (same source of truth)
    assert data == registry_payload()
    # a rescue group is present with its parent file_category
    archive = next(e for e in data if e["id"] == "archive")
    assert archive["file_category"] == "archive"
    assert "zip" in archive["extensions"]


# --------------------------------------------------------------------------- #
# Generated-doc drift guard                                                    #
# --------------------------------------------------------------------------- #
def test_reference_doc_matches_generator():
    path = reference_doc_path()
    committed = path.read_text(encoding="utf-8")
    generated = render_reference_markdown()
    assert committed == generated, (
        "docs-site/reference/file-extension-groups.md is stale — regenerate with "
        "`python -c 'from filearr.file_groups import write_reference_doc; "
        "write_reference_doc()'`"
    )
