"""Extractor hotfix batch (live-found error classes A–E + retry-extracts).

A) unvalidated tag data reaching typed columns: coerce_year/coerce_str tame
   date-string years, multi-value lists and garbage before they hit item.year /
   item.title (which previously raised int()/CannotCoerce).
B) belt-and-braces: a DB-layer failure at the extract commit downgrades to
   _extract_error + rollback + a successful job (never a failed extract job).
C) formats tinytag can't read (APE/WavPack/Musepack) fall back to mutagen.
D) malformed PDF date metadata degrades one field, not the whole document.
E) covered by the coercion + the per-field discipline above.
retry-extracts endpoint: clears _extract_error, re-defers errored + never-hashed
   items, returns the count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# A) coercion helpers (pure unit)                                             #
# --------------------------------------------------------------------------- #
def test_coerce_year_from_date_string():
    from filearr.tasks.extract import coerce_year

    # the exact live failure: int('2007-10-09') -> ValueError; now -> 2007
    assert coerce_year("2007-10-09") == 2007
    assert coerce_year("1999") == 1999
    assert coerce_year(2010) == 2010


def test_coerce_year_from_list():
    from filearr.tasks.extract import coerce_year

    # guessit multi-year filename / multi-value tag -> first element
    assert coerce_year([2007, 2008]) == 2007
    assert coerce_year(("1998-01-01", "x")) == 1998
    assert coerce_year([]) is None


def test_coerce_year_garbage_is_none():
    from filearr.tasks.extract import coerce_year

    assert coerce_year("no year here") is None
    assert coerce_year("") is None
    assert coerce_year(None) is None
    # a plausible-but-not-a-year number: only 19xx/20xx runs count
    assert coerce_year("18000") == 1800 or coerce_year("18000") is None


def test_coerce_str_from_list_and_cap():
    from filearr.tasks.extract import coerce_str

    assert coerce_str(["First", "Second"]) == "First"
    assert coerce_str(123) == "123"
    assert coerce_str("  padded  ") == "padded"
    assert coerce_str([]) is None
    assert coerce_str(None) is None
    assert coerce_str("") is None
    assert len(coerce_str("x" * 5000, cap=500)) == 500


def test_extract_audio_coerces_typed_values(monkeypatch, tmp_path):
    """A/E: a full-date year tag and a multi-value title come back typed-safe."""
    from tinytag import TinyTag

    from filearr.tasks.extract import extract_audio

    class _Tag:
        title = ["First", "Second"]
        artist = "The Artist"
        album = "The Album"
        genre = "Ambient"
        year = "2007-10-09"
        duration = 1.5
        bitrate = 128
        samplerate = 44100
        channels = 2

    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda _p: _Tag()))
    f = tmp_path / "a.wav"
    f.write_bytes(b"\x00")
    meta = extract_audio(str(f))
    assert meta["year"] == 2007
    assert meta["title"] == "First"  # list -> first element
    assert meta["artist"] == "The Artist"


# --------------------------------------------------------------------------- #
# C) mutagen fallback for tinytag-unsupported formats (.ape etc.)             #
# --------------------------------------------------------------------------- #
def test_audio_falls_back_to_mutagen_on_unsupported(monkeypatch, tmp_path):
    import mutagen
    from tinytag import TinyTag, UnsupportedFormatError

    from filearr.tasks.extract import extract_audio

    def _unsupported(_p):
        raise UnsupportedFormatError("No tag reader found to support file type")

    monkeypatch.setattr(TinyTag, "get", staticmethod(_unsupported))

    class _Info:
        length = 12.5
        bitrate = 320000
        channels = 2
        sample_rate = 48000

    class _Fake:
        # APEv2 keys are capitalised; values may be lists / date strings
        tags = {"Title": "Ape Song", "Artist": ["Ape Artist"], "Year": "1998-03-02"}
        info = _Info()

        def get(self, k, default=None):
            return self.tags.get(k, default)

    monkeypatch.setattr(mutagen, "File", lambda *a, **k: _Fake())
    f = tmp_path / "track.ape"
    f.write_bytes(b"MAC ")
    meta = extract_audio(str(f))
    assert meta["title"] == "Ape Song"
    assert meta["artist"] == "Ape Artist"  # list -> first
    assert meta["year"] == 1998  # date-string coerced
    assert meta["duration"] == 12.5
    assert meta["channels"] == 2
    assert meta["samplerate"] == 48000


def test_audio_mutagen_fallback_unknown_container(monkeypatch, tmp_path):
    """mutagen.File returns None (unidentifiable) -> empty tags, never a crash."""
    import mutagen
    from tinytag import TinyTag, UnsupportedFormatError

    from filearr.tasks.extract import extract_audio

    monkeypatch.setattr(
        TinyTag, "get", staticmethod(lambda _p: (_ for _ in ()).throw(UnsupportedFormatError("x")))
    )
    monkeypatch.setattr(mutagen, "File", lambda *a, **k: None)
    f = tmp_path / "x.ape"
    f.write_bytes(b"junk")
    assert extract_audio(str(f)) == {}


# --------------------------------------------------------------------------- #
# D) malformed PDF date -> per-field degrade, not whole-document failure       #
# --------------------------------------------------------------------------- #
def test_pdf_malformed_date_keeps_raw_and_other_fields(tmp_path):
    pypdf = pytest.importorskip("pypdf")

    from filearr.tasks.documents import extract_pdf

    out = tmp_path / "baddate.pdf"
    w = pypdf.PdfWriter()
    w.add_blank_page(width=100, height=100)
    w.add_metadata({"/Title": "Doc", "/CreationDate": "2006/05/24 21:06"})
    with open(out, "wb") as f:
        w.write(f)

    # previously raised "Can not convert date" and failed the whole extract
    meta = extract_pdf(str(out), max_bytes=1 << 30)
    assert meta["pages"] == 1
    assert meta["title"] == "Doc"
    # malformed date preserved as its raw string rather than dropped/erroring
    assert meta["created"] == "2006/05/24 21:06"
    assert "_extract_error" not in meta


# --------------------------------------------------------------------------- #
# B) commit-failure downgrade (DB round-trip)                                 #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def pg(module_db):
    return module_db


@pytest.fixture
async def db(pg, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    uri = pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)

    from filearr.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)

    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync

    monkeypatch.setattr(extract_mod, "SessionLocal", Session)

    async def _noop_defer(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop_defer)
    yield Session
    await engine.dispose()


def _file_facts(path: str):
    p = Path(path)
    return p.name, p.suffix.lstrip("."), p.stat().st_size


async def _make_item(Session, media_type, path: str):
    from filearr.file_groups import detect_category, detect_group
    from filearr.models import Item, Library

    name, ext, size = _file_facts(path)
    async with Session() as s:
        lib = Library(name=f"lib-{name}", root_path="/root")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category=detect_category(path),
            file_group=detect_group(path),
            path=path,
            rel_path=name,
            filename=name,
            extension=ext,
            size=size,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_commit_failure_downgrades_to_extract_error(db, stl_cube, monkeypatch):
    """A DB-layer failure at the first commit must NOT fail the job: it rolls
    back, re-records only _extract_error, and commits successfully. quick_hash
    stays NULL (rolled back) so the null-quick_hash self-heal also covers it."""
    import sqlalchemy.ext.asyncio as sa_async
    from sqlalchemy import select

    from filearr.models import Item
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "model3d", str(stl_cube))

    orig_commit = sa_async.AsyncSession.commit
    calls = {"n": 0}

    async def flaky_commit(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated CannotCoerce: smallint[] -> integer")
        return await orig_commit(self)

    monkeypatch.setattr(sa_async.AsyncSession, "commit", flaky_commit)

    await extract_item(item_id)  # must NOT raise
    monkeypatch.setattr(sa_async.AsyncSession, "commit", orig_commit)

    async with db() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.metadata_["_extract_error"]
    assert "simulated CannotCoerce" in item.metadata_["_extract_error"]
    assert item.quick_hash is None  # rolled back -> self-heal will requeue


# --------------------------------------------------------------------------- #
# retry-extracts endpoint                                                     #
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def wired(pg_uri, monkeypatch):
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command
    from filearr import db as db_mod
    from filearr.api import libraries as lib_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM api_keys"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    deferred: list[list[str]] = []

    async def _fake_defer(ids):
        deferred.append(list(ids))

    monkeypatch.setattr(lib_mod, "defer_extract", _fake_defer)

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    yield {"app": app, "maker": maker, "deferred": deferred}
    app.dependency_overrides.clear()
    await engine.dispose()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _mk_item(maker, library_id, rel_path, *, metadata=None, quick_hash=None, status="active"):
    from filearr.models import Item

    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category="audio", file_group="audio-lossy",
            status=status,
            path=f"/d/{rel_path}",
            rel_path=rel_path,
            filename=rel_path,
            extension="mp3",
            size=1,
            mtime=datetime.now(UTC),
            metadata_=metadata or {},
            quick_hash=quick_hash,
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_retry_extracts_clears_errors_and_redefers(wired):
    from sqlalchemy import select

    from filearr.models import Item, Library

    maker = wired["maker"]
    async with maker() as s:
        lib = Library(name="music", root_path="/d")
        s.add(lib)
        await s.commit()
        lib_id = lib.id

    errored = await _mk_item(
        maker, lib_id, "bad.mp3", metadata={"_extract_error": "int('2007-10-09')"}, quick_hash="h1"
    )
    nohash = await _mk_item(maker, lib_id, "died.mp3", metadata={}, quick_hash=None)
    clean = await _mk_item(maker, lib_id, "ok.mp3", metadata={"title": "x"}, quick_hash="h2")
    # a missing errored item must be ignored (status filter)
    await _mk_item(
        maker, lib_id, "gone.mp3",
        metadata={"_extract_error": "x"}, quick_hash="h3", status="missing",
    )

    async with _client(wired["app"]) as c:
        r = await c.post(f"/api/v1/libraries/{lib_id}/retry-extracts")
        assert r.status_code == 200
        body = r.json()
        assert body["retried"] == 2  # errored + never-hashed, active only

    # both affected ids were re-deferred (one batch)
    assert wired["deferred"], "defer_extract was not called"
    deferred_ids = {i for batch in wired["deferred"] for i in batch}
    assert deferred_ids == {errored, nohash}

    # the _extract_error marker was cleared on the errored item
    async with maker() as s:
        it = (await s.execute(select(Item).where(Item.id == errored))).scalar_one()
        assert "_extract_error" not in it.metadata_
        # the clean item is untouched (not requeued, metadata intact)
        ok = (await s.execute(select(Item).where(Item.id == clean))).scalar_one()
        assert ok.metadata_.get("title") == "x"


async def test_retry_extracts_unknown_library_404(wired):
    async with _client(wired["app"]) as c:
        r = await c.post(
            "/api/v1/libraries/00000000-0000-0000-0000-000000000000/retry-extracts"
        )
        assert r.status_code == 404
