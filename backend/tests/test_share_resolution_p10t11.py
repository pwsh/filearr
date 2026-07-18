"""P10-T11/T12 — the item→network-share resolver precedence
(:func:`filearr.share_resolution.resolve_item_share`) and its projection onto the
single-item GET payload (``share_url`` / ``share_source``).

Pure precedence matrix (no DB) + an end-to-end item-payload test against the
migrated Postgres asserting the FROZEN cross-agent contract fields are computed
via the resolver: agent hint > agent_share_maps mapping > library share_prefix >
none (no fabricated location).
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from filearr.models import Agent, AgentShareMap, Item, Library, MediaType
from filearr.share_resolution import resolve_item_share
from filearr.transfers import ShareMapping

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure precedence matrix (no DB)                                               #
# --------------------------------------------------------------------------- #
_MAP = [ShareMapping(local_prefix="/srv/media", share_prefix="smb://nas/map", agent_id="A")]


def test_hint_wins_over_mapping_and_library():
    url, src = resolve_item_share(
        share_hint={"share_url": "smb://nas/hint/x.mkv"},
        source_agent_id="A",
        agent_mappings=_MAP,
        library_share_prefix="smb://nas/lib",
        library_root_path="/srv/media",
        item_path="/srv/media/x.mkv",
        rel_path="x.mkv",
    )
    assert (url, src) == ("smb://nas/hint/x.mkv", "agent_hint")


def test_mapping_wins_over_library_when_no_hint():
    url, src = resolve_item_share(
        share_hint=None,
        source_agent_id="A",
        agent_mappings=_MAP,
        library_share_prefix="smb://nas/lib",
        library_root_path="/srv/media",
        item_path="x.mkv",  # agent item: path == rel_path (apply_batch)
        rel_path="movies/x.mkv",
    )
    # agent-local abspath = /srv/media/movies/x.mkv -> mapping
    assert (url, src) == ("smb://nas/map/movies/x.mkv", "mapping")


def test_library_used_when_no_hint_no_mapping():
    url, src = resolve_item_share(
        share_hint=None,
        source_agent_id="A",
        agent_mappings=[],  # no covering agent mapping
        library_share_prefix="smb://nas/lib",
        library_root_path="/srv/media",
        item_path="/srv/media/x.mkv",
        rel_path="sub/x.mkv",
    )
    assert (url, src) == ("smb://nas/lib/sub/x.mkv", "library")


def test_none_when_nothing_applies():
    assert resolve_item_share(
        share_hint=None,
        source_agent_id=None,
        agent_mappings=[],
        library_share_prefix=None,
        library_root_path="/srv/media",
        item_path="/srv/media/x.mkv",
        rel_path="x.mkv",
    ) == (None, None)


def test_agent_scoped_mapping_does_not_leak_to_other_agent():
    # Item hosted by agent B; the only mapping is scoped to agent A -> no mapping,
    # falls through to library (here None) -> no location.
    url, src = resolve_item_share(
        share_hint=None,
        source_agent_id="B",
        agent_mappings=_MAP,  # agent_id="A"
        library_share_prefix=None,
        library_root_path="/srv/media",
        item_path="x.mkv",
        rel_path="x.mkv",
    )
    assert (url, src) == (None, None)


def test_empty_hint_dict_is_ignored():
    # A hint object without a usable share_url must not short-circuit precedence.
    url, src = resolve_item_share(
        share_hint={"host": "nas"},  # no share_url
        source_agent_id="A",
        agent_mappings=_MAP,
        library_share_prefix=None,
        library_root_path="/srv/media",
        item_path="x.mkv",
        rel_path="x.mkv",
    )
    assert (url, src) == ("smb://nas/map/x.mkv", "mapping")


# --------------------------------------------------------------------------- #
# Item-payload integration (migrated Postgres)                                 #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def client_and_maker(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_share_maps"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_agent_item(maker, *, share_hint=None) -> tuple[str, str]:
    """An agent-owned library + one agent-hosted item. Returns (agent_id, item_id)."""
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux")
        s.add(agent)
        await s.flush()
        lib = Library(
            name="agent-lib",
            root_path="/srv/media",
            source_agent_id=agent.id,
            agent_library_ref="/srv/media",
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.video,
            path="movies/x.mp4",  # agent item: apply_batch sets path == rel_path
            rel_path="movies/x.mp4",
            filename="x.mp4",
            extension=".mp4",
            size=1,
            mtime=datetime.now(UTC),
            source_agent_id=agent.id,
            share_hint=share_hint,
        )
        s.add(item)
        await s.commit()
        return str(agent.id), str(item.id)


async def test_payload_uses_agent_hint(client_and_maker):
    c, maker = client_and_maker
    _agent, item_id = await _seed_agent_item(
        maker, share_hint={"share_url": "smb://nas/media/movies/x.mp4", "source": "agent"}
    )
    body = (await c.get(f"/api/v1/items/{item_id}")).json()
    assert body["share_url"] == "smb://nas/media/movies/x.mp4"
    assert body["share_source"] == "agent_hint"


async def test_payload_uses_mapping_when_no_hint(client_and_maker):
    c, maker = client_and_maker
    agent_id, item_id = await _seed_agent_item(maker)  # no hint
    async with maker() as s:
        s.add(
            AgentShareMap(
                agent_id=agent_id,
                local_prefix="/srv/media",
                share_prefix="smb://nas/mapped",
            )
        )
        await s.commit()
    body = (await c.get(f"/api/v1/items/{item_id}")).json()
    assert body["share_url"] == "smb://nas/mapped/movies/x.mp4"
    assert body["share_source"] == "mapping"


async def test_payload_none_when_no_hint_no_mapping(client_and_maker):
    c, maker = client_and_maker
    _agent, item_id = await _seed_agent_item(maker)  # no hint, no mapping, no lib prefix
    body = (await c.get(f"/api/v1/items/{item_id}")).json()
    assert body["share_url"] is None
    assert body["share_source"] is None


async def test_payload_agent_mapping_does_not_leak_across_agents(client_and_maker):
    c, maker = client_and_maker
    _agent, item_id = await _seed_agent_item(maker)  # item hosted by "nas" agent
    # A mapping scoped to a DIFFERENT agent must not resolve this item.
    async with maker() as s:
        other = Agent(name="other", hostname="other", platform="linux")
        s.add(other)
        await s.flush()
        s.add(
            AgentShareMap(
                agent_id=other.id,
                local_prefix="/srv/media",
                share_prefix="smb://nas/other",
            )
        )
        await s.commit()
    body = (await c.get(f"/api/v1/items/{item_id}")).json()
    assert body["share_url"] is None and body["share_source"] is None
