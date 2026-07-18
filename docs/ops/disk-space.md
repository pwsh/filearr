# Ops runbook — disk space (FIX-11)

Filearr guards against a full filesystem taking the platform down (the live
2026-07 incident: thumbnails filled `/config`, Postgres crashed). This page is
the operator's reference for the thresholds, what pauses when, and how to recover.

## What is monitored

`os.statvfs` on, by default:

* the thumbnail cache dir (`{FILEARR_CONFIG_DIR}/thumbnails`),
* the tmp dir (`$TMPDIR`, OCR rasterisation + atomic-write staging),
* the Postgres data dir **only if** `FILEARR_DISK_PG_PATH` is set to a path the
  app/worker container can see.

Override the whole list with `FILEARR_DISK_WATCH_PATHS` (JSON).

> **Single-volume deploys (the Proxmox LXC):** `/config`, the Postgres volume and
> the Meili store all sit on one filesystem, so watching the thumbnail dir already
> covers Postgres. In the split-container compose stack Postgres has its own
> volume that the app cannot statvfs — set `FILEARR_DISK_PG_PATH` inside the
> Postgres container's world if you want the extract PG-pause, otherwise it stays
> inert.

## Thresholds (whichever floor is more conservative wins)

| Level | Absolute | Percent |
|---|---|---|
| **warn** | `< FILEARR_DISK_WARN_FREE_GB` (20 GB) | `< FILEARR_DISK_WARN_PCT_FREE` (10%) |
| **critical** | `< FILEARR_DISK_MIN_FREE_GB` (5 GB) | `< FILEARR_DISK_CRIT_PCT_FREE` (2%) |

## What happens at each level

**WARN** — producers keep writing (log once/path/hour). The 5-minutely monitor
fires the **"System: low disk space"** ops alert (if enabled + a channel is
attached) and the Jobs page shows an amber banner. Act now — free space.

**CRITICAL** — fail-closed:

* **Thumbnails** are refused at the write (`disk_full_guard`); running
  `thumb_item` jobs fail (no retry loop), inline serve returns 404 → placeholder.
  This is the de-facto "thumbnail queue paused": no bytes are written.
* **OCR** is skipped (extract still records the file).
* **Embedding model download** is refused.
* **Extract** pauses (reschedules) *iff* `FILEARR_DISK_PG_PATH` is set and that
  volume is critical — so we stop growing Postgres on a dying disk.
* The monitor runs an **emergency thumbnail GC** (orphan sweep; plus LRU eviction
  of valid thumbnails if `FILEARR_DISK_GC_TARGET_FREE_GB > 0`).
* The ops alert escalates to critical; the Jobs banner turns red.

Other queues (scan diff, index sync, maintenance) keep running; workers stay
alive throughout.

## Enable the low-space alert

The rule is seeded **disabled** (like all `is_system` rules). In the Alerts page:
enable **"System: low disk space"** and attach a channel (webhook / email /
apprise). Until then the monitor still logs + drives the banner, but sends no
external notification.

## Recovering a box that already filled up (post-incident)

1. **Stop the write pressure.** `docker compose stop worker watcher` (or scale
   the worker to 0). The API can stay up read-only.
2. **Reclaim the easy space first — thumbnails are disposable (invariant 1):**
   ```bash
   # inspect
   du -sh /config/thumbnails
   # nuke the cache entirely — every byte regenerates on demand
   rm -rf /config/thumbnails/*
   ```
   (Or, less blunt, let the daily/emergency GC run once space allows.)
3. **Free enough for Postgres to breathe** (a few GB). Check other `/config`
   consumers: `du -sh /config/* | sort -h`. The Meili store and the embed model
   cache (`/config/models`, ~130 MB) also live here.
4. **Bring Postgres back cleanly.** If it crashed mid-write:
   `docker compose up -d postgres` and watch `docker compose logs -f postgres`
   until `database system is ready`. PG recovers its WAL automatically; do **not**
   delete anything under the PG data dir.
5. **Restart workers.** `docker compose up -d worker watcher`. On boot the worker
   logs the disk status; if still critical, thumbnails remain fail-closed.
6. **Re-trigger any thumbnails that failed** with `disk_full_guard` from the Jobs
   page (or just let the serve-path lazily regenerate them), and clear the failed
   list.
7. **Prevent recurrence:** raise the floors if the volume is small
   (`FILEARR_DISK_WARN_FREE_GB` / `FILEARR_DISK_MIN_FREE_GB`), set
   `FILEARR_DISK_GC_TARGET_FREE_GB` so the emergency GC LRU-evicts to a target,
   grow the volume, and enable the low-space alert channel.

## Quick check

```bash
curl -s localhost:8484/api/v1/system/disk | jq
```
