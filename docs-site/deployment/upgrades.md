# Upgrades & migrations

Filearr upgrades are designed to be low-drama: the database bootstrap is
idempotent, the search index is disposable and rebuildable, and migrations are
managed by Alembic with a stamping path for databases that predate it.

## The golden rule: back up Postgres first

Before any upgrade, take a Postgres dump. It is the only irreplaceable store.

```bash
cd /opt/filearr
docker compose exec -T postgres pg_dump -U filearr -Fc filearr > filearr-$(date -u +%Y%m%dT%H%M%SZ).dump
```

See [Backup & restore](../operations.md#backup-and-restore) for the full
procedure and how to verify a dump.

## Docker Compose upgrade

```bash
cd /opt/filearr
git pull                                        # or fetch the new source
docker compose build --pull                     # rebuild the app/worker image
docker compose run --rm app python scripts/init_db.py    # idempotent migrate
docker compose up -d
```

## Proxmox LXC upgrade

Just re-run the deploy script — it pushes the new source, rebuilds, and runs the
idempotent bootstrap for you:

```bash
bash proxmox/deploy-proxmox.sh
```

The script verifies (via a build stamp) that the running image was actually built
from the source it just pushed, and runs a functional smoke test. If the stamp
mismatches, retry with a forced clean build: `FORCE_REBUILD=1 bash proxmox/deploy-proxmox.sh`.

## Migration behavior (Alembic)

The bootstrap, `scripts/init_db.py`, is **idempotent** and handles every state:

- **Brand-new database** — creates the schema, stamps the Alembic baseline, runs
  migrations to head.
- **Pre-Alembic database** — detects that the tables exist but there is no
  `alembic_version` table, **stamps the baseline**, then upgrades to head.
- **Up-to-date database** — a no-op beyond confirming head.

It then applies the Procrastinate job-queue schema (guarded — Procrastinate's own
schema step is not idempotent) and ensures the Meilisearch index exists.

Inspect state directly if you need to:

```bash
docker compose exec app alembic current   # revision the DB is at
docker compose exec app alembic heads      # revision the code expects
```

!!! note "Postgres 18 is required"
    The Alembic baseline uses `uuidv7()` server defaults, so Postgres 18+ is
    required and the migration fails fast on older Postgres.

## Rebuild the search index after field changes

Meilisearch is a disposable projection. Any upgrade that changes **indexed
fields** requires a rebuild so the projection matches the new schema:

```bash
curl -X POST http://localhost:8484/api/v1/system/rebuild-index
```

The rebuild uses a shadow-index swap, so live search stays up during it. A
notable case: enabling **path-scoped RBAC search** adds a scope attribute to the
index; until the rebuild completes, scoped non-admin users fail *closed* to empty
results (admins and API keys are unaffected).

## Downgrades

Alembic downgrades exist but are **best-effort** and can lose recycle-bin data.
Always take a fresh dump before downgrading, and rebuild the search index
afterward.
