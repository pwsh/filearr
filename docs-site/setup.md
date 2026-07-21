# Setup requirements

This page covers what you need before deploying Filearr: hardware, supported
platforms, the pinned dependency versions (and *why* a couple of them are
pinned), the network/port matrix, and the read-only media-mount posture.

## Hardware guidance

Filearr is light for a homelab catalog. The dominant costs are the initial scan
(a filesystem walk plus per-file hashing over your storage) and, if you enable
them, thumbnail generation and semantic embeddings.

| Deployment size | CPU | RAM | Notes |
|---|---|---|---|
| Small (tens of thousands of files) | 2 cores | 2 GB | Comfortable default. |
| Medium (hundreds of thousands) | 4 cores | 4 GB | The reference Proxmox LXC profile. |
| Large (millions) | 4–8 cores | 8 GB+ | Scale out extraction workers; watch disk headroom for thumbnails. |

Additional considerations:

- **Disk for Postgres.** Metadata only — the catalog is small relative to your
  media. Budget growth for extracted metadata, audit history and the job queue.
- **Disk for Meilisearch.** The index lives on a **local** (not network) volume;
  it is latency-critical. A full rebuild temporarily holds *both* the live and a
  shadow index (~2x the index size) until the atomic swap completes.
- **Disk for thumbnails / config.** The thumbnail cache and other caches live
  under the config volume. Filearr has disk-full guardrails, but give this
  volume real headroom (a small LXC rootfs fills fast).
- **Optional iGPU.** An Intel Arc/UHD render node (`/dev/dri/renderD128`) can
  hardware-accelerate video poster frames. Purely an opt-in accelerator — the
  pipeline falls back to software automatically when no device is present.
- **RAM for semantic search (optional).** The local ONNX embedder adds roughly
  half a gigabyte of resident memory when enabled; it is **off by default**.

## Platform / OS matrix

| Path | Best for | Notes |
|---|---|---|
| **Docker Compose** | Any Docker host | The canonical deployment; every other path wraps it. |
| **Unraid** | Unraid servers | Community-Applications-format templates; put the app behind a reverse proxy for HTTPS. |
| **Proxmox LXC** | Proxmox VE homelabs | A guided wizard builds a Docker-in-LXC container and mounts your network storage inside it. |

The container images are Linux. Development is supported on Linux, macOS and
Windows. The optional **agent** cross-compiles to Windows, macOS and Linux from a
single pure-Go codebase (no cgo).

## Dependency versions

The stack is version-pinned. These are the current pins:

| Component | Version | Why it matters |
|---|---|---|
| Python | 3.13 | Backend runtime (the image uses `python:3.13-slim`). |
| PostgreSQL | 18.x (18.4 pinned) | Source of truth **and** job queue; uses native `uuidv7()` primary keys and PG18 async I/O. |
| Meilisearch | v1.49.0 (pin **≥ 1.48.2**) | Search projection. See the security note below. |
| FastAPI | 0.139 | Native Server-Sent Events (requires ≥ 0.135). |
| SQLAlchemy | 2.x + psycopg3 | Async database access. |
| Procrastinate | 3.9 | Postgres-native job queue — **no Redis** anywhere in the stack. |
| Node.js | ≥ 24 | Frontend build only (dev / image build). |
| Go | ≥ 1.26 | Building the optional agent binary. |
| ffmpeg / ffprobe | bundled in image | Video metadata + optional video poster frames. |

Optional native tools bundled in the runtime image: `exiftool` (deep EXIF),
`tesseract` + `poppler-utils` (OCR, per-library opt-in), `libmagic` /
`libmediainfo`. PDF first-page thumbnails need **no** extra system package (the
PDFium binding is bundled in a Python wheel).

!!! warning "Why Meilisearch is pinned to ≥ 1.48.2"
    The Meilisearch pin floor exists for a reason: **CVE-2026-57823 / CVE-2026-57824**
    (a tenant-token pair) are fixed at 1.48.2. The stack ships **v1.49.0**.
    Self-hosted Meilisearch also had an authenticated blind SSRF (v1.8–v1.34.0,
    fixed 1.34.1) via task-webhook / remote-source features — moot for the
    current pin, but catalogued so any future version bump re-checks the **full**
    CVE list rather than just the tenant-token pair. Apply the same discipline to
    the step-ca and Caddy pins if you enable those features.

## Network / port matrix

| Port | Service | Exposure | Notes |
|---|---|---|---|
| 8000 | App (HTTP, in-container) | internal | Published as **8484** on the host in the reference deploys. |
| 8484 | App (HTTP) | LAN | Web UI + API + `/api/docs`. |
| 443 / 8443 | Caddy TLS reverse proxy | LAN / public | Optional TLS front (self-signed LAN CA, or a Let's Encrypt wildcard). **TCP only** — HTTP/3 is disabled, so no UDP rule is needed. |
| 5432 | PostgreSQL | internal only | Not published by default; the stack talks over its private network. |
| 7700 | Meilisearch | internal only | Not published by default. |
| 9000 | step-ca (agents) | LAN / agents | Only when the optional agent CA is enabled. |
| 8686 | Agent local web UI | loopback only | On an agent machine; loopback bind is enforced. |

Only the app (and optionally the TLS front and the agent CA) needs to be
reachable. Postgres and Meilisearch stay on the internal network.

All published ports are **TCP**. Filearr pins Caddy to `h1 h2` (HTTP/3 off), so
you never need a UDP/443 firewall rule — see
[TLS operations](https://github.com/pwsh/filearr/blob/main/docs/ops/tls.md) for
why enabling HTTP/3 here is not a safe drop-in change.

## Read-only media mount posture

Filearr **never writes to your media**. Media is always mounted **read-only**
(`/data/media`) in both the app and worker containers — write-back is a future
capability. Two practical consequences:

- The **same** mount path must be used in the app and worker so that catalog
  paths line up.
- Filearr identifies items by their path **relative to the library root**, so a
  mount can move (host bind today, in-LXC rclone mount tomorrow) without breaking
  identity. A per-library `native_prefix` / share location maps catalog paths
  back to the spelling your other systems expect.

Back up your media with your existing NAS strategy — Filearr treats it as
read-only source data and never touches it. What Filearr *does* need backed up is
Postgres; see [Backup & restore](operations.md#backup-and-restore).
