"""P11-T1/T3/T5/T8 — querydsl->SQL translator + custom-report CRUD/run.

Layers:
* **translator matrix** — each operator compiled to SQL and EXECUTED against
  seeded pgserver rows, asserting on the returned row set (not SQL strings), plus
  injection attempts (quoted/weird keys -> rejected) and the fuzzy->unsupported
  error;
* **migration round-trip** — ``report_definitions`` insert/select;
* **CRUD** — validation (bad DSL 422 w/ position, unknown cf 422), duplicate 409,
  list/get/patch/delete, the column registry + dry-run validate endpoints;
* **run** — JSON round-trip vs. an equivalent direct query, and a streaming CSV.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.custom_fields import CustomFieldDef
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import CustomField, Item, ItemStatus, Library, ReportDefinition
from filearr.query_sql import QueryTranslationError, ast_to_where
from filearr.querydsl import ParseError, parse

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def env(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM report_definitions"))
        await conn.execute(text("DELETE FROM custom_fields"))
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


async def _mk_lib(maker, name="Lib", *, native_prefix=None, share_prefix=None):
    async with maker() as s:
        lib = Library(
            name=name,
            root_path="/data/l",
            native_prefix=native_prefix,
            share_prefix=share_prefix,
        )
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(maker, library_id, rel_path, **kw):
    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category=kw.get("file_category", "other"),
            file_group=kw.get("file_group"),
            status=kw.get("status", ItemStatus.active),
            path=f"/data/l/{rel_path}",
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            extension=kw.get("extension", "bin"),
            size=kw.get("size", 100),
            mtime=kw.get("mtime") or datetime.now(UTC),
            metadata_=kw.get("metadata") or {},
            user_metadata=kw.get("user_metadata") or {},
            external_ids={},
            title=kw.get("title"),
            tags=kw.get("tags") or [],
            content_hash=kw.get("content_hash"),
            quick_hash=kw.get("quick_hash"),
            first_seen=kw.get("first_seen") or datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def _matches(maker, query, defs=None):
    """Compile+execute a query, return the set of matching rel_paths."""
    where = ast_to_where(parse(query), defs or {})
    async with maker() as s:
        rows = (
            await s.execute(
                select(Item.rel_path).where(Item.status == ItemStatus.active, where)
            )
        ).scalars().all()
    return set(rows)


# --------------------------------------------------------------------------- #
# Translator matrix (executed)                                                #
# --------------------------------------------------------------------------- #
async def test_translator_core_filters(env):
    _client, maker = env
    lib = await _mk_lib(maker)
    old = datetime.now(UTC) - timedelta(days=400)
    await _mk_item(maker, lib, "Movies/a.mkv", file_category="video", file_group="video",
                   extension="mkv", size=5_000_000_000, tags=["keep"],
                   quick_hash="deadbeef", content_hash="cafe1234",
                   metadata={"height": 1080, "video_codec": "hevc", "bitrate": 6_000_000},
                   title="Alpha")
    await _mk_item(maker, lib, "Music/b.mp3", file_category="audio", file_group="audio-lossy",
                   extension="mp3", size=3_000_000, tags=["archived"],
                   metadata={"bitrate": 128000})
    await _mk_item(maker, lib, "Movies/c.mp4", file_category="video", file_group="video",
                   extension="mp4", size=700_000, mtime=old,
                   metadata={"height": 480, "video_codec": "mpeg4", "bitrate": 500000})

    assert await _matches(maker, "kind:video") == {"Movies/a.mkv", "Movies/c.mp4"}
    assert await _matches(maker, "ext:mp3;mp4") == {"Music/b.mp3", "Movies/c.mp4"}
    assert await _matches(maker, "size:>1G") == {"Movies/a.mkv"}
    assert await _matches(maker, "size:1M..10M") == {"Music/b.mp3"}
    assert await _matches(maker, "path:Movies/") == {"Movies/a.mkv", "Movies/c.mp4"}
    assert await _matches(maker, "tag:keep") == {"Movies/a.mkv"}
    assert await _matches(maker, "hash:cafe1234") == {"Movies/a.mkv"}
    assert await _matches(maker, "hash:deadbeef") == {"Movies/a.mkv"}
    # free text over filename/title/rel_path
    assert await _matches(maker, "alpha") == {"Movies/a.mkv"}
    # negation
    assert await _matches(maker, "kind:video -tag:keep") == {"Movies/c.mp4"}


async def test_translator_group_filter(env):
    """W8-D: the ``group:`` keyword maps to ``file_group`` (mirrors ``kind:``)."""
    _client, maker = env
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "Photos/a.cr2", file_category="image",
                   file_group="raw-photo", extension="cr2")
    await _mk_item(maker, lib, "Photos/b.jpg", file_category="image",
                   file_group="raster-photo", extension="jpg")
    await _mk_item(maker, lib, "Archives/c.zip", file_category="archive",
                   file_group="archive", extension="zip")

    # group:raw-photo -> Item.file_group == "raw-photo"
    assert await _matches(maker, "group:raw-photo") == {"Photos/a.cr2"}
    # uppercase value is lower-cased on parse, still matches
    assert await _matches(maker, "group:RAW-PHOTO") == {"Photos/a.cr2"}
    # negation excludes the group
    assert await _matches(maker, "-group:raw-photo") == {"Photos/b.jpg", "Archives/c.zip"}
    # an unknown group is a translation error (validated against FILE_GROUPS)
    with pytest.raises(QueryTranslationError):
        ast_to_where(parse("group:bogus"), {})


async def test_translator_time_filters(env):
    _client, maker = env
    lib = await _mk_lib(maker)
    recent = datetime.now(UTC) - timedelta(days=2)
    old = datetime.now(UTC) - timedelta(days=100)
    await _mk_item(maker, lib, "recent.txt", mtime=recent, first_seen=recent)
    await _mk_item(maker, lib, "old.txt", mtime=old, first_seen=old)
    # modified within last 7d
    assert await _matches(maker, "modified:<7d") == {"recent.txt"}
    # modified older than 30d
    assert await _matches(maker, "modified:>30d") == {"old.txt"}
    # created (first_seen) absolute date range
    future = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    weekago = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    assert await _matches(maker, f"created:{weekago}..{future}") == {"recent.txt"}


async def test_translator_meta_and_cf(env):
    _client, maker = env
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "hd.mkv", file_category="video", file_group="video",
                   metadata={"resolution": "1080p", "height": 1080, "bitrate": 6_000_000},
                   user_metadata={"rating": 5, "status": "keep"})
    await _mk_item(maker, lib, "sd.mkv", file_category="video", file_group="video",
                   metadata={"resolution": "480p", "height": 480, "bitrate": 500000},
                   user_metadata={"rating": 2, "status": "archived"})
    defs = {
        "rating": CustomFieldDef(name="rating", label="R", data_type="integer"),
        "status": CustomFieldDef(name="status", label="S", data_type="string"),
    }
    assert await _matches(maker, "meta.resolution:1080p") == {"hd.mkv"}
    assert await _matches(maker, "meta.height:>=1000") == {"hd.mkv"}
    assert await _matches(maker, "meta.height:400..600") == {"sd.mkv"}
    assert await _matches(maker, "cf.rating:>3", defs) == {"hd.mkv"}
    assert await _matches(maker, "cf.rating:5", defs) == {"hd.mkv"}
    assert await _matches(maker, "cf.status:archived", defs) == {"sd.mkv"}


async def test_translator_injection_and_unsupported(env):
    _client, maker = env
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "x.txt", metadata={"a'b": 1})
    # A quoted weird key never becomes a filter — it is free text, so it can't
    # inject a JSONB path; it simply matches nothing here.
    assert await _matches(maker, '"meta.a:1"') == set()
    # weird-charset dynamic key is a hard parse error (never reaches SQL)
    with pytest.raises(ParseError):
        parse("meta.a';drop:1")
    # fuzzy term has no SQL predicate -> explicit unsupported error
    with pytest.raises(QueryTranslationError) as ei:
        ast_to_where(parse("~fuzzy"), {})
    assert ei.value.unsupported == ["fuzzy"]
    # unknown kind / non-numeric numeric operand -> translation error
    with pytest.raises(QueryTranslationError):
        ast_to_where(parse("kind:bogus"), {})
    with pytest.raises(QueryTranslationError):
        ast_to_where(parse("meta.height:>abc"), {})


# --------------------------------------------------------------------------- #
# Migration round-trip                                                         #
# --------------------------------------------------------------------------- #
async def test_report_definitions_roundtrip(env):
    _client, maker = env
    async with maker() as s:
        row = ReportDefinition(
            name="rt", query="kind:video", columns=["rel_path", "size"], sort="-size"
        )
        s.add(row)
        await s.commit()
        got = (await s.execute(select(ReportDefinition))).scalars().one()
        assert got.name == "rt"
        assert list(got.columns) == ["rel_path", "size"]
        assert got.format == "csv"
        assert got.created_at is not None


# --------------------------------------------------------------------------- #
# CRUD + validation                                                            #
# --------------------------------------------------------------------------- #
async def _mk_cf(maker, name, data_type="integer"):
    async with maker() as s:
        s.add(CustomField(name=name, label=name.title(), data_type=data_type))
        await s.commit()


async def test_create_and_run_json_roundtrip(env):
    client, maker = env
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "Movies/a.mkv", file_category="video", file_group="video",
                   size=5_000_000_000, metadata={"height": 1080})
    await _mk_item(
        maker, lib, "Music/b.mp3", file_category="audio", file_group="audio-lossy",
        size=3_000_000,
    )

    r = await client.post("/api/v1/custom-reports", json={
        "name": "videos",
        "query": "kind:video",
        "columns": ["rel_path", "library", "size", "meta.height"],
        "sort": "-size",
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    run = await client.get(f"/api/v1/custom-reports/{rid}/run?format=json")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["columns"] == ["rel_path", "library", "size", "meta.height"]
    assert [row["rel_path"] for row in body["rows"]] == ["Movies/a.mkv"]
    assert body["rows"][0]["library"] == "Lib"
    assert body["rows"][0]["meta.height"] == 1080
    # matches the equivalent direct translation
    assert await _matches(maker, "kind:video") == {"Movies/a.mkv"}


async def test_create_bad_dsl_returns_422_with_position(env):
    client, _maker = env
    r = await client.post("/api/v1/custom-reports", json={
        "name": "bad", "query": '"unterminated', "columns": ["rel_path"],
    })
    assert r.status_code == 422
    detail = r.json()["detail"]["validation"][0]
    assert detail["error"] == "parse_error"
    assert detail["code"] == "unterminated_quote"
    assert detail["position"] == 0


async def test_create_unknown_cf_column_422(env):
    client, _maker = env
    r = await client.post("/api/v1/custom-reports", json={
        "name": "badcol", "query": "kind:video", "columns": ["cf.nope"],
    })
    assert r.status_code == 422
    assert r.json()["detail"]["validation"][0]["error"] == "column_error"


async def test_create_unknown_cf_in_query_422(env):
    client, _maker = env
    r = await client.post("/api/v1/custom-reports", json={
        "name": "badq", "query": "cf.ghost:1", "columns": ["rel_path"],
    })
    assert r.status_code == 422
    assert r.json()["detail"]["validation"][0]["error"] == "translation_error"


async def test_cf_column_valid_when_registered(env):
    client, maker = env
    await _mk_cf(maker, "rating")
    r = await client.post("/api/v1/custom-reports", json={
        "name": "withcf", "query": "cf.rating:>3", "columns": ["rel_path", "cf.rating"],
    })
    assert r.status_code == 201, r.text


async def test_duplicate_name_409(env):
    client, _maker = env
    payload = {"name": "dup", "query": "kind:video", "columns": ["rel_path"]}
    assert (await client.post("/api/v1/custom-reports", json=payload)).status_code == 201
    assert (await client.post("/api/v1/custom-reports", json=payload)).status_code == 409


async def test_list_get_patch_delete(env):
    client, _maker = env
    r = await client.post("/api/v1/custom-reports", json={
        "name": "crud", "query": "kind:video", "columns": ["rel_path"]})
    rid = r.json()["id"]
    assert len((await client.get("/api/v1/custom-reports")).json()) == 1
    assert (await client.get(f"/api/v1/custom-reports/{rid}")).json()["name"] == "crud"
    p = await client.patch(f"/api/v1/custom-reports/{rid}", json={"query": "kind:audio"})
    assert p.status_code == 200 and p.json()["query"] == "kind:audio"
    # patch to an invalid query is rejected
    bad = await client.patch(f"/api/v1/custom-reports/{rid}", json={"query": "cf.x:1"})
    assert bad.status_code == 422
    assert (await client.delete(f"/api/v1/custom-reports/{rid}")).status_code == 204
    assert (await client.get(f"/api/v1/custom-reports/{rid}")).status_code == 404


async def test_columns_registry_and_validate_endpoints(env):
    client, maker = env
    await _mk_cf(maker, "rating")
    cols = (await client.get("/api/v1/custom-reports/columns")).json()
    assert "rel_path" in cols["core"] and "library" in cols["core"]
    assert cols["custom_fields"] == ["rating"]
    ok = await client.post("/api/v1/custom-reports/validate", json={
        "query": "kind:video meta.height:>1080", "columns": ["rel_path", "cf.rating"]})
    assert ok.json() == {"ok": True, "errors": []}
    bad = await client.post("/api/v1/custom-reports/validate", json={
        "query": "kind:video ~fuzzy", "columns": ["rel_path"]})
    body = bad.json()
    assert body["ok"] is False
    assert body["errors"][0]["error"] == "translation_error"


async def test_run_csv_streams_and_guards_formula(env):
    client, maker = env
    lib = await _mk_lib(maker)
    # a filename beginning with '=' must be neutralised in CSV output
    await _mk_item(maker, lib, "=danger.mkv", file_category="video", file_group="video", size=10)
    await _mk_item(maker, lib, "safe.mkv", file_category="video", file_group="video", size=20)
    r = await client.post("/api/v1/custom-reports", json={
        "name": "csvrep", "query": "kind:video", "columns": ["rel_path", "size"]})
    rid = r.json()["id"]
    resp = await client.get(f"/api/v1/custom-reports/{rid}/run?format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    assert "rel_path,size" in body
    assert "'=danger.mkv" in body  # OWASP formula guard applied

    # Streaming property asserted at the StreamingResponse generator level (httpx
    # coalesces chunks): the body yields the header + one chunk per row off the
    # server-side cursor, so peak memory is ~one row.
    from filearr.api.reports import _csv_response
    from filearr.custom_reports import build_custom_report
    from filearr.reports import ReportParams

    report = build_custom_report(
        report_id="c", name="c", query="kind:video",
        columns=["rel_path", "size"], sort="rel_path", custom_defs={},
    )
    sresp = _csv_response(report, ReportParams(limit=1000))
    chunks = [c async for c in sresp.body_iterator]
    assert len(chunks) >= 3  # header + 2 rows, each its own yield


# --------------------------------------------------------------------------- #
# P11 polish — custom-report item_id, path columns, streaming formats         #
# --------------------------------------------------------------------------- #
async def test_custom_run_json_rows_include_item_id(env):
    client, maker = env
    lib = await _mk_lib(maker)
    fid = await _mk_item(maker, lib, "a.mkv", file_category="video", file_group="video", size=10)
    r = await client.post("/api/v1/custom-reports", json={
        "name": "vids", "query": "kind:video", "columns": ["rel_path", "size"]})
    rid = r.json()["id"]
    body = (await client.get(f"/api/v1/custom-reports/{rid}/run?format=json")).json()
    # item_id rides in every row (for ItemDetail) but is NOT a projected column
    assert body["columns"] == ["rel_path", "size"]
    assert body["rows"][0]["item_id"] == fid
    assert body["report"] and "item_id" not in body["columns"]


async def test_custom_path_context_columns(env):
    client, maker = env
    lib = await _mk_lib(
        maker, native_prefix="/mnt/user/media", share_prefix="smb://tower/media"
    )
    await _mk_item(maker, lib, "Movies/a.mkv", file_category="video", file_group="video", size=10)
    # native_path + share_url are computed core columns now
    cols = (await client.get("/api/v1/custom-reports/columns")).json()
    assert "native_path" in cols["core"] and "share_url" in cols["core"]
    assert "share_unc" in cols["core"]  # UI-T15
    assert set(cols["formats"]) == {"csv", "json", "ndjson", "xml"}
    r = await client.post("/api/v1/custom-reports", json={
        "name": "paths", "query": "kind:video",
        "columns": ["rel_path", "native_path", "share_url", "share_unc"]})
    rid = r.json()["id"]
    row = (await client.get(f"/api/v1/custom-reports/{rid}/run?format=json")).json()["rows"][0]
    assert row["native_path"] == "/mnt/user/media/Movies/a.mkv"
    assert row["share_url"] == "smb://tower/media/Movies/a.mkv"
    # UI-T15: SMB URL prefix -> derived UNC counterpart
    assert row["share_unc"] == "\\\\tower\\media\\Movies\\a.mkv"


async def test_custom_ndjson_and_xml_exports(env):
    import json as _json
    import xml.etree.ElementTree as ET

    client, maker = env
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "a.mkv", file_category="video", file_group="video", size=10)
    await _mk_item(maker, lib, "b.mkv", file_category="video", file_group="video", size=20)
    r = await client.post("/api/v1/custom-reports", json={
        "name": "exp", "query": "kind:video", "columns": ["rel_path", "size"]})
    rid = r.json()["id"]

    nd = await client.get(f"/api/v1/custom-reports/{rid}/run?format=ndjson")
    assert nd.headers["content-type"].startswith("application/x-ndjson")
    lines = [ln for ln in nd.text.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert all("item_id" in _json.loads(ln) for ln in lines)

    xr = await client.get(f"/api/v1/custom-reports/{rid}/run?format=xml")
    assert xr.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(xr.text)
    assert root.tag == "report" and len(root.findall("row")) == 2


async def test_custom_bad_format_422(env):
    client, maker = env
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "a.mkv", file_category="video", file_group="video")
    r = await client.post("/api/v1/custom-reports", json={
        "name": "x", "query": "kind:video", "columns": ["rel_path"]})
    rid = r.json()["id"]
    assert (await client.get(f"/api/v1/custom-reports/{rid}/run?format=parquet")).status_code == 422
