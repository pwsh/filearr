"""UI-T4 — server-side folder browser (GET /api/v1/fs/browse).

Security-sensitive: ``path`` is treated as hostile. These tests cover the frozen
response contract, root listing, nested listing, the 500-entry cap, and — most
importantly — that traversal (``..``) and symlinks pointing OUT of the allowlist
cannot escape (422 / silently skipped).
"""

from __future__ import annotations

import os

import httpx
import pytest

BASE = "/api/v1/fs/browse"


@pytest.fixture
def browse_root(tmp_path):
    """A browse root with a nested dir, a file (must be excluded), and a symlink
    pointing OUTSIDE the root (must never be followed)."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "movies").mkdir()
    (root / "music").mkdir()
    (root / "movies" / "kids").mkdir()
    (root / "readme.txt").write_text("not a dir")

    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "loot").mkdir()
    # symlink inside the root that resolves outside the allowlist
    os.symlink(outside, root / "escape")
    return {"root": root, "outside": outside}


@pytest.fixture
async def client(browse_root, monkeypatch):
    from filearr.config import get_settings
    from filearr.main import create_app

    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "auth_enabled", False)
    monkeypatch.setattr(s, "browse_roots", [str(browse_root["root"])])

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.root = browse_root["root"]  # type: ignore[attr-defined]
        c.outside = browse_root["outside"]  # type: ignore[attr-defined]
        yield c
    get_settings.cache_clear()


async def test_empty_path_lists_roots(client):
    r = await client.get(BASE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == ""
    assert body["parent"] is None
    root_real = os.path.realpath(str(client.root))
    assert body["roots"] == [root_real]
    assert body["dirs"] == [{"name": "data", "path": root_real}]


async def test_nested_listing_dirs_only(client):
    root_real = os.path.realpath(str(client.root))
    r = await client.get(BASE, params={"path": root_real})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == root_real
    names = [d["name"] for d in body["dirs"]]
    # files excluded, symlink-out excluded, real subdirs present + sorted
    assert names == ["movies", "music"]
    assert "readme.txt" not in names
    assert "escape" not in names  # symlink resolving outside is skipped
    # parent of a root is "" (back to the roots list)
    assert body["parent"] == ""


async def test_descend_into_subdir_parent_is_root(client):
    root_real = os.path.realpath(str(client.root))
    r = await client.get(BASE, params={"path": os.path.join(root_real, "movies")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["name"] for d in body["dirs"]] == ["kids"]
    assert body["parent"] == root_real


async def test_traversal_outside_root_is_422(client):
    root_real = os.path.realpath(str(client.root))
    r = await client.get(BASE, params={"path": os.path.join(root_real, "..", "..")})
    assert r.status_code == 422


async def test_absolute_outside_path_is_422(client):
    r = await client.get(BASE, params={"path": "/etc"})
    assert r.status_code == 422


async def test_symlink_out_as_path_is_422(client):
    root_real = os.path.realpath(str(client.root))
    r = await client.get(BASE, params={"path": os.path.join(root_real, "escape")})
    # realpath resolves the symlink OUT of the allowlist -> rejected
    assert r.status_code == 422


async def test_symlink_out_target_directly_is_422(client):
    r = await client.get(BASE, params={"path": str(client.outside)})
    assert r.status_code == 422


async def test_nonexistent_within_root_is_404(client):
    root_real = os.path.realpath(str(client.root))
    r = await client.get(BASE, params={"path": os.path.join(root_real, "nope")})
    assert r.status_code == 404


async def test_entry_cap_at_500(client):
    root_real = os.path.realpath(str(client.root))
    big = os.path.join(root_real, "music")
    for i in range(600):
        os.mkdir(os.path.join(big, f"d{i:04d}"))
    r = await client.get(BASE, params={"path": big})
    assert r.status_code == 200
    dirs = r.json()["dirs"]
    assert len(dirs) == 500
    # name-sorted + truncated -> the lexicographically-first 500
    names = [d["name"] for d in dirs]
    assert names == sorted(names)
    assert names[0] == "d0000"
    assert names[-1] == "d0499"
