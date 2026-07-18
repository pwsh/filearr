# Filearr — Unified Media Catalog & Search

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

Self-hosted, unified catalog with **typo-tolerant instant search** over mixed
libraries: video, music, audiobooks, audio samples, images, 3D models, documents
and spreadsheets. Filearr scans your filesystems directly. **Postgres is the
source of truth; Meilisearch is a disposable, rebuildable search projection.** The
public REST API supports search **and** metadata updates. Docker-first,
Unraid/Proxmox-friendly, with an optional distributed-agent fleet.

📖 **Documentation:** <https://pwsh.github.io/filearr/>

## Features

- One search box across every file type, with per-library include/exclude rules.
- Typo tolerance, facets, and optional local semantic/hybrid search (Meilisearch).
- Rich per-type extraction: ffprobe, audio tags, EXIF (GPS hidden by default),
  document body text, archive listings, thumbnails and video poster frames.
- Extracted metadata and your edits are **separate** — a rescan never clobbers
  your changes.
- Scans never hard-delete: missing files are tombstoned into a recycle bin.
- A real REST API (search, edits, batch, reports, exports) with OpenAPI docs at
  `/api/docs`.
- API keys (read/write/admin), sessions, optional OIDC/LDAP, and path-scoped RBAC.
- Optional distributed agents: a single Go binary per machine with a local offline
  index that replicates to central over mTLS.
- **No external telemetry — nothing phones home.**

## Quick start

```bash
cp .env.example .env          # edit the secrets
docker compose up -d postgres meilisearch
docker compose run --rm app python scripts/init_db.py    # idempotent bootstrap
docker compose up -d
# UI at http://localhost:8484 · API docs at /api/docs
```

Full deployment guides (Docker Compose, Unraid, Proxmox LXC), the agent runbook,
security model, data-collection details, operations/recovery, and the full
configuration reference are in the **[documentation site](https://pwsh.github.io/filearr/)**.

## Stack (pinned)

| Layer | Component |
|---|---|
| API | Python 3.13 · FastAPI 0.139 (native SSE) · uvicorn |
| DB | PostgreSQL 18.4 — native `uuidv7()` PKs, async I/O |
| Jobs | Procrastinate 3.9 (Postgres-native queue — no Redis) |
| Search | Meilisearch v1.49.0 (pin ≥ 1.48.2 for tenant-token CVEs) |
| Frontend | Svelte 5 + Vite 8 (Rolldown) + Tailwind v4 SPA, PWA |
| Agent | Go ≥ 1.26, single static binary (no cgo) |

## Development

```bash
cd backend && uv sync && uv run uvicorn filearr.main:app --reload
cd backend && uv run procrastinate --app=filearr.worker.proc_app worker
cd frontend && npm install && npm run dev   # proxies /api to :8000
```

See the [Development guide](https://pwsh.github.io/filearr/reference/development/)
for the full layout, tests, and contribution basics.

## License and source (AGPL §13)

Filearr is free software under the **GNU Affero General Public License v3.0 or
later** (AGPL-3.0-or-later) — see [LICENSE](LICENSE). Self-hosting and commercial
use are permitted; a modified version offered to users over a network must make
its **Corresponding Source** available (AGPL §13). The running instance exposes a
"Source" link (configurable via `FILEARR_SOURCE_URL`) so a fork can point users at
its own source. Contributions are accepted under the same license with a DCO
sign-off.
