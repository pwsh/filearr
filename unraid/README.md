# Unraid templates for the Filearr stack

Four Community Applications–format templates (`Container version="2"`):

| Install order | Template | Image |
|---|---|---|
| 1 | `filearr-postgres.xml` | postgres:18.4 (source of truth + job queue — back this up) |
| 2 | `filearr-meilisearch.xml` | getmeili/meilisearch:v1.49.0 (disposable, rebuildable index) |
| 3 | `filearr.xml` | ghcr.io/CHANGEME/filearr (web UI + API, port 8484) |
| 4 | `filearr-worker.xml` | same image, Post Arguments run the Procrastinate worker |

## One-time setup

1. Create the shared Docker network (container-name DNS doesn't work on Unraid's
   default bridge):

       docker network create filearr

2. Manual template install (until published to CA): copy the four XML files to
   `/boot/config/plugins/dockerMan/templates-user/` on your Unraid server, then
   add each via Docker tab → Add Container → pick the template.

3. Set the same `POSTGRES_PASSWORD` / DSNs and the same `MEILI_MASTER_KEY`
   across containers (templates default to matching values; passwords are masked
   fields you fill once each).

4. After first start, initialise the schema once:
   Docker tab → filearr → Console →  `python scripts/init_db.py`

## Notes

- Media is mounted read-only (`/data/media`) in both app and worker — identical
  mapping in both is required so paths in the catalog match.
- Postgres and Meilisearch data belong in appdata (cache pool), not on the array.
- Port 5432/7700 mappings are intentionally unmapped by default; the stack talks
  over the `filearr` network internally.
- Publishing to Community Applications later: submit via ca.unraid.net/submit
  (needs HTTPS PNG icon, support thread, overview — all present except the
  final icon/repo URLs marked CHANGEME).
- **TLS (OPS-T1):** these CA templates ship the app over plain HTTP on port 8484.
  For HTTPS, either (a) put the app behind Unraid's built-in reverse proxy /
  SWAG / Nginx-Proxy-Manager (recommended on Unraid — real cert, no per-client
  CA trust), or (b) use the repo `docker-compose.yml` which includes the Caddy
  TLS sidecar (self-signed LAN CA, https on 8443). A standalone `filearr-caddy`
  CA template is NOT provided yet — the reverse-proxy route is the Unraid-native
  path. Wave 4 login will require HTTPS (Secure cookies), so set this up before
  enabling auth.
- **iGPU thumbnails (optional, P12/OPS-T7):** to hardware-accelerate video
  poster-frames, add `--device=/dev/dri` and the render group to the
  `filearr-worker` container (Extra Parameters: `--device /dev/dri --group-add
  $(stat -c '%g' /dev/dri/renderD128)`). Safe to skip — the pipeline falls back
  to software automatically when the device is absent.
- Alternative: use the repo's `docker-compose.yml` with the Compose Manager plugin.
