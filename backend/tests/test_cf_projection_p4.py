"""P4-T6 — dynamic custom-field Meili projection + rebuild-and-swap settings."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from filearr import search as search_mod
from filearr.custom_fields import CustomFieldDef, project_custom_fields_to_meili
from filearr.meili_ops import FACET_SEARCH_DISABLED, FILTERABLE_ATTRIBUTES
from filearr.models import Item, ItemStatus, MediaType


def _item(meta=None, user=None) -> Item:
    return Item(
        library_id=None,
        media_type=MediaType.image,
        status=ItemStatus.active,
        path="/data/x.jpg",
        rel_path="x.jpg",
        filename="x.jpg",
        extension=".jpg",
        size=10,
        mtime=datetime.now(UTC),
        metadata_=meta or {},
        user_metadata=user or {},
        tags=[],
    )


def test_only_facetable_or_sortable_fields_projected():
    defs = [
        CustomFieldDef(name="rating", label="Rating", data_type="integer", facetable=True),
        CustomFieldDef(name="mood", label="Mood", data_type="string", sortable=True),
        CustomFieldDef(name="note", label="Note", data_type="string"),
    ]
    eff = {"rating": 5, "mood": "calm", "note": "private"}
    out = project_custom_fields_to_meili(eff, defs)
    assert out == {"cf_rating": 5, "cf_mood": "calm"}
    assert "cf_note" not in out


def test_absent_and_null_values_are_skipped():
    defs = [CustomFieldDef(name="rating", label="R", data_type="integer", facetable=True)]
    assert project_custom_fields_to_meili({}, defs) == {}
    assert project_custom_fields_to_meili({"rating": None}, defs) == {}


def test_date_field_coerced_to_stable_epoch_int_regardless_of_form():
    d = CustomFieldDef(name="shot_on", label="Shot", data_type="date", facetable=True)
    iso = project_custom_fields_to_meili({"shot_on": "2024-05-01"}, [d])["cf_shot_on"]
    iso_dt = project_custom_fields_to_meili({"shot_on": "2024-05-01T00:00:00"}, [d])["cf_shot_on"]
    epoch = project_custom_fields_to_meili({"shot_on": iso}, [d])["cf_shot_on"]
    assert isinstance(iso, int) and isinstance(iso_dt, int) and isinstance(epoch, int)
    assert iso == iso_dt == epoch


def test_unparseable_date_dropped_not_projected_as_string():
    d = CustomFieldDef(name="shot_on", label="Shot", data_type="date", facetable=True)
    assert project_custom_fields_to_meili({"shot_on": "not-a-date"}, [d]) == {}


def test_build_doc_projects_effective_value_user_overlay_wins():
    defs = [CustomFieldDef(name="rating", label="R", data_type="integer", facetable=True)]
    item = _item(meta={"rating": 1}, user={"rating": 9})
    doc = search_mod.build_doc(item, defs)
    assert doc["cf_rating"] == 9


def test_build_doc_without_defs_projects_no_cf_attributes():
    item = _item(user={"rating": 9})
    doc = search_mod.build_doc(item)
    assert not any(k.startswith("cf_") for k in doc)


def test_filterable_settings_appends_cf_attributes_facet_enabled():
    filt = search_mod._filterable_settings(["cf_rating"])
    patterns = {p for f in filt for p in f.attribute_patterns}
    assert "cf_rating" in patterns
    assert patterns == set(FILTERABLE_ATTRIBUTES) | {"cf_rating"}
    cf = next(f for f in filt if f.attribute_patterns == ["cf_rating"])
    assert cf.features.facet_search is True
    assert "cf_rating" not in FACET_SEARCH_DISABLED


def test_desired_settings_includes_cf_filterable_and_sortable():
    des = search_mod._desired_settings(["cf_rating"], ["cf_mood"])
    assert "cf_rating:True" in des["filterableAttributes"]
    assert "cf_mood" in des["sortableAttributes"]


@pytest.fixture(scope="module")
def pg(module_db):
    return module_db


@pytest.fixture
async def maker(pg, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from filearr import db as db_mod
    from filearr.models import Base

    uri = pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # module-scoped pg is shared across the two async tests; clear the table
        # so each test's inserts start clean (name is UNIQUE).
        await conn.execute(text("TRUNCATE custom_fields"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    yield Session
    await engine.dispose()


async def _add_field(Session, **kw):
    from filearr.models import CustomField

    async with Session() as s:
        s.add(CustomField(**kw))
        await s.commit()


async def test_cf_index_attributes_reflect_the_table(maker):
    await _add_field(maker, name="rating", label="R", data_type="integer", facetable=True)
    await _add_field(maker, name="mood", label="M", data_type="string", sortable=True)
    await _add_field(maker, name="note", label="N", data_type="string")
    filt, sort = await search_mod._cf_index_attributes()
    assert filt == ["cf_rating"]
    assert sort == ["cf_mood"]

    defs = await search_mod.load_projection_defs()
    assert {d.name for d in defs} == {"rating", "mood"}


async def test_ensure_index_applies_cf_filterable_via_settings_path(maker, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from meilisearch_python_sdk.models.settings import (
        FilterableAttributes,
        MeilisearchSettings,
    )

    await _add_field(maker, name="rating", label="R", data_type="integer", facetable=True)

    index = MagicMock()
    index.primary_key = "id"
    index.get_settings = AsyncMock(return_value=MeilisearchSettings())
    for m in (
        "update_searchable_attributes",
        "update_filterable_attributes",
        "update_sortable_attributes",
        "update_ranking_rules",
        "update_typo_tolerance",
        "update_faceting",
        "update_search_cutoff_ms",
    ):
        setattr(index, m, AsyncMock())

    c = MagicMock()
    c.get_or_create_index = AsyncMock(return_value=index)
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(search_mod, "client", lambda: c)

    await search_mod.ensure_index()

    filt = index.update_filterable_attributes.call_args.args[0]
    assert all(isinstance(f, FilterableAttributes) for f in filt)
    patterns = {p for f in filt for p in f.attribute_patterns}
    assert "cf_rating" in patterns
