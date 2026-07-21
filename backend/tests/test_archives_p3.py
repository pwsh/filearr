"""P3-T13 — archive member listing (guarded, index-only).

Pure/unit coverage with REAL tiny zip/tar/tgz fixtures built in-test, plus a
DB round-trip through ``extract_item`` (pgserver, importorskip). Verifies:
  * zip / tar / tar.gz member listing is correct (names + declared sizes);
  * the enumeration COUNT cap and the STORED-list cap are enforced independently;
  * a ratio-bomb zip is rejected by the reused decompression guard BEFORE any
    member is enumerated (zipfile.ZipFile.infolist is the only read the guard
    does; the extractor never proceeds to member enumeration);
  * a tar.gz whose compressed stream exceeds the byte ceiling stops CLEANLY
    (truncated, no exception);
  * hostile member names (``../evil``, absolute, ``..``-laden) are stored VERBATIM
    as strings and are never resolved/joined to a filesystem path;
  * the flat ``archive_members`` projection is index-capped and is the LAST
    searchable attribute;
  * dispatch (``is_archive``/``detect_archive``) fires only for archive extensions.
"""

from __future__ import annotations

import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from filearr.tasks.archives import (
    ArchiveError,
    detect_archive,
    is_archive,
    list_archive_members,
)

BIG = 1 << 30


# --------------------------------------------------------------- builders
def _make_zip(path: Path, entries: list[tuple[str, bytes]], *, compress=zipfile.ZIP_STORED) -> Path:
    with zipfile.ZipFile(path, "w", compression=compress) as zf:
        for name, data in entries:
            zi = zipfile.ZipInfo(filename=name)
            zi.compress_type = compress
            zf.writestr(zi, data)
    return path


def _make_tar(path: Path, entries: list[tuple[str, bytes]], *, mode="w") -> Path:
    with tarfile.open(path, mode) as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return path


# --------------------------------------------------------------- dispatch
def test_detect_and_is_archive():
    assert detect_archive("/x/a.zip") == "zip"
    assert detect_archive("/x/a.cbz") == "zip"
    assert detect_archive("/x/a.jar") == "zip"
    assert detect_archive("/x/a.tar") == "tar"
    assert detect_archive("/x/a.tar.gz") == "tar"
    assert detect_archive("/x/a.tgz") == "tar"
    assert detect_archive("/x/a.tar.bz2") == "tar"
    assert detect_archive("/x/a.tar.xz") == "tar"
    # non-archives -> None (dispatch skips them)
    assert detect_archive("/x/a.mkv") is None
    assert detect_archive("/x/a.txt") is None
    assert detect_archive("/x/a.7z") is None  # roadmap follow-up (no stdlib reader)
    assert detect_archive("/x/a.rar") is None
    assert is_archive("/x/a.zip") is True
    assert is_archive("/x/movie.mp4") is False


# --------------------------------------------------------------- happy paths
def test_zip_member_listing(tmp_path):
    _make_zip(tmp_path / "a.zip", [("alpha.txt", b"aaaa"), ("dir/beta.md", b"bb"), ("g.bin", b"")])
    out = list_archive_members(str(tmp_path / "a.zip"))
    arc = out["archive"]
    assert arc["member_count"] == 3
    assert arc["format"] == "zip"
    assert arc["truncated"] is False
    names = {m["name"]: m["size"] for m in arc["members"]}
    assert names == {"alpha.txt": 4, "dir/beta.md": 2, "g.bin": 0}
    assert arc["total_uncompressed"] == 6
    # flat searchable string carries every name, newline-joined.
    assert set(out["archive_members"].split("\n")) == {"alpha.txt", "dir/beta.md", "g.bin"}


def test_zip_skips_directory_entries(tmp_path):
    # An explicit directory entry (trailing slash) is not a member file.
    _make_zip(tmp_path / "a.zip", [("sub/", b""), ("sub/x.txt", b"hi")])
    arc = list_archive_members(str(tmp_path / "a.zip"))["archive"]
    assert [m["name"] for m in arc["members"]] == ["sub/x.txt"]
    assert arc["member_count"] == 1


def test_tar_member_listing(tmp_path):
    _make_tar(tmp_path / "a.tar", [("one.txt", b"12345"), ("two/three.txt", b"xy")])
    out = list_archive_members(str(tmp_path / "a.tar"))
    arc = out["archive"]
    assert arc["member_count"] == 2
    assert arc["format"] == "tar"
    names = {m["name"]: m["size"] for m in arc["members"]}
    assert names == {"one.txt": 5, "two/three.txt": 2}


def test_targz_member_listing(tmp_path):
    _make_tar(tmp_path / "a.tar.gz", [("readme", b"hello"), ("data.bin", b"0" * 100)], mode="w:gz")
    out = list_archive_members(str(tmp_path / "a.tar.gz"))
    arc = out["archive"]
    assert arc["member_count"] == 2
    assert arc["format"] == "tar.gz"
    assert {m["name"] for m in arc["members"]} == {"readme", "data.bin"}


# --------------------------------------------------------------- caps
def test_member_count_cap_enforced(tmp_path):
    _make_zip(tmp_path / "a.zip", [(f"f{i}.txt", b"x") for i in range(10)])
    arc = list_archive_members(str(tmp_path / "a.zip"), max_members=3)["archive"]
    assert arc["member_count"] == 3
    assert arc["truncated"] is True
    assert len(arc["members"]) == 3


def test_stored_list_cap_independent_of_count(tmp_path):
    _make_zip(tmp_path / "a.zip", [(f"f{i}.txt", b"x") for i in range(10)])
    out = list_archive_members(str(tmp_path / "a.zip"), max_members=100, max_stored=3)
    arc = out["archive"]
    assert arc["member_count"] == 10  # ALL counted
    assert arc["truncated"] is False  # count cap not hit
    assert len(arc["members"]) == 3  # but only 3 {name,size} stored
    # flat string may hold more names than the stored list (denser packing).
    assert len(out["archive_members"].split("\n")) == 10


def test_index_chars_cap_on_flat_string(tmp_path):
    _make_zip(tmp_path / "a.zip", [(f"name-{i:04d}.txt", b"x") for i in range(200)])
    out = list_archive_members(str(tmp_path / "a.zip"), index_chars=50)
    assert len(out["archive_members"]) <= 50


# --------------------------------------------------------------- bomb guard
def test_zip_ratio_bomb_rejected_before_enumeration(tmp_path):
    # 11 MiB of zeros compresses ~1000:1 -> total>10MiB ratio_min AND ratio>100:1,
    # so the reused central-directory guard REJECTS it (reading only the central
    # directory, never a member payload) and raises ArchiveError BEFORE the member
    # enumeration pass runs (the guard is the first statement in _list_zip).
    bomb = tmp_path / "bomb.zip"
    _make_zip(bomb, [("zeros", b"\0" * (11 * 1024 * 1024))], compress=zipfile.ZIP_DEFLATED)
    with pytest.raises(ArchiveError) as ei:
        list_archive_members(str(bomb))
    assert "guard" in str(ei.value).lower()


def test_targz_byte_ceiling_stops_cleanly(tmp_path):
    # Incompressible (random) members so the compressed stream grows fast; a tiny
    # byte ceiling forces a clean truncation partway, never an exception.
    entries = [(f"m{i}.bin", os.urandom(200 * 1024)) for i in range(20)]
    _make_tar(tmp_path / "big.tar.gz", entries, mode="w:gz")
    out = list_archive_members(str(tmp_path / "big.tar.gz"), scan_max_bytes=100_000)
    arc = out["archive"]
    assert arc["truncated"] is True
    assert 1 <= arc["member_count"] < 20  # bounded, not the full set


# --------------------------------------------------------------- hostile names
@pytest.mark.parametrize("hostile", ["../evil.txt", "/etc/passwd", "a/../../b", "..", "./x"])
def test_hostile_member_names_stored_verbatim(tmp_path, hostile):
    # A traversal/absolute member name is a STRING only — stored verbatim, never
    # resolved or joined to the filesystem. No file is ever created outside the
    # archive (we list, never extract).
    _make_tar(tmp_path / "h.tar", [(hostile, b"x")])
    before = set(p.name for p in tmp_path.iterdir())
    arc = list_archive_members(str(tmp_path / "h.tar"))["archive"]
    assert arc["members"][0]["name"] == hostile
    assert all(isinstance(m["name"], str) for m in arc["members"])
    # nothing was unpacked to disk (only h.tar exists)
    assert set(p.name for p in tmp_path.iterdir()) == before


def test_control_chars_stripped_from_names(tmp_path):
    # ANSI escape (C0 control) is stripped for safe JSON/index storage; the path
    # text is otherwise preserved (a real archive, listed end-to-end).
    _make_tar(tmp_path / "c.tar", [("ev\x1b[31mil.txt", b"x")])
    arc = list_archive_members(str(tmp_path / "c.tar"))["archive"]
    assert arc["members"][0]["name"] == "ev[31mil.txt"


def test_clean_member_name_strips_controls_and_caps():
    from filearr.tasks.archives import _MEMBER_NAME_CAP, _clean_member_name

    # NUL, C0 (ESC) and C1 (0x9b) controls dropped; traversal text kept verbatim.
    assert _clean_member_name("a\x00b\x1bc\x9bd") == "abcd"
    assert _clean_member_name("../keep/../verbatim") == "../keep/../verbatim"
    assert len(_clean_member_name("z" * 5000)) == _MEMBER_NAME_CAP


def test_non_archive_returns_empty(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    assert list_archive_members(str(tmp_path / "a.txt")) == {}


# --------------------------------------------------------------- projection
def test_archive_members_is_last_searchable_attribute():
    from filearr.meili_ops import SEARCHABLE_ATTRIBUTES

    assert SEARCHABLE_ATTRIBUTES[-1] == "archive_members"
    assert "archive_members" in SEARCHABLE_ATTRIBUTES


def test_build_doc_projects_and_caps_archive_members():
    import uuid
    from datetime import UTC, datetime

    from filearr import search as search_mod
    from filearr.models import Item, ItemStatus

    item = Item(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="other", file_group="other",
        path="/data/a.zip",
        rel_path="a.zip",
        filename="a.zip",
        extension="zip",
        size=10,
        mtime=datetime.now(UTC),
        metadata_={"archive_members": "x" * 30_000, "archive": {"member_count": 2}},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
    )
    doc = search_mod.build_doc(item)
    # projected + capped at archive_members_index_chars (default 20k)
    from filearr.config import get_settings

    assert len(doc["archive_members"]) == get_settings().archive_members_index_chars


def test_build_doc_omits_absent_archive_members():
    import uuid
    from datetime import UTC, datetime

    from filearr import search as search_mod
    from filearr.models import Item, ItemStatus

    item = Item(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="video", file_group="video",
        path="/data/a.mkv",
        rel_path="a.mkv",
        filename="a.mkv",
        extension="mkv",
        size=1,
        mtime=datetime.now(UTC),
        metadata_={},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
    )
    doc = search_mod.build_doc(item)
    assert doc["archive_members"] is None


# --------------------------------------------------------------- DB dispatch
# asyncio_mode = "auto" (pyproject) -> async tests below auto-run; the pure sync
# tests above run as-is (no module-level asyncio marker, which would break them).
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
    # Sync helper (ASYNC240: no blocking fs stat directly in an async function).
    p = Path(path)
    return p.name, p.suffix.lstrip("."), p.stat().st_size


async def _make_item(Session, media_type, path: str):
    from datetime import UTC, datetime

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


async def _fetch(Session, item_id):
    from sqlalchemy import select

    from filearr.models import Item

    async with Session() as s:
        return (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()


async def test_zip_item_populates_archive_metadata(db, tmp_path):
    from filearr.tasks.extract import extract_item

    _make_zip(tmp_path / "bundle.zip", [("a.txt", b"aa"), ("b/c.txt", b"ccc")])
    item_id = await _make_item(db, "other", str(tmp_path / "bundle.zip"))
    await extract_item(item_id)
    item = await _fetch(db, item_id)
    assert item.metadata_["archive"]["member_count"] == 2
    assert "a.txt" in item.metadata_["archive_members"]
    assert item.user_metadata in (None, {})  # extractor never touches user overlay
    assert "_extract_error" not in item.metadata_


async def test_cbz_item_drops_unsupported_marker(db, tmp_path):
    # cbz maps to media_type ``document`` (no property parser -> "unsupported"),
    # but the archive pass lists its pages and drops the marker.
    from filearr.tasks.extract import extract_item

    _make_zip(tmp_path / "comic.cbz", [("p01.jpg", b"\xff\xd8"), ("p02.jpg", b"\xff\xd8")])
    item_id = await _make_item(db, "document", str(tmp_path / "comic.cbz"))
    await extract_item(item_id)
    item = await _fetch(db, item_id)
    assert item.metadata_["archive"]["member_count"] == 2
    assert "unsupported" not in item.metadata_
    assert "_extract_error" not in item.metadata_


async def test_zipbomb_archive_records_error_not_crash(db, tmp_path):
    from filearr.tasks.extract import extract_item

    _make_zip(
        tmp_path / "bomb.zip",
        [("zeros", b"\0" * (11 * 1024 * 1024))],
        compress=zipfile.ZIP_DEFLATED,
    )
    item_id = await _make_item(db, "other", str(tmp_path / "bomb.zip"))
    await extract_item(item_id)  # must NOT raise
    item = await _fetch(db, item_id)
    assert "_extract_error" in item.metadata_
    assert "archive" not in item.metadata_
