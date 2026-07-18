# Docker Compose

The compose stack is the canonical Filearr deployment. This page walks through
the compose file, the one-time bootstrap, the environment variables you must set,
and the two gotchas that bite people.

## Quick start

```bash
cp .env.example .env          # then edit the secrets (see below)
docker compose up -d postgres meilisearch
docker compose run --rm app python scripts/init_db.py    # idempotent bootstrap
docker compose up -d
```

The web UI is then at `http://localhost:8484` and the interactive API docs at
`http://localhost:8484/api/docs`.

## The compose file, service by service

- **`app`** — the FastAPI application (REST API + the built SPA), listening on
  `8000` in-container and published as `8484` on the host. It mounts your media
  **read-only** at `/data/media` and a `./config` volume for caches/thumbnails.
- **`worker`** — the Procrastinate worker. Runs the scan, extract, index and
  maintenance jobs. Concurrency and which queues it serves are env-driven:
    - `FILEARR_WORKER_CONCURRENCY` — parallel jobs per worker (default 4).
    - `FILEARR_WORKER_QUEUES` — comma-separated queues, or empty for all.
    - Scale out extraction with `docker compose up -d --scale worker=3`, or pin a
      dedicated worker to the `extract` queue (see the comments in
      `docker-compose.yml`). Extract jobs run at a lower priority than scan
      control, so a freshly triggered scan is never stuck behind a big extract
      backlog.
- **`postgres`** — `postgres:18.4`. The source of truth and the job queue.
- **`meilisearch`** — `getmeili/meilisearch:v1.49.0`, analytics disabled, master
  key from `.env`. Its LMDB store lives on a **local** named volume, never on the
  media mount.
- **`caddy`** *(optional TLS front)* and **`step-ca`** *(optional `agents`
  profile)* — see [Proxmox](proxmox.md) and [Distributed agents](../agents.md).
- **`watcher`** *(optional)* — filesystem watch mode. Watch is **local-disk
  only**; inotify is unreliable over SMB/NFS, so scheduled polling scans are the
  default for network mounts.

### The media bind uses `rslave` propagation

The media bind is a long-form bind with `bind.propagation: rslave`. This is
deliberate: if the underlying FUSE/SMB mount is remounted on the host (for
example after an rclone restart), a running container sees the **new** mount
instead of a dead endpoint. Without it you get `OSError: EIO` after the mount
flaps. Keep it.

## Environment variables

Start from `.env.example` and change at least these:

```bash
POSTGRES_PASSWORD=change-me-too
MEILI_MASTER_KEY=change-me

FILEARR_DATABASE_URL=postgresql+psycopg://filearr:change-me-too@postgres:5432/filearr
FILEARR_PROCRASTINATE_DSN=postgresql://filearr:change-me-too@postgres:5432/filearr
FILEARR_MEILI_URL=http://meilisearch:7700
FILEARR_MEILI_MASTER_KEY=change-me
FILEARR_AUTH_ENABLED=true

# Host path to your media, mounted read-only at /data/media
MEDIA_PATH=/mnt/user/data/media
```

A few more you will likely want:

- `FILEARR_SECRET_KEY` — the envelope key used to encrypt alert-channel secrets
  (AES-GCM). **Required** to create alert channels; when unset the alert-channels
  API returns 503 rather than storing plaintext. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`. It is **never
  rotated automatically** (rotating orphans already-encrypted secrets).
- `FILEARR_SOURCE_URL` — the AGPL section 13 "Source" link shown in the UI footer.
  Point it at *your* source if you run a fork.
- `FILEARR_AUTH_ENABLED=false` — turns authentication off (handy for a first
  look; do not run open on an untrusted network).

The full, grouped list is in the [Configuration reference](../reference/configuration.md).

!!! danger "Secrets never belong in a committed file"
    `FILEARR_SECRET_KEY`, `MEILI_MASTER_KEY`, `POSTGRES_PASSWORD`, and (for the
    agent CA) `FILEARR_CA_PROVISIONER_JWK` / `FILEARR_PROXY_SHARED_SECRET` are
    secrets. Keep them in `.env` (the compose `env_file`), never in the committed
    compose file or in a deploy config that gets checked in.

## The bootstrap: `init_db.py`

```bash
docker compose run --rm app python scripts/init_db.py
```

This is **idempotent** and safe to re-run. It:

1. Creates or migrates the schema. On a brand-new database it stamps the Alembic
   baseline and runs migrations to head; on a pre-Alembic database it detects
   that and stamps the baseline before migrating.
2. Applies the Procrastinate job-queue schema (checking first — the Procrastinate
   `apply_schema` step is *not* itself idempotent, so Filearr guards it).
3. Ensures the Meilisearch index exists.

## Two gotchas that will bite you

!!! bug "PostgreSQL 18 mounts at `/var/lib/postgresql`, not `.../data`"
    The Postgres 18 Docker image changed the volume convention: mount the
    **parent** directory `/var/lib/postgresql`, **not** `/var/lib/postgresql/data`.
    The shipped compose file already does this. If you copy an older compose file
    from elsewhere, fix this or Postgres will not persist correctly.

!!! bug "`PYTHONPATH=/app` is required"
    Scripts and the Procrastinate CLI in the image need `PYTHONPATH=/app`. It is
    set in the image's `ENV`; do not drop it if you customize the entrypoint or
    the worker command.

## Verifying it works

```bash
curl http://localhost:8484/api/v1/health          # -> 200
curl -X POST http://localhost:8484/api/v1/libraries \
  -H 'Content-Type: application/json' \
  -d '{"name":"media","root_path":"/data/media"}'
# then start a scan with the returned id:
curl -X POST http://localhost:8484/api/v1/libraries/<id>/scan
```

Results appear in the UI search as extraction completes.
