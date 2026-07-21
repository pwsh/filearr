"""Search projection: sidecars are marked + filterable, and the search endpoint
excludes them by default. Meili is mocked (no real server)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from meilisearch_python_sdk.models.settings import MeilisearchSettings

from filearr import search as search_mod
from filearr.models import Item, ItemStatus


def _make_item(sidecar_of=None):
    return Item(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="image", file_group="raster-photo" if sidecar_of else "video",
        path="/data/a.jpg" if sidecar_of else "/data/a.mkv",
        rel_path="a.jpg" if sidecar_of else "a.mkv",
        filename="a.jpg" if sidecar_of else "a.mkv",
        extension="jpg" if sidecar_of else "mkv",
        size=1,
        mtime=datetime.now(UTC),
        metadata_={},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
        sidecar_of=sidecar_of,
    )


def test_build_doc_marks_sidecar():
    parent = _make_item()
    doc = search_mod.build_doc(parent)
    assert doc["is_sidecar"] is False
    assert doc["sidecar_of"] is None

    sidecar = _make_item(sidecar_of=parent.id)
    sdoc = search_mod.build_doc(sidecar)
    assert sdoc["is_sidecar"] is True
    assert sdoc["sidecar_of"] == str(parent.id)


def test_is_sidecar_is_filterable():
    assert "is_sidecar" in search_mod.FILTERABLE
    assert "sidecar_of" in search_mod.FILTERABLE


@pytest.mark.asyncio
async def test_ensure_index_sets_sidecar_filterable(monkeypatch):
    index = MagicMock()
    # FIX-2: ensure_index enforces primary_key == 'id'; a correct pk is a no-op.
    index.primary_key = "id"
    # fresh index -> defaults so settings_drift is non-empty and the apply fires
    index.get_settings = AsyncMock(return_value=MeilisearchSettings())
    index.update_searchable_attributes = AsyncMock()
    index.update_filterable_attributes = AsyncMock()
    index.update_sortable_attributes = AsyncMock()
    index.update_ranking_rules = AsyncMock()
    index.update_typo_tolerance = AsyncMock()
    index.update_faceting = AsyncMock()
    index.update_search_cutoff_ms = AsyncMock()

    fake_client = MagicMock()
    fake_client.get_or_create_index = AsyncMock(return_value=index)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(search_mod, "client", lambda: fake_client)
    await search_mod.ensure_index()

    # filterable attributes are now object-form; sidecar fields still present
    filt = index.update_filterable_attributes.call_args.args[0]
    patterns = {p for fa in filt for p in fa.attribute_patterns}
    assert "is_sidecar" in patterns
    assert "sidecar_of" in patterns
    # typo tolerance still uses a typed model, not a dict
    tt = index.update_typo_tolerance.call_args.args[0]
    assert not isinstance(tt, dict)


def test_search_filters_exclude_sidecars_by_default():
    from filearr.api.search import build_filters

    # default → is_sidecar = false appended
    assert "is_sidecar = false" in build_filters()

    # include_sidecars=True → no sidecar exclusion
    assert "is_sidecar = false" not in build_filters(include_sidecars=True)

    # sidecar_of=<id> → filter by that parent, no exclusion
    pid = str(uuid.uuid4())
    f = build_filters(sidecar_of=pid)
    assert f"sidecar_of = '{pid}'" in f
    assert "is_sidecar = false" not in f

    # combined with other filters, exclusion still applied
    f2 = build_filters(file_category=["video"], status="active")
    assert any("file_category = 'video'" in c for c in f2)
    assert "is_sidecar = false" in f2
