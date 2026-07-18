"""Regression: ltree-typed columns must not get a ``::VARCHAR`` bind cast.

``items.path_scope`` and ``path_grants.scope`` are real ``ltree`` columns in
production (migration ``d7e4c1b9f3a2``; contrib is present in postgres:18) but
plain ``text`` in the pgserver sandbox, so the suite historically never
exercised the ltree-typed column. The psycopg dialect renders ``::VARCHAR``
bind casts for ``Text``-typed parameters, and Postgres has no varchar→ltree
assignment cast — so with the columns mapped as ``Text``, EVERY item insert
(agent replication apply, the central scanner's new-file path, reconcile
upserts, the RBAC backfill) and every path-grant insert failed with 42804 on a
real deployment while passing everywhere in this suite. Found live on the first
agent ``push`` (2026-07-18).

Fix: :class:`filearr.models.LtreeCompat` (a ``UserDefinedType``) renders the
bare parameter, which binds as ``unknown`` and coerces server-side to the
column's actual type. These tests pin both halves:

* compiled-SQL: no bind cast on ``path_scope`` / ``scope`` (and the cast IS
  still rendered for ordinary Text columns, so the assertion can't rot into a
  tautology);
* live round-trip through an extension-typed column the driver doesn't know —
  the real ``ltree`` when the server offers it (CI postgres:18), else pgvector
  (bundled with pgserver) as an equivalent unknown-OID stand-in — proving the
  Text mapping fails with 42804 and the LtreeCompat mapping round-trips.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Text, insert, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from filearr.models import Item, LtreeCompat, PathGrant


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Compiled-SQL contract                                                        #
# --------------------------------------------------------------------------- #
def test_item_path_scope_binds_without_cast():
    d = postgresql.psycopg.dialect()
    ins = str(insert(Item).values(path_scope="lib_a.b", rel_path="x").compile(dialect=d))
    upd = str(
        update(Item).where(Item.rel_path == "x").values(path_scope="lib_a.b").compile(dialect=d)
    )
    assert "%(path_scope)s::" not in ins
    assert "%(path_scope)s::" not in upd
    # Guard the guard: ordinary Text columns still carry the dialect bind cast,
    # so an SQLAlchemy behaviour change can't silently void these assertions.
    assert "%(rel_path)s::VARCHAR" in ins


def test_path_grant_scope_binds_without_cast():
    d = postgresql.psycopg.dialect()
    ins = str(insert(PathGrant).values(scope="lib_a.b", action="download").compile(dialect=d))
    assert "%(scope)s::" not in ins
    assert "%(action)s::VARCHAR" in ins


def test_ltree_compat_ddl_falls_back_to_text():
    # create_all bootstrap stays extension-free; only the migration types ltree.
    assert LtreeCompat().get_col_spec() == "TEXT"


# --------------------------------------------------------------------------- #
# Live round-trip through an extension-typed column                            #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def ext_column(pg_uri):
    """``(engine, column_ddl_type, sample_value)`` for an extension type psycopg
    has no adapter for — real ``ltree`` when available, else pgvector."""
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        names = {
            r[0]
            for r in await conn.execute(
                text(
                    "SELECT name FROM pg_available_extensions "
                    "WHERE name IN ('ltree', 'vector')"
                )
            )
        }
    if "ltree" in names:
        ext, coltype, value = "ltree", "ltree", "lib_a.sub.file_2emkv"
    elif "vector" in names:
        ext, coltype, value = "vector", "vector(2)", "[1,2]"
    else:  # pragma: no cover - neither contrib nor pgvector present
        await engine.dispose()
        pytest.skip("no extension-typed column available on this Postgres")
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
        await conn.execute(text("DROP TABLE IF EXISTS ltree_bind_probe"))
        await conn.execute(
            text(f"CREATE TABLE ltree_bind_probe (id int primary key, v {coltype})")
        )
    yield engine, value
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS ltree_bind_probe"))
    await engine.dispose()


def _probe_table(scope_type) -> Table:
    return Table(
        "ltree_bind_probe",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("v", scope_type),
    )


async def test_text_mapping_fails_on_extension_column(ext_column):
    """The pre-fix mapping: Text-typed bind → ::VARCHAR cast → 42804."""
    engine, value = ext_column
    async with engine.begin() as conn:
        with pytest.raises(ProgrammingError) as exc:
            await conn.execute(insert(_probe_table(Text())).values(id=1, v=value))
    assert "42804" in str(exc.value) or "is of type" in str(exc.value)


async def test_ltree_compat_round_trips_on_extension_column(ext_column):
    engine, value = ext_column
    t = _probe_table(LtreeCompat())
    async with engine.begin() as conn:
        await conn.execute(insert(t).values(id=1, v=value))
        await conn.execute(update(t).where(t.c.id == 1).values(v=value))
        got = (await conn.execute(select(t.c.v).where(t.c.id == 1))).scalar_one()
    assert got == value
    assert isinstance(got, str)
