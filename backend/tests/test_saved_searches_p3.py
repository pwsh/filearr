"""P3-T7 — saved searches (named, persisted /search queries).

Three layers:
* pure: the ``SEARCH_PARAM_NAMES`` vocabulary is DERIVED from the /search endpoint
  signature (minus ``cursor``) — a rename/removal there is caught here;
* endpoint CRUD with real Postgres (pgserver): create/list/get/patch/delete,
  unknown-key 422 on BOTH create and update, per-owner duplicate 409;
* round-trip: replaying a saved search's params through /search produces a
  BYTE-IDENTICAL engine filter/sort/q as passing those params directly (recording
  fake Meili — no server).
"""

from __future__ import annotations

import inspect
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
from filearr.api.search import SEARCH_PARAM_NAMES
from filearr.api.search import search as search_endpoint
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure — the param vocabulary tracks the endpoint signature                   #
# --------------------------------------------------------------------------- #
def test_search_param_names_derived_from_signature():
    sig = set(inspect.signature(search_endpoint).parameters)
    # ``scope_filter`` is the P6-T3 injected RBAC dependency (Depends), not a
    # user-supplied search param, so it is excluded alongside ``cursor``.
    assert SEARCH_PARAM_NAMES == frozenset(sig - {"cursor", "scope_filter"})


def test_search_param_names_excludes_cursor():
    assert "cursor" not in SEARCH_PARAM_NAMES


def test_search_param_names_cover_known_params():
    # A representative slice of today's params; if one is RENAMED the saved-search
    # round-trip test below fails, and this makes the vocabulary explicit.
    for name in ("q", "type", "library", "hash", "sort", "size_gte", "mtime_lte"):
        assert name in SEARCH_PARAM_NAMES


# --------------------------------------------------------------------------- #
# CRUD + round-trip fixture (real Postgres + recording fake Meili)            #
# --------------------------------------------------------------------------- #
class _FakeSearchIndex:
    def __init__(self, sink):
        self._sink = sink

    async def search(self, q, **kwargs):
        # Record exactly what the endpoint asked the engine for.
        self._sink.clear()
        self._sink["q"] = q
        self._sink.update(kwargs)
        return SimpleNamespace(
            hits=[], estimated_total_hits=0, facet_distribution={}, facet_stats={}
        )


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
async def ss_client(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM saved_searches"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    sink: dict = {}
    monkeypatch.setattr(search_api, "client", lambda: _FakeClient(sink))
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, sink
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_saved_search_crud_roundtrip(ss_client):
    client, _sink = ss_client
    body = {
        "name": "Big videos",
        "params": {"type": "video", "q": "arcane", "size_gte": "1000000"},
        "owner_principal": "alice",
    }
    r = await client.post("/api/v1/saved-searches", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    sid = created["id"]
    assert created["name"] == "Big videos"
    assert created["params"] == body["params"]
    assert created["owner_principal"] == "alice"
    assert created["created_at"] and created["updated_at"]

    # survives a "restart" — a fresh GET reads it from Postgres
    r = await client.get(f"/api/v1/saved-searches/{sid}")
    assert r.status_code == 200
    assert r.json()["params"] == body["params"]

    # list
    r = await client.get("/api/v1/saved-searches")
    assert r.status_code == 200
    assert any(x["id"] == sid for x in r.json())

    # patch rename + replace params
    r = await client.patch(
        f"/api/v1/saved-searches/{sid}",
        json={"name": "Huge videos", "params": {"type": "video", "size_gte": "5000000"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Huge videos"
    assert r.json()["params"] == {"type": "video", "size_gte": "5000000"}

    # delete
    r = await client.delete(f"/api/v1/saved-searches/{sid}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/saved-searches/{sid}")
    assert r.status_code == 404


async def test_saved_search_unknown_param_422_on_create(ss_client):
    client, _sink = ss_client
    r = await client.post(
        "/api/v1/saved-searches",
        json={"name": "bad", "params": {"type": "video", "bogus_key": "x"}},
    )
    assert r.status_code == 422, r.text
    assert "bogus_key" in r.text


async def test_saved_search_unknown_param_422_on_update(ss_client):
    client, _sink = ss_client
    r = await client.post(
        "/api/v1/saved-searches", json={"name": "ok", "params": {"type": "video"}}
    )
    sid = r.json()["id"]
    r = await client.patch(
        f"/api/v1/saved-searches/{sid}", json={"params": {"not_a_param": "1"}}
    )
    assert r.status_code == 422, r.text
    assert "not_a_param" in r.text


async def test_saved_search_duplicate_name_per_owner_409(ss_client):
    client, _sink = ss_client
    payload = {"name": "dupe", "params": {}, "owner_principal": "bob"}
    r1 = await client.post("/api/v1/saved-searches", json=payload)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/saved-searches", json=payload)
    assert r2.status_code == 409, r2.text


async def test_saved_search_roundtrip_byte_identical_engine_call(ss_client):
    """A saved search replayed through /search must produce the SAME engine
    filter/sort/q as passing those params directly."""
    client, sink = ss_client
    params = {
        "type": "video",
        "q": "arcane",
        "size_gte": "1048576",
        "sort": "newest",
        "tags": "hdr,4k",
    }

    # 1) direct search with the params — record the engine call
    r = await client.get("/api/v1/search", params=params)
    assert r.status_code == 200, r.text
    direct = {"q": sink.get("q"), "filter": sink.get("filter"), "sort": sink.get("sort")}

    # 2) save the exact params, read them back, replay through /search
    r = await client.post(
        "/api/v1/saved-searches", json={"name": "replay", "params": params}
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    stored = (await client.get(f"/api/v1/saved-searches/{sid}")).json()["params"]

    r = await client.get("/api/v1/search", params=stored)
    assert r.status_code == 200, r.text
    replay = {"q": sink.get("q"), "filter": sink.get("filter"), "sort": sink.get("sort")}

    assert replay == direct
    # and the fix-3 clamped sort key travelled through untouched
    assert replay["sort"] == ["mtime_sort:desc"]
