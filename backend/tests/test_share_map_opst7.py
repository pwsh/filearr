"""OPS-T7 — deploy mount map → auto library ``share_prefix``.

Two layers:

* **Pure** (no Postgres): load/validate/missing/malformed never crash the app;
  longest-container_prefix-wins resolution incl. nested mounts; the
  ``effective_library_share`` fallback (manual wins / map covers / neither); and
  ``item_share_url`` for report rows.
* **API** (Postgres): the library create/PATCH/list responses carry
  ``share_prefix_effective`` + ``share_prefix_source``; a manual override wins over
  a covering map; ``GET /system/share-map`` echoes the loaded map.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import share_map
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Pure unit tests (no DB, no network)                                         #
# --------------------------------------------------------------------------- #
def _write_map(tmp_path: Path, rows) -> Path:
    p = tmp_path / "share-map.json"
    p.write_text(json.dumps(rows) if not isinstance(rows, str) else rows)
    return p


@pytest.fixture(autouse=True)
def _reset_share_cache():
    share_map.reset_cache()
    yield
    share_map.reset_cache()


def _point(monkeypatch, path: str) -> None:
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "share_map_path", str(path))
    share_map.reset_cache()


_SAMPLE = [
    {"container_prefix": "/data/media/media", "share_url": "smb://tower/Media Management",
     "storage_type": "smb", "host": "tower", "unc": r"\\tower\Media Management"},
    {"container_prefix": "/data/media/movies", "share_url": "smb://tower/media/movies",
     "storage_type": "smb", "host": "tower", "unc": r"\\tower\media\movies"},
    {"container_prefix": "/data/media/docs", "share_url": "sftp://files/srv/docs",
     "storage_type": "sftp", "host": "files"},
]


def test_missing_file_is_empty_and_never_raises(monkeypatch, tmp_path):
    _point(monkeypatch, tmp_path / "nope.json")
    assert share_map.get_entries() == []
    assert share_map.resolve("/data/media/media/x") is None


def test_malformed_json_is_empty(monkeypatch, tmp_path):
    p = _write_map(tmp_path, "{not valid json")
    _point(monkeypatch, p)
    assert share_map.get_entries() == []


def test_non_list_payload_is_empty(monkeypatch, tmp_path):
    p = _write_map(tmp_path, '{"a": 1}')
    _point(monkeypatch, p)
    assert share_map.get_entries() == []


def test_bad_rows_skipped_good_kept(monkeypatch, tmp_path):
    p = _write_map(
        tmp_path,
        [
            {"container_prefix": "/data/media/m", "share_url": "smb://h/m"},
            {"container_prefix": "", "share_url": "smb://h/x"},  # empty prefix -> skip
            {"share_url": "smb://h/y"},  # missing prefix -> skip
            "notadict",  # skip
            {"container_prefix": "/data/media/n", "share_url": "smb://h/n", "junk": 1},
        ],
    )
    _point(monkeypatch, p)
    prefixes = [e.container_prefix for e in share_map.get_entries()]
    assert prefixes == ["/data/media/m", "/data/media/n"]


def test_resolve_longest_prefix_and_nested(monkeypatch, tmp_path):
    p = _write_map(tmp_path, _SAMPLE)
    _point(monkeypatch, p)
    # /data/media/movies is a distinct, longer-covering mount than any other.
    assert share_map.resolve("/data/media/movies/a/b.mkv").share_url == (
        "smb://tower/media/movies/a/b.mkv"
    )
    # root-level mount + remainder, native separators, spaces preserved.
    h = share_map.resolve("/data/media/media/Movies/x.mkv")
    assert h.share_url == "smb://tower/Media Management/Movies/x.mkv"
    assert h.unc == r"\\tower\Media Management\Movies\x.mkv"
    assert h.scheme == "url"


def test_resolve_uncovered_is_none(monkeypatch, tmp_path):
    _point(monkeypatch, _write_map(tmp_path, _SAMPLE))
    assert share_map.resolve("/data/other/z") is None


def test_effective_manual_wins(monkeypatch, tmp_path):
    _point(monkeypatch, _write_map(tmp_path, _SAMPLE))
    val, src = share_map.effective_library_share(r"\\host\m", "/data/media/media")
    assert (val, src) == (r"\\host\m", "manual")


def test_effective_mount_map(monkeypatch, tmp_path):
    _point(monkeypatch, _write_map(tmp_path, _SAMPLE))
    val, src = share_map.effective_library_share(None, "/data/media/docs")
    assert (val, src) == ("sftp://files/srv/docs", "mount-map")


def test_effective_none(monkeypatch, tmp_path):
    _point(monkeypatch, _write_map(tmp_path, _SAMPLE))
    assert share_map.effective_library_share(None, "/data/nope") == (None, "none")


def test_item_share_url_manual_vs_map(monkeypatch, tmp_path):
    _point(monkeypatch, _write_map(tmp_path, _SAMPLE))
    # manual prefix wins, joined with rel_path
    assert share_map.item_share_url(
        "smb://h/s", "/data/media/media/A/b.mkv", "A/b.mkv"
    ) == "smb://h/s/A/b.mkv"
    # no manual -> resolve absolute item path
    assert share_map.item_share_url(
        None, "/data/media/media/A/b.mkv", "A/b.mkv"
    ) == "smb://tower/Media Management/A/b.mkv"
    # uncovered -> None
    assert share_map.item_share_url(None, "/data/x/y", "y") is None


# --------------------------------------------------------------------------- #
# UI-T15: SMB URL <-> UNC derivation + both-format ShareLocation              #
# --------------------------------------------------------------------------- #
def test_derive_unc_from_url_matrix():
    d = share_map._derive_unc_from_url
    assert d("smb://tower/media") == r"\\tower\media"
    # subpaths + spaces preserved verbatim
    assert d("smb://tower/Media Management/Movies/x.mkv") == (
        r"\\tower\Media Management\Movies\x.mkv"
    )
    # non-SMB schemes have no UNC form
    assert d("sftp://host/srv/docs") is None
    assert d("ftp://host/pub") is None
    assert d("nfs://host/export") is None
    assert d("webdav://host/dav") is None
    assert d("file:///Volumes/media") is None
    # a port is not representable in UNC
    assert d("smb://host:445/share") is None
    # credentials are dropped, never emitted
    assert d("smb://user:pass@host/share/sub") == r"\\host\share\sub"
    # IPv6 literal -> Windows ipv6-literal.net form
    assert d("smb://[fe80::1]/share") == r"\\fe80--1.ipv6-literal.net\share"
    assert d("smb://[fe80::1]:445/share") is None  # port again
    assert d("") is None


def test_derive_url_from_unc_matrix():
    d = share_map._derive_url_from_unc
    assert d(r"\\tower\media") == "smb://tower/media"
    assert d(r"\\tower\Media Management\Movies\x.mkv") == (
        "smb://tower/Media Management/Movies/x.mkv"
    )
    assert d("smb://not/unc") is None
    assert d("") is None
    # ipv6-literal host restored to a bracketed literal (round-trips)
    assert d(r"\\fe80--1.ipv6-literal.net\share") == "smb://[fe80::1]/share"


def test_derive_round_trips():
    d1, d2 = share_map._derive_unc_from_url, share_map._derive_url_from_unc
    for url in ("smb://tower/media", "smb://tower/Media Management/a/b c.mkv"):
        assert d2(d1(url)) == url


def test_location_from_prefix_classification():
    loc = share_map._location_from_prefix
    m = loc(r"\\host\m")
    assert (m.url, m.unc) == ("smb://host/m", r"\\host\m")
    m = loc("smb://h/s")
    assert (m.url, m.unc) == ("smb://h/s", r"\\h\s")
    m = loc("sftp://h/p")
    assert (m.url, m.unc) == ("sftp://h/p", None)
    m = loc("/Volumes/media")
    assert (m.url, m.unc) == ("/Volumes/media", None)


def test_effective_library_share_location(monkeypatch, tmp_path):
    _point(monkeypatch, _write_map(tmp_path, _SAMPLE))
    loc, src = share_map.effective_library_share_location(r"\\host\m", "/data/media/media")
    assert (loc.url, loc.unc, src) == ("smb://host/m", r"\\host\m", "manual")
    loc, src = share_map.effective_library_share_location(None, "/data/media/media")
    assert (loc.url, loc.unc, src) == (
        "smb://tower/Media Management", r"\\tower\Media Management", "mount-map",
    )
    loc, src = share_map.effective_library_share_location(None, "/data/media/docs")
    assert (loc.url, loc.unc, src) == ("sftp://files/srv/docs", None, "mount-map")
    loc, src = share_map.effective_library_share_location(None, "/data/nope")
    assert (loc.url, loc.unc, src) == (None, None, "none")


def test_item_share_location(monkeypatch, tmp_path):
    _point(monkeypatch, _write_map(tmp_path, _SAMPLE))
    loc = share_map.item_share_location("smb://h/s", "/x", "A/b c.mkv")
    assert (loc.url, loc.unc) == ("smb://h/s/A/b c.mkv", r"\\h\s\A\b c.mkv")
    loc = share_map.item_share_location(r"\\h\s", "/x", "A/b.mkv")
    assert loc.unc == r"\\h\s\A\b.mkv"
    loc = share_map.item_share_location(None, "/data/media/media/A/b.mkv", "A/b.mkv")
    assert loc.unc == r"\\tower\Media Management\A\b.mkv"
    loc = share_map.item_share_location(None, "/data/x/y", "y")
    assert (loc.url, loc.unc) == (None, None)


def test_mtime_cache_reloads_on_change(monkeypatch, tmp_path):
    p = _write_map(tmp_path, [{"container_prefix": "/a", "share_url": "smb://h/a"}])
    _point(monkeypatch, p)
    assert len(share_map.get_entries()) == 1
    import os
    import time

    p.write_text(json.dumps([
        {"container_prefix": "/a", "share_url": "smb://h/a"},
        {"container_prefix": "/b", "share_url": "smb://h/b"},
    ]))
    os.utime(p, (time.time() + 5, time.time() + 5))  # bump mtime deterministically
    assert len(share_map.get_entries()) == 2


# --------------------------------------------------------------------------- #
# API tests (Postgres-backed)                                                 #
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def client(pg_uri, monkeypatch, tmp_path):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    # Point the app at a deploy-style share map covering /data/media/media.
    mp = tmp_path / "share-map.json"
    mp.write_text(json.dumps([
        {"container_prefix": "/data/media/media", "share_url": "smb://tower/media",
         "storage_type": "smb", "host": "tower", "unc": r"\\tower\media"},
    ]))
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    monkeypatch.setattr(get_settings(), "share_map_path", str(mp))
    share_map.reset_cache()

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


BASE = "/api/v1/libraries"


async def test_create_auto_populates_from_mount_map(client):
    r = await client.post(BASE, json={"name": "M", "root_path": "/data/media/media"})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["share_prefix"] is None  # raw manual override untouched
    assert b["share_prefix_effective"] == "smb://tower/media"
    assert b["share_prefix_source"] == "mount-map"


async def test_manual_override_wins(client):
    r = await client.post(
        BASE,
        json={"name": "M2", "root_path": "/data/media/media",
              "share_prefix": r"\\custom\share"},
    )
    b = r.json()
    assert b["share_prefix_effective"] == r"\\custom\share"
    assert b["share_prefix_source"] == "manual"


async def test_uncovered_root_has_no_effective(client):
    r = await client.post(BASE, json={"name": "M3", "root_path": "/data/elsewhere"})
    b = r.json()
    assert b["share_prefix_effective"] is None
    assert b["share_prefix_source"] == "none"


async def test_patch_clearing_manual_falls_back_to_map(client):
    r = await client.post(
        BASE,
        json={"name": "M4", "root_path": "/data/media/media",
              "share_prefix": "smb://manual/x"},
    )
    lib_id = r.json()["id"]
    # clear the manual override -> effective falls back to the mount map live
    r2 = await client.patch(f"{BASE}/{lib_id}", json={"share_prefix": None})
    assert r2.status_code == 200, r2.text
    b = r2.json()
    assert b["share_prefix"] is None
    assert b["share_prefix_effective"] == "smb://tower/media"
    assert b["share_prefix_source"] == "mount-map"


async def test_list_libraries_carries_effective(client):
    await client.post(BASE, json={"name": "M5", "root_path": "/data/media/media"})
    r = await client.get(BASE)
    assert r.status_code == 200
    row = next(x for x in r.json() if x["name"] == "M5")
    assert row["share_prefix_effective"] == "smb://tower/media"
    assert row["share_prefix_source"] == "mount-map"


async def test_library_carries_share_unc_effective(client):
    await client.post(BASE, json={"name": "U1", "root_path": "/data/media/media"})
    r = await client.get(BASE)
    row = next(x for x in r.json() if x["name"] == "U1")
    assert row["share_prefix_effective"] == "smb://tower/media"
    assert row["share_unc_effective"] == r"\\tower\media"


async def test_library_manual_unc_derives_url_side(client):
    r = await client.post(
        BASE,
        json={"name": "U2", "root_path": "/data/elsewhere",
              "share_prefix": r"\\host\m"},
    )
    b = r.json()
    assert b["share_prefix_effective"] == r"\\host\m"
    assert b["share_unc_effective"] == r"\\host\m"


async def test_library_manual_sftp_has_no_unc(client):
    r = await client.post(
        BASE,
        json={"name": "U3", "root_path": "/data/elsewhere",
              "share_prefix": "sftp://h/p"},
    )
    b = r.json()
    assert b["share_prefix_effective"] == "sftp://h/p"
    assert b["share_unc_effective"] is None


async def test_system_share_map_endpoint(client):

    r = await client.get("/api/v1/system/share-map")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 1
    assert data[0]["container_prefix"] == "/data/media/media"
    assert data[0]["share_url"] == "smb://tower/media"
    assert data[0]["unc"] == r"\\tower\media"
    # never leaks credentials: only the documented fields are present
    assert set(data[0]) == {"container_prefix", "share_url", "storage_type", "host", "unc"}


# --------------------------------------------------------------------------- #
# Reports path-context uses the effective value (pure — no DB)                #
# --------------------------------------------------------------------------- #
def test_reports_path_context_uses_mount_map(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from filearr.reports import _path_context

    _point(monkeypatch, _write_map(tmp_path, _SAMPLE))
    # No manual share_prefix -> share_url resolved from the deploy mount map
    # against the item's absolute container path.
    row = SimpleNamespace(
        rel_path="Movies/x.mkv",
        path="/data/media/media/Movies/x.mkv",
        native_prefix=None,
        share_prefix=None,
    )
    ctx = _path_context(row)
    assert ctx["share_url"] == "smb://tower/Media Management/Movies/x.mkv"

    # Manual share_prefix still wins (map ignored).
    row2 = SimpleNamespace(
        rel_path="Movies/x.mkv",
        path="/data/media/media/Movies/x.mkv",
        native_prefix=None,
        share_prefix="smb://manual/lib",
    )
    assert _path_context(row2)["share_url"] == "smb://manual/lib/Movies/x.mkv"
