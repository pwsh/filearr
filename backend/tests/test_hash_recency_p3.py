"""P3-T1 (hash search + on-demand digests) and P3-T2 (recency ranking bucket +
explicit sort toggle).

Split into three layers:
* pure projection/spec tests (no Meili, no DB) — build_doc fields, recency
  buckets, filter composition, settings spec/ranking-rule placement;
* endpoint-shape tests with a FAKE Meili client (no server) — hash= filter reaches
  the engine, invalid hash is a 422, sort=newest maps to mtime:desc;
* one real-Postgres integration test (pgserver) for the digest endpoint — compute
  once + cache, size ceiling 413, missing file 409, algorithm validation.
"""

from __future__ import annotations

import hashlib
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
from filearr import search as search_mod
from filearr.api import digests as digests_mod
from filearr.api.search import HASH_RE, build_filters
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.meili_ops import (
    DISABLE_TYPO_ATTRIBUTES,
    FILTERABLE_ATTRIBUTES,
    INDEX_SETTINGS_SPEC,
    RANKING_RULES,
    SORTABLE_ATTRIBUTES,
    settings_drift,
)
from filearr.models import Item, ItemStatus, Library
from filearr.search import RECENCY_OLDEST_BUCKET, build_doc, recency_bucket

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def _make_item(**kw) -> Item:
    base = dict(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="video", file_group="video",
        path="/data/a.mkv",
        rel_path="a.mkv",
        filename="a.mkv",
        extension="mkv",
        size=1,
        mtime=datetime.now(UTC),
        quick_hash=None,
        content_hash=None,
        metadata_={},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
    )
    base.update(kw)
    return Item(**base)


# --------------------------------------------------------------------------- #
# P3-T1 — build_doc emits hash fields                                         #
# --------------------------------------------------------------------------- #
def test_build_doc_emits_hash_fields():
    item = _make_item(quick_hash="deadbeef", content_hash="a" * 64)
    doc = build_doc(item)
    assert doc["quick_hash"] == "deadbeef"
    assert doc["content_hash"] == "a" * 64


def test_build_doc_hash_fields_none_when_unhashed():
    doc = build_doc(_make_item())
    assert doc["quick_hash"] is None
    assert doc["content_hash"] is None


def test_hash_fields_filterable_and_typo_disabled():
    assert "quick_hash" in FILTERABLE_ATTRIBUTES
    assert "content_hash" in FILTERABLE_ATTRIBUTES
    # typo tolerance already off (HASH_ATTRIBUTES single source of truth)
    assert "quick_hash" in DISABLE_TYPO_ATTRIBUTES
    assert "content_hash" in DISABLE_TYPO_ATTRIBUTES


# --------------------------------------------------------------------------- #
# P3-T2 — recency bucket boundaries                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("age_days", "expected"),
    [
        (0, 0), (6, 0),          # [0, 7)
        (7, 1), (29, 1),         # [7, 30)
        (30, 2), (179, 2),       # [30, 180)
        (180, 3), (364, 3),      # [180, 365)
        (365, 4), (4000, 4),     # [365, inf)
    ],
)
def test_recency_bucket_boundaries(age_days, expected):
    now = datetime(2026, 7, 10, tzinfo=UTC)
    mtime = now - timedelta(days=age_days)
    assert recency_bucket(mtime, now=now) == expected


def test_recency_bucket_none_is_oldest():
    assert recency_bucket(None) == RECENCY_OLDEST_BUCKET == 4


def test_recency_bucket_future_within_skew_is_newest():
    # FIX-3: a future mtime WITHIN the 48h clock-skew window is still "just
    # touched" (bucket 0) — genuine host/source clock skew is not penalised.
    now = datetime(2026, 7, 10, tzinfo=UTC)
    assert recency_bucket(now + timedelta(hours=1), now=now) == 0
    assert recency_bucket(now + timedelta(hours=47), now=now) == 0


def test_recency_bucket_far_future_is_suspect_oldest():
    # FIX-3 (the bug): a bogus mtime years/hours-beyond-skew into the future is
    # SUSPECT and buckets OLDEST (4), so it can never dominate a recency tie-break
    # or a sort=newest. Previously any future date bucketed 0.
    now = datetime(2026, 7, 10, tzinfo=UTC)
    assert recency_bucket(now + timedelta(hours=49), now=now) == RECENCY_OLDEST_BUCKET
    assert recency_bucket(now + timedelta(days=3650), now=now) == RECENCY_OLDEST_BUCKET


def test_build_doc_includes_recency_bucket():
    doc = build_doc(_make_item(mtime=datetime.now(UTC)))
    assert doc["recency_bucket"] == 0
    # an old item buckets high
    old = build_doc(_make_item(mtime=datetime.now(UTC) - timedelta(days=1000)))
    assert old["recency_bucket"] == 4


def test_recency_bucket_is_sortable():
    assert "recency_bucket" in SORTABLE_ATTRIBUTES


# --------------------------------------------------------------------------- #
# FIX-3 — clamped mtime_sort projection                                       #
# --------------------------------------------------------------------------- #
def test_mtime_sort_is_sortable():
    assert "mtime_sort" in SORTABLE_ATTRIBUTES


def test_build_doc_mtime_sort_equals_mtime_for_past():
    # A normal (past) mtime is not clamped: mtime_sort == mtime.
    past = datetime.now(UTC) - timedelta(days=10)
    doc = build_doc(_make_item(mtime=past))
    assert doc["mtime_sort"] == doc["mtime"] == int(past.timestamp())


def test_build_doc_mtime_sort_clamps_future():
    # A bogus far-future mtime is clamped down to ~index-time: mtime_sort < mtime.
    future = datetime.now(UTC) + timedelta(days=3650)
    doc = build_doc(_make_item(mtime=future))
    assert doc["mtime"] == int(future.timestamp())          # raw kept for display
    assert doc["mtime_sort"] < doc["mtime"]                 # sort key clamped
    # clamped to roughly now (allow a few seconds of test execution slack)
    assert doc["mtime_sort"] <= int(datetime.now(UTC).timestamp()) + 5


def test_build_doc_mtime_sort_none_when_no_mtime():
    doc = build_doc(_make_item(mtime=None))
    assert doc["mtime_sort"] is None


# --------------------------------------------------------------------------- #
# P3-T2 — ranking-rule placement (custom rule AFTER exactness)                #
# --------------------------------------------------------------------------- #
def test_ranking_rules_recency_after_exactness():
    rr = list(RANKING_RULES)
    assert "recency_bucket:asc" in rr
    assert rr.index("recency_bucket:asc") == len(rr) - 1
    assert rr.index("recency_bucket:asc") > rr.index("exactness")
    # default six rules preserved, in order, ahead of it
    assert rr[:6] == ["words", "typo", "proximity", "attribute", "sort", "exactness"]


def test_index_spec_includes_ranking_rules():
    assert INDEX_SETTINGS_SPEC["rankingRules"] == list(RANKING_RULES)


def test_ranking_rules_order_sensitive_in_drift():
    desired = {"rankingRules": list(RANKING_RULES)}
    reordered = {"rankingRules": ["recency_bucket:asc", *RANKING_RULES[:-1]]}
    assert settings_drift(desired, desired) == []
    assert settings_drift(reordered, desired) == ["rankingRules"]


def test_desired_settings_projects_ranking_rules():
    # the runtime projection carries the same ordered ranking rules
    assert search_mod._desired_settings()["rankingRules"] == list(RANKING_RULES)


# --------------------------------------------------------------------------- #
# P3-T1 — hash filter composition                                             #
# --------------------------------------------------------------------------- #
def test_build_filters_hash_or_clause():
    h = "deadbeefcafe1234"
    f = build_filters(hash=h)
    assert f"(quick_hash = '{h}' OR content_hash = '{h}')" in f


def test_build_filters_hash_composes_with_type():
    h = "abcdef1234567890"
    f = build_filters(file_category=["video"], hash=h)
    assert any("file_category = 'video'" in c for c in f)
    assert any("quick_hash" in c for c in f)
    # default sidecar exclusion still applied alongside
    assert "is_sidecar = false" in f


def test_build_filters_invalid_hash_raises():
    for bad in ("XYZ", "short", "g" * 16, "AA" * 16, "abc"):  # non-hex/too short
        with pytest.raises(ValueError):
            build_filters(hash=bad)


def test_hash_re_bounds():
    assert HASH_RE.match("a" * 8)
    assert HASH_RE.match("f" * 64)
    assert not HASH_RE.match("a" * 7)
    assert not HASH_RE.match("a" * 65)
    assert not HASH_RE.match("A" * 16)  # uppercase rejected


# --------------------------------------------------------------------------- #
# Endpoint-shape tests with a fake Meili client (no server, no DB)            #
# --------------------------------------------------------------------------- #
class _FakeSearchIndex:
    def __init__(self, sink):
        self._sink = sink

    async def search(self, q, **kwargs):
        self._sink["q"] = q
        self._sink.update(kwargs)
        return SimpleNamespace(hits=[], estimated_total_hits=0, facet_distribution={})


class _FakeClient:
    def __init__(self, sink):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def index(self, name):
        return _FakeSearchIndex(self._sink)


@pytest.fixture
def search_app(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    sink: dict = {}
    monkeypatch.setattr("filearr.api.search.client", lambda: _FakeClient(sink))
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return app, transport, sink


@pytest.mark.asyncio
async def test_search_hash_param_reaches_meili_filter(search_app):
    app, transport, sink = search_app
    h = "deadbeefcafe1234"
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/v1/search?hash={h}&file_category=video")
    assert r.status_code == 200, r.text
    assert f"(quick_hash = '{h}' OR content_hash = '{h}')" in sink["filter"]
    assert "file_category = 'video'" in sink["filter"]


@pytest.mark.asyncio
async def test_search_invalid_hash_is_422(search_app):
    app, transport, sink = search_app
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?hash=NOTHEX")
    assert r.status_code == 422  # Query pattern rejects before any Meili call
    assert "filter" not in sink  # engine never touched


@pytest.mark.asyncio
async def test_search_sort_newest_maps_to_mtime_sort_desc(search_app):
    # FIX-3: sort=newest maps to the CLAMPED mtime_sort (not raw mtime) so a
    # bogus future mtime cannot float to the top of a "newest" sort.
    app, transport, sink = search_app
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?sort=newest")
    assert r.status_code == 200, r.text
    assert sink["sort"] == ["mtime_sort:desc"]


@pytest.mark.asyncio
async def test_search_explicit_sort_passthrough(search_app):
    app, transport, sink = search_app
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?sort=year:asc")
    assert r.status_code == 200, r.text
    assert sink["sort"] == ["year:asc"]


# --------------------------------------------------------------------------- #
# P3-T1 — digest endpoint (real Postgres, real tmp file)                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def digest_client(pg_uri, monkeypatch):
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


async def _seed(maker, path: str, size: int = 100) -> str:
    async with maker() as s:
        lib = Library(name="L", root_path="/data/l")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id, file_category="document", file_group="document-text",
            path=path, rel_path="f.bin", filename="f.bin", extension="bin",
            size=size, mtime=datetime.now(UTC), metadata_={}, user_metadata={},
            external_ids={}, tags=[],
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_digest_computes_then_caches(digest_client, tmp_path, monkeypatch):
    client, maker = digest_client
    data = b"hello filearr digest\n" * 10
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    item_id = await _seed(maker, str(f))

    calls = {"n": 0}
    real = digests_mod.compute_digests

    def counting(path, algorithms=("md5", "sha256"), **kw):
        calls["n"] += 1
        return real(path, algorithms=algorithms, **kw)

    monkeypatch.setattr(digests_mod, "compute_digests", counting)

    r1 = await client.post(f"/api/v1/items/{item_id}/digests?algorithms=md5,sha256")
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["digests"]["md5"] == hashlib.md5(data).hexdigest()
    assert body1["digests"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert set(body1["computed"]) == {"md5", "sha256"}
    assert calls["n"] == 1

    # cached in metadata_.digests
    async with maker() as s:
        row = (await s.execute(text(
            "SELECT metadata->'digests' FROM items WHERE id = :i"
        ), {"i": item_id})).scalar()
    assert row["sha256"] == hashlib.sha256(data).hexdigest()

    # second call: returns cached WITHOUT re-reading the file
    r2 = await client.post(f"/api/v1/items/{item_id}/digests?algorithms=md5,sha256")
    assert r2.status_code == 200, r2.text
    assert r2.json()["digests"] == body1["digests"]
    assert r2.json()["computed"] == []
    assert calls["n"] == 1  # compute_digests NOT called again


async def test_digest_only_computes_missing_algorithm(digest_client, tmp_path, monkeypatch):
    client, maker = digest_client
    data = b"partial cache\n"
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    item_id = await _seed(maker, str(f))

    # first cache md5 only
    r1 = await client.post(f"/api/v1/items/{item_id}/digests?algorithms=md5")
    assert r1.status_code == 200
    # now request md5+sha256 -> only sha256 is (re)computed
    calls = {"n": 0}
    real = digests_mod.compute_digests

    def counting(path, algorithms=("md5", "sha256"), **kw):
        calls["n"] += 1
        assert list(algorithms) == ["sha256"]  # md5 already cached, not recomputed
        return real(path, algorithms=algorithms, **kw)

    monkeypatch.setattr(digests_mod, "compute_digests", counting)
    r2 = await client.post(f"/api/v1/items/{item_id}/digests?algorithms=md5,sha256")
    assert r2.status_code == 200, r2.text
    assert r2.json()["computed"] == ["sha256"]
    assert calls["n"] == 1


async def test_digest_size_ceiling_413(digest_client, tmp_path, monkeypatch):
    client, maker = digest_client
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 4096)
    item_id = await _seed(maker, str(f), size=4096)
    monkeypatch.setattr(get_settings(), "digest_max_bytes", 1024)  # below file size
    r = await client.post(f"/api/v1/items/{item_id}/digests")
    assert r.status_code == 413, r.text


async def test_digest_missing_file_409(digest_client, tmp_path):
    client, maker = digest_client
    item_id = await _seed(maker, str(tmp_path / "does-not-exist.bin"))
    r = await client.post(f"/api/v1/items/{item_id}/digests")
    assert r.status_code == 409, r.text


async def test_digest_unknown_algorithm_422(digest_client, tmp_path):
    client, maker = digest_client
    f = tmp_path / "f.bin"
    f.write_bytes(b"z")
    item_id = await _seed(maker, str(f))
    r = await client.post(f"/api/v1/items/{item_id}/digests?algorithms=md5,rot13")
    assert r.status_code == 422, r.text


async def test_digest_missing_item_404(digest_client):
    client, maker = digest_client
    r = await client.post(f"/api/v1/items/{uuid.uuid4()}/digests")
    assert r.status_code == 404, r.text
