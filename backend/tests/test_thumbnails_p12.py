"""S12/P12 slice 1 thumbnails — pure keying/fanout, WebP generation + byte caps,
audio-cover extraction, sidecar-artwork-first precedence, serve endpoint
(headers/404/tier), manifest upsert + orphan GC (both directions), and the
extract ride-along deferral.

A single pgserver (the shared session ``pg_uri``) backs the DB tests; proc_app is
opened against it so the ride-along ``defer_async`` enqueues a real job."""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import thumbs as th
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, Library, MediaType, ThumbnailManifest

from .conftest import FFMPEG, requires_ffmpeg


@pytest.fixture(autouse=True)
def _no_disk_guard(monkeypatch):
    """FIX-11 guard reads the REAL statvfs of the tmp-based store; force 'ok'
    here (guard behaviour is covered by test_diskguard_fix11.py)."""
    from filearr import diskguard

    monkeypatch.setattr(diskguard, "is_critical", lambda *a, **k: False)
    monkeypatch.setattr(diskguard, "guard_write", lambda *a, **k: None)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure keying / fanout / generation (no DB).                                   #
# --------------------------------------------------------------------------- #

class _Settings:
    thumbnail_grid_px = 320
    thumbnail_preview_px = 800
    thumbnail_grid_quality = 70
    thumbnail_preview_quality = 78
    thumbnail_grid_max_bytes = 20_000
    thumbnail_preview_max_bytes = 60_000
    thumbnail_quality_floor = 40
    thumbnail_quality_step = 10
    thumbnail_max_pixels = 50_000_000
    thumbnail_generator_version = 1
    # P12-T5 PDF knobs.
    document_max_bytes = 268_435_456
    thumbnail_pdf_max_pixels = 16_000_000
    config_dir = "/tmp/filearr-thumb-pure"
    # P12 slice 2 video-frame knobs.
    ffmpeg_path = "ffmpeg"
    thumb_ffmpeg_timeout_s = 60.0
    thumbnail_video_min_seek_s = 1.0
    thumbnail_video_max_frame_bytes = 33_554_432
    thumb_accel = "auto"
    thumb_hdr_tonemap = True


def test_cache_key_is_deterministic_hex_and_axis_sensitive():
    k = th.cache_key("deadbeef", 1, th.TIER_GRID)
    assert k == th.cache_key("deadbeef", 1, th.TIER_GRID)  # deterministic
    assert len(k) == 32 and all(c in "0123456789abcdef" for c in k)
    # Distinct on every axis: hash, generator version, tier.
    assert k != th.cache_key("deadbee0", 1, th.TIER_GRID)
    assert k != th.cache_key("deadbeef", 2, th.TIER_GRID)
    assert k != th.cache_key("deadbeef", 1, th.TIER_PREVIEW)


def test_fanout_is_two_level_and_traversal_proof():
    k = th.cache_key("abc123", 1, 0)
    rel = th.fanout_path(k)
    assert rel == f"{k[:2]}/{k[2:4]}/{k}.webp"
    assert ".." not in rel
    # A non-hex/short key (accidental caller misuse) fails loudly.
    for bad in ("../etc/passwd", "x", "AB/CD", "g0f1"):
        with pytest.raises(ValueError):
            th.fanout_path(bad)


def test_tier_name_allowlist():
    assert th.tier_from_name("grid") == th.TIER_GRID
    assert th.tier_from_name("preview") == th.TIER_PREVIEW
    for bad in ("../", "GRID", "", "big", "0"):
        assert th.tier_from_name(bad) is None


def _gradient_image(path, w=1000, h=700) -> None:
    """A smooth, photo-like (compressible) image with enough structure that the
    encoder produces a non-trivial file yet fits the tier byte caps."""
    img = Image.new("RGB", (w, h))
    px = [
        ((x * 255) // w, (y * 255) // h, ((x + y) * 255) // (w + h))
        for y in range(h)
        for x in range(w)
    ]
    img.putdata(px)
    img.save(path)


def _noisy_png(tmp: Path, w=1000, h=700) -> str:
    p = str(tmp / "src.png")
    _gradient_image(p, w, h)
    return p


def test_image_generation_hits_dims_and_byte_caps(tmp_path):
    s = _Settings()
    src = _noisy_png(tmp_path)
    grid = th.generate_image_thumb(src, th.TIER_GRID, s)
    assert grid is not None
    assert max(grid.width, grid.height) == 320
    assert len(grid.data) <= s.thumbnail_grid_max_bytes
    assert grid.data[:4] == b"RIFF" and grid.data[8:12] == b"WEBP"

    preview = th.generate_image_thumb(src, th.TIER_PREVIEW, s)
    assert preview is not None
    assert max(preview.width, preview.height) == 800
    assert len(preview.data) <= s.thumbnail_preview_max_bytes


def test_generation_soft_fails_on_corrupt_and_oversized(tmp_path):
    s = _Settings()
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"definitely not an image")
    assert th.generate_image_thumb(str(bad), th.TIER_GRID, s) is None

    src = _noisy_png(tmp_path, 400, 400)
    tiny = _Settings()
    tiny.thumbnail_max_pixels = 100  # 400x400 exceeds the pixel ceiling
    assert th.generate_image_thumb(src, th.TIER_GRID, tiny) is None


@requires_ffmpeg
def test_extract_audio_cover_apic_and_covr_and_none(tmp_path):
    cover = io.BytesIO()
    Image.new("RGB", (200, 200), (120, 40, 200)).save(cover, "JPEG")
    cover_bytes = cover.getvalue()

    # MP3 with an ID3 APIC frame.
    mp3 = str(tmp_path / "a.mp3")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", mp3],
        capture_output=True, check=True,
    )
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError

    try:
        tags = ID3(mp3)
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="c", data=cover_bytes))
    tags.save(mp3)
    got = th.extract_audio_cover(mp3)
    assert got is not None and len(got) > 100

    # M4A with an MP4 'covr' atom.
    m4a = str(tmp_path / "a.m4a")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "aac", m4a],
        capture_output=True, check=True,
    )
    from mutagen.mp4 import MP4, MP4Cover

    m = MP4(m4a)
    m["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
    m.save()
    assert th.extract_audio_cover(m4a) is not None

    # No embedded art -> None.
    noart = str(tmp_path / "noart.mp3")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", noart],
        capture_output=True, check=True,
    )
    assert th.extract_audio_cover(noart) is None


# --------------------------------------------------------------------------- #
# P12 slice 2 — video poster-frame generation (pure, ffmpeg fixtures).         #
# --------------------------------------------------------------------------- #

def _testsrc(path: str, *, duration=2, size="640x480", rate=10) -> None:
    """A tiny deterministic test video (ffmpeg ``testsrc`` pattern, no audio)."""
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi",
         "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
         "-pix_fmt", "yuv420p", path],
        capture_output=True, check=True,
    )


def test_video_seek_point_math():
    # max(min_seek, 10% of duration); clamped below duration-0.5 for short clips.
    assert th._seek_seconds(100.0, 1.0) == 10.0       # 10% dominates
    assert th._seek_seconds(2.0, 1.0) == 1.0          # min_seek floor
    assert th._seek_seconds(3.0, 1.0) == 1.0          # max(1,.3)=1, <2.5 ok
    # Very short clip: seek clamps below the end so a frame still exists.
    assert th._seek_seconds(1.0, 1.0) == 0.5          # min(1.0, 1.0-0.5)
    # Missing / zero / negative duration -> min_seek fallback (no % assumption).
    assert th._seek_seconds(None, 1.0) == 1.0
    assert th._seek_seconds(0.0, 1.0) == 1.0
    assert th._seek_seconds(-5.0, 2.0) == 2.0


@requires_ffmpeg
def test_generate_video_thumb_dims_webp_and_tiers(tmp_path):
    src = str(tmp_path / "clip.mp4")
    _testsrc(src)
    s = _Settings()

    grid = th.generate_video_thumb(src, th.TIER_GRID, 2.0, s)
    assert grid is not None
    # WebP magic (RIFF....WEBP) + fits the grid edge, AR preserved (640x480).
    assert grid.data[:4] == b"RIFF" and grid.data[8:12] == b"WEBP"
    assert max(grid.width, grid.height) <= s.thumbnail_grid_px
    assert grid.width == 320 and grid.height == 240
    assert len(grid.data) <= s.thumbnail_grid_max_bytes

    preview = th.generate_video_thumb(src, th.TIER_PREVIEW, 2.0, s)
    assert preview is not None
    assert preview.width == 640 and preview.height == 480  # never upscaled past src


@requires_ffmpeg
def test_generate_video_thumb_missing_duration_uses_min_seek(tmp_path):
    src = str(tmp_path / "nodur.mp4")
    _testsrc(src)
    # No duration -> min-seek fallback, still yields a frame.
    tb = th.generate_video_thumb(src, th.TIER_GRID, None, _Settings())
    assert tb is not None and tb.data[8:12] == b"WEBP"


def test_generate_video_thumb_soft_fails(tmp_path):
    s = _Settings()
    # Missing file and non-video bytes both degrade to None (never raise).
    assert th.generate_video_thumb(str(tmp_path / "nope.mp4"), th.TIER_GRID, 2.0, s) is None
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"definitely not a container")
    assert th.generate_video_thumb(str(bad), th.TIER_GRID, 2.0, s) is None


@requires_ffmpeg
def test_video_accel_fallback_to_software(tmp_path, monkeypatch):
    """With the accel probe forced TRUE, the qsv strategy is attempted FIRST; when
    it fails the loop transparently falls back to software and still succeeds.

    We force the probe on (no real /dev/dri here) and make the frame-grab fail for
    any argv carrying ``qsv``, succeed otherwise -- exercising the exact fallback
    branch OPS-T7's degrade path relies on."""
    src = str(tmp_path / "clip.mp4")
    _testsrc(src)
    monkeypatch.setattr(th, "_accel_available", lambda: True)

    real = th._run_frame_grab
    seen = {"qsv": 0, "sw": 0}

    def fake(argv, timeout_s, max_bytes):
        if "qsv" in argv:
            seen["qsv"] += 1
            return None  # simulate qsv init/decode failure -> nonzero exit
        seen["sw"] += 1
        return real(argv, timeout_s, max_bytes)

    monkeypatch.setattr(th, "_run_frame_grab", fake)
    tb = th.generate_video_thumb(src, th.TIER_GRID, 2.0, _Settings(), accel="auto")
    assert tb is not None and tb.data[8:12] == b"WEBP"
    assert seen["qsv"] >= 1 and seen["sw"] >= 1  # tried qsv, then software


@requires_ffmpeg
def test_video_accel_off_never_attempts_qsv(tmp_path, monkeypatch):
    src = str(tmp_path / "clip.mp4")
    _testsrc(src)
    monkeypatch.setattr(th, "_accel_available", lambda: True)  # even if present
    saw_qsv = {"n": 0}
    real = th._run_frame_grab

    def fake(argv, timeout_s, max_bytes):
        if "qsv" in argv:
            saw_qsv["n"] += 1
        return real(argv, timeout_s, max_bytes)

    monkeypatch.setattr(th, "_run_frame_grab", fake)
    tb = th.generate_video_thumb(src, th.TIER_GRID, 2.0, _Settings(), accel="off")
    assert tb is not None
    assert saw_qsv["n"] == 0  # 'off' forces software; qsv never in the argv


def test_video_argv_shapes_sw_and_qsv_and_tonemap():
    # Software: no -hwaccel, -ss before -i, scale filter, stdout PNG.
    argv = th._video_frame_argv("ffmpeg", "/m/v.mkv", 3.5, 320, accel=None, tonemap=False)
    assert "-hwaccel" not in argv
    assert argv.index("-ss") < argv.index("-i")           # fast pre-input seek
    assert argv[argv.index("-ss") + 1] == "3.500"
    assert argv[argv.index("-i") + 1] == "/m/v.mkv"        # untrusted path is -i's value
    assert argv[-2:] == ["--", "-"]                        # PNG to stdout
    vf = argv[argv.index("-vf") + 1]
    assert "scale=" in vf and "tonemap" not in vf
    # QSV: -hwaccel qsv appears BEFORE the input.
    q = th._video_frame_argv("ffmpeg", "/m/v.mkv", 1.0, 800, accel="qsv", tonemap=False)
    assert q[q.index("-hwaccel") + 1] == "qsv"
    assert q.index("-hwaccel") < q.index("-i")
    # Tonemap: HDR chain is prepended to the scale filter.
    t = th._video_frame_argv("ffmpeg", "/m/v.mkv", 1.0, 320, accel=None, tonemap=True)
    tvf = t[t.index("-vf") + 1]
    assert "tonemap" in tvf and "zscale" in tvf and "scale=" in tvf


# --------------------------------------------------------------------------- #
# DB-backed: migration + serve + manifest upsert + GC + ride-along.            #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# P12-T5 — PDF first-page render (pure, pypdfium2 fixtures).                    #
# --------------------------------------------------------------------------- #


def _make_pdf(path: str, *, pages: int = 1, w: float = 612, h: float = 792) -> str:
    """Write a real PDF with ``pages`` blank pages of size ``w`` x ``h`` points
    (US-Letter default) using pypdfium2 itself -- no external fixture bytes."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(w, h)
    pdf.save(path)
    pdf.close()
    return path


def _make_encrypted_pdf(path: str, password: str = "secret") -> str:
    """Write a password-protected PDF (pypdf encrypt) so the loader must fail
    without the password -- the exact encrypted-PDF skip path."""
    from pypdf import PdfReader, PdfWriter

    plain = path + ".plain"
    _make_pdf(plain)
    reader = PdfReader(plain)
    writer = PdfWriter()
    for pg in reader.pages:
        writer.add_page(pg)
    writer.encrypt(password)
    with open(path, "wb") as fh:
        writer.write(fh)
    os.remove(plain)
    return path


def test_generate_pdf_thumb_dims_webp_and_tiers(tmp_path):
    s = _Settings()
    src = _make_pdf(str(tmp_path / "doc.pdf"), w=612, h=792)  # portrait

    grid = th.generate_pdf_thumb(src, th.TIER_GRID, s)
    assert grid is not None
    assert grid.data[:4] == b"RIFF" and grid.data[8:12] == b"WEBP"
    # Longest edge (portrait -> height) targets the grid tier px.
    assert max(grid.width, grid.height) <= s.thumbnail_grid_px
    assert len(grid.data) <= s.thumbnail_grid_max_bytes
    assert grid.height >= grid.width  # portrait preserved

    preview = th.generate_pdf_thumb(src, th.TIER_PREVIEW, s)
    assert preview is not None
    assert max(preview.width, preview.height) <= s.thumbnail_preview_px
    assert len(preview.data) <= s.thumbnail_preview_max_bytes
    # Preview tier renders larger than grid.
    assert max(preview.width, preview.height) > max(grid.width, grid.height)


def test_generate_pdf_thumb_encrypted_zero_malformed_and_oversized_soft_fail(tmp_path):
    s = _Settings()
    # Encrypted / password-protected -> clean skip (no crash).
    enc = _make_encrypted_pdf(str(tmp_path / "enc.pdf"))
    assert th.generate_pdf_thumb(enc, th.TIER_GRID, s) is None
    # Zero-page -> skip (pdfium refuses a 0-page load; our len()<1 guard backs it).
    zero = _make_pdf(str(tmp_path / "zero.pdf"), pages=0)
    assert th.generate_pdf_thumb(zero, th.TIER_GRID, s) is None
    # Malformed bytes with a PDF header -> skip.
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7 this is not a real pdf body\n%%EOF")
    assert th.generate_pdf_thumb(str(bad), th.TIER_GRID, s) is None
    # Total garbage -> skip.
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"\x00\x01\x02not a pdf at all")
    assert th.generate_pdf_thumb(str(junk), th.TIER_GRID, s) is None
    # Missing file -> skip.
    assert th.generate_pdf_thumb(str(tmp_path / "nope.pdf"), th.TIER_GRID, s) is None
    # Oversized source (bytes gate) -> skip BEFORE the parser opens it.
    ok = _make_pdf(str(tmp_path / "ok.pdf"))

    class _Tiny(_Settings):
        document_max_bytes = 10

    assert th.generate_pdf_thumb(ok, th.TIER_GRID, _Tiny()) is None


def test_generate_pdf_thumb_pixel_budget_caps_render(tmp_path):
    """A tiny render pixel budget forces the scale down so the intermediate
    bitmap can never exceed it (memory bound on an absurd page box)."""
    src = _make_pdf(str(tmp_path / "big.pdf"), w=612, h=792)

    class _Budget(_Settings):
        thumbnail_pdf_max_pixels = 2500  # 50x50 render ceiling

    tb = th.generate_pdf_thumb(src, th.TIER_PREVIEW, _Budget())
    assert tb is not None
    # The render buffer is bounded by the budget up to per-axis integer rounding
    # (each dimension rounds up <1px, so the product overshoots by < w + h + 1).
    assert tb.width * tb.height <= 2500 + tb.width + tb.height + 1


@pytest.fixture
async def env(pg_uri, tmp_path, monkeypatch):
    """Alembic-migrated DB (proves the thumbnail_manifest migration applies),
    proc_app opened against it, config_dir pointed at a temp thumbnail root,
    and an ASGI client with auth off."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")

    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM thumbnail_manifest"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    # extract/thumbs bind ``SessionLocal`` at import; patch those module-level
    # names too (import order across files can otherwise leave them on the default
    # host) -- the same pattern the other extract DB tests use.
    import filearr.tasks.extract as extract_mod
    import filearr.tasks.thumbs as thumbs_mod

    monkeypatch.setattr(extract_mod, "SessionLocal", maker)
    monkeypatch.setattr(thumbs_mod, "SessionLocal", maker)

    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    thumb_root = tmp_path / "config"
    monkeypatch.setattr(settings, "config_dir", str(thumb_root))

    # Open proc_app on the same DB so the ride-along defer_async works.
    from procrastinate import PsycopgConnector

    from filearr.worker import proc_app

    connector = PsycopgConnector(conninfo=pg_uri)
    original = proc_app.connector
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            exists = await connector.execute_query_one_async(
                "SELECT to_regclass('procrastinate_jobs') AS r"
            )
            if exists["r"] is None:
                await proc_app.schema_manager.apply_schema_async()
            import psycopg

            with psycopg.connect(pg_uri, autocommit=True) as conn:
                conn.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")

            app = create_app()

            async def _test_session():
                async with maker() as s:
                    yield s

            app.dependency_overrides[get_session] = _test_session
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                yield {
                    "client": c,
                    "maker": maker,
                    "settings": settings,
                    "pg_uri": pg_uri,
                }
            app.dependency_overrides.clear()
    proc_app.connector = original
    await engine.dispose()


async def _seed_image(maker, tmp_path, *, name="pic.png", hashed=True) -> str:
    src = tmp_path / name
    _gradient_image(str(src), 1200, 800)
    async with maker() as s:
        lib = Library(name=f"L-{name}", root_path=str(tmp_path))
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.image,
            path=str(src),
            rel_path=name,
            filename=name,
            extension=".png",
            size=src.stat().st_size,
            mtime=__import__("datetime").datetime.now(__import__("datetime").UTC),
            quick_hash="qh_" + name if hashed else None,
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_migration_created_thumbnail_manifest(env):
    async with env["maker"]() as s:
        reg = (
            await s.execute(text("SELECT to_regclass('thumbnail_manifest') AS r"))
        ).scalar_one()
        assert reg == "thumbnail_manifest"


async def test_serve_generates_grid_with_immutable_headers(env, tmp_path):
    item_id = await _seed_image(env["maker"], tmp_path)
    r = await env["client"].get(f"/api/v1/items/{item_id}/thumb?tier=grid")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["cache-control"].startswith("public, max-age=31536000")
    assert r.headers["etag"].strip('"')  # ETag == cache_key, non-empty
    assert r.content[:4] == b"RIFF" and r.content[8:12] == b"WEBP"

    # Manifest row upserted for (item, grid).
    async with env["maker"]() as s:
        row = (
            await s.execute(
                select(ThumbnailManifest).where(
                    ThumbnailManifest.item_id == item_id,
                    ThumbnailManifest.tier == th.TIER_GRID,
                )
            )
        ).scalar_one()
        assert row.source == "image"
        assert row.bytes == len(r.content)
        assert r.headers["etag"].strip('"') == row.cache_key


async def test_serve_tier_validation_and_missing(env, tmp_path):
    item_id = await _seed_image(env["maker"], tmp_path)
    # Strict tier allowlist.
    bad = await env["client"].get(f"/api/v1/items/{item_id}/thumb?tier=../etc")
    assert bad.status_code == 422
    # Unknown item.
    import uuid

    missing = await env["client"].get(f"/api/v1/items/{uuid.uuid4()}/thumb?tier=grid")
    assert missing.status_code == 404


async def test_serve_404_for_undecodable_source(env, tmp_path):
    # A document item has no slice-1 source -> 404 (client falls back to icon).
    async with env["maker"]() as s:
        lib = Library(name="Ldoc", root_path=str(tmp_path))
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.document,
            path=str(tmp_path / "x.pdf"),
            rel_path="x.pdf",
            filename="x.pdf",
            extension=".pdf",
            size=10,
            mtime=__import__("datetime").datetime.now(__import__("datetime").UTC),
            quick_hash="qhdoc",
        )
        s.add(item)
        await s.commit()
        iid = str(item.id)
    r = await env["client"].get(f"/api/v1/items/{iid}/thumb?tier=grid")
    assert r.status_code == 404


async def test_preview_tier_is_lazy_generated_on_request(env, tmp_path):
    item_id = await _seed_image(env["maker"], tmp_path)
    async with env["maker"]() as s:
        rows = (
            await s.execute(
                select(ThumbnailManifest).where(ThumbnailManifest.item_id == item_id)
            )
        ).scalars().all()
        assert rows == []  # nothing until requested
    r = await env["client"].get(f"/api/v1/items/{item_id}/thumb?tier=preview")
    assert r.status_code == 200
    async with env["maker"]() as s:
        row = (
            await s.execute(
                select(ThumbnailManifest).where(
                    ThumbnailManifest.item_id == item_id,
                    ThumbnailManifest.tier == th.TIER_PREVIEW,
                )
            )
        ).scalar_one()
        assert max(row.width, row.height) == 800


@requires_ffmpeg
async def test_sidecar_artwork_first_beats_embedded_art(env, tmp_path):
    """An audiobook with BOTH embedded cover art AND a linked poster.jpg sidecar
    uses the SIDECAR (rule 0), recorded as source='artwork'."""
    from filearr.tasks.thumbs import generate_and_store

    # Poster sidecar image on disk.
    poster = tmp_path / "poster.jpg"
    Image.new("RGB", (300, 300), (10, 200, 90)).save(poster, "JPEG")

    # An m4b audiobook carrying its own embedded cover.
    m4b = str(tmp_path / "book.m4b")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "aac", m4b],
        capture_output=True, check=True,
    )
    embed = io.BytesIO()
    Image.new("RGB", (256, 256), (200, 10, 10)).save(embed, "JPEG")
    from mutagen.mp4 import MP4, MP4Cover

    mm = MP4(m4b)
    mm["covr"] = [MP4Cover(embed.getvalue(), imageformat=MP4Cover.FORMAT_JPEG)]
    mm.save()

    import datetime as _dt

    async with env["maker"]() as s:
        lib = Library(name="Laudio", root_path=str(tmp_path))
        s.add(lib)
        await s.flush()
        parent = Item(
            library_id=lib.id, media_type=MediaType.audiobook, path=m4b,
            rel_path="book.m4b", filename="book.m4b", extension=".m4b",
            size=os.path.getsize(m4b), mtime=_dt.datetime.now(_dt.UTC),
            quick_hash="qhbook",
        )
        s.add(parent)
        await s.flush()
        art = Item(
            library_id=lib.id, media_type=MediaType.image, path=str(poster),
            rel_path="poster.jpg", filename="poster.jpg", extension=".jpg",
            size=poster.stat().st_size, mtime=_dt.datetime.now(_dt.UTC),
            sidecar_of=parent.id, quick_hash="qhposter",
        )
        s.add(art)
        await s.commit()
        row = await generate_and_store(s, parent, th.TIER_GRID, env["settings"])
        await s.commit()
        assert row is not None
        assert row.source == "artwork"  # sidecar wins over embedded art


async def test_gc_reclaims_orphan_file_and_orphan_row(env, tmp_path):
    from filearr.tasks.thumbs import gc_thumbnails

    settings = env["settings"]
    item_id = await _seed_image(env["maker"], tmp_path)
    # Generate a real grid so there is a live (referenced) file + row.
    ok = await env["client"].get(f"/api/v1/items/{item_id}/thumb?tier=grid")
    assert ok.status_code == 200
    live_key = ok.headers["etag"].strip('"')
    live_path = th.abs_path(settings, live_key)
    assert os.path.exists(live_path)

    # Direction 1 — ORPHAN FILE: a .webp under the fanout with no manifest row.
    orphan_key = th.cache_key("no-such-hash", 1, 0)
    orphan_path = th.abs_path(settings, orphan_key)
    os.makedirs(os.path.dirname(orphan_path), exist_ok=True)
    with open(orphan_path, "wb") as fh:
        fh.write(b"RIFF????WEBPstub")

    # Direction 2 — ORPHAN ROW: a manifest row whose backing file never existed.
    async with env["maker"]() as s:
        s.add(ThumbnailManifest(
            item_id=item_id, tier=th.TIER_PREVIEW,
            cache_key=th.cache_key("ghost", 1, 1), bytes=123,
            width=10, height=10, source="image",
        ))
        await s.commit()

    result = await gc_thumbnails(0)
    assert result["files_removed"] >= 1
    assert result["rows_removed"] >= 1

    # Orphan file gone; live file + row survive; ghost row deleted.
    assert not os.path.exists(orphan_path)
    assert os.path.exists(live_path)
    async with env["maker"]() as s:
        remaining = (
            await s.execute(select(ThumbnailManifest.tier).order_by(ThumbnailManifest.tier))
        ).scalars().all()
        assert remaining == [th.TIER_GRID]  # only the live grid row is left


async def test_gc_reclaims_file_after_item_hard_deleted(env, tmp_path):
    """Deleting an item CASCADE-drops its manifest rows; the on-disk file then
    becomes a file-orphan the GC walk reclaims."""
    from filearr.tasks.thumbs import gc_thumbnails

    settings = env["settings"]
    item_id = await _seed_image(env["maker"], tmp_path, name="gone.png")
    ok = await env["client"].get(f"/api/v1/items/{item_id}/thumb?tier=grid")
    key = ok.headers["etag"].strip('"')
    path = th.abs_path(settings, key)
    assert os.path.exists(path)
    async with env["maker"]() as s:
        await s.execute(text("DELETE FROM items WHERE id = :i"), {"i": item_id})
        await s.commit()
    await gc_thumbnails(0)
    assert not os.path.exists(path)


async def test_extract_ride_along_defers_thumb_item(env, tmp_path):
    """A successful image extract defers a thumb_item job on the thumbs queue
    (the staged ride-along; deferred AFTER the extract commit)."""
    from filearr.tasks.extract import extract_item

    item_id = await _seed_image(env["maker"], tmp_path, name="ride.png", hashed=False)
    await extract_item(item_id)

    import psycopg

    with psycopg.connect(env["pg_uri"], autocommit=True) as conn:
        rows = conn.execute(
            "SELECT queue_name, task_name FROM procrastinate_jobs "
            "WHERE task_name = 'filearr.tasks.thumbs.thumb_item'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == get_settings().queue_thumbnail

    # And thumb_item is registered at the configured low priority.
    from filearr.tasks.thumbs import thumb_item

    assert thumb_item.priority == get_settings().thumbs_priority == -15


def test_is_thumbnailable_matrix():
    from filearr.tasks.thumbs import is_thumbnailable

    assert is_thumbnailable(MediaType.image)
    assert is_thumbnailable(MediaType.audio)
    assert is_thumbnailable(MediaType.audiobook)
    assert is_thumbnailable(MediaType.sample)
    assert is_thumbnailable(MediaType.video)  # slice 2: ffmpeg poster-frame
    # P12-T5: ``document`` is thumbnailable ONLY for a .pdf extension. A
    # media-type-only probe (no rel_path) reports not-thumbnailable so a caller
    # never blindly enqueues every doc; office/ebook/comic docs stay placeholder.
    assert not is_thumbnailable(MediaType.document)
    assert is_thumbnailable(MediaType.document, "book.pdf")
    assert is_thumbnailable(MediaType.document, "PAPER.PDF")  # case-insensitive
    assert not is_thumbnailable(MediaType.document, "report.docx")
    assert not is_thumbnailable(MediaType.document, "notes.txt")
    assert not is_thumbnailable(MediaType.document, "comic.cbz")
    assert not is_thumbnailable(MediaType.model3d)


# --------------------------------------------------------------------------- #
# P12 slice 2 — DB-backed: video source resolution, serve-queue, stats.        #
# --------------------------------------------------------------------------- #

async def _seed_video(maker, tmp_path, *, name="clip.mp4", duration=2.0, hdr=False) -> str:
    """Seed a video Item backed by a real tiny testsrc file, with ``duration``
    (and optional HDR) already in ``metadata_`` (the ffprobe pass' output)."""
    import datetime as _dt

    src = tmp_path / name
    _testsrc(str(src))
    async with maker() as s:
        lib = Library(name=f"V-{name}", root_path=str(tmp_path))
        s.add(lib)
        await s.flush()
        meta = {"duration": duration}
        if hdr:
            meta["hdr"] = True
        item = Item(
            library_id=lib.id,
            media_type=MediaType.video,
            path=str(src),
            rel_path=name,
            filename=name,
            extension=".mp4",
            size=src.stat().st_size,
            mtime=_dt.datetime.now(_dt.UTC),
            quick_hash="qh_" + name,
            metadata_=meta,
        )
        s.add(item)
        await s.commit()
        return str(item.id)


@requires_ffmpeg
async def test_video_generate_and_store_records_source_video(env, tmp_path):
    from filearr.tasks.thumbs import generate_and_store

    item_id = await _seed_video(env["maker"], tmp_path)
    async with env["maker"]() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
        row = await generate_and_store(s, item, th.TIER_GRID, env["settings"])
        await s.commit()
        assert row is not None
        assert row.source == "video"
        assert row.width is not None and row.width <= env["settings"].thumbnail_grid_px
        # The backing WebP file exists under the fanout.
        path = th.abs_path(env["settings"], row.cache_key)
        assert os.path.exists(path)


@requires_ffmpeg
async def test_sidecar_artwork_beats_video_generation(env, tmp_path):
    """A video WITH a linked poster.jpg uses the poster (source='artwork'), never
    an ffmpeg frame -- rule 0 wins for video exactly as it does for audio."""
    import datetime as _dt

    from filearr.tasks.thumbs import generate_and_store

    poster = tmp_path / "poster.jpg"
    Image.new("RGB", (300, 300), (10, 200, 90)).save(poster, "JPEG")
    video_id = await _seed_video(env["maker"], tmp_path, name="movie.mp4")
    async with env["maker"]() as s:
        parent = (await s.execute(select(Item).where(Item.id == video_id))).scalar_one()
        art = Item(
            library_id=parent.library_id, media_type=MediaType.image,
            path=str(poster), rel_path="poster.jpg", filename="poster.jpg",
            extension=".jpg", size=poster.stat().st_size,
            mtime=_dt.datetime.now(_dt.UTC), sidecar_of=parent.id,
            quick_hash="qhposter",
        )
        s.add(art)
        await s.commit()
        row = await generate_and_store(s, parent, th.TIER_GRID, env["settings"])
        await s.commit()
        assert row is not None
        assert row.source == "artwork"  # poster wins, no ffmpeg call


@requires_ffmpeg
async def test_serve_video_miss_queues_job_and_404s(env, tmp_path):
    """A serve miss on a VIDEO never runs ffmpeg inline: it enqueues thumb_item
    for the requested tier and returns 404 (client retries)."""
    import psycopg

    video_id = await _seed_video(env["maker"], tmp_path, name="lazy.mp4")
    resp = await env["client"].get(f"/api/v1/items/{video_id}/thumb?tier=preview")
    assert resp.status_code == 404

    with psycopg.connect(env["pg_uri"], autocommit=True) as conn:
        rows = conn.execute(
            "SELECT task_name, args FROM procrastinate_jobs "
            "WHERE task_name = 'filearr.tasks.thumbs.thumb_item'"
        ).fetchall()
    # A thumb_item was queued for the PREVIEW tier of this video.
    assert any(
        r[1].get("item_id") == video_id and r[1].get("tier") == th.TIER_PREVIEW
        for r in rows
    )


async def test_stats_thumbnail_aggregates(env, tmp_path):
    """/stats reports thumbnail cache count/bytes/by_source from the manifest."""
    # Seed two manifest rows of different sources directly (cheap, deterministic).
    item_id = await _seed_image(env["maker"], tmp_path, name="s.png")
    async with env["maker"]() as s:
        s.add(ThumbnailManifest(
            item_id=item_id, tier=th.TIER_GRID,
            cache_key=th.cache_key("h1", 1, 0), bytes=1000,
            width=10, height=10, source="image",
        ))
        s.add(ThumbnailManifest(
            item_id=item_id, tier=th.TIER_PREVIEW,
            cache_key=th.cache_key("h2", 1, 1), bytes=2500,
            width=20, height=20, source="video",
        ))
        await s.commit()

    resp = await env["client"].get("/api/v1/stats")
    assert resp.status_code == 200
    thumbs = resp.json()["thumbs"]
    assert thumbs["count"] == 2
    assert thumbs["bytes"] == 3500
    assert thumbs["by_source"]["image"] == {"count": 1, "bytes": 1000}
    assert thumbs["by_source"]["video"] == {"count": 1, "bytes": 2500}
    assert thumbs["over_budget"] is False


# --------------------------------------------------------------------------- #
# P12-T5 — DB-backed: PDF source resolution, serve-inline, ride-along, GC.      #
# --------------------------------------------------------------------------- #


async def _seed_pdf(maker, tmp_path, *, name="doc.pdf", media=MediaType.document) -> str:
    """Seed a document Item backed by a real 1-page PDF on disk."""
    import datetime as _dt

    src = tmp_path / name
    _make_pdf(str(src))
    async with maker() as s:
        lib = Library(name=f"P-{name}", root_path=str(tmp_path))
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=media,
            path=str(src),
            rel_path=name,
            filename=name,
            extension="." + name.rsplit(".", 1)[-1],
            size=src.stat().st_size,
            mtime=_dt.datetime.now(_dt.UTC),
            quick_hash="qh_" + name,
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_pdf_generate_and_store_records_source_pdf(env, tmp_path):
    from filearr.tasks.thumbs import generate_and_store

    item_id = await _seed_pdf(env["maker"], tmp_path)
    async with env["maker"]() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
        row = await generate_and_store(s, item, th.TIER_GRID, env["settings"])
        await s.commit()
        assert row is not None
        assert row.source == "pdf"
        assert row.width is not None and row.width <= env["settings"].thumbnail_grid_px
        path = th.abs_path(env["settings"], row.cache_key)
        assert os.path.exists(path)


async def test_serve_pdf_generates_inline_with_immutable_headers(env, tmp_path):
    """A PDF thumbnail miss is generated INLINE on the serve path (sub-second,
    in-process) -- NOT queued like video. Verifies the serve/retrigger path
    covers PDFs automatically once the source resolver knows documents."""
    item_id = await _seed_pdf(env["maker"], tmp_path)
    r = await env["client"].get(f"/api/v1/items/{item_id}/thumb?tier=grid")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    assert r.content[:4] == b"RIFF" and r.content[8:12] == b"WEBP"
    async with env["maker"]() as s:
        row = (
            await s.execute(
                select(ThumbnailManifest).where(
                    ThumbnailManifest.item_id == item_id,
                    ThumbnailManifest.tier == th.TIER_GRID,
                )
            )
        ).scalar_one()
        assert row.source == "pdf"


async def test_serve_non_pdf_document_404s(env, tmp_path):
    """A .docx document has no render primitive -> 404 (client placeholder),
    proving the extension gate (only .pdf renders among documents)."""
    item_id = await _seed_pdf(env["maker"], tmp_path, name="report.docx")
    r = await env["client"].get(f"/api/v1/items/{item_id}/thumb?tier=grid")
    assert r.status_code == 404


async def test_pdf_ride_along_enqueues_but_docx_does_not(env, tmp_path):
    """The extract ride-along defers a thumb_item for a PDF (eligible) but NOT
    for a docx (placeholder-only) -- the enqueue path video used now covers PDFs."""
    from filearr.tasks.extract import extract_item

    pdf_id = await _seed_pdf(env["maker"], tmp_path, name="paper.pdf")
    docx_id = await _seed_pdf(env["maker"], tmp_path, name="memo.docx")
    await extract_item(pdf_id)
    await extract_item(docx_id)

    import psycopg

    with psycopg.connect(env["pg_uri"], autocommit=True) as conn:
        rows = conn.execute(
            "SELECT args->>'item_id' FROM procrastinate_jobs "
            "WHERE task_name = 'filearr.tasks.thumbs.thumb_item'"
        ).fetchall()
    enqueued = {r[0] for r in rows}
    assert pdf_id in enqueued
    assert docx_id not in enqueued


async def test_gc_reclaims_pdf_thumb_like_any_other(env, tmp_path):
    """GC is source-agnostic: a PDF thumbnail whose item is hard-deleted is
    reclaimed (file + manifest row) exactly like an image/video thumbnail."""
    from sqlalchemy import delete

    from filearr.tasks.thumbs import generate_and_store, run_thumbnail_gc

    item_id = await _seed_pdf(env["maker"], tmp_path)
    async with env["maker"]() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
        row = await generate_and_store(s, item, th.TIER_GRID, env["settings"])
        await s.commit()
        key = row.cache_key
    path = th.abs_path(env["settings"], key)
    assert os.path.exists(path)

    # Hard-delete the item (CASCADE removes the row); the file is now an orphan.
    async with env["maker"]() as s:
        await s.execute(delete(Item).where(Item.id == item_id))
        await s.commit()

    res = await run_thumbnail_gc()
    assert res["files_removed"] >= 1 or res["rows_removed"] >= 1
    assert not os.path.exists(path)
