# Development

How to work on Filearr locally: the repo layout, dev commands, tests, and
contribution basics under the AGPL.

## Repository layout

```text
backend/filearr/
  main.py          FastAPI app factory (SPA static mount, /api/v1)
  config.py        pydantic-settings — every FILEARR_* env var
  models.py        SQLAlchemy 2.0 models (libraries, items, scans, agents, ...)
  media_types.py   extension -> media-type map
  security.py      Bearer API keys + scopes (read/write/admin)
  rbac.py          path-scoped RBAC (ltree grants)
  search.py        Meilisearch projection (build_doc, ensure_index, rebuild)
  querydsl.py      the saved-query / filter-builder grammar
  agentsync.py     the agent replication wire contract (central half)
  worker.py        Procrastinate app + periodic jobs (purge, reconcile, reap)
  api/             search, items, libraries, scans (SSE), system, agents, ...
  tasks/           scan (walk/diff/tombstone), extract (per-type), index_sync, thumbs, ...
backend/alembic/   migrations (baseline + revisions)
backend/tests/     pytest suite
frontend/src/      Svelte 5 SPA (App.svelte, lib/AdminPage.svelte, lib/api.ts)
agent/             the Go agent (cmd/filearr-agent, cmd/filearr-release, internal/*)
docker-compose.yml, Dockerfile, unraid/, proxmox/, scripts/
```

## Dev commands

Backend (Python 3.13 via [uv](https://docs.astral.sh/uv/)):

```bash
cd backend && uv sync
uv run uvicorn filearr.main:app --reload          # API on :8000
uv run procrastinate --app=filearr.worker.proc_app worker   # a worker
```

Frontend (Node ≥ 24):

```bash
cd frontend && npm install
npm run dev            # Vite dev server, proxies /api to :8000
npm run build          # production build (served from FastAPI static)
npm run check          # svelte-check / typecheck
```

Agent (Go ≥ 1.26, no cgo):

```bash
cd agent
go build ./cmd/filearr-agent
go test ./...
# cross-compile:
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build ./cmd/filearr-agent
```

Full stack via Docker:

```bash
docker compose up -d
docker compose run --rm app python scripts/init_db.py   # idempotent bootstrap
```

The bootstrap is idempotent. API docs are at `/api/docs`. For a quick look with
auth off, set `FILEARR_AUTH_ENABLED=false`.

## Tests

```bash
cd backend && uv run pytest          # backend suite (pytest-asyncio)
cd agent && go test ./...            # agent suite (incl. an in-process step-ca authority)
cd frontend && npm run check         # frontend typecheck
```

The backend suite prefers an external Postgres 18 service — point
`FILEARR_TEST_DATABASE_URL` at it (an embedded Postgres fallback is used only on
older Python). Because some Postgres **extension types** (e.g. `ltree`) are only
present on a real Postgres, run tests against a real Postgres 18 to catch the
extension-type behaviors that a bare test database would miss.

Lint/format uses ruff:

```bash
cd backend && uv run ruff check .
```

## Contribution basics (AGPL)

Filearr is licensed under **AGPL-3.0-or-later**. Contributions are accepted under
the same license with a **DCO sign-off** — add `Signed-off-by:` to your commits
(`git commit -s`).

Keep the [architecture invariants](../index.md#architecture-invariants) intact —
they are load-bearing:

1. The search index is disposable (rebuildable from Postgres).
2. Extracted `metadata` and `user_metadata` are separate columns; scans/extractors
   write only the former, API/UI edits only the latter.
3. Item identity is `(library, relative path)`.
4. Scans never hard-delete (tombstone + recycle-bin purge).
5. Defer extract jobs only **after** the batch commit (workers race uncommitted
   rows otherwise).
6. Media mounts are read-only.
7. A crashed scan must mark its scan run `failed` — never leave it `running`.

If you run a modified Filearr for others over a network, AGPL §13 requires you to
offer them the Corresponding Source — set `FILEARR_SOURCE_URL` to your fork.
