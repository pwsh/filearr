"""End-to-end sidecar association against a real Postgres (pgserver).

Creates library + item rows and on-disk NFO files, runs the async association
pass, and asserts: sidecars link to parents, NFO metadata lands in the PARENT's
extracted `metadata` (never user_metadata), and a rescan is idempotent.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, ItemStatus, Library, MediaType
from filearr.tasks.associate import associate_sidecars

BACKEND_DIR = Path(__file__).resolve().parent.parent

MOVIE_NFO = b"""<movie>
  <title>Dune</title>
  <year>2021</year>
  <plot>War for a desert planet.</plot>
</movie>
"""


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def session(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    # clean slate between tests
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _mk_item(session, lib, rel, mt, path, size=100):
    it = Item(
        library_id=lib.id,
        media_type=mt,
        path=path,
        rel_path=rel,
        filename=rel.split("/")[-1],
        extension=rel.rsplit(".", 1)[-1] if "." in rel else None,
        size=size,
        mtime=datetime.now(UTC),
        status=ItemStatus.active,
    )
    session.add(it)
    await session.flush()
    return it


async def test_nfo_and_thumb_link_and_metadata(session, tmp_path):
    lib = Library(name="movies", root_path=str(tmp_path))
    session.add(lib)
    await session.flush()

    d = tmp_path / "Dune (2021)"
    d.mkdir()
    (d / "Dune (2021).nfo").write_bytes(MOVIE_NFO)

    video = await _mk_item(
        session, lib, "Dune (2021)/Dune (2021).mkv", MediaType.video,
        str(d / "Dune (2021).mkv"), size=5_000_000,
    )
    nfo = await _mk_item(
        session, lib, "Dune (2021)/Dune (2021).nfo", MediaType.other,
        str(d / "Dune (2021).nfo"),
    )
    thumb = await _mk_item(
        session, lib, "Dune (2021)/Dune (2021)-thumb.jpg", MediaType.image,
        str(d / "Dune (2021)-thumb.jpg"),
    )
    await session.commit()

    stats = await associate_sidecars(session, lib.id)
    await session.commit()

    await session.refresh(nfo)
    await session.refresh(thumb)
    await session.refresh(video)

    # links
    assert nfo.sidecar_of == video.id
    assert thumb.sidecar_of == video.id
    assert video.sidecar_of is None

    # NFO metadata folded into PARENT's extracted metadata (not user_metadata)
    assert video.metadata_.get("nfo_title") == "Dune"
    assert video.metadata_.get("nfo_year") == 2021
    assert "War for a desert planet." in video.metadata_.get("nfo_plot", "")
    assert video.user_metadata == {}  # extractors never touch user_metadata
    assert video.title == "Dune"  # promoted to typed column (was empty)
    assert video.year == 2021

    assert stats["sidecars"] == 2
    assert stats["linked"] == 2
    assert stats["nfo_parsed"] == 1


async def test_rescan_idempotent(session, tmp_path):
    lib = Library(name="movies2", root_path=str(tmp_path))
    session.add(lib)
    await session.flush()
    d = tmp_path / "X"
    d.mkdir()
    (d / "X.nfo").write_bytes(MOVIE_NFO)
    video = await _mk_item(session, lib, "X/X.mkv", MediaType.video, str(d / "X.mkv"), size=9)
    nfo = await _mk_item(session, lib, "X/X.nfo", MediaType.other, str(d / "X.nfo"))
    await session.commit()

    s1 = await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(nfo)
    first_parent = nfo.sidecar_of

    s2 = await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(nfo)

    assert nfo.sidecar_of == first_parent == video.id
    assert s1["linked"] == s2["linked"] == 1


async def test_directory_poster_links_to_primary(session, tmp_path):
    lib = Library(name="movies3", root_path=str(tmp_path))
    session.add(lib)
    await session.flush()
    d = tmp_path / "Dir"
    d.mkdir()
    big = await _mk_item(
        session, lib, "Dir/big.mkv", MediaType.video, str(d / "big.mkv"), size=10**9
    )
    poster = await _mk_item(session, lib, "Dir/poster.jpg", MediaType.image, str(d / "poster.jpg"))
    await session.commit()

    await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(poster)
    assert poster.sidecar_of == big.id
