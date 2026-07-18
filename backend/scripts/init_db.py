"""Bootstrap: apply migrations + procrastinate schema + meili index. Idempotent.

Usage: python -m scripts.init_db  (or: uv run python scripts/init_db.py)

- Fresh database        -> alembic upgrade head (creates full schema)
- Pre-alembic database  -> stamped with the baseline revision, then upgraded
- Already migrated      -> upgrade head is a no-op
"""

import asyncio
import sys
from pathlib import Path

# Windows dev hosts: async psycopg cannot run on the default ProactorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))  # make 'filearr' importable

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from filearr.db import engine
from filearr.alerts.ops import seed_system_alert_rules
from filearr.profiles import seed_profiles_to_db
from filearr.search import ensure_index
from filearr.worker import proc_app

BASELINE_REVISION = "2bfb3fd1d09a"


def _alembic_cfg() -> AlembicConfig:
    return AlembicConfig(str(BACKEND_DIR / "alembic.ini"))


def _apply_migrations(pre_alembic: bool) -> None:
    cfg = _alembic_cfg()
    if pre_alembic:
        print("filearr: existing pre-alembic schema found, stamping baseline")
        alembic_command.stamp(cfg, BASELINE_REVISION)
    alembic_command.upgrade(cfg, "head")


async def main() -> None:
    async with engine.begin() as conn:
        # sanity: Postgres 18 uuidv7() must exist
        await conn.execute(text("SELECT uuidv7()"))
        has_alembic = (
            await conn.execute(text("SELECT to_regclass('alembic_version')"))
        ).scalar() is not None
        has_items = (
            await conn.execute(text("SELECT to_regclass('items')"))
        ).scalar() is not None
    await asyncio.to_thread(_apply_migrations, pre_alembic=(has_items and not has_alembic))

    # procrastinate's apply_schema is NOT idempotent — skip if already applied
    async with engine.begin() as conn:
        proc_exists = (
            await conn.execute(text("SELECT to_regclass('procrastinate_jobs')"))
        ).scalar()
    if proc_exists is None:
        async with proc_app.open_async():
            await proc_app.schema_manager.apply_schema_async()
    else:
        print("filearr: procrastinate schema already present, skipping")
    # P4-T1: seed the code-shipped metadata profiles (idempotent).
    await seed_profiles_to_db()
    # P8-T9/T10: seed the disabled is_system operational alert rules (idempotent).
    await seed_system_alert_rules()
    await ensure_index()
    print("filearr: database, job queue and search index initialised")


if __name__ == "__main__":
    asyncio.run(main())
