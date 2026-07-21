"""P3-T10 (duplicate awareness), P3-T12 (tag type-ahead), P3-T14 (timeline).

Layers:
* real-Postgres (pgserver + alembic) integration for the copy endpoints and the
  timeline aggregate — they are pure Postgres grouped counts (no Meili);
* a FAKE Meili client (no server) for the tag facet-search proxy, asserting the
  call shape and count-ordered passthrough.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.api import search as search_api
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Integration fixture (real Postgres) for copies + timeline                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def api(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
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


async def _mk_lib(maker, name="Lib", native_prefix=None):
    async with maker() as s:
        lib = Library(name=name, root_path="/data/l", native_prefix=native_prefix)
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(
    maker,
    library_id,
    rel_path,
    *,
    status="active",
    content_hash=None,
    quick_hash=None,
    size=100,
    mtime=None,
):
    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category="document", file_group="document-text",
            status=status,
            path=f"/data/l/{rel_path}",
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            extension="bin",
            size=size,
            mtime=mtime or datetime.now(UTC),
            content_hash=content_hash,
            quick_hash=quick_hash,
            metadata_={},
            user_metadata={},
            external_ids={},
            tags=[],
        )
        s.add(item)
        await s.commit()
        return str(item.id)


# --------------------------------------------------------------------------- #
# P3-T10 — copies endpoint                                                    #
# --------------------------------------------------------------------------- #
async def test_copies_content_hash_group_self_excluded(api):
    client, maker = api
    lib = await _mk_lib(maker, native_prefix="\\\\tower\\media")
    lib2 = await _mk_lib(maker, name="Other", native_prefix="/mnt/other")
    a = await _mk_item(maker, lib, "a.bin", content_hash="deadbeef")
    await _mk_item(maker, lib, "b.bin", content_hash="deadbeef")
    await _mk_item(maker, lib2, "sub/c.bin", content_hash="deadbeef")
    # a different-content item must never appear
    await _mk_item(maker, lib, "z.bin", content_hash="cafef00d")

    r = await client.get(f"/api/v1/items/{a}/copies")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3  # full group INCLUDING self
    assert body["match"] == "content_hash"
    assert body["capped"] is False
    ids = {c["id"] for c in body["copies"]}
    assert a not in ids  # self excluded
    assert len(body["copies"]) == 2
    # native_path resolves through the owning library's native_prefix (invariant 3)
    by_rel = {c["rel_path"]: c for c in body["copies"]}
    assert by_rel["b.bin"]["native_path"] == "\\\\tower\\media\\b.bin"
    assert by_rel["sub/c.bin"]["native_path"] == "/mnt/other/sub/c.bin"
    assert by_rel["sub/c.bin"]["library_name"] == "Other"


async def test_copies_quick_hash_fallback_when_no_content_hash(api):
    client, maker = api
    lib = await _mk_lib(maker)
    a = await _mk_item(maker, lib, "a.bin", quick_hash="q1", size=500)
    await _mk_item(maker, lib, "b.bin", quick_hash="q1", size=500)
    # same quick_hash but DIFFERENT size -> not a copy
    await _mk_item(maker, lib, "c.bin", quick_hash="q1", size=999)
    # same quick_hash+size BUT has a content_hash -> different (disjoint) partition
    await _mk_item(maker, lib, "d.bin", quick_hash="q1", size=500, content_hash="xx")

    r = await client.get(f"/api/v1/items/{a}/copies")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match"] == "quick_hash"
    assert body["count"] == 2
    assert [c["rel_path"] for c in body["copies"]] == ["b.bin"]


async def test_copies_trashed_not_counted(api):
    client, maker = api
    lib = await _mk_lib(maker)
    a = await _mk_item(maker, lib, "a.bin", content_hash="h")
    await _mk_item(maker, lib, "b.bin", content_hash="h")
    await _mk_item(maker, lib, "gone.bin", content_hash="h", status="trashed")
    r = await client.get(f"/api/v1/items/{a}/copies")
    body = r.json()
    assert body["count"] == 2
    assert {c["rel_path"] for c in body["copies"]} == {"b.bin"}


async def test_copies_no_hash_returns_empty(api):
    client, maker = api
    lib = await _mk_lib(maker)
    a = await _mk_item(maker, lib, "a.bin")  # no hashes at all
    r = await client.get(f"/api/v1/items/{a}/copies")
    body = r.json()
    assert body["id"] == a
    assert body["count"] == 1
    assert body["match"] == "none"
    assert body["copies"] == []
    assert body["capped"] is False


async def test_copies_capped_at_50(api):
    client, maker = api
    lib = await _mk_lib(maker)
    a = await _mk_item(maker, lib, "a.bin", content_hash="big")
    for i in range(60):
        await _mk_item(maker, lib, f"c{i}.bin", content_hash="big")
    r = await client.get(f"/api/v1/items/{a}/copies")
    body = r.json()
    assert body["count"] == 61  # full group incl self
    assert len(body["copies"]) == 50  # capped
    assert body["capped"] is True


async def test_copies_unknown_item_404(api):
    client, _maker = api
    r = await client.get(f"/api/v1/items/{uuid.uuid4()}/copies")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# P3-T10 — copy-counts batch                                                  #
# --------------------------------------------------------------------------- #
async def test_copy_counts_only_returns_gt_one(api):
    client, maker = api
    lib = await _mk_lib(maker)
    dup1 = await _mk_item(maker, lib, "d1.bin", content_hash="dup")
    dup2 = await _mk_item(maker, lib, "d2.bin", content_hash="dup")
    dup3 = await _mk_item(maker, lib, "d3.bin", content_hash="dup")
    uniq = await _mk_item(maker, lib, "u.bin", content_hash="uniq")
    qdup1 = await _mk_item(maker, lib, "q1.bin", quick_hash="qq", size=7)
    qdup2 = await _mk_item(maker, lib, "q2.bin", quick_hash="qq", size=7)
    nohash = await _mk_item(maker, lib, "n.bin")

    r = await client.post(
        "/api/v1/items/copy-counts",
        json={"ids": [dup1, dup2, dup3, uniq, qdup1, qdup2, nohash]},
    )
    assert r.status_code == 200, r.text
    counts = r.json()
    assert counts[dup1] == 3
    assert counts[dup2] == 3
    assert counts[dup3] == 3
    assert counts[qdup1] == 2
    assert counts[qdup2] == 2
    # unique / no-hash items are ABSENT (count == 1 not returned)
    assert uniq not in counts
    assert nohash not in counts


async def test_copy_counts_empty_body(api):
    client, _maker = api
    r = await client.post("/api/v1/items/copy-counts", json={"ids": []})
    assert r.status_code == 200
    assert r.json() == {}


async def test_copy_counts_over_200_rejected(api):
    client, _maker = api
    ids = [str(uuid.uuid4()) for _ in range(201)]
    r = await client.post("/api/v1/items/copy-counts", json={"ids": ids})
    assert r.status_code == 422, r.text


async def test_copy_counts_exactly_200_ok(api):
    client, _maker = api
    ids = [str(uuid.uuid4()) for _ in range(200)]
    r = await client.post("/api/v1/items/copy-counts", json={"ids": ids})
    assert r.status_code == 200
    assert r.json() == {}  # none exist


# --------------------------------------------------------------------------- #
# P3-T14 — timeline aggregate                                                 #
# --------------------------------------------------------------------------- #
async def test_timeline_month_buckets_and_library_filter(api):
    client, maker = api
    lib = await _mk_lib(maker)
    other = await _mk_lib(maker, name="Other")
    jan = datetime(2024, 1, 15, tzinfo=UTC)
    jan2 = datetime(2024, 1, 20, tzinfo=UTC)
    mar = datetime(2024, 3, 5, tzinfo=UTC)
    await _mk_item(maker, lib, "a.bin", mtime=jan)
    await _mk_item(maker, lib, "b.bin", mtime=jan2)
    await _mk_item(maker, lib, "c.bin", mtime=mar)
    await _mk_item(maker, other, "o.bin", mtime=jan)

    # unscoped: 3 in lib + 1 in other = jan bucket 3, mar bucket 1
    r = await client.get("/api/v1/stats/timeline?bucket=month")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bucket"] == "month"
    counts = {b["start"][:7]: b["count"] for b in body["buckets"]}
    assert counts == {"2024-01": 3, "2024-03": 1}
    # half-open window end == next month boundary
    jan_bucket = next(b for b in body["buckets"] if b["start"].startswith("2024-01"))
    assert jan_bucket["end_epoch"] == int(datetime(2024, 2, 1, tzinfo=UTC).timestamp())
    assert jan_bucket["start_epoch"] == int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # scoped to `lib`: jan 2, mar 1
    r = await client.get(f"/api/v1/stats/timeline?bucket=month&library={lib}")
    counts = {b["start"][:7]: b["count"] for b in r.json()["buckets"]}
    assert counts == {"2024-01": 2, "2024-03": 1}


async def test_timeline_year_buckets(api):
    client, maker = api
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "a.bin", mtime=datetime(2022, 6, 1, tzinfo=UTC))
    await _mk_item(maker, lib, "b.bin", mtime=datetime(2024, 6, 1, tzinfo=UTC))
    r = await client.get("/api/v1/stats/timeline?bucket=year")
    counts = {b["start"][:4]: b["count"] for b in r.json()["buckets"]}
    assert counts == {"2022": 1, "2024": 1}


async def test_timeline_invalid_future_bucket(api):
    client, maker = api
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "now.bin", mtime=datetime.now(UTC))
    # > 48h in the future -> suspect, into the invalid bucket, NOT a bar
    future = datetime.now(UTC) + timedelta(days=10)
    await _mk_item(maker, lib, "future.bin", mtime=future)

    r = await client.get("/api/v1/stats/timeline?bucket=month")
    body = r.json()
    assert body["invalid_count"] == 1
    # the future item is excluded from the histogram bars
    total_in_bars = sum(b["count"] for b in body["buckets"])
    assert total_in_bars == 1
    # invalid_mtime_gte is strictly beyond the 48h window
    assert body["invalid_mtime_gte"] > int((datetime.now(UTC) + timedelta(hours=48)).timestamp())


async def test_timeline_bad_bucket_rejected(api):
    client, _maker = api
    r = await client.get("/api/v1/stats/timeline?bucket=week")
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# P3-T12 — tag facet-search proxy (fake Meili)                                #
# --------------------------------------------------------------------------- #
class _FakeIndex:
    def __init__(self, sink, hits):
        self._sink = sink
        self._hits = hits

    async def facet_search(self, **kwargs):
        self._sink.update(kwargs)
        return SimpleNamespace(
            facet_hits=[SimpleNamespace(value=v, count=c) for v, c in self._hits],
            facet_query=kwargs.get("facet_query"),
            processing_time_ms=1,
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
        return _FakeIndex(self._sink, self._hits)


def _tags_app(monkeypatch, hits):
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    sink: dict = {}
    monkeypatch.setattr(search_api, "client", lambda: _FakeClient(sink, hits))
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return transport, sink


@pytest.mark.asyncio
async def test_tags_typeahead_count_ordered(monkeypatch):
    # Meili returns count-ordered (sortFacetValuesBy=count) — the proxy passes the
    # order through verbatim.
    hits = [("hdr", 42), ("4k", 30), ("hdr10", 5)]
    transport, sink = _tags_app(monkeypatch, hits)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search/tags?q=hd")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == [
        {"value": "hdr", "count": 42},
        {"value": "4k", "count": 30},
        {"value": "hdr10", "count": 5},
    ]
    # proxied to the tags facet with the typed query
    assert sink["facet_name"] == "tags"
    assert sink["facet_query"] == "hd"


@pytest.mark.asyncio
async def test_tags_typeahead_scopes_filter(monkeypatch):
    transport, sink = _tags_app(monkeypatch, [("a", 1)])
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search/tags?q=a&file_category=video")
    assert r.status_code == 200, r.text
    # the file_category scope reached the facet_search filter
    assert "file_category = 'video'" in (sink["filter"] or "")


@pytest.mark.asyncio
async def test_tags_typeahead_limit(monkeypatch):
    hits = [(f"t{i}", 100 - i) for i in range(30)]
    transport, sink = _tags_app(monkeypatch, hits)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search/tags?q=t&limit=5")
    assert r.status_code == 200
    assert len(r.json()["tags"]) == 5
