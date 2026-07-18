"""P2-T5 — presets/extension-groups catalogue endpoint + library validation.

Drives the real FastAPI app over an in-process ASGI transport (mirrors
test_libraries_api_t7): the read-only catalogue, and create/PATCH validation of
``enabled_presets`` (incl. the ``-name`` opt-out sentinel discipline) and
``enabled_extension_groups`` (unknown -> 422, round-trip persistence).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def client(pg_uri, monkeypatch):
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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


LIBS = "/api/v1/libraries"
PRESETS = "/api/v1/presets"


# --- catalogue endpoint ----------------------------------------------------


async def test_list_presets_shape(client):
    r = await client.get(PRESETS)
    assert r.status_code == 200, r.text
    body = r.json()
    names = {p["name"] for p in body["presets"]}
    assert {"system_files", "hidden_dotfiles", "os_metadata"} <= names
    hd = next(p for p in body["presets"] if p["name"] == "hidden_dotfiles")
    assert hd["default_enabled"] is True
    assert hd["patterns"] == [".*"]
    assert hd["caveat"]
    groups = {g["name"]: g for g in body["extension_groups"]}
    assert groups["office_docs"]["media_type"] == "document"
    assert set(groups["office_docs"]["extensions"]) == {"doc", "docx", "odt", "rtf"}


async def test_get_single_preset_and_404(client):
    r = await client.get(f"{PRESETS}/node_modules_build")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "node_modules_build"
    assert (await client.get(f"{PRESETS}/does_not_exist")).status_code == 404


# --- create validation + round-trip ---------------------------------------


async def test_create_echoes_presets_and_groups(client):
    r = await client.post(
        LIBS,
        json={
            "name": "L1", "root_path": "/d1",
            "enabled_presets": ["node_modules_build", "-hidden_dotfiles"],
            "enabled_extension_groups": ["office_docs", "ebooks"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["enabled_presets"] == ["node_modules_build", "-hidden_dotfiles"]
    assert body["enabled_extension_groups"] == ["office_docs", "ebooks"]


async def test_create_rejects_unknown_preset(client):
    r = await client.post(
        LIBS, json={"name": "Lb", "root_path": "/d", "enabled_presets": ["bogus"]}
    )
    assert r.status_code == 422


async def test_create_rejects_unknown_group(client):
    r = await client.post(
        LIBS,
        json={"name": "Lc", "root_path": "/d", "enabled_extension_groups": ["nope"]},
    )
    assert r.status_code == 422


async def test_negation_only_valid_for_default_on_bundle(client):
    # -hidden_dotfiles is fine (default-on); -node_modules_build is nonsensical.
    ok = await client.post(
        LIBS, json={"name": "Lok", "root_path": "/d", "enabled_presets": ["-hidden_dotfiles"]}
    )
    assert ok.status_code == 201, ok.text
    bad = await client.post(
        LIBS,
        json={"name": "Lbad", "root_path": "/d", "enabled_presets": ["-node_modules_build"]},
    )
    assert bad.status_code == 422
    worse = await client.post(
        LIBS, json={"name": "Lw", "root_path": "/d", "enabled_presets": ["-ghost"]}
    )
    assert worse.status_code == 422


# --- PATCH ------------------------------------------------------------------


async def test_patch_updates_and_clears(client):
    created = (await client.post(LIBS, json={"name": "L2", "root_path": "/d2"})).json()
    lib_id = created["id"]
    r = await client.patch(
        f"{LIBS}/{lib_id}",
        json={"enabled_presets": ["caches_temp"], "enabled_extension_groups": ["lossless_audio"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled_presets"] == ["caches_temp"]
    assert r.json()["enabled_extension_groups"] == ["lossless_audio"]
    # [] clears (model_fields_set discipline).
    cleared = await client.patch(f"{LIBS}/{lib_id}", json={"enabled_extension_groups": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["enabled_extension_groups"] == []


async def test_patch_rejects_unknown(client):
    created = (await client.post(LIBS, json={"name": "L3", "root_path": "/d3"})).json()
    r = await client.patch(f"{LIBS}/{created['id']}", json={"enabled_presets": ["ghost"]})
    assert r.status_code == 422
