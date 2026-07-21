"""W9 — targeted / scoped rescan of a single FILE or a DIRECTORY (optionally
recursive), exposed in the API.

Layers:
  * pure ``walk`` recursion-mode unit tests (direct-children vs full descent);
  * DB + real-files scoped scan execution over the migrated Postgres:
      - single-file scope ingests exactly its one item;
      - a brand-new on-disk directory NOT in the catalog gets its files ingested;
      - the tombstone blast radius is EXACTLY the scanned set for a file scope, a
        non-recursive dir scope and a recursive dir scope (a sibling / parent /
        out-of-scope descendant is never tombstoned);
  * the ``POST /libraries/{id}/scan/targeted`` endpoint (202 + echoes, 404 path
    absent on disk, traversal 422, recursive flag threaded, dedupe coalesce,
    audit, agent-owned refusal).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from pathspec import GitIgnoreSpec
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, ItemStatus, Library, ScanRun

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


async def _reset(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM scan_paths"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM libraries"))


# --------------------------------------------------------------------------- #
# walk() recursion mode (pure, real files, no DB)                             #
# --------------------------------------------------------------------------- #
def test_walk_non_recursive_emits_direct_children_only(tmp_path):
    from filearr.tasks.scan import walk

    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "a.mkv").write_bytes(b"a")
    (tmp_path / "Downloads" / "b.mkv").write_bytes(b"b")
    (tmp_path / "Downloads" / "Sub").mkdir()
    (tmp_path / "Downloads" / "Sub" / "deep.mkv").write_bytes(b"c")

    spec = GitIgnoreSpec.from_lines([])
    rels = {
        rel for _p, rel, _s, _m in walk(
            str(tmp_path), spec, start_rel="Downloads", recursive=False
        )
    }
    # Direct-child files only; the subdirectory is neither descended nor emitted.
    assert rels == {"Downloads/a.mkv", "Downloads/b.mkv"}


def test_walk_recursive_default_descends(tmp_path):
    from filearr.tasks.scan import walk

    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "a.mkv").write_bytes(b"a")
    (tmp_path / "Downloads" / "Sub").mkdir()
    (tmp_path / "Downloads" / "Sub" / "deep.mkv").write_bytes(b"c")

    spec = GitIgnoreSpec.from_lines([])
    rels = {rel for _p, rel, _s, _m in walk(str(tmp_path), spec, start_rel="Downloads")}
    # Default recursive=True still descends (byte-for-byte prior behaviour).
    assert rels == {"Downloads/a.mkv", "Downloads/Sub/deep.mkv"}


# --------------------------------------------------------------------------- #
# scoped scan execution                                                        #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def scan_env(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    await _reset(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    from filearr.tasks import scan as scan_mod

    async def _noop_reindex(sess, lib_id):
        return None

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)
    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _noop_defer)
    yield {"maker": maker, "scan": scan_mod}
    await engine.dispose()


async def _mk_lib(maker, root):
    async with maker() as s:
        lib = Library(name="L", root_path=str(root))
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        return lib.id


async def _run(scan_mod, session, library, scope_rel, *, recursive=True):
    run = ScanRun(library_id=library.id, rel_path=scope_rel, stats={})
    session.add(run)
    await session.commit()
    stats = await scan_mod._scan_body(
        session, library, run, scope_rel=scope_rel, recursive=recursive
    )
    return run, stats


async def _items(session, lib_id) -> dict[str, Item]:
    return {
        i.rel_path: i
        for i in (
            await session.execute(select(Item).where(Item.library_id == lib_id))
        ).scalars()
    }


async def test_single_file_scope_ingests_one_item(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    (tmp_path / "Movies").mkdir()
    (tmp_path / "Movies" / "a.mkv").write_bytes(b"a" * 10)
    (tmp_path / "Movies" / "b.mkv").write_bytes(b"b" * 10)
    lib_id = await _mk_lib(maker, tmp_path)

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        # Scope EXACTLY one file that is not yet in the catalog.
        run, stats = await _run(scan_mod, s, lib, "Movies/a.mkv")
        assert stats["scope"] == "Movies/a.mkv"
        assert stats["scope_is_file"] is True
        assert stats["seen"] == 1
        items = await _items(s, lib_id)
    # Only the targeted file was ingested; its sibling was never walked.
    assert "Movies/a.mkv" in items
    assert items["Movies/a.mkv"].status == ItemStatus.active
    assert "Movies/b.mkv" not in items


async def test_single_file_scope_tombstones_only_itself(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    (tmp_path / "Movies").mkdir()
    gone = tmp_path / "Movies" / "gone.mkv"
    gone.write_bytes(b"x" * 10)
    (tmp_path / "Movies" / "stay.mkv").write_bytes(b"y" * 10)
    lib_id = await _mk_lib(maker, tmp_path)

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        await _run(scan_mod, s, lib, None)  # full scan first

    gone.unlink()  # the targeted file vanishes

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        # Scope the now-absent file: on-disk classification is "absent", the
        # catalog disambiguates it as a single file -> tombstone exactly it.
        run, stats = await _run(scan_mod, s, lib, "Movies/gone.mkv")
        items = await _items(s, lib_id)
    assert items["Movies/gone.mkv"].status == ItemStatus.missing
    # The sibling (outside the single-file scope) is untouched.
    assert items["Movies/stay.mkv"].status == ItemStatus.active


async def test_brand_new_dir_not_in_catalog_gets_ingested(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    lib_id = await _mk_lib(maker, tmp_path)
    # Automation lays a NEW directory down after the library exists; it is not in
    # the catalog. A targeted scan of it ingests its files without a full rescan.
    (tmp_path / "New").mkdir()
    (tmp_path / "New" / "x.mkv").write_bytes(b"x" * 10)
    (tmp_path / "New" / "Sub").mkdir()
    (tmp_path / "New" / "Sub" / "y.mkv").write_bytes(b"y" * 10)

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        run, stats = await _run(scan_mod, s, lib, "New")
        assert stats["scope"] == "New"
        assert stats["scope_is_file"] is False
        items = await _items(s, lib_id)
    assert items["New/x.mkv"].status == ItemStatus.active
    assert items["New/Sub/y.mkv"].status == ItemStatus.active  # recursive default


async def test_non_recursive_dir_scope_scans_direct_children_only(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    (tmp_path / "Dir").mkdir()
    (tmp_path / "Dir" / "top.mkv").write_bytes(b"t" * 10)
    (tmp_path / "Dir" / "Sub").mkdir()
    (tmp_path / "Dir" / "Sub" / "deep.mkv").write_bytes(b"d" * 10)
    lib_id = await _mk_lib(maker, tmp_path)

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        run, stats = await _run(scan_mod, s, lib, "Dir", recursive=False)
        assert stats["recursive"] is False
        assert stats["seen"] == 1  # only the direct child
        items = await _items(s, lib_id)
    assert items["Dir/top.mkv"].status == ItemStatus.active
    # The descendant one level deeper was NOT walked by a non-recursive scan.
    assert "Dir/Sub/deep.mkv" not in items


async def test_non_recursive_dir_scope_never_tombstones_descendant(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    (tmp_path / "Dir").mkdir()
    (tmp_path / "Dir" / "top.mkv").write_bytes(b"t" * 10)
    (tmp_path / "Dir" / "Sub").mkdir()
    deep = tmp_path / "Dir" / "Sub" / "deep.mkv"
    deep.write_bytes(b"d" * 10)
    lib_id = await _mk_lib(maker, tmp_path)

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        await _run(scan_mod, s, lib, None)  # full scan: both indexed

    deep.unlink()  # a DESCENDANT (outside a non-recursive scope) vanishes

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        await _run(scan_mod, s, lib, "Dir", recursive=False)
        items = await _items(s, lib_id)
    # The non-recursive scope only considers Dir's direct children, so the deeper
    # (now-deleted) file is NEVER tombstoned by this scan.
    assert items["Dir/Sub/deep.mkv"].status == ItemStatus.active
    assert items["Dir/top.mkv"].status == ItemStatus.active


async def test_recursive_dir_scope_never_tombstones_out_of_scope_sibling(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "keep.mkv").write_bytes(b"k" * 10)
    b_gone = tmp_path / "B" / "gone.mkv"
    b_gone.write_bytes(b"g" * 10)
    lib_id = await _mk_lib(maker, tmp_path)

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        await _run(scan_mod, s, lib, None)  # full scan

    b_gone.unlink()  # a file in a DIFFERENT subtree vanishes

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        await _run(scan_mod, s, lib, "A", recursive=True)
        items = await _items(s, lib_id)
    # Recursive scope of A must not tombstone B's vanished file (out of scope).
    assert items["B/gone.mkv"].status == ItemStatus.active
    assert items["A/keep.mkv"].status == ItemStatus.active


# --------------------------------------------------------------------------- #
# endpoint                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def client(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    await _reset(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    import filearr.api.libraries as lib_api

    calls: list[dict] = []

    async def fake_defer_scan(library_id, *, rel_path=None, recursive=True, force=False):
        calls.append(
            {"library_id": library_id, "rel_path": rel_path, "recursive": recursive,
             "force": force}
        )
        return 4242

    monkeypatch.setattr(lib_api, "defer_scan", fake_defer_scan)

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.maker = maker  # type: ignore[attr-defined]
        c.calls = calls  # type: ignore[attr-defined]
        c.defer_setter = lambda fn: monkeypatch.setattr(lib_api, "defer_scan", fn)  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def _mk_api_lib(client, root):
    async with client.maker() as s:  # type: ignore[attr-defined]
        lib = Library(name="L", root_path=str(root))
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        return lib.id


async def test_targeted_scan_202_and_threads_recursive(client, tmp_path):
    (tmp_path / "Dir").mkdir()
    (tmp_path / "Dir" / "a.mkv").write_bytes(b"a")
    lib = await _mk_api_lib(client, tmp_path)

    r = await client.post(
        f"/api/v1/libraries/{lib}/scan/targeted",
        json={"path": "Dir", "recursive": False},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["scan_id"] == 4242
    assert body["path"] == "Dir"
    assert body["recursive"] is False
    assert body["is_file"] is False
    assert body["coalesced"] is False
    # defer_scan received the scoped rel_path + the recursive=False flag.
    assert client.calls[-1]["rel_path"] == "Dir"  # type: ignore[attr-defined]
    assert client.calls[-1]["recursive"] is False  # type: ignore[attr-defined]


async def test_targeted_scan_single_file_reports_is_file(client, tmp_path):
    (tmp_path / "Dir").mkdir()
    (tmp_path / "Dir" / "a.mkv").write_bytes(b"a")
    lib = await _mk_api_lib(client, tmp_path)

    r = await client.post(
        f"/api/v1/libraries/{lib}/scan/targeted",
        json={"path": "Dir/a.mkv"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["is_file"] is True
    assert client.calls[-1]["rel_path"] == "Dir/a.mkv"  # type: ignore[attr-defined]


async def test_targeted_scan_empty_path_is_full_library(client, tmp_path):
    lib = await _mk_api_lib(client, tmp_path)
    r = await client.post(
        f"/api/v1/libraries/{lib}/scan/targeted", json={"path": ""}
    )
    assert r.status_code == 202, r.text
    assert r.json()["path"] == ""
    # Empty path maps to a full-library scan (rel_path=None).
    assert client.calls[-1]["rel_path"] is None  # type: ignore[attr-defined]


async def test_targeted_scan_404_when_absent_on_disk(client, tmp_path):
    lib = await _mk_api_lib(client, tmp_path)
    r = await client.post(
        f"/api/v1/libraries/{lib}/scan/targeted",
        json={"path": "does/not/exist"},
    )
    assert r.status_code == 404, r.text
    assert "not found on disk" in r.json()["detail"]
    # Nothing was enqueued.
    assert client.calls == []  # type: ignore[attr-defined]


async def test_targeted_scan_rejects_traversal(client, tmp_path):
    lib = await _mk_api_lib(client, tmp_path)
    for bad in ["../etc", "a/../../b", "/abs/path", "C:\\win", "a/./b"]:
        r = await client.post(
            f"/api/v1/libraries/{lib}/scan/targeted", json={"path": bad}
        )
        assert r.status_code == 422, f"{bad!r} -> {r.status_code}"
    assert client.calls == []  # type: ignore[attr-defined]


async def test_targeted_scan_dedupe_coalesces(client, tmp_path):
    (tmp_path / "Dir").mkdir()
    lib = await _mk_api_lib(client, tmp_path)

    async def coalescing_defer(library_id, *, rel_path=None, recursive=True, force=False):
        return None  # an unfinished scan for this scope already exists

    client.defer_setter(coalescing_defer)  # type: ignore[attr-defined]
    r = await client.post(
        f"/api/v1/libraries/{lib}/scan/targeted", json={"path": "Dir"}
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["scan_id"] is None
    assert body["coalesced"] is True


async def test_targeted_scan_audited(client, tmp_path):
    (tmp_path / "Dir").mkdir()
    lib = await _mk_api_lib(client, tmp_path)
    await client.post(
        f"/api/v1/libraries/{lib}/scan/targeted",
        json={"path": "Dir", "recursive": False},
    )
    async with client.maker() as s:  # type: ignore[attr-defined]
        rows = (
            await s.execute(
                text("SELECT event_type, details FROM security_events "
                     "WHERE event_type = 'scan_targeted'")
            )
        ).all()
    assert len(rows) == 1
    details = rows[0][1]
    assert details["path"] == "Dir"
    assert details["recursive"] is False


async def test_targeted_scan_agent_owned_422(client, tmp_path):
    import uuid as _uuid

    from filearr.models import Agent

    async with client.maker() as s:  # type: ignore[attr-defined]
        agent = Agent(
            name="a", hostname="a", platform="linux",
            cert_fingerprint="FP:" + _uuid.uuid4().hex,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        lib = Library(
            name="agent-lib", root_path=str(tmp_path),
            source_agent_id=agent.id, agent_library_ref=str(tmp_path),
        )
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        lib_id = lib.id

    r = await client.post(
        f"/api/v1/libraries/{lib_id}/scan/targeted", json={"path": ""}
    )
    assert r.status_code == 422
    assert "agent" in r.json()["detail"].lower()
    assert client.calls == []  # type: ignore[attr-defined]
