"""P11 reporting v1 — canned reports, registry, JSON pagination, CSV streaming.

Layers:
* **pure** scorer matrix (`filearr.quality_score.score_item`) — no DB;
* **integration** (pgserver + alembic) over the canned registry and endpoints:
  each report against seeded rows, JSON pagination, strict param validation,
  unknown-report 404, and a CSV export asserted to STREAM (multi-chunk, one row
  per generator step) with the OWASP formula-injection guard applied.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, Library, MediaType
from filearr.quality_score import (
    BAND_OK,
    BAND_REACQUIRE,
    BAND_REVIEW,
    score_item,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure scorer matrix (P11-T7)                                                  #
# --------------------------------------------------------------------------- #
def test_score_well_encoded_1080p_hevc_is_ok():
    res = score_item(
        {"height": 1080, "width": 1920, "video_codec": "hevc", "bitrate": 5_000_000}
    )
    assert res.score == 0
    assert res.band == BAND_OK
    assert res.reasons == []


def test_score_sub_hd_only_fires_resolution():
    res = score_item(
        {"height": 480, "width": 640, "video_codec": "mpeg4", "bitrate": 1_500_000}
    )
    # sub-HD (+40) only; bare mpeg4 is deliberately NOT a legacy codec.
    assert res.score == 40
    assert res.band == BAND_REVIEW
    assert any("sub-HD" in r for r in res.reasons)


def test_score_legacy_codec_and_sub_hd_stack_to_reacquire():
    res = score_item(
        {"height": 576, "width": 720, "video_codec": "mpeg2video", "bitrate": 3_000_000}
    )
    assert res.score == 40 + 25  # sub-HD + legacy codec
    assert res.band == BAND_REACQUIRE
    assert any("legacy codec" in r for r in res.reasons)


def test_score_low_bitrate_1080p_fires_bitrate_component():
    # 500 kbps 1080p h264: 0.00024 kbpp << 0.00080 floor -> full +25.
    res = score_item(
        {"height": 1080, "width": 1920, "video_codec": "h264", "bitrate": 500_000}
    )
    assert res.score == 25
    assert res.band == BAND_REVIEW
    assert any("low bitrate" in r for r in res.reasons)


def test_score_efficient_codec_halves_bitrate_floor():
    # Same bitrate that would trip h264 stays under the halved hevc floor here,
    # but a healthy hevc bitrate must NOT fire.
    ok = score_item(
        {"height": 1080, "width": 1920, "video_codec": "hevc", "bitrate": 2_500_000}
    )
    assert ok.score == 0


def test_score_4k_stereo_only_audio_downmix():
    res = score_item(
        {
            "height": 2160,
            "width": 3840,
            "video_codec": "hevc",
            "bitrate": 25_000_000,
            "audio_tracks": [{"codec": "aac", "channels": 2}],
        }
    )
    assert res.score == 10
    assert any("stereo-only" in r for r in res.reasons)


def test_score_4k_lossless_audio_not_flagged():
    res = score_item(
        {
            "height": 2160,
            "width": 3840,
            "video_codec": "hevc",
            "bitrate": 25_000_000,
            "audio_tracks": [{"codec": "truehd", "channels": 8}],
        }
    )
    assert res.score == 0


def test_score_hdr_mismatch():
    res = score_item(
        {
            "height": 2160,
            "width": 3840,
            "video_codec": "hevc",
            "bitrate": 30_000_000,
            "hdr": True,
            "color_transfer": "bt709",
        }
    )
    assert res.score == 10
    assert any("HDR" in r for r in res.reasons)


def test_score_missing_fields_never_raises():
    assert score_item({}).score == 0
    assert score_item({"height": "not-a-number"}).score == 0


# --------------------------------------------------------------------------- #
# Integration fixture (real Postgres)                                         #
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


async def _mk_item(
    maker,
    library_id,
    rel_path,
    *,
    media_type=MediaType.other,
    status="active",
    extension="bin",
    size=100,
    mtime=None,
    metadata=None,
    content_hash=None,
    quick_hash=None,
):
    async with maker() as s:
        item = Item(
            library_id=library_id,
            media_type=media_type,
            status=status,
            path=f"/data/l/{rel_path}",
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            extension=extension,
            size=size,
            mtime=mtime or datetime.now(UTC),
            metadata_=metadata or {},
            user_metadata={},
            external_ids={},
            content_hash=content_hash,
            quick_hash=quick_hash,
            tags=[],
        )
        s.add(item)
        await s.commit()
        return str(item.id)


# --------------------------------------------------------------------------- #
# Registry + validation                                                       #
# --------------------------------------------------------------------------- #
async def test_registry_lists_all_six(api):
    client, _ = api
    r = await client.get("/api/v1/reports")
    assert r.status_code == 200
    ids = {rep["id"] for rep in r.json()["reports"]}
    assert ids == {
        "unmapped_extensions",
        "bad_mtime",
        "corrupt_media",
        "largest_files",
        "low_quality_video",
        "duplicate_files",
    }


async def test_unknown_report_404(api):
    client, _ = api
    r = await client.get("/api/v1/reports/nope")
    assert r.status_code == 404


async def test_bad_format_and_limit_422(api):
    client, _ = api
    assert (await client.get("/api/v1/reports/largest_files?format=parquet")).status_code == 422
    assert (await client.get("/api/v1/reports/largest_files?limit=0")).status_code == 422
    assert (
        await client.get("/api/v1/reports/largest_files?limit=99999999")
    ).status_code == 422
    assert (await client.get("/api/v1/reports/largest_files?offset=-1")).status_code == 422


# --------------------------------------------------------------------------- #
# Canned reports                                                              #
# --------------------------------------------------------------------------- #
async def test_unmapped_extensions_grouped_counts(api):
    client, maker = api
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "a.foo", extension="foo", size=10)
    await _mk_item(maker, lib, "b.foo", extension="foo", size=20)
    await _mk_item(maker, lib, "c.bar", extension="bar", size=5)
    # a mapped file must not appear
    await _mk_item(maker, lib, "v.mkv", media_type=MediaType.video, extension="mkv")
    r = await client.get("/api/v1/reports/unmapped_extensions")
    rows = r.json()["rows"]
    assert rows[0] == {"extension": "foo", "file_count": 2, "total_bytes": 30}
    assert {row["extension"] for row in rows} == {"foo", "bar"}


async def test_bad_mtime_future_only(api):
    client, maker = api
    lib = await _mk_lib(maker)
    future = datetime.now(UTC) + timedelta(days=3)
    past = datetime.now(UTC) - timedelta(days=1)
    await _mk_item(maker, lib, "future.bin", mtime=future)
    await _mk_item(maker, lib, "normal.bin", mtime=past)
    r = await client.get("/api/v1/reports/bad_mtime")
    rows = r.json()["rows"]
    assert [row["rel_path"] for row in rows] == ["future.bin"]


async def test_corrupt_media_classifies_ffprobe_vs_tag(api):
    client, maker = api
    lib = await _mk_lib(maker)
    await _mk_item(
        maker, lib, "bad.mkv", media_type=MediaType.video,
        metadata={"_extract_error": "ffprobe failed: Invalid data found when processing input"},
    )
    await _mk_item(
        maker, lib, "song.mp3", media_type=MediaType.audio,
        metadata={"_extract_error": "tinytag could not read tags"},
    )
    await _mk_item(maker, lib, "clean.bin", metadata={})
    r = await client.get("/api/v1/reports/corrupt_media")
    rows = {row["rel_path"]: row["error_class"] for row in r.json()["rows"]}
    assert rows == {"bad.mkv": "ffprobe", "song.mp3": "tag"}


async def test_largest_files_capped_and_ordered(api):
    client, maker = api
    lib = await _mk_lib(maker)
    for i, sz in enumerate([100, 500, 250, 999, 10]):
        await _mk_item(maker, lib, f"f{i}.bin", size=sz)
    r = await client.get("/api/v1/reports/largest_files?limit=3")
    rows = r.json()["rows"]
    assert [row["size"] for row in rows] == [999, 500, 250]
    assert r.json()["has_more"] is True


async def test_low_quality_video_scored_and_filtered(api):
    client, maker = api
    lib = await _mk_lib(maker)
    # sub-HD -> flagged (score 40)
    await _mk_item(
        maker, lib, "old.avi", media_type=MediaType.video,
        metadata={"height": 480, "width": 640, "video_codec": "mpeg4", "resolution": "640x480"},
    )
    # well-encoded 1080p hevc -> score 0, excluded (below review band)
    await _mk_item(
        maker, lib, "good.mkv", media_type=MediaType.video,
        metadata={"height": 1080, "width": 1920, "video_codec": "hevc", "bitrate": 5_000_000},
    )
    r = await client.get("/api/v1/reports/low_quality_video")
    rows = r.json()["rows"]
    assert [row["rel_path"] for row in rows] == ["old.avi"]
    assert rows[0]["score"] == 40
    assert rows[0]["band"] == BAND_REVIEW


async def test_duplicate_files_groups_and_wasted_bytes(api):
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    await _mk_item(maker, lib, "a.bin", size=100, content_hash="dead")
    await _mk_item(maker, lib, "b.bin", size=100, content_hash="dead")
    await _mk_item(maker, lib, "c.bin", size=100, content_hash="dead")
    # a quick_hash-only fallback pair (same size)
    await _mk_item(maker, lib, "d.bin", size=50, quick_hash="q1")
    await _mk_item(maker, lib, "e.bin", size=50, quick_hash="q1")
    # a singleton -> excluded
    await _mk_item(maker, lib, "z.bin", size=999, content_hash="beef")
    r = await client.get("/api/v1/reports/duplicate_files")
    rows = {row["dup_key"]: row for row in r.json()["rows"]}
    assert rows["dead"]["copies"] == 3
    assert rows["dead"]["wasted_bytes"] == 200  # 3*100 - 100
    assert "q1:50" in rows
    assert rows["q1:50"]["copies"] == 2
    assert "beef" not in rows


async def test_duplicate_files_hash_tier_column(api):
    """QH-T5: every duplicate row carries a hash_tier ('content_hash' vs
    'quick_hash') so a caller sees whether the grouping is byte-verified or a
    sampled signal, without inferring it client-side."""
    client, maker = api
    lib = await _mk_lib(maker, name="T")
    # content-hash group (byte-verified)
    await _mk_item(maker, lib, "a.bin", size=100, content_hash="dead")
    await _mk_item(maker, lib, "b.bin", size=100, content_hash="dead")
    # quick-hash fallback group (sampled)
    await _mk_item(maker, lib, "d.bin", size=50, quick_hash="q1")
    await _mk_item(maker, lib, "e.bin", size=50, quick_hash="q1")
    r = await client.get("/api/v1/reports/duplicate_files")
    rows = {row["dup_key"]: row for row in r.json()["rows"]}
    assert rows["dead"]["hash_tier"] == "content_hash"
    assert rows["q1:50"]["hash_tier"] == "quick_hash"


async def test_duplicate_files_excludes_zero_byte(api):
    """QH-T5 (§3b): zero-byte files never form a duplicate group — every empty
    file trivially shares quick_hash("")+size=0, which produced the live
    3,711-copy false-positive cluster."""
    client, maker = api
    lib = await _mk_lib(maker, name="Z")
    # A large cluster of empty files sharing the same empty-file quick_hash.
    for name in ("z1.nfo", "z2.nfo", "z3.stl"):
        await _mk_item(maker, lib, name, size=0, quick_hash="emptyhash")
    # A real (non-empty) duplicate group must still appear.
    await _mk_item(maker, lib, "a.bin", size=100, content_hash="dead")
    await _mk_item(maker, lib, "b.bin", size=100, content_hash="dead")
    r = await client.get("/api/v1/reports/duplicate_files")
    rows = {row["dup_key"]: row for row in r.json()["rows"]}
    assert "emptyhash:0" not in rows  # zero-byte cluster suppressed
    assert rows["dead"]["copies"] == 2  # non-empty duplicate group survives


async def test_library_filter(api):
    client, maker = api
    lib1 = await _mk_lib(maker, name="One")
    lib2 = await _mk_lib(maker, name="Two")
    await _mk_item(maker, lib1, "a.foo", extension="foo")
    await _mk_item(maker, lib2, "b.foo", extension="foo")
    r = await client.get(f"/api/v1/reports/unmapped_extensions?library_id={lib1}")
    rows = r.json()["rows"]
    assert rows[0]["file_count"] == 1


# --------------------------------------------------------------------------- #
# Pagination                                                                  #
# --------------------------------------------------------------------------- #
async def test_pagination_offset_limit(api):
    client, maker = api
    lib = await _mk_lib(maker)
    for i in range(5):
        await _mk_item(maker, lib, f"f{i}.bin", size=(10 - i))  # decreasing size
    p1 = (await client.get("/api/v1/reports/largest_files?limit=2&offset=0")).json()
    p2 = (await client.get("/api/v1/reports/largest_files?limit=2&offset=2")).json()
    assert len(p1["rows"]) == 2 and p1["has_more"] is True
    assert p1["rows"][0]["rel_path"] != p2["rows"][0]["rel_path"]


async def test_low_quality_pagination_respects_python_filter(api):
    client, maker = api
    lib = await _mk_lib(maker)
    # three flagged (sub-HD) videos interleaved with a clean one
    for i in range(3):
        await _mk_item(
            maker, lib, f"bad{i}.avi", media_type=MediaType.video, size=100 - i,
            metadata={"height": 480, "width": 640, "video_codec": "mpeg4"},
        )
    await _mk_item(
        maker, lib, "good.mkv", media_type=MediaType.video,
        metadata={"height": 1080, "width": 1920, "video_codec": "hevc", "bitrate": 5_000_000},
    )
    p1 = (await client.get("/api/v1/reports/low_quality_video?limit=2&offset=0")).json()
    assert len(p1["rows"]) == 2
    assert p1["has_more"] is True
    p2 = (await client.get("/api/v1/reports/low_quality_video?limit=2&offset=2")).json()
    assert len(p2["rows"]) == 1  # only 3 flagged rows total (good.mkv excluded)
    assert p2["has_more"] is False


# --------------------------------------------------------------------------- #
# CSV streaming + formula-injection guard                                     #
# --------------------------------------------------------------------------- #
async def test_csv_formula_injection_guarded(api):
    client, maker = api
    lib = await _mk_lib(maker)
    # a filename beginning with '=' is untrusted spreadsheet-formula bait
    await _mk_item(maker, lib, "=cmd|calc.bin", extension="foo", size=1)
    r = await client.get("/api/v1/reports/largest_files?format=csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    # the '=cmd...' path must be defanged with a leading single quote
    assert "'=cmd|calc.bin" in body


async def test_csv_streams_in_chunks(api):
    client, maker = api
    lib = await _mk_lib(maker)
    # bulk-insert a few thousand future-dated rows so the export is large enough
    # to force multiple body chunks off the server-side cursor.
    n = 3000
    future = datetime.now(UTC) + timedelta(days=5)
    async with maker() as s:
        await s.execute(
            text(
                "INSERT INTO items "
                "(library_id, media_type, status, path, rel_path, filename, "
                " extension, size, mtime, metadata, user_metadata, external_ids, tags) "
                "SELECT :lib, 'other', 'active', '/p/'||g, 'f'||g||'.bin', "
                "'f'||g||'.bin', 'bin', 1, :mt, '{}'::jsonb, '{}'::jsonb, "
                "'{}'::jsonb, '{}'::text[] "
                "FROM generate_series(1, :n) AS g"
            ),
            {"lib": str(lib), "mt": future, "n": n},
        )
        await s.commit()

    # Assert at the StreamingResponse generator level: the body yields once per
    # row off the server-side cursor, so peak memory is ~one row, not the whole
    # 3000-row result. (Asserting HTTP chunk count through httpx is unreliable —
    # its ASGI transport coalesces small body events.)
    from filearr.api.reports import _csv_response
    from filearr.reports import ReportParams, get_report

    resp = _csv_response(get_report("bad_mtime"), ReportParams(limit=1000))
    yields = 0
    lines = 0
    async for chunk in resp.body_iterator:
        yields += 1
        lines += chunk.count("\n")
    assert lines == n + 1  # header + n rows
    assert yields >= n  # one yield per row (+header) -> genuinely streamed

    # And a full end-to-end fetch returns the same complete body with CSV headers.
    r = await client.get("/api/v1/reports/bad_mtime?format=csv")
    assert r.status_code == 200
    assert r.text.count("\n") == n + 1


# --------------------------------------------------------------------------- #
# P11 polish — row-link registry field                                        #
# --------------------------------------------------------------------------- #
async def test_registry_exposes_row_link(api):
    client, _ = api
    reports = {r["id"]: r for r in (await client.get("/api/v1/reports")).json()["reports"]}
    assert reports["unmapped_extensions"]["row_link"] == "search_ext"
    assert reports["duplicate_files"]["row_link"] == "search_hash"
    for rid in ("bad_mtime", "corrupt_media", "largest_files", "low_quality_video"):
        assert reports[rid]["row_link"] == "item", rid


# --------------------------------------------------------------------------- #
# P11 polish — item_id + full path context on per-item rows                   #
# --------------------------------------------------------------------------- #
async def test_per_item_reports_carry_item_id(api):
    client, maker = api
    lib = await _mk_lib(maker)
    fid = await _mk_item(maker, lib, "big.bin", size=999)
    row = (await client.get("/api/v1/reports/largest_files")).json()["rows"][0]
    assert row["item_id"] == fid  # matches the DB id so the UI can open ItemDetail


async def test_path_columns_populated_with_prefixes(api):
    client, maker = api
    lib = await _mk_lib(
        maker,
        native_prefix="/mnt/user/media",
        share_prefix="\\\\tower\\media",
    )
    await _mk_item(maker, lib, "Movies/x.bin", size=10)
    row = (await client.get("/api/v1/reports/largest_files")).json()["rows"][0]
    assert set(("path", "native_path", "share_url", "share_unc")).issubset(row)
    assert row["native_path"] == "/mnt/user/media/Movies/x.bin"
    # UNC share prefix -> backslash separators, rel_path slashes rewritten
    assert row["share_url"] == "\\\\tower\\media\\Movies\\x.bin"
    # UI-T15: a UNC prefix's share_unc column mirrors the UNC form
    assert row["share_unc"] == "\\\\tower\\media\\Movies\\x.bin"


async def test_path_columns_null_when_no_prefix(api):
    client, maker = api
    lib = await _mk_lib(maker)  # no native/share prefix
    await _mk_item(maker, lib, "Movies/x.bin", size=10)
    row = (await client.get("/api/v1/reports/largest_files")).json()["rows"][0]
    assert row["native_path"] is None
    assert row["share_url"] is None
    assert row["share_unc"] is None  # UI-T15: null when no prefix
    assert row["path"] == "/data/l/Movies/x.bin"  # container-absolute still present


async def test_duplicate_row_carries_hash_for_link(api):
    client, maker = api
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "a.bin", size=100, content_hash="deadbeef")
    await _mk_item(maker, lib, "b.bin", size=100, content_hash="deadbeef")
    # quick-only fallback group
    await _mk_item(maker, lib, "d.bin", size=50, quick_hash="cafef00d")
    await _mk_item(maker, lib, "e.bin", size=50, quick_hash="cafef00d")
    resp = (await client.get("/api/v1/reports/duplicate_files")).json()
    rows = {r["dup_key"]: r for r in resp["rows"]}
    assert rows["deadbeef"]["content_hash"] == "deadbeef"
    # fallback group: content_hash null, quick_hash present (the search target)
    assert rows["cafef00d:50"]["content_hash"] is None
    assert rows["cafef00d:50"]["quick_hash"] == "cafef00d"


# --------------------------------------------------------------------------- #
# P11 polish — machine-readable streaming formats (NDJSON / XML)              #
# --------------------------------------------------------------------------- #
async def test_ndjson_export_line_per_row(api):
    import json as _json

    client, maker = api
    lib = await _mk_lib(maker)
    for i in range(3):
        await _mk_item(maker, lib, f"f{i}.bin", size=100 - i)
    r = await client.get("/api/v1/reports/largest_files?format=ndjson")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.ndjson"')
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == 3
    objs = [_json.loads(ln) for ln in lines]  # every line is valid JSON
    # item_id rides in NDJSON (ingestion) though it's absent from CSV columns
    assert all("item_id" in o for o in objs)
    assert [o["size"] for o in objs] == [100, 99, 98]


async def test_xml_export_wellformed_and_escapes_hostile(api):
    import xml.etree.ElementTree as ET

    client, maker = api
    lib = await _mk_lib(maker)
    # a rel_path packed with XML metacharacters must not break well-formedness
    hostile = "<>&\"'.bin"
    await _mk_item(maker, lib, hostile, size=5)
    r = await client.get("/api/v1/reports/largest_files?format=xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert r.headers["content-disposition"].endswith('.xml"')
    root = ET.fromstring(r.text)  # raises on any malformed / unescaped output
    assert root.tag == "report"
    cols = {c.get("name"): c.text for c in root.iter("col")}
    assert cols["rel_path"] == hostile  # round-trips exactly after escape/parse


async def test_format_content_type_matrix(api):
    client, maker = api
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "a.bin", size=1)
    cases = {
        "csv": "text/csv",
        "ndjson": "application/x-ndjson",
        "xml": "application/xml",
    }
    for fmt, ctype in cases.items():
        r = await client.get(f"/api/v1/reports/largest_files?format={fmt}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(ctype), fmt
        assert f".{fmt}" in r.headers["content-disposition"], fmt


async def test_streaming_export_honors_optional_cap(api):
    client, maker = api
    lib = await _mk_lib(maker)
    future = datetime.now(UTC) + timedelta(days=5)
    for i in range(5):
        await _mk_item(maker, lib, f"f{i}.bin", size=1, mtime=future)
    # bad_mtime is NOT capped: without limit it streams all 5; with limit=2 it caps.
    full = await client.get("/api/v1/reports/bad_mtime?format=ndjson")
    assert len([ln for ln in full.text.splitlines() if ln.strip()]) == 5
    capped = await client.get("/api/v1/reports/bad_mtime?format=ndjson&limit=2")
    assert len([ln for ln in capped.text.splitlines() if ln.strip()]) == 2


async def test_csv_excludes_item_id_column(api):
    client, maker = api
    lib = await _mk_lib(maker)
    await _mk_item(maker, lib, "a.bin", size=1)
    r = await client.get("/api/v1/reports/largest_files?format=csv")
    header = r.text.splitlines()[0]
    assert "item_id" not in header  # item_id is JSON/NDJSON/XML only, never CSV
    assert "native_path" in header and "share_url" in header  # path context IS in CSV
    assert "share_unc" in header  # UI-T15: UNC column rides the export too
