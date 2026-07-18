# Database migrations (Alembic)

Postgres schema is managed by Alembic. The search index is a disposable
projection and never migrated — rebuild it (`rebuild_index`) after schema
changes that affect indexed fields.

## Layout
- `backend/alembic.ini` — config; DB URL comes from `FILEARR_DATABASE_URL`
  (via `filearr.config`), never hardcoded.
- `backend/alembic/` — env.py + versions/. Baseline revision `2bfb3fd1d09a`
  ("baseline schema") captures the v0.1 schema. Requires Postgres 18+
  (uuidv7() defaults; the migration fails fast if absent).
  - `a1c3f7e9b204` ("add items.sidecar_of") — T3 sidecar association: nullable
    self-referencing FK `items.sidecar_of -> items.id` (`ondelete=CASCADE`,
    integrity-first: a parent hard-purge removes orphaned sidecars) + index.
- Procrastinate tables are owned by procrastinate (env.py excludes
  `procrastinate_*` from autogenerate; `scripts/init_db.py` applies its schema).

## Flows
**Fresh deploy** — `python -m scripts.init_db` runs `alembic upgrade head`,
applies the procrastinate schema, and ensures the Meili index.

**Existing pre-alembic DB** — init_db detects tables without
`alembic_version`, stamps the baseline, then upgrades. Run once; idempotent.

**Upgrading a deployment** — pull new code/image, then rerun init_db (or
`alembic upgrade head` from `backend/`). Deploy scripts already rerun init_db.

**Creating a migration after model changes**
```bash
cd backend
uv run alembic revision --autogenerate -m "describe change"
# review the generated file in alembic/versions/ — always
uv run alembic upgrade head
```

## Notes
- `alembic check` (CI candidate, T10): fails if models drifted from migrations.
- Downgrades exist but are best-effort; recycle-bin data loss applies. Back up
  Postgres before downgrading. Meili never needs backup (rebuildable).
