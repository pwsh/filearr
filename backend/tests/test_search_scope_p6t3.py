"""P6-T3 — scoped-search integration: grants seeded across two libraries produce
a Meilisearch scope filter that partitions results exactly like ``rbac.evaluate``
(deny-wins, no-grant-default-deny), and admin is unrestricted.

Exercises the DB glue (``load_principal_grants`` + ``scope_filter_for_principal``)
end to end against the migrated Postgres, then interprets the emitted filter with
Meili array-membership semantics over each item's ``path_scope`` ancestor array —
no live Meilisearch needed. Reuses the filter interpreter + fixtures style from
``test_tenant_tokens_p6t3`` / ``test_rbac_p6t2``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import rbac
from filearr.models import (
    Item,
    Library,
    MediaType,
    PathGrant,
    Principal,
    User,
)
from filearr.search import build_doc
from filearr.search_scope import scope_filter_for_principal
from filearr.tenant_tokens import scope_ancestors
from tests.test_tenant_tokens_p6t3 import meili_eval

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for tbl in (
            "path_grants",
            "principal_group_members",
            "principal_groups",
            "items",
            "libraries",
            "users",
            "principals",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _seed(m):
    async with m() as s:
        lib1 = Library(name="movies", root_path="/data/movies")
        lib2 = Library(name="music", root_path="/data/music")
        s.add_all([lib1, lib2])
        await s.flush()

        def _item(lib, rel):
            return Item(
                library_id=lib.id,
                media_type=MediaType.video,
                path=f"/data/{lib.name}/{rel}",
                rel_path=rel,
                filename=rel.rsplit("/", 1)[-1],
                extension=rel.rsplit(".", 1)[-1],
                size=1,
                mtime=datetime.now(UTC),
                metadata_={},
                user_metadata={},
                external_ids={},
                tags=[],
                path_scope=rbac.path_to_ltree(rel, library_id=lib.id),
            )

        items = {
            "movie": _item(lib1, "movies/a.mkv"),
            "secret": _item(lib1, "movies/private/secret.mkv"),
            "doc": _item(lib1, "docs/x.pdf"),
            "song": _item(lib2, "music/song.mp3"),
        }
        s.add_all(list(items.values()))

        user = Principal(kind="user", global_role="user")
        admin = Principal(kind="user", global_role="admin")
        s.add_all([user, admin])
        await s.flush()
        s.add(User(principal_id=user.id, username="u", auth_provider="local"))
        s.add(User(principal_id=admin.id, username="a", auth_provider="local"))

        # allow the whole movies/ subtree, deny the private/ carve-out.
        s.add(
            PathGrant(
                subject_kind="principal",
                subject_id=user.id,
                library_id=lib1.id,
                scope=rbac.path_to_ltree("movies", library_id=lib1.id),
                action="search_metadata",
                effect="allow",
            )
        )
        s.add(
            PathGrant(
                subject_kind="principal",
                subject_id=user.id,
                library_id=lib1.id,
                scope=rbac.path_to_ltree("movies/private", library_id=lib1.id),
                action="search_metadata",
                effect="deny",
            )
        )
        await s.commit()
        return user.id, admin.id, {k: v.path_scope for k, v in items.items()}


async def test_scoped_search_partitions_results(maker):
    user_id, admin_id, scopes = await _seed(maker)

    async with maker() as s:
        user = await s.get(Principal, user_id)
        expr = await scope_filter_for_principal(s, user)

    assert expr is not None
    visible = {
        name: meili_eval(expr, set(scope_ancestors(ps))) for name, ps in scopes.items()
    }
    # allowed subtree minus the deny carve-out; ungranted lib/paths hidden.
    assert visible == {
        "movie": True,
        "secret": False,  # deny wins
        "doc": False,  # no grant
        "song": False,  # other library
    }


async def test_admin_is_unrestricted(maker):
    _user_id, admin_id, _scopes = await _seed(maker)
    async with maker() as s:
        admin = await s.get(Principal, admin_id)
        expr = await scope_filter_for_principal(s, admin)
    assert expr is None  # inject no filter — sees everything


async def test_projection_roundtrip_scope_array(maker):
    """build_doc projects the ancestor array; a granted item's array intersects
    the compiled filter, an ungranted one does not (round-trip of the projection
    the live query relies on)."""
    user_id, _admin_id, _scopes = await _seed(maker)
    async with maker() as s:
        user = await s.get(Principal, user_id)
        expr = await scope_filter_for_principal(s, user)
        rows = (
            (await s.execute(select(Item))).scalars().all()
        )
    by_name = {r.filename: build_doc(r) for r in rows}
    assert "path_scope" in by_name["a.mkv"]
    assert meili_eval(expr, set(by_name["a.mkv"]["path_scope"])) is True
    assert meili_eval(expr, set(by_name["secret.mkv"]["path_scope"])) is False
