"""P3-T8/T9 — local embedder pipeline, hybrid search wiring, and /similar.

The embedding ENGINE (fastembed/ONNX) is never imported: ``embed_texts`` is
monkeypatched everywhere so the model libs stay out of the test process (they are
lazy-loaded in production and deliberately absent from the sandbox). Coverage:

* pure text builder shape (filename + title + a/a/a + tags + 512-char body);
* ``build_doc`` attaches ``_vectors`` ONLY for a current-fingerprint embedding and
  omits it on drift or when semantic search is off;
* ``_apply_settings`` registers the userProvided embedder iff semantic is enabled;
* the embed task stores vector+fp and re-syncs; disabled => no-op;
* the backfill caps per run and skips already-current items;
* /search passes hybrid+vector through only when enabled and ratio>0 (validated);
* /similar shapes hits (self excluded) and 409s when disabled/unembedded;
* the /stats semantic snapshot counts embedded/pending/drift.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from filearr import search as search_mod
from filearr.config import get_settings
from filearr.embed import (
    EMBEDDING_KEY,
    FINGERPRINT_KEY,
    EmbedderConfig,
    embed_source_from_item,
    embed_text_for_item,
    embed_texts,
    embedder_fingerprint,
    has_current_embedding,
    strip_embedding,
)
from filearr.models import Item, ItemStatus

DIM = 384
VEC = [0.01 * i for i in range(DIM)]


def _item(**over):
    base = dict(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="document", file_group="document-text",
        path="/data/a.pdf",
        rel_path="a.pdf",
        filename="a.pdf",
        extension="pdf",
        size=1,
        mtime=datetime.now(UTC),
        metadata_={},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
        title="A Title",
    )
    base.update(over)
    return Item(**base)


# --------------------------------------------------------------- text builder
def test_embed_text_for_item_shape():
    doc = {
        "filename": "song.flac",
        "title": "My Song",
        "artist": "The Artist",
        "album": "The Album",
        "author": None,
        "tags": ["live", "1999"],
        "body_text": "x" * 1000,
    }
    text = embed_text_for_item(doc)
    assert text.startswith("song.flac My Song The Artist The Album")
    assert "live" in text and "1999" in text
    # body is capped at 512 chars (plus the leading fields)
    assert text.count("x") == 512


def test_embed_source_from_item_uses_effective_metadata():
    it = _item(metadata_={"artist": "X", "body_text": "hello"}, tags=["t"])
    src = embed_source_from_item(it)
    assert src["artist"] == "X"
    assert src["body_text"] == "hello"
    assert src["tags"] == ["t"]
    assert embed_text_for_item(src)  # non-empty


def test_embed_source_prefers_ocr_when_no_body():
    it = _item(metadata_={"ocr_text": "scanned words"})
    assert embed_source_from_item(it)["body_text"] == "scanned words"


# ------------------------------------------------- has_current_embedding gate
def test_has_current_embedding_matches_fingerprint():
    cfg = EmbedderConfig(model_id="m", dim=DIM)
    fp = embedder_fingerprint(cfg)
    assert has_current_embedding({EMBEDDING_KEY: VEC, FINGERPRINT_KEY: fp}, cfg)
    # drift: wrong fingerprint
    assert not has_current_embedding({EMBEDDING_KEY: VEC, FINGERPRINT_KEY: "nope"}, cfg)
    # missing vector
    assert not has_current_embedding({FINGERPRINT_KEY: fp}, cfg)
    # empty
    assert not has_current_embedding({}, cfg)


def test_strip_embedding_removes_internal_keys():
    meta = {"title": "t", EMBEDDING_KEY: VEC, FINGERPRINT_KEY: "fp"}
    out = strip_embedding(meta)
    assert out == {"title": "t"}
    # non-mutating + no-op when absent
    assert EMBEDDING_KEY in meta
    assert strip_embedding({"a": 1}) == {"a": 1}


# ----------------------------------------------- FIX-7 ONNX engine internals
def test_cls_pool_normalize_exact():
    import numpy as np

    from filearr.embed import _cls_pool_normalize

    # batch=2, seq=3, dim=2. BGE pools the CLS token (index 0); trailing tokens
    # are decoys that must be ignored. Then L2-normalize each row.
    mo = np.array(
        [
            [[3.0, 4.0], [99.0, 99.0], [-1.0, -1.0]],  # CLS [3,4] -> [0.6, 0.8]
            [[0.0, 2.0], [7.0, 7.0], [0.0, 0.0]],  # CLS [0,2] -> [0.0, 1.0]
        ],
        dtype=np.float32,
    )
    out = _cls_pool_normalize(mo)
    assert np.allclose(out, [[0.6, 0.8], [0.0, 1.0]], atol=1e-6)


def test_cls_pool_normalize_2d_passthrough():
    import numpy as np

    from filearr.embed import _cls_pool_normalize

    out = _cls_pool_normalize(np.array([[3.0, 4.0]], dtype=np.float32))
    assert np.allclose(out, [[0.6, 0.8]], atol=1e-6)


def test_cls_pool_normalize_bad_shape():
    import numpy as np
    import pytest as _pytest

    from filearr.embed import _cls_pool_normalize

    with _pytest.raises(ValueError):
        _cls_pool_normalize(np.zeros((2, 3, 4, 5), dtype=np.float32))


def _fake_engine(input_names):
    import numpy as np

    from filearr.embed import _Engine

    class _Tok:
        def encode_batch(self, texts):
            return [
                SimpleNamespace(ids=[1, 2, 3], attention_mask=[1, 1, 1]) for _ in texts
            ]

    class _Sess:
        def __init__(self):
            self.last = None

        def run(self, out_names, feed):
            self.last = feed
            n = feed["input_ids"].shape[0]
            # last_hidden_state (n, seq=3, dim=2); CLS token = [3, 4] every row.
            base = np.array([[[3.0, 4.0], [0.0, 0.0], [0.0, 0.0]]], dtype=np.float32)
            return [np.tile(base, (n, 1, 1))]

    sess = _Sess()
    return _Engine(session=sess, tokenizer=_Tok(), input_names=frozenset(input_names)), sess


def test_embed_batch_feeds_and_pools():
    import numpy as np

    from filearr.embed import _embed_batch

    eng, sess = _fake_engine({"input_ids", "attention_mask", "token_type_ids"})
    res = _embed_batch(eng, ["a", "b"])
    assert np.allclose(res, [[0.6, 0.8], [0.6, 0.8]], atol=1e-6)
    # token_type_ids injected as int64 zeros; attention_mask injected; ids int64
    assert np.all(sess.last["token_type_ids"] == 0)
    assert sess.last["token_type_ids"].dtype == np.int64
    assert sess.last["input_ids"].dtype == np.int64
    assert "attention_mask" in sess.last


def test_embed_batch_omits_undeclared_inputs():
    from filearr.embed import _embed_batch

    eng, sess = _fake_engine({"input_ids"})
    _embed_batch(eng, ["x"])
    assert set(sess.last.keys()) == {"input_ids"}


def test_embed_texts_batches_via_fake_engine(monkeypatch):
    import filearr.embed as embed_mod

    eng, _ = _fake_engine({"input_ids", "attention_mask"})
    monkeypatch.setattr(embed_mod, "_load_engine", lambda cfg: eng)
    monkeypatch.setattr(get_settings(), "embed_batch", 1)  # force >1 chunk
    cfg = EmbedderConfig(model_id="m", dim=2)
    out = embed_texts(["a", "b", "c"], cfg)
    assert [row == pytest.approx([0.6, 0.8], abs=1e-6) for row in out] == [True] * 3
    assert len(out) == 3
    assert all(isinstance(x, float) for x in out[0])  # JSON-serialisable floats
    assert embed_texts([], cfg) == []  # empty short-circuits (no engine load)


# ------------------------------------------------ config repo/threads plumbing
def test_embedder_config_carries_repo_and_file(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "embed_model", "BAAI/bge-small-en-v1.5")
    monkeypatch.setattr(s, "embed_model_repo", "Qdrant/bge-small-en-v1.5-onnx-Q")
    monkeypatch.setattr(s, "embed_model_file", "model_optimized.onnx")
    cfg = s.embedder_config
    assert cfg.repo == "Qdrant/bge-small-en-v1.5-onnx-Q"
    assert cfg.model_file == "model_optimized.onnx"


def test_fingerprint_incorporates_repo_and_file():
    a = EmbedderConfig(model_id="m", dim=DIM, repo="r1", model_file="f")
    b = EmbedderConfig(model_id="m", dim=DIM, repo="r2", model_file="f")
    c = EmbedderConfig(model_id="m", dim=DIM, repo="r1", model_file="g")
    fps = {embedder_fingerprint(x) for x in (a, b, c)}
    assert len(fps) == 3  # repo change AND file change each shift the fingerprint


def test_settings_and_config_defaults_fingerprint_equal(monkeypatch):
    # A settings-derived config (default repo/file) and a hand-built config with
    # only model_id/dim/version must fingerprint identically -- the invariant the
    # build_doc/embed-task drift checks rely on.
    s = get_settings()
    monkeypatch.setattr(s, "embed_model", "m")
    monkeypatch.setattr(s, "embed_dim", DIM)
    monkeypatch.setattr(s, "embed_version", "1")
    hand = EmbedderConfig(model_id="m", dim=DIM, version="1")
    assert embedder_fingerprint(s.embedder_config) == embedder_fingerprint(hand)


# --------------------------------------------------- build_doc vector attach
def _enable_semantic(monkeypatch, enabled=True, dim=DIM):
    s = get_settings()
    monkeypatch.setattr(s, "semantic_enabled", enabled)
    monkeypatch.setattr(s, "embed_model", "m")
    monkeypatch.setattr(s, "embed_dim", dim)
    monkeypatch.setattr(s, "embed_version", "1")
    return EmbedderConfig(model_id="m", dim=dim, version="1")


def test_build_doc_attaches_vector_on_match(monkeypatch):
    cfg = _enable_semantic(monkeypatch)
    fp = embedder_fingerprint(cfg)
    it = _item(metadata_={EMBEDDING_KEY: VEC, FINGERPRINT_KEY: fp})
    doc = search_mod.build_doc(it)
    assert doc["_vectors"] == {"default": VEC}


def test_build_doc_omits_vector_on_drift(monkeypatch):
    _enable_semantic(monkeypatch)
    it = _item(metadata_={EMBEDDING_KEY: VEC, FINGERPRINT_KEY: "old-model-fp"})
    doc = search_mod.build_doc(it)
    assert "_vectors" not in doc


def test_build_doc_omits_vector_when_disabled(monkeypatch):
    cfg = _enable_semantic(monkeypatch, enabled=False)
    fp = embedder_fingerprint(cfg)
    it = _item(metadata_={EMBEDDING_KEY: VEC, FINGERPRINT_KEY: fp})
    doc = search_mod.build_doc(it)
    assert "_vectors" not in doc


# -------------------------------------------------- embedder settings apply
@pytest.mark.asyncio
async def test_apply_embedder_settings_when_enabled(monkeypatch):
    _enable_semantic(monkeypatch)
    index = MagicMock()
    index.get_embedders = AsyncMock(return_value=None)  # none configured yet
    index.update_embedders = AsyncMock(return_value=SimpleNamespace(task_uid=1))
    await search_mod._apply_embedder_settings(index)
    index.update_embedders.assert_awaited_once()
    embedders = index.update_embedders.call_args.args[0]
    emb = embedders.embedders["default"]
    assert emb.source == "userProvided"
    assert emb.dimensions == DIM


@pytest.mark.asyncio
async def test_apply_embedder_settings_idempotent(monkeypatch):
    _enable_semantic(monkeypatch)
    index = MagicMock()
    existing = SimpleNamespace(
        embedders={"default": SimpleNamespace(dimensions=DIM)}
    )
    index.get_embedders = AsyncMock(return_value=existing)
    index.update_embedders = AsyncMock()
    await search_mod._apply_embedder_settings(index)
    index.update_embedders.assert_not_awaited()  # already matches -> no re-push


@pytest.mark.asyncio
async def test_apply_embedder_settings_skipped_when_disabled(monkeypatch):
    _enable_semantic(monkeypatch, enabled=False)
    index = MagicMock()
    index.get_embedders = AsyncMock()
    await search_mod._apply_embedder_settings(index)
    index.get_embedders.assert_not_awaited()


# ----------------------------------------------------------- /search hybrid
class _FakeSimIndex:
    def __init__(self, sink, hits, similar_hits):
        self._sink = sink
        self._hits = hits
        self._similar = similar_hits

    async def search(self, q, **kwargs):
        self._sink["q"] = q
        self._sink.update(kwargs)
        return SimpleNamespace(
            hits=self._hits,
            estimated_total_hits=len(self._hits),
            facet_distribution={},
            facet_stats={},
        )

    async def search_similar_documents(self, item_id, **kwargs):
        self._sink["similar_id"] = item_id
        self._sink.update(kwargs)
        return SimpleNamespace(hits=self._similar)


class _FakeClient:
    def __init__(self, sink, hits, similar_hits):
        self._a = _FakeSimIndex(sink, hits, similar_hits)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def index(self, name):
        return self._a


def _make_app(monkeypatch, *, enabled, hits=None, similar_hits=None):
    from filearr.main import create_app

    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "auth_enabled", False)
    monkeypatch.setattr(s, "semantic_enabled", enabled)
    monkeypatch.setattr(s, "embed_dim", DIM)
    monkeypatch.setattr(s, "embed_model", "m")
    sink: dict = {}
    fake = _FakeClient(sink, hits or [], similar_hits or [])
    monkeypatch.setattr("filearr.api.search.client", lambda: fake)
    monkeypatch.setattr("filearr.api.search.embed_query", lambda q, cfg=None: list(VEC))
    app = create_app()
    return httpx.ASGITransport(app=app), sink


@pytest.mark.asyncio
async def test_search_hybrid_passthrough_when_enabled(monkeypatch):
    transport, sink = _make_app(monkeypatch, enabled=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=cats&semantic=0.6")
    assert r.status_code == 200, r.text
    assert sink["vector"] == list(VEC)
    assert sink["hybrid"].semantic_ratio == 0.6
    assert sink["hybrid"].embedder == "default"


@pytest.mark.asyncio
async def test_search_no_hybrid_when_ratio_zero(monkeypatch):
    transport, sink = _make_app(monkeypatch, enabled=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=cats")
    assert r.status_code == 200, r.text
    assert sink["hybrid"] is None
    assert sink["vector"] is None


@pytest.mark.asyncio
async def test_search_no_hybrid_when_disabled(monkeypatch):
    transport, sink = _make_app(monkeypatch, enabled=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=cats&semantic=0.9")
    assert r.status_code == 200, r.text
    assert sink["hybrid"] is None  # disabled server-side -> keyword only


@pytest.mark.asyncio
async def test_search_semantic_out_of_range_422(monkeypatch):
    transport, _ = _make_app(monkeypatch, enabled=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=x&semantic=2")
    assert r.status_code == 422


def test_semantic_param_in_saved_search_vocab():
    # The saved-search vocabulary is derived from the /search signature; the new
    # param must auto-extend it (no second edit) so a saved bundle can carry it.
    from filearr.api.search import SEARCH_PARAM_NAMES

    assert "semantic" in SEARCH_PARAM_NAMES


# --------------------------------------------------------------- DB fixtures
@pytest.fixture(scope="module")
def pg(module_db):
    return module_db


@pytest.fixture
async def db(pg, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from filearr.models import Base

    uri = pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def _mk_lib(Session):
    from filearr.models import Library

    async with Session() as s:
        lib = Library(name="L", root_path="/data/l")
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(Session, lib, rel, meta=None, status="active"):
    async with Session() as s:
        it = _item(
            library_id=lib,
            rel_path=rel,
            filename=rel,
            path=f"/data/l/{rel}",
            metadata_=meta or {},
            status=ItemStatus(status),
        )
        s.add(it)
        await s.commit()
        return str(it.id)


# ------------------------------------------------------------- embed_item task
@pytest.mark.asyncio
async def test_embed_item_stores_vector_and_syncs(db, monkeypatch):
    import filearr.tasks.embed as embed_task
    import filearr.tasks.index_sync as index_sync

    s = get_settings()
    monkeypatch.setattr(s, "semantic_enabled", True)
    monkeypatch.setattr(s, "embed_model", "m")
    monkeypatch.setattr(s, "embed_dim", DIM)
    monkeypatch.setattr(embed_task, "SessionLocal", db)
    monkeypatch.setattr(embed_task, "embed_texts", lambda texts, cfg: [list(VEC)])
    synced: list = []

    async def _defer(**kw):
        synced.append(kw["item_ids"])

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _defer)

    lib = await _mk_lib(db)
    iid = await _mk_item(db, lib, "a.pdf")

    assert await embed_task.embed_item(iid) is True
    async with db() as sess:
        from sqlalchemy import select

        it = (await sess.execute(select(Item).where(Item.id == iid))).scalar_one()
        assert it.metadata_[EMBEDDING_KEY] == list(VEC)
        assert it.metadata_[FINGERPRINT_KEY] == embedder_fingerprint(s.embedder_config)
    assert synced == [[iid]]


@pytest.mark.asyncio
async def test_embed_item_noop_when_disabled(db, monkeypatch):
    import filearr.tasks.embed as embed_task

    monkeypatch.setattr(get_settings(), "semantic_enabled", False)
    monkeypatch.setattr(embed_task, "SessionLocal", db)

    def _boom(*a, **k):
        raise AssertionError("embed_texts must not run when disabled")

    monkeypatch.setattr(embed_task, "embed_texts", _boom)

    lib = await _mk_lib(db)
    iid = await _mk_item(db, lib, "a.pdf")
    assert await embed_task.embed_item(iid) is False
    async with db() as sess:
        from sqlalchemy import select

        it = (await sess.execute(select(Item).where(Item.id == iid))).scalar_one()
        assert EMBEDDING_KEY not in it.metadata_


# ------------------------------------------------------------ embed_missing
@pytest.mark.asyncio
async def test_embed_missing_caps_and_skips_current(db, monkeypatch):
    import filearr.tasks.embed as embed_task

    s = get_settings()
    monkeypatch.setattr(s, "semantic_enabled", True)
    monkeypatch.setattr(s, "embed_model", "m")
    monkeypatch.setattr(s, "embed_dim", DIM)
    monkeypatch.setattr(s, "embed_backfill_batch", 2)
    monkeypatch.setattr(embed_task, "SessionLocal", db)
    fp = embedder_fingerprint(s.embedder_config)

    recorded: list = []

    class _Deferrer:
        async def defer_async(self, **kw):
            recorded.append(kw["item_id"])

    monkeypatch.setattr(embed_task.proc_app, "configure_task", lambda *a, **k: _Deferrer())

    lib = await _mk_lib(db)
    # 3 missing, 1 drifted, 1 current -> 4 eligible, capped at 2
    for i in range(3):
        await _mk_item(db, lib, f"m{i}.pdf")
    await _mk_item(db, lib, "drift.pdf", meta={EMBEDDING_KEY: VEC, FINGERPRINT_KEY: "old"})
    current = await _mk_item(db, lib, "cur.pdf", meta={EMBEDDING_KEY: VEC, FINGERPRINT_KEY: fp})

    n = await embed_task.embed_missing()
    assert n == 2
    assert len(recorded) == 2
    assert current not in recorded  # already-current item never re-embedded


# ------------------------------------------------------------- semantic stats
@pytest.mark.asyncio
async def test_semantic_snapshot_counts(db, monkeypatch):
    from filearr.embed_stats import semantic_snapshot

    s = get_settings()
    monkeypatch.setattr(s, "semantic_enabled", True)
    monkeypatch.setattr(s, "embed_model", "m")
    monkeypatch.setattr(s, "embed_dim", DIM)
    fp = embedder_fingerprint(s.embedder_config)

    lib = await _mk_lib(db)
    await _mk_item(db, lib, "e.pdf", meta={EMBEDDING_KEY: VEC, FINGERPRINT_KEY: fp})
    await _mk_item(db, lib, "p.pdf")  # pending
    await _mk_item(db, lib, "d.pdf", meta={EMBEDDING_KEY: VEC, FINGERPRINT_KEY: "old"})

    async with db() as sess:
        snap = await semantic_snapshot(sess)
    assert snap["enabled"] is True
    assert snap["model"] == "m"
    assert snap["embedded_count"] == 1
    assert snap["pending"] == 1
    assert snap["fp_mismatches"] == 1


@pytest.mark.asyncio
async def test_semantic_snapshot_disabled(db, monkeypatch):
    from filearr.embed_stats import semantic_snapshot

    monkeypatch.setattr(get_settings(), "semantic_enabled", False)
    lib = await _mk_lib(db)
    await _mk_item(db, lib, "x.pdf")
    async with db() as sess:
        snap = await semantic_snapshot(sess)
    assert snap == {
        "enabled": False,
        "model": get_settings().embed_model,
        "embedded_count": 0,
        "pending": 0,
        "fp_mismatches": 0,
    }


# ----------------------------------------------------------------- /similar
@pytest.fixture
async def sim_api(db, monkeypatch):
    from filearr.db import get_session
    from filearr.main import create_app

    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "auth_enabled", False)

    async def _test_session():
        async with db() as sess:
            yield sess

    app = create_app()
    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, s
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_similar_disabled_409(sim_api, db, monkeypatch):
    client, s = sim_api
    monkeypatch.setattr(s, "semantic_enabled", False)
    lib = await _mk_lib(db)
    iid = await _mk_item(db, lib, "a.pdf")
    r = await client.get(f"/api/v1/items/{iid}/similar")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_similar_unembedded_409(sim_api, db, monkeypatch):
    client, s = sim_api
    monkeypatch.setattr(s, "semantic_enabled", True)
    monkeypatch.setattr(s, "embed_model", "m")
    monkeypatch.setattr(s, "embed_dim", DIM)
    lib = await _mk_lib(db)
    iid = await _mk_item(db, lib, "a.pdf")  # no embedding
    r = await client.get(f"/api/v1/items/{iid}/similar")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_similar_returns_hits_excluding_self(sim_api, db, monkeypatch):
    client, s = sim_api
    monkeypatch.setattr(s, "semantic_enabled", True)
    monkeypatch.setattr(s, "embed_model", "m")
    monkeypatch.setattr(s, "embed_dim", DIM)
    fp = embedder_fingerprint(s.embedder_config)
    lib = await _mk_lib(db)
    iid = await _mk_item(db, lib, "a.pdf", meta={EMBEDDING_KEY: VEC, FINGERPRINT_KEY: fp})

    sink: dict = {}
    other = str(uuid.uuid4())
    hits = [
        {"id": iid, "filename": "a.pdf", "body_text": "self", "_vectors": {"default": VEC}},
        {"id": other, "filename": "b.pdf", "title": "B", "path": "/data/l/b.pdf"},
    ]
    fake = _FakeClient(sink, [], hits)
    monkeypatch.setattr("filearr.search.client", lambda: fake)

    r = await client.get(f"/api/v1/items/{iid}/similar?limit=5")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [h["id"] for h in body["hits"]]
    assert iid not in ids  # self excluded
    assert other in ids
    # index-side machinery stripped from the shaped hit
    assert "body_text" not in body["hits"][0]
    assert "_vectors" not in body["hits"][0]
    # the endpoint asked Meili with the right embedder + sidecar filter
    assert sink["embedder"] == "default"
    assert sink["filter"] == "is_sidecar = false"
