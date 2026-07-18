#!/usr/bin/env bash
# deploy-local.sh — run DIRECTLY on the Unraid terminal (no SSH).
#
# 1. Get the filearr folder onto the server first (SMB copy to a share,
#    e.g. /mnt/user/appdata/filearr-src, or git clone once the repo exists).
# 2. From the Unraid web terminal:
#      cd /mnt/user/appdata/filearr-src
#      bash deploy-local.sh [-m /mnt/user/data/media] [--no-templates] [--down]
#
# Steps: stage Unraid XML templates -> ensure docker network -> generate .env
#        -> docker compose up -d --build -> bootstrap DB/queue/index.

set -euo pipefail

MEDIA="/mnt/user/data/media" TEMPLATES=1 DOWN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -m) MEDIA="$2"; shift 2 ;;
    --no-templates) TEMPLATES=0; shift ;;
    --down) DOWN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

cd "$(dirname "$0")"

# compose CLI: prefer 'docker compose' (v2); fall back to docker-compose;
# neither -> Compose Manager plugin is required (Apps tab).
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "ERROR: docker compose not found."
  echo "Install the 'Docker Compose Manager' plugin from the Apps tab, then re-run."
  exit 1
fi

if [[ "$DOWN" == 1 ]]; then
  $DC down
  echo "stack stopped."
  exit 0
fi

if [[ "$TEMPLATES" == 1 && -d /boot/config/plugins/dockerMan/templates-user ]]; then
  echo "==> 1/5 staging Unraid templates (Docker tab -> Add Container -> template dropdown)"
  cp unraid/*.xml /boot/config/plugins/dockerMan/templates-user/ 2>/dev/null || true
else
  echo "==> 1/5 template staging skipped"
fi

echo "==> 2/5 ensure 'filearr' docker network"
docker network inspect filearr >/dev/null 2>&1 || docker network create filearr

echo "==> 3/5 .env (random secrets generated on first run)"
if [[ ! -f .env ]]; then
  PG_PW=$(openssl rand -hex 16)
  MEILI_KEY=$(openssl rand -hex 16)
  {
    echo "POSTGRES_PASSWORD=$PG_PW"
    echo "MEILI_MASTER_KEY=$MEILI_KEY"
    echo "FILEARR_DATABASE_URL=postgresql+psycopg://filearr:$PG_PW@postgres:5432/filearr"
    echo "FILEARR_PROCRASTINATE_DSN=postgresql://filearr:$PG_PW@postgres:5432/filearr"
    echo "FILEARR_MEILI_URL=http://meilisearch:7700"
    echo "FILEARR_MEILI_MASTER_KEY=$MEILI_KEY"
    echo "FILEARR_AUTH_ENABLED=false"   # test mode: no API keys needed on LAN
    echo "MEDIA_PATH=$MEDIA"
    echo "PUID=99"
    echo "PGID=100"
    echo "TZ=$(cat /etc/timezone 2>/dev/null || echo UTC)"
  } > .env
  echo "    .env created"
else
  echo "    .env exists, keeping it"
fi

echo "==> 4/5 build + start stack (first build takes a few minutes)"
$DC up -d --build

echo "==> 5/5 bootstrap database / job queue / search index (idempotent)"
$DC run --rm app python scripts/init_db.py

IP=$(ip -4 addr show br0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)
IP=${IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}
echo
echo "done. UI: http://${IP:-<server-ip>}:8484  ·  API docs: http://${IP:-<server-ip>}:8484/api/docs"
echo "add a library:"
echo "  curl -X POST http://localhost:8484/api/v1/libraries \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"name\":\"media\",\"root_path\":\"/data/media\"}'"
echo "then: curl -X POST http://localhost:8484/api/v1/libraries/<id>/scan"
echo
echo "teardown: bash deploy-local.sh --down"
