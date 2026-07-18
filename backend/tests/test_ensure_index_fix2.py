"""FIX-2 — ensure_index must enforce primaryKey on an already-existing index.

get_or_create_index only sets the primary key when it CREATES the index; an
index that already exists (e.g. an implicit one from a stray document push after
a volume wipe) keeps whatever primaryKey it had — possibly null, possibly wrong.
ensure_index now patches a null primaryKey to "id" and refuses loudly on a
mismatch. upsert_docs also passes primary_key="id" explicitly.

Pure unit tests: the Meili client is faked, _apply_settings is stubbed to a
no-op so only the primary-key logic is under test.
"""

from __future__ import annotations

import pytest

from filearr import search


class _FakeIndex:
    def __init__(self, primary_key):
        self.primary_key = primary_key
        self.updated_to = None

    async def update(self, primary_key):
        self.updated_to = primary_key
        self.primary_key = primary_key
        return self


class _FakeClient:
    def __init__(self, index):
        self._index = index
        self.requested = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_or_create_index(self, uid, primary_key=None):
        self.requested = (uid, primary_key)
        return self._index


@pytest.fixture(autouse=True)
def _stub_apply_settings(monkeypatch):
    async def _noop(index):
        return []

    monkeypatch.setattr(search, "_apply_settings", _noop)


async def test_null_primary_key_is_patched_to_id(monkeypatch):
    idx = _FakeIndex(None)
    monkeypatch.setattr(search, "client", lambda: _FakeClient(idx))
    await search.ensure_index()
    assert idx.updated_to == "id"
    assert idx.primary_key == "id"


async def test_correct_primary_key_is_left_alone(monkeypatch):
    idx = _FakeIndex("id")
    monkeypatch.setattr(search, "client", lambda: _FakeClient(idx))
    await search.ensure_index()
    assert idx.updated_to is None


async def test_wrong_primary_key_raises_loudly(monkeypatch):
    idx = _FakeIndex("uid")
    monkeypatch.setattr(search, "client", lambda: _FakeClient(idx))
    with pytest.raises(RuntimeError, match="primary_key"):
        await search.ensure_index()
    assert idx.updated_to is None  # never silently patched


class _CapturingIndex:
    def __init__(self):
        self.calls = []

    async def update_documents(self, documents, primary_key=None):
        self.calls.append((documents, primary_key))


class _CapturingClient:
    def __init__(self, index):
        self._index = index

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def index(self, name):
        return self._index


async def test_upsert_docs_passes_primary_key_id(monkeypatch):
    idx = _CapturingIndex()
    monkeypatch.setattr(search, "client", lambda: _CapturingClient(idx))
    await search.upsert_docs([{"id": "1", "title": "x"}])
    assert idx.calls == [([{"id": "1", "title": "x"}], "id")]
