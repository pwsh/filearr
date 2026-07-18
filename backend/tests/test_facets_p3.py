"""P3-T4 — filter chips + facetStats range sliders (backend enablement).

Endpoint-shape tests with a FAKE Meili client (no server, no DB):
* size/mtime range params reach the engine as numeric filters;
* ``size``/``mtime`` are requested as facets so Meili emits ``facetStats``;
* the SDK's ``facet_stats`` is passed through on SearchResponse;
* a client that omits ``facet_stats`` degrades to an empty dict.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from filearr.api.search import FACETS, build_filters
from filearr.config import get_settings
from filearr.main import create_app


# --------------------------------------------------------------------------- #
# Pure filter composition (no engine)                                         #
# --------------------------------------------------------------------------- #
def test_size_range_filters_compose():
    f = build_filters(status=None, size_gte=1024, size_lte=1048576)
    assert "size >= 1024" in f
    assert "size <= 1048576" in f


def test_mtime_range_filters_compose():
    f = build_filters(status=None, mtime_gte=1000, mtime_lte=2000)
    assert "mtime >= 1000" in f
    assert "mtime <= 2000" in f


def test_range_filters_absent_when_none():
    f = build_filters(status=None)
    assert not any(c.startswith(("size ", "mtime ")) for c in f)


def test_facets_include_size_and_mtime():
    # Required for Meili to emit facetStats on these numeric fields.
    assert "size" in FACETS
    assert "mtime" in FACETS


# --------------------------------------------------------------------------- #
# Endpoint-shape tests with a fake Meili client                               #
# --------------------------------------------------------------------------- #
class _FakeSearchIndex:
    def __init__(self, sink, facet_stats):
        self._sink = sink
        self._facet_stats = facet_stats

    async def search(self, q, **kwargs):
        self._sink["q"] = q
        self._sink.update(kwargs)
        ns = SimpleNamespace(
            hits=[],
            estimated_total_hits=0,
            facet_distribution={"media_type": {"video": 3}},
        )
        # Only attach facet_stats when the test asked for it, so we can exercise
        # the getattr(...) fallback for clients that omit the attribute.
        if self._facet_stats is not None:
            ns.facet_stats = self._facet_stats
        return ns


class _FakeClient:
    def __init__(self, sink, facet_stats):
        self._sink = sink
        self._facet_stats = facet_stats

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def index(self, name):
        return _FakeSearchIndex(self._sink, self._facet_stats)


def _make_app(monkeypatch, facet_stats):
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    sink: dict = {}
    monkeypatch.setattr(
        "filearr.api.search.client", lambda: _FakeClient(sink, facet_stats)
    )
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return transport, sink


@pytest.mark.asyncio
async def test_size_mtime_params_reach_meili_filter(monkeypatch):
    transport, sink = _make_app(monkeypatch, None)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(
            "/api/v1/search?size_gte=1024&size_lte=2048&mtime_gte=100&mtime_lte=200"
        )
    assert r.status_code == 200, r.text
    assert "size >= 1024" in sink["filter"]
    assert "size <= 2048" in sink["filter"]
    assert "mtime >= 100" in sink["filter"]
    assert "mtime <= 200" in sink["filter"]


@pytest.mark.asyncio
async def test_search_requests_size_mtime_facets(monkeypatch):
    transport, sink = _make_app(monkeypatch, None)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=foo")
    assert r.status_code == 200, r.text
    assert "size" in sink["facets"]
    assert "mtime" in sink["facets"]


@pytest.mark.asyncio
async def test_facet_stats_passthrough(monkeypatch):
    stats = {"size": {"min": 10.0, "max": 5000.0}, "mtime": {"min": 100.0, "max": 900.0}}
    transport, _ = _make_app(monkeypatch, stats)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=foo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["facet_stats"] == stats
    # facets distribution still surfaced for the chips.
    assert body["facets"]["media_type"]["video"] == 3


@pytest.mark.asyncio
async def test_facet_stats_defaults_empty(monkeypatch):
    # Client omits facet_stats entirely -> endpoint returns {} (not null/error).
    transport, _ = _make_app(monkeypatch, None)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=foo")
    assert r.status_code == 200, r.text
    assert r.json()["facet_stats"] == {}


@pytest.mark.asyncio
async def test_negative_size_rejected(monkeypatch):
    transport, sink = _make_app(monkeypatch, None)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?size_gte=-5")
    assert r.status_code == 422  # ge=0 rejects before any engine call
    assert "filter" not in sink
