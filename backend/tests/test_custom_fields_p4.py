"""P4-T3 (custom-field CRUD) + P4-T4 (user_metadata edit validation).

CRUD matrix: create / invalid-type 422 / reserved-name 422 / reserved-prefix
422 / duplicate 409 / immutable name+data_type 422 / soft-delete leaves values.
Validation: wrong-type structured 422, valid passes, unregistered passthrough,
per-library applicability, deleted field behaves as unregistered — on both the
single PATCH and the batch path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, Library, MediaType

BACKEND_DIR = Path(__file__).resolve().parent.parent
CF = "/api/v1/custom-fields"
ITEMS = "/api/v1/items"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def ctx(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM item_versions"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM custom_fields"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    # A successful metadata write defers an index-sync job; the procrastinate
    # queue is not wired to this throwaway DB, so stub the defer to a no-op.
    import filearr.api.items as items_mod

    async def _noop_defer(ids):
        return None

    monkeypatch.setattr(items_mod, "defer_index_sync", _noop_defer)

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def _mk(client, name="rating", data_type="integer", **extra):
    """POST a custom field; return the raw response."""
    body = {"name": name, "label": name.upper(), "data_type": data_type, **extra}
    return await client.post(CF, json=body)


async def _mk_id(client, name="rating", data_type="integer", **extra) -> str:
    r = await _mk(client, name, data_type, **extra)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_library(maker, name="L", root="/data/l") -> str:
    async with maker() as s:
        lib = Library(name=name, root_path=root)
        s.add(lib)
        await s.commit()
        return str(lib.id)


async def _seed_item(maker, library_id, rel="a.pdf", user_metadata=None) -> str:
    async with maker() as s:
        item = Item(
            library_id=library_id,
            media_type=MediaType.document,
            path=f"/data/l/{rel}",
            rel_path=rel,
            filename=rel,
            extension=".pdf",
            size=1,
            mtime=datetime.now(UTC),
            user_metadata=user_metadata or {},
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def _um(maker, item_id) -> dict:
    async with maker() as s:
        row = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
        return row.user_metadata


# --------------------------------------------------------------------------- #
# P4-T3 — CRUD
# --------------------------------------------------------------------------- #
async def test_create_and_list(ctx):
    client, _ = ctx
    r = await _mk(client, "Rating", "integer")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "rating"  # normalised (lowercased)
    assert body["data_type"] == "integer"
    assert body["library_ids"] == [] and body["applies_to"] == []

    lst = await client.get(CF)
    assert lst.status_code == 200
    assert [f["name"] for f in lst.json()] == ["rating"]


async def test_create_invalid_data_type_422(ctx):
    client, _ = ctx
    r = await _mk(client, "x", "colour")
    assert r.status_code == 422
    assert "data_type" in r.text


@pytest.mark.parametrize("bad", ["genre", "mtime", "user_metadata", "id"])
async def test_create_reserved_core_name_422(ctx, bad):
    client, _ = ctx
    r = await _mk(client, bad, "string")
    assert r.status_code == 422, r.text
    assert "reserved" in r.text or "collides" in r.text


@pytest.mark.parametrize("bad", ["cf_x", "_hidden", "cf_rating"])
async def test_create_reserved_prefix_422(ctx, bad):
    client, _ = ctx
    r = await _mk(client, bad, "string")
    assert r.status_code == 422, r.text


async def test_create_bad_name_shape_422(ctx):
    client, _ = ctx
    r = await _mk(client, "2cool has spaces", "string")
    assert r.status_code == 422


async def test_duplicate_name_409(ctx):
    client, _ = ctx
    assert (await _mk(client, "shelf", "string")).status_code == 201
    dup = await _mk(client, "Shelf", "string")  # case-normalised collision
    assert dup.status_code == 409, dup.text


async def test_patch_mutable_fields(ctx):
    client, _ = ctx
    fid = await _mk_id(client, "rating", "integer")
    r = await client.patch(
        f"{CF}/{fid}", json={"label": "Star rating", "facetable": True, "required": True}
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["label"] == "Star rating"
    assert b["facetable"] is True and b["required"] is True


@pytest.mark.parametrize(
    "payload",
    [{"name": "other"}, {"data_type": "string"}, {"name": "x", "data_type": "float"}],
)
async def test_patch_immutable_name_or_type_422(ctx, payload):
    client, _ = ctx
    fid = await _mk_id(client, "rating", "integer")
    r = await client.patch(f"{CF}/{fid}", json=payload)
    assert r.status_code == 422, r.text
    assert "cannot be changed" in r.text


async def test_patch_unknown_404(ctx):
    client, _ = ctx
    r = await client.patch(f"{CF}/{uuid.uuid4()}", json={"label": "x"})
    assert r.status_code == 404


async def test_soft_delete_leaves_user_metadata_values(ctx):
    client, maker = ctx
    lib = await _seed_library(maker)
    item_id = await _seed_item(maker, lib, user_metadata={"rating": 5, "note": "keep"})
    fid = await _mk_id(client, "rating", "integer")

    d = await client.delete(f"{CF}/{fid}")
    assert d.status_code == 204
    assert (await client.get(CF)).json() == []
    # the value under that key is untouched on the item
    assert await _um(maker, item_id) == {"rating": 5, "note": "keep"}


# --------------------------------------------------------------------------- #
# P4-T4 — value validation on write (PATCH + batch)
# --------------------------------------------------------------------------- #
async def test_patch_wrong_type_structured_422(ctx):
    client, maker = ctx
    lib = await _seed_library(maker)
    item_id = await _seed_item(maker, lib)
    await _mk_id(client, "rating", "integer")

    r = await client.patch(f"{ITEMS}/{item_id}", json={"user_metadata": {"rating": "high"}})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["field"] == "rating"
    assert "reason" in detail[0]
    assert detail[0]["field_source"] == "custom_field"
    # rejected write did not mutate the item
    assert "rating" not in await _um(maker, item_id)


async def test_patch_valid_value_passes(ctx):
    client, maker = ctx
    lib = await _seed_library(maker)
    item_id = await _seed_item(maker, lib)
    await _mk_id(client, "rating", "integer")
    r = await client.patch(f"{ITEMS}/{item_id}", json={"user_metadata": {"rating": 5}})
    assert r.status_code == 200, r.text
    assert r.json()["user_metadata"]["rating"] == 5


async def test_patch_unregistered_key_passthrough(ctx):
    client, maker = ctx
    lib = await _seed_library(maker)
    item_id = await _seed_item(maker, lib)
    payload = {"user_metadata": {"whatever": "abc", "n": 3}}
    r = await client.patch(f"{ITEMS}/{item_id}", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["user_metadata"] == {"whatever": "abc", "n": 3}


async def test_per_library_applicability(ctx):
    client, maker = ctx
    lib_a = await _seed_library(maker, name="A", root="/data/a")
    lib_b = await _seed_library(maker, name="B", root="/data/b")
    item_a = await _seed_item(maker, lib_a, rel="a.pdf")
    item_b = await _seed_item(maker, lib_b, rel="b.pdf")
    await _mk_id(client, "rating", "integer", library_ids=[lib_a])  # scoped to A

    ra = await client.patch(f"{ITEMS}/{item_a}", json={"user_metadata": {"rating": "bad"}})
    assert ra.status_code == 422, ra.text  # validated in A
    rb = await client.patch(f"{ITEMS}/{item_b}", json={"user_metadata": {"rating": "bad"}})
    assert rb.status_code == 200, rb.text  # unregistered in B -> passes
    assert rb.json()["user_metadata"]["rating"] == "bad"


async def test_deleted_field_is_unregistered(ctx):
    client, maker = ctx
    lib = await _seed_library(maker)
    item_id = await _seed_item(maker, lib)
    fid = await _mk_id(client, "rating", "integer")
    bad = {"user_metadata": {"rating": "x"}}
    assert (await client.patch(f"{ITEMS}/{item_id}", json=bad)).status_code == 422
    await client.delete(f"{CF}/{fid}")
    assert (await client.patch(f"{ITEMS}/{item_id}", json=bad)).status_code == 200


async def test_required_not_enforced_on_write(ctx):
    client, maker = ctx
    lib = await _seed_library(maker)
    item_id = await _seed_item(maker, lib)
    await _mk_id(client, "rating", "integer", required=True)
    # omitting the required field is fine (R3, display-only)
    r = await client.patch(f"{ITEMS}/{item_id}", json={"user_metadata": {"note": "hi"}})
    assert r.status_code == 200, r.text


async def test_batch_validation_and_passthrough(ctx):
    client, maker = ctx
    lib = await _seed_library(maker)
    good = await _seed_item(maker, lib, rel="good.pdf")
    bad = await _seed_item(maker, lib, rel="bad.pdf")
    await _mk_id(client, "rating", "integer")

    r = await client.post(
        f"{ITEMS}/batch",
        json={
            good: {"user_metadata": {"rating": 4, "adhoc": "ok"}},
            bad: {"user_metadata": {"rating": "nope"}},
        },
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[good] == "ok"
    assert isinstance(results[bad], dict)
    assert results[bad]["error"] == "validation"
    assert results[bad]["detail"][0]["field"] == "rating"

    assert await _um(maker, good) == {"rating": 4, "adhoc": "ok"}
    assert await _um(maker, bad) == {}  # rejected item unchanged
