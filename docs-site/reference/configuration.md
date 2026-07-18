# Configuration reference

Filearr is configured entirely through environment variables prefixed
`FILEARR_` (plus the container-level `POSTGRES_PASSWORD`, `MEILI_MASTER_KEY`, and
`MEDIA_PATH`). Values load from the process environment and the `.env` file. This
page lists the **operationally meaningful** settings grouped by area — not every
internal knob. Defaults shown are the built-in defaults.

!!! tip "Only override what you need"
    Every setting has a sensible default. A minimal deployment sets the database /
    Meili connection strings, the two passwords, `MEDIA_PATH`, and (if you use
    alerts) `FILEARR_SECRET_KEY`. Everything else is tuning.

## Core / connections

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_DATABASE_URL` | `postgresql+psycopg://filearr:filearr@postgres:5432/filearr` | SQLAlchemy DSN (source of truth). |
| `FILEARR_PROCRASTINATE_DSN` | `postgresql://filearr:filearr@postgres:5432/filearr` | Job-queue DSN. |
| `FILEARR_MEILI_URL` | `http://meilisearch:7700` | Meilisearch endpoint. |
| `FILEARR_MEILI_MASTER_KEY` | `change-me` | Meilisearch master key. |
| `FILEARR_MEILI_INDEX` | `items` | Index name. |
| `FILEARR_CONFIG_DIR` | `/config` | Thumbnails, caches, models, exports, staging. |
| `FILEARR_LOG_LEVEL` | `INFO` | Log verbosity. |
| `FILEARR_SOURCE_URL` | GitHub repo URL | AGPL §13 "Source" link (point at your fork). |
| `FILEARR_SECRET_KEY` | *(unset)* | Envelope key for alert-channel secret encryption (**required** for alerts; never auto-rotated). |
| `FILEARR_PUBLIC_BASE_URL` | *(unset)* | Absolute prefix for export/report download links; blank = site-relative. |
| `FILEARR_SHARE_MAP_PATH` | `/config/share-map.json` | Deploy-written share map for auto share locations. |

## Authentication & sessions

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_AUTH_ENABLED` | `true` | Master switch for auth. |
| `FILEARR_SESSION_TTL_HOURS` | `720` | Absolute session lifetime (30d). |
| `FILEARR_SESSION_INACTIVITY_HOURS` | `168` | Idle window (7d). |
| `FILEARR_SESSION_ROTATION_MINUTES` | `10` | Opaque-token rotation cadence. |
| `FILEARR_SESSION_COOKIE_SAMESITE` | `lax` | `lax` (SSO-safe) / `strict` / `none`. |
| `FILEARR_AUTH_RATELIMIT_ENABLED` | `true` | Brute-force limiter. |
| `FILEARR_AUTH_RATELIMIT_MAX_ATTEMPTS` | `3` | Failures per window → lock. |
| `FILEARR_AUTH_RATELIMIT_WINDOW_SECONDS` | `120` | Find window. |
| `FILEARR_AUTH_RATELIMIT_LOCK_SECONDS` | `300` | Lockout duration. |
| `FILEARR_AUTH_RATELIMIT_TRUST_FORWARDED_FOR` | `false` | Only enable behind a trusted proxy. |
| `FILEARR_AUDIT_READS` | `false` | Record a per-query search event (high volume). |

OIDC (`FILEARR_OIDC_*`) and LDAP (`FILEARR_LDAP_*`) are extensive, env-only
provider configs; both default **off**. See [Security](../security.md) for the
model and the source `config.py` for every field.

## Scanning & hashing

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_SCAN_HASH_FULL_MAX_BYTES` | `1073741824` | Skip the full content hash above this size (1 GiB). |
| `FILEARR_SCAN_BATCH_SIZE` | `500` | Files per batch commit. |
| `FILEARR_RECYCLE_RETENTION_DAYS` | `30` | Recycle-bin retention before purge. |
| `FILEARR_STAGED_PIPELINE` | `true` | Defer all extraction to scan end (vs trickle during walk). |
| `FILEARR_AUDIT_RETENTION_DAYS` | `90` | Retention for extractor-sourced item audit rows (user edits exempt). |

## Workers, queues & the reaper

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_WORKER_CONCURRENCY` | `4` | Parallel jobs per worker. |
| `FILEARR_WORKER_QUEUES` | *(all)* | Comma-separated queues a worker serves. |
| `FILEARR_JOB_HISTORY_RETENTION_DAYS` | `14` | Purge terminal job rows older than this. |
| `FILEARR_JOB_STALL_HEARTBEAT_SECONDS` | `30` | Heartbeat net for stalled jobs. |
| `FILEARR_JOB_STALL_SECONDS` | `3600` | Age net for non-scan doing jobs. |
| `FILEARR_REAP_MAX_ATTEMPTS` | `10` | Requeue budget for a stalled non-scan job before it is failed. |
| `FILEARR_SCAN_SCHEDULE_MAX_CATCHUP_MINUTES` | `2880` | Furthest-back missed cron a recovery tick fires (48h). |
| `FILEARR_SCAN_RUN_RECONCILE_GRACE_SECONDS` | `600` | Grace before finalizing an orphaned scan run. |

## Search reconciliation & rebuild

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_MEILI_SEARCH_CUTOFF_MS` | `1500` | Per-search wall-clock circuit breaker. |
| `FILEARR_RECONCILE_MAX_FIXES` | `10000` | Cap on repairs per hourly reconcile sweep. |
| `FILEARR_MEILI_REBUILD_WAIT_S` | `900` | Total wait budget for a shadow rebuild before it fails cleanly. |
| `FILEARR_MEILI_SHADOW_MAX_AGE_HOURS` | `6` | Age at which an orphaned shadow index is reaped. |
| `FILEARR_MEILI_SCOPE_FILTER_CEILING` | `4096` | Max compiled RBAC scope-filter length (over → refuse). |

## Extraction limits (safety caps)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_FFPROBE_TIMEOUT_S` | `30` | ffprobe wall-clock cap. |
| `FILEARR_MODEL3D_MAX_BYTES` | `268435456` | Mesh size ceiling handed to trimesh (256 MiB). |
| `FILEARR_DOCUMENT_MAX_BYTES` | `268435456` | Doc/spreadsheet size ceiling. |
| `FILEARR_DIGEST_MAX_BYTES` | `53687091200` | On-demand MD5/SHA-256 size ceiling (50 GiB). |
| `FILEARR_ARCHIVE_MAX_MEMBERS` | `10000` | Archive member-listing cap. |

### EXIF / GPS

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_GPS_EXPOSE_DEFAULT` | `false` | Per-library GPS-exposure default (no global default-on). |
| `FILEARR_EXIF_TIMEOUT_S` | `30` | exiftool wall-clock cap. |

### OCR (per-library opt-in)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_OCR_ENABLED` | `false` | Global default off (per-library toggle gates it). |
| `FILEARR_OCR_MAX_PAGES` | `10` | Scanned-PDF page ceiling. |
| `FILEARR_OCR_TIMEOUT_S` | `120` | Per-subprocess wall clock. |
| `FILEARR_OCR_LANG` | `eng` | Tesseract language. |

### Semantic search (opt-in)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_SEMANTIC_ENABLED` | `false` | Load the local ONNX embedder (off = zero cost). |
| `FILEARR_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Local embedding model (downloaded once). |
| `FILEARR_EMBEDDER_CONCURRENCY` | `1` | One memory-capped, lowest-priority worker. |

## Thumbnails

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_THUMBS_ENABLED` | `true` | Generate WebP thumbnails / posters. |
| `FILEARR_THUMBNAIL_GRID_PX` | `320` | Grid tier longest edge. |
| `FILEARR_THUMBNAIL_PREVIEW_PX` | `800` | Preview tier longest edge. |
| `FILEARR_THUMB_ACCEL` | `auto` | `auto` (QSV if `/dev/dri` present) / `off`. |
| `FILEARR_THUMBNAIL_TOTAL_BUDGET_BYTES` | `5368709120` | Soft alarm on cache size (5 GiB). |

## Disk guardrails

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_DISK_MIN_FREE_GB` | `5` | Critical below this (absolute floor). |
| `FILEARR_DISK_WARN_FREE_GB` | `20` | Warn below this (absolute floor). |
| `FILEARR_DISK_CRIT_PCT_FREE` | `2` | Critical below this percent free. |
| `FILEARR_DISK_WARN_PCT_FREE` | `10` | Warn below this percent free. |
| `FILEARR_DISK_PG_PATH` | *(unset)* | Postgres data path to watch; when critical, extract pauses. |
| `FILEARR_DISK_GC_TARGET_FREE_GB` | `0` | `>0` LRU-evicts valid thumbnails to this target at critical. |

## Distributed agents (all off unless enabled)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_AGENTS_ENABLED` | `false` | Master switch for the agent fleet surface. |
| `FILEARR_ENROLLMENT_TOKEN_TTL_MINUTES` | `60` | Single-use enrollment-token TTL. |
| `FILEARR_CA_URL` | *(unset)* | step-ca URL handed to agents. |
| `FILEARR_CA_FINGERPRINT` | *(unset)* | Public root fingerprint (pin). |
| `FILEARR_CA_PROVISIONER` | `filearr-agents` | Provisioner name. |
| `FILEARR_CA_PROVISIONER_JWK` | *(unset)* | **Secret** — decrypted private JWK; without it `ca_ott` is null. |
| `FILEARR_AGENT_CERT_TTL_HOURS` | `48` | Advisory agent cert lifetime (24–72h band). |
| `FILEARR_AGENT_AUTH_MODE` | `fingerprint` | `fingerprint` / `mtls-header` / `both`. |
| `FILEARR_PROXY_SHARED_SECRET` | *(unset)* | **Secret** — mTLS proxy ↔ backend trust (required for mtls modes). |
| `FILEARR_AGENT_OFFLINE_ALERT_SECONDS` | `172800` | Agent-offline alert threshold (48h). |
| `FILEARR_AGENT_REPLICATION_STALL_ALERT_SECONDS` | `21600` | Replication-stall alert threshold (6h). |

## Alerting

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS` | `false` | Permit RFC1918/ULA webhook targets (loopback/link-local still denied). |
| `FILEARR_ALERT_WEBHOOK_TIMEOUT_S` | `10` | Per-POST wall clock. |
| `FILEARR_ALERT_RULE_MAX_PER_HOUR` | `100` | Per-rule dispatch ceiling (storm safety net). |
| `FILEARR_ALERT_EVENTS_RETENTION_DAYS` | `30` | Terminal alert-event retention. |

For the complete, authoritative list see `backend/filearr/config.py`.
