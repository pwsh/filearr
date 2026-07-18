# Unraid

Filearr ships four Community-Applications-format templates (one per container).
Until they are published to Community Applications, install them manually.

## The four templates

| Install order | Template | Image | Role |
|---|---|---|---|
| 1 | `filearr-postgres.xml` | `postgres:18.4` | Source of truth + job queue — **back this up** |
| 2 | `filearr-meilisearch.xml` | `getmeili/meilisearch:v1.49.0` | Disposable, rebuildable index |
| 3 | `filearr.xml` | Filearr app image | Web UI + API (port 8484) |
| 4 | `filearr-worker.xml` | same image | Post-arguments run the Procrastinate worker |

## One-time setup

1. **Create the shared Docker network.** Container-name DNS does not work on
   Unraid's default bridge, so the containers need a user-defined network:

    ```bash
    docker network create filearr
    ```

2. **Install the templates.** Copy the four XML files to
   `/boot/config/plugins/dockerMan/templates-user/` on the server, then in the
   Docker tab choose **Add Container** and pick each template.

3. **Set matching secrets across containers.** Use the same `POSTGRES_PASSWORD`
   and DSNs, and the same `MEILI_MASTER_KEY`, everywhere they appear. These are
   masked fields you fill once each.

4. **Initialise the schema once.** After first start:
   Docker tab → `filearr` → **Console** → `python scripts/init_db.py`.

## Notes and conventions

- **Media is read-only** at `/data/media` in **both** the app and worker, with
  identical mappings — the catalog paths must match between the two.
- **Data volumes belong on the cache pool**, not the array: put Postgres and
  Meilisearch data in appdata.
- **Ports 5432 / 7700 stay unmapped** by default — the stack talks over the
  `filearr` network internally. Only the app's 8484 needs to be reachable.

## HTTPS on Unraid

The CA templates serve the app over **plain HTTP on 8484**. For HTTPS, the
Unraid-native path is to put the app behind a reverse proxy:

- **Recommended:** Unraid's built-in reverse proxy, SWAG, or Nginx Proxy Manager
  — you get a real certificate and no per-client CA trust to manage.
- **Alternative:** use the repo's `docker-compose.yml` (via the Compose Manager
  plugin), which includes the Caddy TLS sidecar (self-signed LAN CA, HTTPS on
  8443).

!!! note "Set up HTTPS before enabling login"
    Session cookies are marked `Secure`, so the login flow requires HTTPS. Put
    the reverse proxy in place before you turn authentication on.

## Optional: hardware-accelerated video thumbnails

To use an Intel iGPU for video poster frames, add the render device and group to
the **`filearr-worker`** container's Extra Parameters:

```text
--device /dev/dri --group-add $(stat -c '%g' /dev/dri/renderD128)
```

Safe to skip — the thumbnail pipeline falls back to software decoding
automatically when no render device is present.
