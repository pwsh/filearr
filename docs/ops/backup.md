# Backup & restore (Postgres)

Postgres is the **source of truth**. Everything a user cannot get back by
re-scanning lives there, so Postgres is the only thing that MUST be backed up.
Meilisearch and the thumbnail cache are **disposable projections** — rebuilt
from Postgres on demand — and are deliberately NOT part of the backup.

## What's irreplaceable vs. rebuildable

| Data | Store | Backed up? | How it comes back |
|---|---|---|---|
| `user_metadata` edits, tags, custom-field values | Postgres | **YES** | restore only |
| Saved searches, alert channels/rules, alert history | Postgres | **YES** | restore only |
| Provenance / attributed audit trail | Postgres | **YES** | restore only |
| Libraries, scan_paths, schedules, presets | Postgres | **YES** | restore only |
| Extracted `metadata` (ffprobe/exif/…) | Postgres | YES (via dump) | or re-scan the files |
| Job queue (procrastinate_*) | Postgres | YES (via dump) | re-enqueued by scans |
| Search index | Meilisearch | **NO** (by design) | `rebuild-index` from Postgres |
| Thumbnails / poster frames | thumbnail cache volume | **NO** (by design) | regenerated lazily on serve |

The media files themselves are read-only source data on your NAS/shares and are
never touched by Filearr — back them up with your existing NAS strategy, not here.

## Back up (one-liner)

Run on the host where the stack lives (Proxmox: inside the CT; Unraid: on the
server), from the compose project directory (`/opt/filearr` on the Proxmox
deploy):

```bash
cd /opt/filearr
docker compose exec -T postgres pg_dump -U filearr -Fc filearr > filearr-$(date -u +%Y%m%dT%H%M%SZ).dump
```

- `-Fc` = compressed **custom format** → selective/parallel restore with
  `pg_restore`. For a plain-SQL dump you can eyeball, use `-Fp` and redirect to
  `.sql`.
- `-T` (no TTY) is required when piping through `docker compose exec`.
- The dump lands on the host filesystem; copy it off-box (another host, NAS, or
  object storage) — a backup on the same disk as the database is not a backup.

### Helper script + retention

`scripts/backup.sh` wraps the above: it writes a timestamped `-Fc` dump into
`<compose-dir>/config/backups/` (persisted with the rest of `./config`) and
prunes to the newest **7** (override `BACKUP_KEEP`). It is `set -euo pipefail`
with an ERR trap and is safe to run unattended:

```bash
# manual
bash /opt/filearr/scripts/backup.sh

# nightly at 03:30 via the Proxmox host's crontab (runs inside the CT):
30 3 * * *  pct exec 300 -- bash /opt/filearr/scripts/backup.sh >> /var/log/filearr-backup.log 2>&1
```

**Retention suggestion:** keep 7 daily dumps on-box (the default) and copy at
least one weekly dump off-box. Dumps are small — they hold metadata, not media —
so a few weeks of history costs little.

## Restore

A restore rebuilds Postgres, then the disposable projections are regenerated —
you do NOT need a Meili or thumbnail backup.

1. **Bring up a fresh stack** (empty volumes) with the SAME `.env` — in
   particular the same `POSTGRES_PASSWORD`, `FILEARR_DATABASE_URL`, and
   `MEILI_MASTER_KEY`:
   ```bash
   cd /opt/filearr
   docker compose up -d postgres
   docker compose exec -T postgres pg_isready -U filearr   # wait for ready
   ```

2. **Restore the dump** into the (empty) database. `--clean --if-exists` makes it
   safe to re-run over an existing schema:
   ```bash
   docker compose exec -T postgres \
     pg_restore -U filearr -d filearr --clean --if-exists --no-owner \
     < filearr-YYYYmmddTHHMMSSZ.dump
   ```

3. **Stamp / migrate** — bring the schema to head. `init_db.py` is idempotent:
   it detects a pre-Alembic DB and stamps the baseline, otherwise runs
   `alembic upgrade head`, applies the procrastinate schema, and ensures the
   Meili index exists (see docs/migrations.md):
   ```bash
   docker compose run --rm app python scripts/init_db.py
   ```

4. **Start the rest of the stack and rebuild the search index** (Meili was never
   backed up — it is rebuilt from the restored Postgres rows):
   ```bash
   docker compose up -d
   curl -X POST http://localhost:8484/api/v1/system/rebuild-index
   ```

5. **Thumbnails regenerate lazily** — the first grid/detail view of an item with
   no cached thumbnail re-queues generation (video posters via the worker). No
   manual step; the cache refills over normal use. To pre-warm, re-run a scan
   (the extract ride-along pregenerates grid tiers).

## Verify a backup (scratch round-trip)

Prove a dump actually restores before you trust it (do this on a throwaway DB, not
production):

```bash
# spin a scratch postgres, load the dump, count the irreplaceable rows
docker run -d --name pg-scratch -e POSTGRES_PASSWORD=x -e POSTGRES_USER=filearr -e POSTGRES_DB=filearr postgres:18.4
sleep 8
docker exec -i pg-scratch pg_restore -U filearr -d filearr --clean --if-exists --no-owner < filearr-YYYYmmddTHHMMSSZ.dump
docker exec -it pg-scratch psql -U filearr -d filearr -c "select count(*) from items;"
docker rm -f pg-scratch
```

## Downgrades / schema rollbacks

Alembic downgrades exist but are best-effort and can lose recycle-bin data
(see docs/migrations.md). **Always take a fresh dump before downgrading.** Meili
never needs a backup — rebuild it after any schema change that touches indexed
fields.

## Related: trusting the LAN TLS CA (OPS-T1)

Not a backup, but the other file operators reach for post-deploy. Caddy's
self-signed root CA lives on the `caddy_data` volume. Export it to trust the
HTTPS UI on client machines (removes the browser warning):

```bash
cd /opt/filearr
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./filearr-root-ca.crt
```

Import `filearr-root-ca.crt` into each client's OS/browser trust store. The cert
persists across restarts because `caddy_data` is a named volume — losing it just
means clients must re-trust a freshly minted root.
