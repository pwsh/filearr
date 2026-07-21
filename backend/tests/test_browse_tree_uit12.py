"""UI-T12 — folder-browse tree endpoint + share_prefix API round-trip.

GET /api/v1/libraries/{id}/tree?path=&limit=&offset= (read scope):
  * folders  = distinct next path segment among nested items (with subtree count),
  * items    = rows whose rel_path dirname == path exactly,
  * sidecars + trashed tombstones excluded from both,
  * path traversal / absolute / drive input rejected 422,
  * items paginated (limit default 100, capped 500), folders capped 500,
  * LIKE metacharacters (% _) in a path segment are escaped (no wildcarding),
  * empty root returns empty listings.

Plus: share_prefix survives create (POST) / read (GET) / update (PATCH).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASE = "/api/v1/libraries"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def wired(pg_uri, monkeypatch):
    from alembic.config import Config

    from alembic import command
    from filearr import db as db_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
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
    yield {"app": app, "maker": maker, "engine": engine}
    app.dependency_overrides.clear()
    await engine.dispose()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _mk_lib(maker, name="Lib", **kw):
    from filearr.models import Library

    async with maker() as s:
        lib = Library(name=name, root_path="/d", **kw)
        s.add(lib)
        await s.commit()
        return str(lib.id)


async def _mk_item(maker, library_id, rel_path, *, status="active",
                   sidecar_of=None, title=None, year=None):
    from filearr.file_groups import detect_category, detect_group
    from filearr.models import Item

    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category=detect_category(rel_path),
            file_group=detect_group(rel_path),
            status=status,
            path=f"/d/{rel_path}",
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            extension=rel_path.rsplit(".", 1)[-1] if "." in rel_path else None,
            size=1,
            mtime=datetime.now(UTC),
            title=title,
            year=year,
            sidecar_of=sidecar_of,
            metadata_={},
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def _tree(app, lib, path="", **q):
    async with _client(app) as c:
        params = {"path": path, **q}
        return await c.get(f"{BASE}/{lib}/tree", params=params)


# --------------------------------------------------------------------------- #
# share_prefix round-trip                                                     #
# --------------------------------------------------------------------------- #
async def test_share_prefix_create_read_update(wired):
    async with _client(wired["app"]) as c:
        r = await c.post(
            BASE,
            json={"name": "Shared", "root_path": "/d", "share_prefix": "\\\\tower\\media"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["share_prefix"] == "\\\\tower\\media"
        lib_id = r.json()["id"]

        g = await c.get(BASE)
        assert g.status_code == 200
        got = next(x for x in g.json() if x["id"] == lib_id)
        assert got["share_prefix"] == "\\\\tower\\media"

        p = await c.patch(f"{BASE}/{lib_id}", json={"share_prefix": "smb://tower/media"})
        assert p.status_code == 200, p.text
        assert p.json()["share_prefix"] == "smb://tower/media"

        # clearing it back to null
        p2 = await c.patch(f"{BASE}/{lib_id}", json={"share_prefix": None})
        assert p2.status_code == 200
        assert p2.json()["share_prefix"] is None


# --------------------------------------------------------------------------- #
# tree listing                                                                #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def tree_lib(wired):
    maker = wired["maker"]
    lib = await _mk_lib(maker, "Media")
    # top-level file
    await _mk_item(maker, lib, "readme.txt")
    # Movies subtree with a sidecar and a trashed sibling
    vid = await _mk_item(maker, lib, "Movies/Arcane (2021)/Arcane.mp4", title="Arcane", year=2021)
    await _mk_item(maker, lib, "Movies/Arcane (2021)/Arcane.nfo", sidecar_of=vid)
    await _mk_item(maker, lib, "Movies/Dune/Dune.mp4", title="Dune")
    await _mk_item(maker, lib, "Movies/old.mp4", status="trashed")
    # a second top-level folder
    await _mk_item(maker, lib, "Music/song.mp3")
    # LIKE-metacharacter folders (escaping)
    await _mk_item(maker, lib, "Weird%Folder/a.mp4")
    await _mk_item(maker, lib, "WeirdXXXFolder/other.mp4")
    await _mk_item(maker, lib, "Under_score/b.mp4")
    await _mk_item(maker, lib, "UnderXscore/c.mp4")
    # pagination folder
    for i in range(5):
        await _mk_item(maker, lib, f"Bulk/f{i}.mp4")
    return {"app": wired["app"], "lib": lib}


async def test_root_listing_folders_and_top_level_items(tree_lib):
    r = await _tree(tree_lib["app"], tree_lib["lib"], "")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == ""
    assert body["library_name"] == "Media"
    names = {f["name"]: f["item_count"] for f in body["folders"]}
    # every top-level folder present
    assert {"Movies", "Music", "Weird%Folder", "WeirdXXXFolder",
            "Under_score", "UnderXscore", "Bulk"} <= set(names)
    # Movies subtree: Arcane.mp4 + Dune.mp4 (nfo sidecar + trashed old.mp4 excluded)
    assert names["Movies"] == 2
    assert names["Bulk"] == 5
    # only the top-level file is an item at the root
    item_names = {i["filename"] for i in body["items"]}
    assert item_names == {"readme.txt"}
    assert body["total_items"] == 1


async def test_nested_dir_items_are_exact_dir_only(tree_lib):
    r = await _tree(tree_lib["app"], tree_lib["lib"], "Movies/Arcane (2021)")
    assert r.status_code == 200
    body = r.json()
    # the sidecar (.nfo) is excluded; only the primary mp4 is listed
    assert [i["filename"] for i in body["items"]] == ["Arcane.mp4"]
    assert body["items"][0]["title"] == "Arcane"
    assert body["items"][0]["year"] == 2021
    assert body["folders"] == []
    assert body["total_items"] == 1


async def test_intermediate_dir_has_folders_no_items(tree_lib):
    r = await _tree(tree_lib["app"], tree_lib["lib"], "Movies")
    assert r.status_code == 200
    body = r.json()
    names = {f["name"]: f["item_count"] for f in body["folders"]}
    assert names == {"Arcane (2021)": 1, "Dune": 1}
    # old.mp4 is trashed -> no direct items
    assert body["items"] == []
    assert body["total_items"] == 0


async def test_like_metacharacters_do_not_wildcard(tree_lib):
    # Browsing "Weird%Folder" must NOT also match "WeirdXXXFolder".
    r = await _tree(tree_lib["app"], tree_lib["lib"], "Weird%Folder")
    assert r.status_code == 200
    body = r.json()
    assert [i["filename"] for i in body["items"]] == ["a.mp4"]
    assert body["total_items"] == 1
    # Same for the '_' single-char wildcard.
    r2 = await _tree(tree_lib["app"], tree_lib["lib"], "Under_score")
    assert [i["filename"] for i in r2.json()["items"]] == ["b.mp4"]
    assert r2.json()["total_items"] == 1


async def test_pagination(tree_lib):
    first = await _tree(tree_lib["app"], tree_lib["lib"], "Bulk", limit=2, offset=0)
    assert first.status_code == 200
    b1 = first.json()
    assert len(b1["items"]) == 2
    assert b1["total_items"] == 5
    assert [i["filename"] for i in b1["items"]] == ["f0.mp4", "f1.mp4"]
    last = await _tree(tree_lib["app"], tree_lib["lib"], "Bulk", limit=2, offset=4)
    b2 = last.json()
    assert [i["filename"] for i in b2["items"]] == ["f4.mp4"]
    assert b2["total_items"] == 5


async def test_empty_root(wired):
    lib = await _mk_lib(wired["maker"], "Empty")
    r = await _tree(wired["app"], lib, "")
    assert r.status_code == 200
    body = r.json()
    assert body["folders"] == []
    assert body["items"] == []
    assert body["total_items"] == 0


async def test_unknown_library_404(wired):
    r = await _tree(wired["app"], "00000000-0000-0000-0000-000000000000", "")
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["../etc", "a/../b", "/abs", "\\\\unc\\x", "C:/x", "a/./b"])
async def test_path_traversal_rejected_422(tree_lib, bad):
    r = await _tree(tree_lib["app"], tree_lib["lib"], bad)
    assert r.status_code == 422, (bad, r.status_code)


async def test_folder_pagination_beyond_cap(tree_lib, monkeypatch):
    """Directories with more subfolders than the per-page cap paginate via
    folders_offset/folders_total (live bug: >500 shows truncated at 'S')."""
    import filearr.api.libraries as lib_mod

    monkeypatch.setattr(lib_mod, "_TREE_FOLDER_CAP", 2)
    app, lib = tree_lib["app"], tree_lib["lib"]

    r = await _tree(app, lib, "")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["folders"]) == 2
    total = body["folders_total"]
    assert total > 2

    seen = [f["name"] for f in body["folders"]]
    off = 2
    while len(seen) < total:
        page = (await _tree(app, lib, "", folders_offset=off)).json()
        assert page["folders_offset"] == off
        got = [f["name"] for f in page["folders"]]
        assert got, "empty page before folders_total reached"
        seen += got
        off += len(got)
    assert len(seen) == total == len(set(seen))
    assert seen == sorted(seen)
