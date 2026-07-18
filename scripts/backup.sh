#!/usr/bin/env bash
# backup.sh — Postgres logical backup for a compose-deployed Filearr stack (OPS-T3).
#
# Postgres is the SOURCE OF TRUTH (user_metadata, tags, custom fields, saved
# searches, alert config, provenance/audit). Meilisearch and the thumbnail cache
# are DISPOSABLE projections rebuilt from Postgres — they are NOT backed up here
# by design (see docs/ops/backup.md).
#
# Writes a compressed custom-format dump (pg_dump -Fc) into <config>/backups with
# a UTC timestamp, then prunes to the newest N. Idempotent, safe to run from cron
# or `pct exec`. Restore procedure: docs/ops/backup.md.
#
# Usage:   scripts/backup.sh
# Env:
#   FILEARR_DIR    compose project dir (default /opt/filearr, else script's ..)
#   BACKUP_DIR     output dir (default ${FILEARR_DIR}/config/backups)
#   BACKUP_KEEP    how many dumps to retain (default 7)
#   PG_SERVICE     compose service name for Postgres (default postgres)
#   PG_USER/PG_DB  role/db (default filearr/filearr)

set -euo pipefail
set -E
trap 'rc=$?; echo "✗ BACKUP FAILED (exit $rc) at line $LINENO: $BASH_COMMAND" >&2' ERR

SELF_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FILEARR_DIR="${FILEARR_DIR:-$( [[ -f /opt/filearr/docker-compose.yml ]] && echo /opt/filearr || echo "$SELF_DIR" )}"
BACKUP_DIR="${BACKUP_DIR:-${FILEARR_DIR}/config/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PG_USER="${PG_USER:-filearr}"
PG_DB="${PG_DB:-filearr}"

command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
cd "$FILEARR_DIR" || { echo "compose dir not found: $FILEARR_DIR" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="${BACKUP_DIR}/filearr-${ts}.dump"

echo "==> checking Postgres is ready"
docker compose exec -T "$PG_SERVICE" pg_isready -U "$PG_USER" >/dev/null

echo "==> dumping ${PG_DB} -> ${out}"
# -Fc: compressed custom format (parallel/selective restore via pg_restore).
# Stream to a temp file, then atomically rename so a crash never leaves a
# half-written dump that looks complete to the retention prune.
docker compose exec -T "$PG_SERVICE" pg_dump -U "$PG_USER" -Fc "$PG_DB" > "${out}.partial"
mv "${out}.partial" "$out"
size="$(du -h "$out" | cut -f1)"
echo "    wrote ${out} (${size})"

echo "==> pruning to newest ${BACKUP_KEEP} dump(s)"
# Newest-first; delete everything past the keep count. NUL-safe-ish: our own
# filenames never contain spaces/newlines (timestamped), so plain ls is fine.
mapfile -t dumps < <(ls -1t "${BACKUP_DIR}"/filearr-*.dump 2>/dev/null || true)
if (( ${#dumps[@]} > BACKUP_KEEP )); then
  for old in "${dumps[@]:BACKUP_KEEP}"; do
    echo "    removing old backup: $old"
    rm -f "$old"
  done
fi

echo "✓ backup complete: ${out}"
