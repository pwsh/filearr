# Operations & recovery

A runbook for keeping Filearr healthy and getting it back when something breaks.
Each section is **symptom → diagnosis → fix**, drawn from real incidents.

!!! note "Placeholders"
    Examples use placeholder identifiers — replace `filearr.example.com` /
    `ca.example.com` / `agents.example.com`, `192.0.2.10`, and `<your-vmid>` with
    your own. The stack is assumed at `/opt/filearr` with the API on `:8484`, the
    CA on `:9000`, and TLS on `:8443`.

## Working inside the containers

This is the toolkit everything below uses. On a Proxmox LXC the compose stack runs
*inside* the container, so from the Proxmox host prefix commands with `pct exec`:

```bash
# what's running / health
pct exec <your-vmid> -- docker ps
pct exec <your-vmid> -- docker compose -f /opt/filearr/docker-compose.yml ps

# follow one service's logs
pct exec <your-vmid> -- docker compose logs -f app
pct exec <your-vmid> -- docker compose logs -f worker
pct exec <your-vmid> -- docker compose logs -f postgres
pct exec <your-vmid> -- docker compose logs step-ca | grep -i "password is"

# a psql shell into the database
pct exec <your-vmid> -- docker compose exec -T postgres psql -U filearr -d filearr -c "SELECT 1;"

# run python / alembic inside the app image
pct exec <your-vmid> -- docker compose run --rm app python scripts/init_db.py
pct exec <your-vmid> -- docker compose exec app alembic current
```

If you are already inside the container (or on a single-host Unraid/Compose
deploy), drop the `pct exec <your-vmid> --` prefix and run the `docker compose …`
part from `/opt/filearr`.

## Scan-scheduling storms / stalled jobs / the reaper

**Symptom.** A library's scheduled scan fires every scheduler tick instead of on
its cron, stacking several concurrent scan jobs per library. The worker dies
repeatedly (OOM — each full scan holds a whole-library map in RAM). Job attempt
counts climb into the dozens; the Jobs page shows many `doing` jobs with no live
worker.

**Diagnosis.**

```sql
-- concurrent / stalled scan jobs
SELECT task_name, status, attempts, count(*)
FROM procrastinate_jobs
WHERE task_name = 'filearr.tasks.scan.scan_library'
GROUP BY task_name, status, attempts ORDER BY attempts DESC;

-- scan runs that never finished (a crashed scan must end 'failed')
SELECT id, library_id, status, started_at
FROM scan_runs WHERE status IN ('running','stopping');

-- runaway retry counts on any job
SELECT task_name, status, attempts FROM procrastinate_jobs WHERE attempts >= 10;
```

Healthy is at most **one** `scan_library` per library in `todo`/`doing`
(also visible via `GET /api/v1/system/jobs`).

**Fix.**

1. Deploy the current version (dedupe now covers `todo`/`doing`/`aborting`; scans
   fire once per cron occurrence; the reaper caps non-scan requeues and finalizes
   orphaned scan runs).
2. Clean up rows the old code left (idempotent — or wait ~5 min for the reaper):

    ```sql
    UPDATE procrastinate_jobs SET status='failed'
    WHERE task_name='filearr.tasks.scan.scan_library' AND status='doing';

    UPDATE scan_runs SET status='failed', finished_at=now()
    WHERE status IN ('running','stopping');

    UPDATE procrastinate_jobs SET status='failed'
    WHERE status='doing' AND attempts >= 10;
    ```

3. Force a reaper pass and confirm: `GET /api/v1/system/jobs/reap` (admin).
4. Re-enable schedules (Admin → each library → Scan schedule, or `PATCH
   /api/v1/libraries/{id}` with `scan_cron`).

**Tunables.** `FILEARR_SCAN_SCHEDULE_MAX_CATCHUP_MINUTES` (default 2880 = 48h —
the furthest-back missed occurrence a recovery tick fires; never storms, never
back-fills a week of downtime) and `FILEARR_REAP_MAX_ATTEMPTS` (default 10).

**If OOM recurs.** Concurrent full scans over SMB each hold a whole-library map in
RAM. **Stagger** library `scan_cron` times so two large libraries never scan at
once, and/or raise the worker container's memory limit.

## Scan runs stuck in `stopping` (or orphaned `running`)

**Symptom.** A scan run is wedged in `stopping` (or `running`) with nothing to
clear it, and that library's *scheduled* scans silently stop happening (the
scheduler's busy-set counts those states, so one wedged row blocks the library
forever).

**Diagnosis.**

```sql
SELECT id, library_id, status, started_at FROM scan_runs
WHERE status IN ('running','stopping') ORDER BY started_at;
```

It is stuck if there is **no** `scan_library` job for that library still in
`todo`/`doing`/`aborting`.

**Fix.**

- **Auto-heal:** the 5-minutely maintenance reconciler drives any run older than
  the grace window terminal (`stopping → stopped`, `running → failed`).
- **Immediate:** `POST /api/v1/scans/{id}/force-clear` (admin; audited). Also a
  **Force clear** button on the Admin page. Returns `409 "still active; use stop"`
  only if a live worker is genuinely draining it.
- A **manual** library scan (`POST /libraries/{id}/scan`) also self-heals leftover
  rows before deferring.

**Tunable.** `FILEARR_SCAN_RUN_RECONCILE_GRACE_SECONDS` (default 600). Verify via
`GET /api/v1/system/jobs/reap` → `scan_runs_reconciled > 0`.

## Disk fills up (unbounded generation → Postgres crash)

**Symptom.** On single-volume deploys the thumbnail cache, Postgres data dir and
the Meili store share one filesystem. Unbounded thumbnail generation fills it; at
0 bytes free Postgres can no longer extend a file and the platform crashes.

**Diagnosis.**

```bash
curl -s localhost:8484/api/v1/system/disk | jq     # {status, paths:[{label,free,pct_free,status}]}
du -sh /config/thumbnails
du -sh /config/* | sort -h                           # biggest consumer
```

`/api/v1/stats` also carries a `disk` section; the Jobs page shows an amber
(warn) / red (critical) banner.

**How the guardrails behave.** Two floors, the more conservative winning: absolute
GB (warn 20 / critical 5) and percent-free (warn 10% / critical 2%). At **WARN**
producers keep writing; at **CRITICAL** thumbnail writes fail-closed (no retry
loop; serve path returns a 404 placeholder, never a 500), OCR is skipped, the
embedding-model download is refused, and — if `FILEARR_DISK_PG_PATH` is set —
extract pauses. Other queues and the workers stay alive.

**Recovery of a box that already filled.**

```bash
docker compose stop worker watcher     # 1. stop the write pressure (API stays read-only)
rm -rf /config/thumbnails/*             # 2. thumbnails are disposable — every byte regenerates
du -sh /config/* | sort -h              # 3. free a few GB for Postgres to breathe
docker compose up -d postgres           # 4. bring PG back
docker compose logs -f postgres         #    watch for "database system is ready" (PG replays WAL)
docker compose up -d worker watcher     # 5. restart workers
```

Do **not** delete Postgres data. Then re-trigger the failed thumbnails from the
Jobs page (or let the serve path regenerate them lazily).

**Prevent recurrence.** Raise the floors on a small volume
(`FILEARR_DISK_WARN_FREE_GB` / `FILEARR_DISK_MIN_FREE_GB`), set
`FILEARR_DISK_GC_TARGET_FREE_GB > 0` so the emergency GC LRU-evicts to a target,
grow the volume, and enable a low-space alert.

## Migration failures / Alembic state / stamping

**Symptom.** After an upgrade or restore the app errors on schema mismatch, or a
pre-Alembic database has no `alembic_version` table.

**Diagnosis.**

```bash
docker compose exec app alembic current    # what revision the DB is at
docker compose exec app alembic heads       # what the code expects
docker compose exec -T postgres psql -U filearr -d filearr \
  -c "SELECT version_num FROM alembic_version;"
```

**Fix.** `scripts/init_db.py` is idempotent and does the right thing in every case
— it detects a pre-Alembic DB, **stamps the baseline**, then upgrades to head,
applies the procrastinate schema, and ensures the Meili index exists:

```bash
docker compose run --rm app python scripts/init_db.py
```

For a manual upgrade only: `docker compose exec app alembic upgrade head`.
Requires Postgres 18+ (the baseline uses `uuidv7()` defaults). **Always take a
fresh dump before a downgrade** — downgrades are best-effort and can lose
recycle-bin data.

## The ltree bind-cast (42804) error class

**Symptom.** Item writes 500 with a Postgres `42804` (datatype mismatch) — e.g. an
agent push returns HTTP 500. Blast radius is every INSERT/UPDATE that writes an
item's path scope, and every path-grant creation.

**Diagnosis.**

```bash
curl -sk -X POST https://filearr.example.com/api/v1/... -d '<payload>'   # observe the 500
docker compose logs app | grep -i 42804                                   # confirm the class
```

**Cause.** The scope columns are real Postgres `ltree` columns (PG18 ships the
`ltree` contrib), but if they are ORM-mapped as plain text the driver renders a
`::VARCHAR` bind cast, and Postgres has no `varchar → ltree` assignment cast. This
was invisible in tests where the test Postgres shipped no contrib.

**Fix.** Upgrade to the version whose ORM binds the parameter as *unknown* (no
cast) and lets the server coerce it, with a text DDL fallback where the extension
is absent. Nothing operator-side beyond deploying it. **General lesson:** any
column backed by a Postgres **extension type** (`ltree`, `vector`, …) must not be
mapped as a plain scalar, or the driver emits a cast the server rejects.

## Agent enrollment / CA (step-ca) failures {#agent-enrollment-ca-step-ca-failures}

**Symptom.** An agent registers successfully but the register response's `ca_ott`
is **null**, so it can never fetch a certificate. By design a bad CA config never
takes registration down — it just yields a null token.

**Diagnosis — narrow it down.**

```bash
# 1. Is the CA healthy and reachable?
docker compose exec step-ca step ca health \
  --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt
docker compose logs step-ca | tail -50

# 2. Central emits an audit event per successful mint (jti only, never the token):
docker compose exec -T postgres psql -U filearr -d filearr \
  -c "SELECT event_type, details, ts FROM security_events
      WHERE event_type='agent_ca_ott_minted' ORDER BY ts DESC LIMIT 5;"
```

**Root causes seen live, and their fixes.**

- **`FILEARR_CA_PROVISIONER_JWK` unset or malformed.** Central signs the OTT with
  the provisioner's decrypted private JWK; without a valid one, `ca_ott` is null.
  Central validates the shape (EC P-256, private) on first use and logs only
  *that* it is unset/malformed — never the key.

- **JWK read from the wrong place (remote-management gotcha).** The compose stack
  enables remote management, so step-ca keeps provisioners in its **admin
  database**, not in `authority.provisioners` in `ca.json` — any procedure that
  edits/reads `ca.json` is a no-op. Confirm:

    ```bash
    docker compose exec step-ca cat /home/step/config/ca.json | grep -c provisioners  # 0 under remote mgmt
    ```

    The provisioner list (including the JWE-encrypted key) is instead served by the
    CA's public `/provisioners` endpoint (publishing the JWE is by design — only
    the password opens it). Extract and decrypt:

    ```bash
    ENC=$(curl -sk https://localhost:9000/provisioners \
      | python3 -c 'import json,sys; d=json.load(sys.stdin);
          print(next(p["encryptedKey"] for p in d.get("provisioners", d if isinstance(d,list) else [])
          if p.get("name")=="filearr-agents" and p.get("type")=="JWK"), end="")')
    printf '%s' "$ENC" | docker compose exec -T step-ca \
      step crypto jwe decrypt --password-file /home/step/secrets/password
    # -> {"kty":"EC","crv":"P-256","kid":"...","x":"...","y":"...","d":"..."}
    ```

    Paste the decrypted private JWK into `FILEARR_CA_PROVISIONER_JWK` (a secret —
    never commit it, never echo it) and recreate the app/worker.

- **Which password opens the JWE?** Under remote management the key is encrypted
  with the CA **administrative password**, printed **once** in the first-boot log
  — *not* the `secrets/password` (CA-key) password:

    ```bash
    docker compose logs step-ca | grep -i "password is"
    ```

    The deploy automation tries `secrets/password`, then `secrets/admin_password`,
    then recovers the log-printed password and persists it (mode 0600 in the CA
    volume) so recovery never again depends on log retention. If the first-boot
    log is gone and the password was never persisted, it is unrecoverable — rotate
    the provisioner key (`step ca provisioner update filearr-agents
    --private-key=…`) and put the new plaintext private JWK in `.env`.

- **Provisioner claims (cert lifetimes) not set.** Under remote management set
  them through the admin API:

    ```bash
    docker compose exec -T step-ca step ca provisioner update filearr-agents \
      --x509-min-dur=24h --x509-default-dur=48h --x509-max-dur=72h \
      --allow-renewal-after-expiry \
      --admin-subject=step --admin-provisioner=filearr-agents \
      --admin-password-file=/home/step/secrets/password \
      --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt
    ```

    `allow-renewal-after-expiry` lets a long-offline agent renew a just-expired
    cert over mTLS instead of re-enrolling.

**Recovery once the key is fixed.** Hand a fresh OTT to already-registered agents:
`POST /api/v1/agents/{id}/ca-ott` (admin; works for a pending or active agent;
`409` if revoked, `404` unknown, `503` if the JWK is still unconfigured).
Enrollment tokens are **single-use** — if a re-enroll fails with "token
consumed", mint a **new** token rather than reusing the old one.

## Orphaned pending agents; revoke vs delete

A failed enrollment leaves a **pending** agent (registered, no cert bound). To
clean up: **revoke** to deny it at the application layer while keeping its row and
history, or **hard delete** (`DELETE /api/v1/agents/{id}?purge=true`) to remove
the row entirely — refused (409) while any library/item still references it. See
[Agents → revoke vs delete](agents.md#killing-an-agent-revoke-vs-delete).

## TLS and ACME issuance failures

**Symptom.** Wildcard cert issuance hangs with `timed out waiting for record to
fully propagate` and no Cloudflare API errors above it.

**Cause / fix.** The TXT record published fine but the propagation self-check
can't see it — almost always **split-horizon DNS** (a LAN resolver answering
authoritatively for your domain hides the public `_acme-challenge` record from the
container). The shipped Caddyfile pins the check to public resolvers
(`1.1.1.1`, `8.8.8.8`) for exactly this — don't remove it on a homelab network.

**Related gotchas.**

- With split-horizon overrides, **every** hostname the box serves needs its own
  LAN override → container IP (`192.0.2.10`): `filearr.example.com`,
  `agents.example.com`, `ca.example.com`. A missing `ca.` override breaks agent
  cert renewal from inside the LAN.
- A scoped Cloudflare token needs **both** `Zone:Read` and `DNS:Edit` on the zone.
- `ca.example.com` must be **raw SNI/L4 passthrough** to step-ca — an L7
  terminator silently breaks `/renew` (which authenticates with the agent's client
  cert on the direct TLS connection).
- **LAN/homelab mode** (`FILEARR_TLS_MODE=internal`) needs no DNS/ACME/egress;
  Caddy mints a self-signed root. Trust it on clients:

    ```bash
    docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./filearr-root-ca.crt
    # import into each client's OS/browser trust store
    ```

## Alerting doesn't fire

**Symptom.** No notifications for scan failures, extract spikes, low disk, agent
offline/stall, or failed report deliveries.

**Fix.** All system rules ship **seeded, disabled, with no channel**. In Admin →
Alerts: create a channel (webhook / SMTP), attach it to the rule, and **enable**
the rule. Use the channel-row **Test** button to confirm delivery.

**Webhook specifics.** A Discord webhook rejects a generic body (`400 … Cannot
send an empty message`). Set the channel's payload format to `discord`
(auto-detected from a `discord.com/api/webhooks/…` URL) or `slack`; leave the HMAC
secret blank for those (they don't verify it). All other protections (SSRF
default-deny, no-redirect, bounded I/O) are identical.

**Agent alert thresholds.** *Agent offline* defaults to a generous 48h (offline is
normal for laptops); *replication stalled* is the sharper 6h signal (alive but not
draining its outbox). Tune with `FILEARR_AGENT_OFFLINE_ALERT_SECONDS` /
`FILEARR_AGENT_REPLICATION_STALL_ALERT_SECONDS`.

## Search index drift → rebuild-index

**Symptom.** Search results are missing items, stale, or (for scoped non-admin
users after an RBAC upgrade) empty. Postgres and Meilisearch have diverged.

**Diagnosis.** An hourly reconcile sweep detects divergence and never writes
Postgres — check the Jobs page / worker logs for its results.

**Fix — always safe** (Meili is a disposable projection):

```bash
curl -X POST http://localhost:8484/api/v1/system/rebuild-index
```

The rebuild uses a **shadow-index swap**, so live search stays up. A rebuild is
**required** after any upgrade that changes indexed fields — notably enabling
path-scoped RBAC search (until the rebuild finishes, scoped non-admin users fail
*closed* to empty results; admins/API keys are unaffected). A crashed rebuild can
leave an orphaned shadow index; an hourly reaper deletes shadows older than
`FILEARR_MEILI_SHADOW_MAX_AGE_HOURS` (6h), never touching a young in-flight one.

## Recycle-bin / tombstone recovery {#recycle-bin-tombstone-recovery}

**Model.** Scans never hard-delete. A file gone from disk is tombstoned `missing`;
a user-deleted item becomes `trashed` (awaiting recycle-bin purge). Only `active`
items appear in search and browse.

**Recovery.**

- A **`missing`** item returns to `active` automatically the next time a scan sees
  the file again (identity is `(library, rel_path)`, so it re-attaches — no manual
  step).
- A **`trashed`** item is recoverable until the recycle-bin purge removes it.
  Retention is `FILEARR_RECYCLE_RETENTION_DAYS` (default **30**); the purge runs
  daily.
- Inspect what is recoverable:

    ```sql
    SELECT status, count(*) FROM items GROUP BY status;
    SELECT id, rel_path, status, deleted_at FROM items
    WHERE status='trashed' ORDER BY deleted_at DESC LIMIT 50;
    ```

- **Buy time before a purge:** raise `FILEARR_RECYCLE_RETENTION_DAYS` and restart
  before the daily purge runs.

## Backup and restore {#backup-and-restore}

**What must be backed up: Postgres only.** It holds `user_metadata` edits, tags,
custom fields, saved searches, alert config/history, libraries/schedules,
provenance, the extracted metadata, and the job queue. Meilisearch and the
thumbnail cache are disposable projections — deliberately **not** backed up
(rebuilt from Postgres). Media is read-only source on your NAS — back it up with
your existing NAS strategy.

**Back up:**

```bash
cd /opt/filearr
docker compose exec -T postgres pg_dump -U filearr -Fc filearr \
  > filearr-$(date -u +%Y%m%dT%H%M%SZ).dump
```

`-Fc` = compressed custom format (selective/parallel `pg_restore`); `-T` (no TTY)
is required through `docker compose exec`. **Copy the dump off-box** — a backup on
the same disk isn't a backup. `scripts/backup.sh` wraps this (timestamped dump
into `./config/backups/`, prunes to the newest 7; `BACKUP_KEEP` overrides).
Schedule it, e.g. from the Proxmox host crontab:

```bash
30 3 * * *  pct exec <your-vmid> -- bash /opt/filearr/scripts/backup.sh >> /var/log/filearr-backup.log 2>&1
```

**Restore:**

```bash
cd /opt/filearr
# 1. fresh stack, SAME .env (POSTGRES_PASSWORD, FILEARR_DATABASE_URL, MEILI_MASTER_KEY)
docker compose up -d postgres
docker compose exec -T postgres pg_isready -U filearr
# 2. load the dump (--clean --if-exists makes it re-runnable)
docker compose exec -T postgres \
  pg_restore -U filearr -d filearr --clean --if-exists --no-owner < filearr-YYYYmmddTHHMMSSZ.dump
# 3. bring schema to head (idempotent)
docker compose run --rm app python scripts/init_db.py
# 4. start everything and rebuild the search index (Meili was never backed up)
docker compose up -d
curl -X POST http://localhost:8484/api/v1/system/rebuild-index
```

Thumbnails regenerate lazily on first view (or pre-warm with a scan). **Verify a
dump before you trust it** by restoring into a throwaway `postgres:18.4` container
and counting rows (`SELECT count(*) FROM items;`).

**Why Meilisearch needs no backup:** it is a projection, fully rebuildable from
Postgres in one `rebuild-index` call.

**Source of truth for code:** the repository itself is the backup for the
application; the project's history is kept in a git bundle that you clone rather
than initializing a repo on a network share (SMB corrupts git lock/rename ops).

## Enabling authentication

Auth is **off by default** (`FILEARR_AUTH_ENABLED=false`). Turning it on is
additive and zero-downtime — existing Bearer API keys keep working alongside
cookie sessions.

1. **Serve over TLS first** (the session cookie is `Secure` only over HTTPS;
   behind Caddy, start uvicorn with `--proxy-headers`).
2. Set `FILEARR_AUTH_ENABLED=true`, restart the app.
3. Bootstrap the first admin (once only; returns 409 after any user exists — and
   the last admin can't be deleted, so you can't lock yourself out):

    ```bash
    curl -X POST https://filearr.example.com:8443/api/v1/auth/bootstrap \
      -H 'Content-Type: application/json' \
      -d '{"username":"admin","password":"a-strong-passphrase"}'
    ```

**Break-glass (SSO/LDAP lockout).** The first admin is always local. If an IdP is
down or a role map locks everyone out, set `FILEARR_OIDC_ENABLED=false` (and/or
`FILEARR_LDAP_ENABLED=false`), restart, and log in with the local admin — the
local password path is always available.

**Locked out by rate limiting (429).** Inspect/clear locks:

```sql
SELECT * FROM auth_rate_limits ORDER BY locked_until DESC NULLS LAST LIMIT 20;
```

The auth audit trail is in `security_events` (`GET /api/v1/audit`, admin scope).
