"""Shared fixtures. A single session-scoped Postgres — either an externally
provided server (``FILEARR_TEST_DATABASE_URL``, used by CI with a postgres:18
service) or one embedded pgserver instance (local fallback, Python <=3.12) —
backs the migration + integration tests. The provider hands out isolated
per-purpose databases, each carrying a uuidv7() shim (native to PG18, absent
on older / pgserver-bundled Postgres)."""

import datetime as _dt

# Python 3.10 sandbox shim: the project targets 3.13 where datetime.UTC exists.
# Provide it so modules using `from datetime import UTC` import under 3.10.
if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

# Windows shim: async psycopg cannot run on the default ProactorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# uuidv7() shim so the baseline migration's `SELECT uuidv7()` guard + server
# defaults work on stock Postgres in CI/sandbox.
# --------------------------------------------------------------------------- #
# pgserver leak hygiene (sandbox disk-full backstop).                          #
# pgserver's own cleanup has repeatedly leaked its ~39MB data dir per run,     #
# filling the sandbox disk and bricking the VM mid-run. Two defensive backstops#
# (both best-effort — cleanup must NEVER fail a test):                         #
#   * a session-start sweep of stale ``filearr-pg-*`` dirs (>1h old) in both   #
#     /tmp and $TMPDIR, from earlier crashed runs; and                         #
#   * a per-fixture teardown ``shutil.rmtree`` of THIS run's data dir (below). #
# --------------------------------------------------------------------------- #
_PG_DIR_PREFIX = "filearr-pg-"
_STALE_AGE_SECONDS = 3600  # 1h


def _sweep_stale_pg_dirs() -> None:
    """Best-effort delete of stale pgserver data dirs from prior crashed runs."""
    import glob
    import time

    seen: set[str] = set()
    roots = {"/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR", "")}
    now = time.time()
    for root in roots:
        if not root:
            continue
        # Also catch pg data dirs created with generic tempfile names (pgserver
        # internals / older fixtures): any stale tmp* dir carrying PG_VERSION.
        candidates = glob.glob(os.path.join(root, _PG_DIR_PREFIX + "*"))
        for generic in glob.glob(os.path.join(root, "tmp*")):
            if os.path.exists(os.path.join(generic, "PG_VERSION")) or os.path.exists(
                os.path.join(generic, "pg", "PG_VERSION")
            ):
                candidates.append(generic)
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            try:
                if not os.path.isdir(path):
                    continue
                if now - os.path.getmtime(path) < _STALE_AGE_SECONDS:
                    continue  # possibly an in-flight run's dir — leave it
                shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass  # cleanup is best-effort; never fail collection over it


# Sweep once at import (session start), before any pgserver is created.
_sweep_stale_pg_dirs()


UUIDV7_SHIM = (
    "CREATE OR REPLACE FUNCTION uuidv7() RETURNS uuid "
    "AS 'SELECT gen_random_uuid()' LANGUAGE sql;"
)

_PGSERVER_MISSING = (
    "No test Postgres available. Either:\n"
    "  * set FILEARR_TEST_DATABASE_URL to an admin postgresql:// URL with "
    "CREATEDB rights (e.g. a local `docker run postgres:18`), or\n"
    "  * run the suite from a Python <=3.12 venv where `pgserver` is installed "
    "(pgserver ships no cp313 wheel, so 3.13 has no embedded fallback).\n"
    "See docs/dev-windows.md."
)


def _swap_db(uri: str, dbname: str) -> str:
    """Return ``uri`` with its database path replaced by ``dbname`` (query kept)."""
    return urlunsplit(urlsplit(uri)._replace(path=f"/{dbname}"))


class _PGProvider:
    """One Postgres for the whole session — external
    (``FILEARR_TEST_DATABASE_URL``, an admin postgresql:// URL with CREATEDB
    rights) or a single embedded pgserver. Hands out isolated per-purpose
    databases."""

    def __init__(self) -> None:
        self._srv = None
        self._data_dir: str | None = None
        self._created: list[str] = []
        external = os.environ.get("FILEARR_TEST_DATABASE_URL")
        if external:
            # Normalize away any SQLAlchemy driver suffix (e.g. +psycopg) so the
            # admin URI is a plain libpq/psycopg connection string.
            parts = urlsplit(external)
            scheme = parts.scheme.split("+", 1)[0]
            self._admin_uri = urlunsplit(parts._replace(scheme=scheme))
        else:
            try:
                import pgserver
            except ImportError:
                pytest.exit(_PGSERVER_MISSING, returncode=1)
            self._data_dir = tempfile.mkdtemp(prefix="filearr-pg-")
            self._srv = pgserver.get_server(self._data_dir)
            self._admin_uri = self._srv.get_uri()

    def new_database(self, prefix: str) -> str:
        """Create a fresh, isolated database and return its postgresql:// URI.

        The database is C-collation (pgserver initdb inherits the OS locale, and
        ordering assertions compare against Python's byte-order sorted(); pin
        LOCALE 'C' so sorts match on every host) and carries the uuidv7() shim.
        """
        import psycopg

        name = f"{prefix}_{secrets.token_hex(4)}"
        with psycopg.connect(self._admin_uri, autocommit=True) as conn:
            conn.execute(
                f'CREATE DATABASE "{name}" LOCALE \'C\' TEMPLATE template0'
            )
        self._created.append(name)
        uri = _swap_db(self._admin_uri, name)
        with psycopg.connect(uri, autocommit=True) as conn:
            try:
                # gen_random_uuid() is core since PG13; pgcrypto only matters on
                # ancient servers and is absent from pgserver's bundled build.
                conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            except psycopg.Error:
                pass
            conn.execute(UUIDV7_SHIM)
        return uri

    def cleanup(self) -> None:
        import psycopg

        # Drop the databases we created — vital for a persistent external server.
        for name in self._created:
            try:
                with psycopg.connect(self._admin_uri, autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception:
                pass  # best-effort; never fail teardown over cleanup
        # Embedded pgserver leaks its data dir (~39MB) — a recurring disk-full
        # brick. Stop the server, then best-effort rmtree OUR data dir.
        if self._srv is not None:
            try:
                self._srv.cleanup()
            finally:
                if self._data_dir:
                    shutil.rmtree(self._data_dir, ignore_errors=True)


@dataclass
class _ModuleDB:
    """Drop-in replacement for the old private pgserver fixtures: the consumers
    only ever call ``.get_uri()`` on it."""

    _uri: str

    def get_uri(self) -> str:
        return self._uri


@pytest.fixture(scope="session")
def _pg_provider():
    provider = _PGProvider()
    try:
        yield provider
    finally:
        provider.cleanup()


@pytest.fixture(scope="session")
def pg_uri(_pg_provider):
    uri = _pg_provider.new_database("filearr_test")
    os.environ["FILEARR_DATABASE_URL"] = uri
    # Drop any get_settings() result cached at import time (a test module importing
    # filearr.db / a task module caches the DEFAULT DSN before this fixture runs);
    # clearing here makes every integration test read the test URL regardless of
    # collection/import order.
    from filearr.config import get_settings

    get_settings.cache_clear()
    return uri


@pytest.fixture(scope="module")
def module_db(_pg_provider):
    """A fresh isolated database for a single test module — the shared-server
    replacement for the per-file private pgserver fixtures."""
    return _ModuleDB(_pg_provider.new_database("filearr_mod"))


# --- media fixtures (T1): tiny real files generated with ffmpeg at test time ---
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not on PATH"
)


def _run(argv: list[str]) -> None:
    subprocess.run(argv, capture_output=True, check=True)


@pytest.fixture(scope="session")
def sample_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """1s H.264 video + AAC audio in an MP4 container."""
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    _run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(out),
    ])
    return out


@pytest.fixture(scope="session")
def sample_mkv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """1s H.264 video + AAC audio + a SubRip subtitle track in Matroska."""
    d = tmp_path_factory.mktemp("media_mkv")
    srt = d / "sub.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    out = d / "sample.mkv"
    _run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-i", str(srt),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "srt",
        "-shortest", str(out),
    ])
    return out


@pytest.fixture(scope="session")
def corrupt_video(sample_mp4: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A truncated MP4 that ffprobe cannot analyse."""
    out = tmp_path_factory.mktemp("media_bad") / "corrupt.mp4"
    out.write_bytes(sample_mp4.read_bytes()[:200])
    return out


# --- T6 media fixtures: tiny real files for model3d/document/spreadsheet/m4b ---
# All generated at test time so no binaries are committed. Kept well under the
# 200 KiB fixture ceiling.

@pytest.fixture(scope="session")
def stl_cube(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A binary STL of a box, exported via trimesh (12 triangles)."""
    trimesh = pytest.importorskip("trimesh")
    out = tmp_path_factory.mktemp("model3d") / "cube.stl"
    trimesh.creation.box(extents=[2.0, 3.0, 4.0]).export(str(out))
    return out


@pytest.fixture(scope="session")
def glb_scene(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A GLB holding two meshes (a multi-mesh scene for aggregation tests)."""
    trimesh = pytest.importorskip("trimesh")
    out = tmp_path_factory.mktemp("model3d_glb") / "scene.glb"
    scene = trimesh.Scene([trimesh.creation.box(), trimesh.creation.icosphere()])
    scene.export(str(out))
    return out


@pytest.fixture(scope="session")
def corrupt_stl(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("model3d_bad") / "bad.stl"
    out.write_bytes(b"this is not a mesh " * 32)
    return out


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 2-page PDF with Title/Author metadata, built with pypdf's writer."""
    pypdf = pytest.importorskip("pypdf")
    out = tmp_path_factory.mktemp("doc_pdf") / "sample.pdf"
    w = pypdf.PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_blank_page(width=200, height=200)
    w.add_metadata({"/Title": "The Title", "/Author": "The Author"})
    with open(out, "wb") as f:
        w.write(f)
    return out


@pytest.fixture(scope="session")
def encrypted_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A password-encrypted PDF (non-empty password) to exercise the locked path."""
    pypdf = pytest.importorskip("pypdf")
    out = tmp_path_factory.mktemp("doc_pdf_enc") / "locked.pdf"
    w = pypdf.PdfWriter()
    w.add_blank_page(width=100, height=100)
    w.encrypt("s3cret")
    with open(out, "wb") as f:
        w.write(f)
    return out


@pytest.fixture(scope="session")
def corrupt_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("doc_pdf_bad") / "bad.pdf"
    out.write_bytes(b"%PDF-1.4 this is not really a pdf body " * 4)
    return out


@pytest.fixture(scope="session")
def sample_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    docx = pytest.importorskip("docx")
    out = tmp_path_factory.mktemp("doc_docx") / "sample.docx"
    d = docx.Document()
    d.core_properties.title = "Docx Title"
    d.core_properties.author = "Docx Author"
    d.add_paragraph("First")
    d.add_paragraph("Second")
    d.save(str(out))
    return out


@pytest.fixture(scope="session")
def corrupt_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("doc_docx_bad") / "bad.docx"
    out.write_bytes(b"PK\x03\x04 not a real docx zip " * 8)
    return out


@pytest.fixture(scope="session")
def sample_xlsx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    out = tmp_path_factory.mktemp("sheet_xlsx") / "sample.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Alpha"
    wb.create_sheet("Beta")
    wb.properties.title = "Xlsx Title"
    wb.properties.creator = "Xlsx Author"
    wb.save(str(out))
    return out


@pytest.fixture(scope="session")
def corrupt_xlsx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("sheet_xlsx_bad") / "bad.xlsx"
    out.write_bytes(b"PK\x03\x04 not a real xlsx " * 8)
    return out


def _minimal_text_pdf(text: str) -> bytes:
    """A hand-built single-page PDF whose content stream draws ``text`` — pypdf's
    ``extract_text`` reads it back. No PDF-authoring dep needed (P3-T5 body text)."""
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += (f"{off:010d} 00000 n \n").encode()
    out += (
        b"trailer\n<< /Size " + str(len(objs) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    )
    return out


@pytest.fixture(scope="session")
def sample_pdf_text(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PDF with real extractable body text (P3-T5)."""
    out = tmp_path_factory.mktemp("doc_pdf_text") / "text.pdf"
    out.write_bytes(_minimal_text_pdf("Hello body text extraction world"))
    return out


@pytest.fixture(scope="session")
def sample_docx_text(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A DOCX with several paragraphs of body text (P3-T5)."""
    docx = pytest.importorskip("docx")
    out = tmp_path_factory.mktemp("doc_docx_text") / "text.docx"
    d = docx.Document()
    d.core_properties.title = "Body Docx"
    d.add_paragraph("The quick brown fox")
    d.add_paragraph("jumps over the lazy dog")
    d.add_paragraph("wombat marsupial paragraph")
    d.save(str(out))
    return out


@pytest.fixture(scope="session")
def sample_txt(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A plain-text file for the txt/md body-text path (P3-T5)."""
    out = tmp_path_factory.mktemp("doc_txt") / "notes.txt"
    out.write_text("plain text notes about   aardvarks\nand\tbandicoots\n", encoding="utf-8")
    return out


@pytest.fixture(scope="session")
def zipbomb_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A crafted high-ratio zip masquerading as a .docx: one member that declares a
    huge uncompressed size for a tiny compressed payload. The decompression guard
    must reject it from the CENTRAL DIRECTORY before any parser opens it (P3-T5)."""
    import zipfile

    out = tmp_path_factory.mktemp("doc_bomb") / "bomb.docx"
    # 40 MiB of a single repeated byte compresses to a few KB -> ratio well past
    # 100:1 and payload well past the 10 MiB floor, so the guard trips.
    payload = b"\x00" * (40 * 1024 * 1024)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", payload)
    return out


@pytest.fixture(scope="session")
def sample_m4b(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 2s AAC m4b with two embedded chapters, built with ffmpeg + ffmetadata."""
    if not FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    d = tmp_path_factory.mktemp("audiobook")
    meta = d / "chapters.txt"
    meta.write_text(
        ";FFMETADATA1\n"
        "title=Test Book\n"
        "artist=Test Author\n"
        "album=Test Album\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Chapter One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Chapter Two\n"
    )
    out = d / "book.m4b"
    _run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-i", str(meta), "-map_metadata", "1",
        "-c:a", "aac", str(out),
    ])
    return out


@pytest.fixture(scope="session")
def corrupt_m4b(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("audiobook_bad") / "bad.m4b"
    out.write_bytes(b"\x00\x00\x00\x18ftypM4B not a valid mp4 " * 4)
    return out


# --------------------------------------------------------------------------- #
# T10: reusable realistic mixed-media fixture tree                            #
# --------------------------------------------------------------------------- #
# A single directory tree covering EVERY enabled MediaType plus sidecars and
# junk, generated at test time (ffmpeg + the pure-python libs are all available;
# nothing binary is committed and every file stays well under the 200 KiB
# fixture ceiling). Used by the end-to-end scan+extract integration test to
# assert stats.by_type is populated for each type and that sidecars are linked
# and hidden from default search.


def _make_wav(path: Path, seconds: float = 0.2, rate: int = 8000) -> None:
    """A tiny mono WAV written with the stdlib (no ffmpeg needed)."""
    import math
    import struct
    import wave

    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(n)
        )
        w.writeframes(frames)


def _make_mp3(path: Path) -> None:
    """A short MP3 with ID3 tags, via ffmpeg (libmp3lame) + mutagen tags."""
    _run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4",
        "-c:a", "libmp3lame", "-b:a", "64k", str(path),
    ])
    try:
        from mutagen.easyid3 import EasyID3

        tags = EasyID3()
        tags["title"] = "Track Title"
        tags["artist"] = "Track Artist"
        tags["album"] = "Track Album"
        tags.save(str(path))
    except Exception:
        pass


@dataclass
class MediaTree:
    """Descriptor for a generated fixture tree.

    ``root``          — the library root directory.
    ``primary``       — {media_type_value: [rel_path, ...]} for primary files.
    ``sidecars``      — [rel_path, ...] for sidecar files (nfo / thumb / poster).
    ``junk``          — [rel_path, ...] for files that must be ignored entirely
                        (dotfiles, partials) — i.e. never produce an item.
    ``primary_count`` — number of non-sidecar, indexable primary files.
    """

    root: Path
    primary: dict
    sidecars: list
    junk: list

    @property
    def primary_count(self) -> int:
        return sum(len(v) for v in self.primary.values())


@pytest.fixture(scope="session")
def media_tree(tmp_path_factory: pytest.TempPathFactory) -> "MediaTree":
    """Build a realistic mixed-media tree covering every enabled MediaType plus
    sidecars (video+nfo+thumb, dir poster) and junk. Session-scoped: the bytes
    are read-only, so tests that scan it use their own DB and never mutate files.
    """
    if not FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    trimesh = pytest.importorskip("trimesh")
    pypdf = pytest.importorskip("pypdf")
    docx = pytest.importorskip("docx")
    openpyxl = pytest.importorskip("openpyxl")

    root = tmp_path_factory.mktemp("media_tree")
    primary: dict[str, list[str]] = {}
    sidecars: list[str] = []
    junk: list[str] = []

    def rel(p: Path) -> str:
        return str(p.relative_to(root))

    # --- video (in a per-title folder, with an .nfo + -thumb + dir poster) ---
    movies = root / "Movies" / "Arcane (2021)"
    movies.mkdir(parents=True)
    vid = movies / "Arcane (2021).mp4"
    _run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(vid),
    ])
    primary.setdefault("video", []).append(rel(vid))
    nfo = movies / "Arcane (2021).nfo"
    nfo.write_text(
        '<?xml version="1.0"?>\n<movie><title>Arcane</title>'
        "<plot>Two sisters.</plot><year>2021</year></movie>\n"
    )
    sidecars.append(rel(nfo))
    thumb = movies / "Arcane (2021)-thumb.jpg"
    poster = movies / "poster.jpg"
    for art in (thumb, poster):
        _run([
            FFMPEG, "-y", "-f", "lavfi",
            "-i", "color=c=blue:s=32x48:d=1", "-frames:v", "1", str(art),
        ])
        sidecars.append(rel(art))

    # --- audio (mp3 with tags) ---
    music = root / "Music"
    music.mkdir()
    mp3 = music / "song.mp3"
    _make_mp3(mp3)
    primary.setdefault("audio", []).append(rel(mp3))

    # --- audiobook (m4b with chapters) ---
    books = root / "Audiobooks"
    books.mkdir()
    meta = books / "chapters.ffmeta"
    meta.write_text(
        ";FFMETADATA1\ntitle=Test Book\nartist=Test Author\nalbum=Test Album\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Chapter One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Chapter Two\n"
    )
    m4b = books / "book.m4b"
    _run([
        FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-i", str(meta), "-map_metadata", "1", "-c:a", "aac", str(m4b),
    ])
    meta.unlink()  # ffmeta helper is not part of the tree
    primary.setdefault("audiobook", []).append(rel(m4b))

    # --- sample (wav) ---
    samples = root / "Samples"
    samples.mkdir()
    wav = samples / "kick.wav"
    _make_wav(wav)
    primary.setdefault("sample", []).append(rel(wav))

    # --- image (png) ---
    images = root / "Images"
    images.mkdir()
    png = images / "photo.png"
    _run([
        FFMPEG, "-y", "-f", "lavfi",
        "-i", "color=c=red:s=48x32:d=1", "-frames:v", "1", str(png),
    ])
    primary.setdefault("image", []).append(rel(png))

    # --- model3d (stl) ---
    models = root / "Models"
    models.mkdir()
    stl = models / "cube.stl"
    trimesh.creation.box(extents=[2.0, 3.0, 4.0]).export(str(stl))
    primary.setdefault("model3d", []).append(rel(stl))

    # --- document (pdf + docx) ---
    docs = root / "Docs"
    docs.mkdir()
    pdf = docs / "manual.pdf"
    w = pypdf.PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_metadata({"/Title": "Manual", "/Author": "Author"})
    with open(pdf, "wb") as f:
        w.write(f)
    primary.setdefault("document", []).append(rel(pdf))
    docxp = docs / "notes.docx"
    d = docx.Document()
    d.core_properties.title = "Notes"
    d.add_paragraph("Body")
    d.save(str(docxp))
    primary.setdefault("document", []).append(rel(docxp))

    # --- spreadsheet (xlsx) ---
    sheets = root / "Sheets"
    sheets.mkdir()
    xlsx = sheets / "budget.xlsx"
    wb = openpyxl.Workbook()
    wb.properties.title = "Budget"
    wb.save(str(xlsx))
    primary.setdefault("spreadsheet", []).append(rel(xlsx))

    # --- junk that must NEVER become an item ---
    # Dotfiles are skipped by walk(); a *.partial is excluded via a glob in the
    # test's library.exclude_globs.
    (music / ".DS_Store").write_bytes(b"junk")
    junk.append("Music/.DS_Store")
    partial = music / "incoming.mp3.partial"
    partial.write_bytes(b"partial download")
    junk.append(rel(partial))

    return MediaTree(root=root, primary=primary, sidecars=sidecars, junk=junk)


# --------------------------------------------------------------------------- #
# Shared DB URL helper (consolidated; T1-T11 files each defined a local copy)  #
# --------------------------------------------------------------------------- #
def psycopg3_uri(uri: str) -> str:
    """Rewrite a bare ``postgresql://`` URI onto the psycopg3 async driver.

    Every integration test needs this to point an async engine at the pgserver
    URI; historically each module carried its own ``_psycopg3``. New tests import
    this one from conftest instead."""
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)
