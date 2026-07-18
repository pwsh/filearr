# Deployment

Filearr ships three supported deployment paths. All of them run the same
container images and the same Postgres + Meilisearch stack — they differ only in
how the host and storage are prepared.

<div class="grid cards" markdown>

- :material-docker: **[Docker Compose](docker-compose.md)**

    The canonical deployment. Works on any Docker host. Every other path wraps
    this compose stack.

- :material-nas: **[Unraid](unraid.md)**

    Community-Applications-format templates for the four containers, with a
    reverse-proxy note for HTTPS.

- :material-server-network: **[Proxmox LXC](proxmox.md)**

    A guided wizard builds a Docker-in-LXC container, mounts your network storage
    inside it, and can stand up TLS and the agent CA.

</div>

After any deploy, see [Upgrades & migrations](upgrades.md) for the redeploy and
schema-migration behavior, and [Operations & recovery](../operations.md) for the
runbook.

## What every deployment runs

| Service | Image | Role | Back up? |
|---|---|---|---|
| `app` | `filearr` | REST API + SPA (port 8000 → 8484) | via Postgres |
| `worker` | `filearr` | Procrastinate job worker (scan/extract/index/maintenance) | — |
| `postgres` | `postgres:18.4` | Source of truth **and** job queue | **YES** |
| `meilisearch` | `getmeili/meilisearch:v1.49.0` | Disposable search projection | No (rebuildable) |
| `caddy` *(optional)* | built locally | TLS reverse proxy | No |
| `step-ca` *(optional, `agents` profile)* | `smallstep/step-ca:0.30.2` | Agent certificate authority | volume only |
| `watcher` *(optional)* | `filearr` | Local-disk filesystem watch mode | — |

Only Postgres holds data you cannot recreate. Meilisearch and the thumbnail
cache are disposable projections.
