"""P9-T5 — rebuild_index redesigned around a shadow index + atomic swap.

Covers ``meili_ops.rebuild_via_swap`` (create-shadow-WITH-pk, settings parity with
the live index via the SHARED ``_apply_settings`` helper, stream+wait, atomic swap
with the correct pair, old-shadow deletion, pre-swap failure safety), the
``reap_stale_shadows`` orphan sweep, the ``needs_rebuild_for_settings`` rule, and
the ``POST /api/v1/system/rebuild-index`` admin endpoint. Meilisearch is a stateful
in-memory fake; Postgres is a throwaway pgserver. The rebuild must NEVER write
Postgres and must leave the LIVE index untouched on any pre-swap failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from meilisearch_python_sdk.models.settings import MeilisearchSettings  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from filearr import search as search_mod  # noqa: E402
from filearr.meili_ops import (  # noqa: E402
    REBUILD_REQUIRING_SETTINGS,
    needs_rebuild_for_settings,
    reap_stale_shadows,
    settings_drift,
    shadow_uid,
)


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Stateful in-memory fake Meilisearch                                         #
# --------------------------------------------------------------------------- #
class FakeIndex:
    def __init__(self, client: FakeClient, uid: str, primary_key=None):
        self._client = client
        self.uid = uid
        self.primary_key = primary_key
        self.docs: dict[str, dict] = {}
        self.settings = MeilisearchSettings()

    async def get_settings(self):
        return self.settings

    async def update_searchable_attributes(self, v):
        self.settings.searchable_attributes = v
        return self._client._task(self.uid)

    async def update_filterable_attributes(self, v):
        self.settings.filterable_attributes = v
        return self._client._task(self.uid)

    async def update_sortable_attributes(self, v):
        self.settings.sortable_attributes = v
        return self._client._task(self.uid)

    async def update_ranking_rules(self, v):
        self.settings.ranking_rules = v
        return self._client._task(self.uid)

    async def update_typo_tolerance(self, v):
        self.settings.typo_tolerance = v
        return self._client._task(self.uid)

    async def update_faceting(self, v):
        self.settings.faceting = v
        return self._client._task(self.uid)

    async def update_search_cutoff_ms(self, v):
        self.settings.search_cutoff_ms = v
        return self._client._task(self.uid)

    async def update_documents(self, docs, primary_key=None):
        assert primary_key == "id"  # P9-T5: explicit pk on every backfill batch
        for d in docs:
            self.docs[d["id"]] = d
        return self._client._task(self.uid)


class FakeClient:
    def __init__(self, *, fail_task_status: str | None = None):
        self.indexes: dict[str, FakeIndex] = {}
        self._task_counter = 0
        self.fail_task_status = fail_task_status
        # observability
        self.created: list[tuple[str, str | None]] = []
        self.waited: list[int] = []
        self.swaps: list[list[tuple[str, str]]] = []
        self.deleted: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _task(self, index_uid: str):
        self._task_counter += 1
        return SimpleNamespace(task_uid=self._task_counter, index_uid=index_uid)

    def _seed(self, uid: str, ids=()):
        idx = FakeIndex(self, uid, primary_key="id")
        idx.docs = {i: {"id": i} for i in ids}
        self.indexes[uid] = idx
        return idx

    async def create_index(self, uid, primary_key=None, **kw):
        idx = FakeIndex(self, uid, primary_key)
        self.indexes[uid] = idx
        self.created.append((uid, primary_key))
        return idx

    def index(self, uid):
        return self.indexes.setdefault(uid, FakeIndex(self, uid, primary_key="id"))

    async def wait_for_task(self, task_id, *, timeout_in_ms=None, **kw):
        self.waited.append(task_id)
        return SimpleNamespace(uid=task_id, status=self.fail_task_status or "succeeded")

    async def swap_indexes(self, pairs, rename=False):
        self.swaps.append(list(pairs))
        a, b = pairs[0]
        ia, ib = self.indexes.get(a), self.indexes.get(b)
        self.indexes[a], self.indexes[b] = ib, ia
        if ib is not None:
            ib.uid = a
        if ia is not None:
            ia.uid = b
        return self._task(a)

    async def delete_index_if_exists(self, uid):
        self.deleted.append(uid)
        return self.indexes.pop(uid, None) is not None

    async def get_indexes(self, *, offset=None, limit=None):
        return list(self.indexes.values())


# --------------------------------------------------------------------------- #
# needs_rebuild_for_settings rule (pure)                                      #
# --------------------------------------------------------------------------- #
def test_needs_rebuild_is_false_for_all_managed_settings_today():
    # Documented P9-T5 rule: every managed setting applies in place, so no drift
    # currently warrants a full rebuild.
    assert REBUILD_REQUIRING_SETTINGS == frozenset()
    assert needs_rebuild_for_settings([]) is False
    assert needs_rebuild_for_settings(
        ["searchableAttributes", "filterableAttributes", "typoTolerance"]
    ) is False


def test_needs_rebuild_flips_when_a_setting_is_reprocessing():
    # A future build_doc/schema migration adds a key here; the helper must then
    # route that drift to the rebuild task. Simulate via monkeypatch-free set math.
    from filearr import meili_ops

    assert needs_rebuild_for_settings({"__reprocess__"}) is False
    marked = frozenset({"__reprocess__"})
    orig = meili_ops.REBUILD_REQUIRING_SETTINGS
    try:
        meili_ops.REBUILD_REQUIRING_SETTINGS = marked
        assert meili_ops.needs_rebuild_for_settings(["__reprocess__"]) is True
        assert meili_ops.needs_rebuild_for_settings(["searchableAttributes"]) is False
    finally:
        meili_ops.REBUILD_REQUIRING_SETTINGS = orig


# --------------------------------------------------------------------------- #
# Settings parity: the shadow gets EXACTLY the live-desired settings          #
# --------------------------------------------------------------------------- #
async def test_shadow_settings_match_live_desired_zero_drift():
    """Applying the SHARED _apply_settings to a fresh shadow yields settings whose
    projection has zero drift against _desired_settings() (provably identical to
    what ensure_index() applies to the live index)."""
    shadow = FakeIndex(FakeClient(), "items_rebuild_1", primary_key="id")
    task_sink: list = []
    drift_applied = await search_mod._apply_settings(shadow, task_sink=task_sink)
    assert drift_applied  # a fresh index drifts from the target, so it applied
    assert len(task_sink) == 7  # one task per settings-update call, all captured

    projected = search_mod._project_current(await shadow.get_settings())
    assert settings_drift(projected, search_mod._desired_settings()) == []


# --------------------------------------------------------------------------- #
# Integration: real Postgres + fake Meili                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def swap_pg(module_db):
    return module_db


@pytest.fixture
async def swap(swap_pg, monkeypatch):
    import filearr.db as db_mod
    from filearr.models import Base

    engine = create_async_engine(_psycopg3(swap_pg.get_uri()))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # rebuild_via_swap imports SessionLocal from filearr.db at call time.
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    try:
        yield SimpleNamespace(maker=maker, engine=engine, monkeypatch=monkeypatch)
    finally:
        await engine.dispose()


async def _mk_library(maker):
    from filearr.models import Library

    async with maker() as s:
        lib = Library(name="lib", root_path="/d")
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(maker, library_id, rel_path, *, status="active"):
    from filearr.models import Item, MediaType

    async with maker() as s:
        item = Item(
            library_id=library_id,
            media_type=MediaType.video,
            status=status,
            path=f"/d/{rel_path}",
            rel_path=rel_path,
            filename=rel_path,
            extension="mp4",
            size=1,
            mtime=datetime.now(UTC),
            metadata_={},
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_full_rebuild_swaps_shadow_into_place(swap):
    from filearr.meili_ops import rebuild_via_swap

    lib = await _mk_library(swap.maker)
    a = await _mk_item(swap.maker, lib, "a.mp4")
    b = await _mk_item(swap.maker, lib, "b.mp4")
    await _mk_item(swap.maker, lib, "gone.mp4", status="missing")  # must NOT index

    fake = FakeClient()
    # live index pre-exists holding STALE data (a swap target must exist).
    fake._seed("items", ids=["stale-1", "stale-2"])
    swap.monkeypatch.setattr(search_mod, "client", lambda: fake)

    total = await rebuild_via_swap()
    assert total == 2  # only the two active items

    # shadow was created WITH primary_key="id" on the deterministic naming scheme
    assert len(fake.created) == 1
    (created_name, created_pk) = fake.created[0]
    assert created_pk == "id"
    assert created_name.startswith("items_rebuild_")

    # swap called once with exactly (base, shadow)
    assert fake.swaps == [[("items", created_name)]]
    # every enqueued shadow task was awaited (7 settings + 1 doc batch + 1 swap)
    assert len(fake.waited) >= 8

    # post-swap: the live "items" now serves the NEW projection (a, b), full docs
    live = fake.indexes["items"]
    assert set(live.docs) == {a, b}
    assert live.docs[a]["rel_path"] == "a.mp4"
    # the old-data shadow index was deleted after the swap
    assert created_name in fake.deleted
    assert created_name not in fake.indexes

    # Postgres was never written (still 3 rows: 2 active + 1 missing).
    async with swap.maker() as s:
        n = (await s.execute(text("SELECT count(*) FROM items"))).scalar()
    assert n == 3


async def test_preswap_task_failure_leaves_live_untouched(swap):
    from filearr.meili_ops import rebuild_via_swap

    lib = await _mk_library(swap.maker)
    await _mk_item(swap.maker, lib, "a.mp4")

    fake = FakeClient(fail_task_status="failed")  # a shadow task fails
    fake._seed("items", ids=["live-1", "live-2"])
    swap.monkeypatch.setattr(search_mod, "client", lambda: fake)

    with pytest.raises(RuntimeError):
        await rebuild_via_swap()

    # NO swap happened; the live index is byte-for-byte untouched.
    assert fake.swaps == []
    assert set(fake.indexes["items"].docs) == {"live-1", "live-2"}
    # the partial shadow was best-effort deleted (create name + delete recorded)
    (shadow_name, _) = fake.created[0]
    assert shadow_name in fake.deleted
    assert shadow_name not in fake.indexes


async def test_rebuild_delegator_calls_swap(swap):
    """The Procrastinate task delegates to rebuild_via_swap (P9-T5 redesign)."""
    from filearr.meili_ops import rebuild_via_swap  # noqa: F401
    from filearr.tasks.index_sync import rebuild_index

    lib = await _mk_library(swap.maker)
    await _mk_item(swap.maker, lib, "solo.mp4")
    fake = FakeClient()
    fake._seed("items", ids=["old"])
    swap.monkeypatch.setattr(search_mod, "client", lambda: fake)

    # rebuild_index is a Procrastinate task; call its wrapped coroutine directly.
    fn = getattr(rebuild_index, "func", rebuild_index)
    total = await fn()
    assert total == 1
    assert len(fake.swaps) == 1


# --------------------------------------------------------------------------- #
# Stale-shadow orphan sweep                                                    #
# --------------------------------------------------------------------------- #
async def test_reap_stale_shadows_deletes_only_old_shadows(monkeypatch):
    now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
    old_ts = now - timedelta(hours=10)
    fresh_ts = now - timedelta(minutes=5)

    fake = FakeClient()
    fake._seed("items")  # the live index — never a shadow, never reaped
    old_shadow = shadow_uid("items", old_ts)
    fresh_shadow = shadow_uid("items", fresh_ts)
    fake._seed(old_shadow)
    fake._seed(fresh_shadow)
    monkeypatch.setattr(search_mod, "client", lambda: fake)

    reaped = await reap_stale_shadows(now=now, max_age=timedelta(hours=6))
    assert reaped == [old_shadow]
    assert old_shadow not in fake.indexes
    assert fresh_shadow in fake.indexes  # in-flight rebuild's shadow survives
    assert "items" in fake.indexes  # live index untouched


async def test_reap_uses_configured_default_age(monkeypatch):
    from filearr.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "meili_shadow_max_age_hours", 6)
    now = datetime.now(UTC)
    fake = FakeClient()
    fake._seed(shadow_uid("items", now - timedelta(hours=7)))
    fake._seed(shadow_uid("items", now - timedelta(hours=1)))
    monkeypatch.setattr(search_mod, "client", lambda: fake)

    reaped = await reap_stale_shadows()  # now/max_age default from config
    assert len(reaped) == 1
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# POST /api/v1/system/rebuild-index endpoint                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def api_client(swap_pg, monkeypatch):
    import httpx

    import filearr.db as db_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    # The rebuild-index endpoint defers a job (mocked) and never queries items, and
    # the auth gate rejects before touching the DB — so no schema is needed here;
    # reusing the module pgserver only to satisfy get_session opening a connection.
    monkeypatch.setenv("FILEARR_DATABASE_URL", swap_pg.get_uri())
    get_settings.cache_clear()
    engine = create_async_engine(_psycopg3(swap_pg.get_uri()))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    def _make(auth_enabled: bool):
        s = get_settings()
        monkeypatch.setattr(s, "auth_enabled", auth_enabled)
        app = create_app()

        async def _test_session():
            async with maker() as sess:
                yield sess

        app.dependency_overrides[get_session] = _test_session
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://t"), app

    try:
        yield SimpleNamespace(make=_make, monkeypatch=monkeypatch)
    finally:
        await engine.dispose()
        get_settings.cache_clear()


BASE = "/api/v1/system/rebuild-index"


async def test_rebuild_endpoint_defers_and_returns_job_id(api_client):
    from unittest.mock import AsyncMock

    import filearr.worker as worker_mod

    deferred = AsyncMock(return_value=4242)
    api_client.monkeypatch.setattr(worker_mod, "defer_rebuild_index", deferred)

    c, _ = api_client.make(auth_enabled=False)
    async with c:
        r = await c.post(BASE)
    assert r.status_code == 202, r.text
    assert r.json() == {"job_id": 4242}
    deferred.assert_awaited_once()


async def test_rebuild_endpoint_requires_admin_scope(api_client):
    # auth ON + no bearer -> 401 (admin scope gate), and the defer is never called.
    from unittest.mock import AsyncMock

    import filearr.worker as worker_mod

    deferred = AsyncMock(return_value=1)
    api_client.monkeypatch.setattr(worker_mod, "defer_rebuild_index", deferred)

    c, _ = api_client.make(auth_enabled=True)
    async with c:
        r = await c.post(BASE)
    assert r.status_code == 401, r.text
    deferred.assert_not_awaited()
