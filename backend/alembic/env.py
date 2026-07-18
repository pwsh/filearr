"""Alembic environment — sync engine, URL from filearr settings (FILEARR_DATABASE_URL)."""

from alembic import context
from sqlalchemy import create_engine, pool

from filearr.config import get_settings
from filearr.models import Base

target_metadata = Base.metadata

# Tables owned by other tools (never create/drop/alter via our migrations)
_FOREIGN_PREFIXES = ("procrastinate_",)


def include_name(name, type_, parent_names):
    if type_ == "table":
        if name is not None and name.startswith(_FOREIGN_PREFIXES):
            return False
        # never emit drops for reflected tables we don't model
        return name in target_metadata.tables
    return True


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_name=include_name,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
