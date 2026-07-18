#!/usr/bin/env bash
# deploy-test.sh — push the filearr stack to an Unraid server for testing.
#
# Run from any machine with SSH access to the server (WSL/macOS/Linux),
# or directly on the Unraid terminal with -H localhost.
#
#   ./deploy-test.sh -H tower [-u root] [-d /mnt/user/appdata/filearr-src] \
#                    [-m /mnt/user/data/media] [--no-templates] [--down]
#
# What it does:
#   1. rsync the project to the server (excludes junk)
#   2. stage unraid/*.xml into /boot/config/plugins/dockerMan/templates-user/
#      (visible in Docker tab -> Add Container for later template-based installs)
#   3. create the 'filearr' docker network if missing
#   4. generate .env with random secrets on first deploy
#   5. docker compose up -d --build   (builds the app image on the server)
#   6. first-run DB/queue/index bootstrap (scripts/init_db.py)

set -euo pipefail

HOST="" USER_="root" DEST="/mnt/user/appdata/filearr-src"
MEDIA="/mnt/user/data/media" TEMPLATES=1 DOWN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -H) HOST="$2"; shift 2 ;;
    -u) USER_="$2"; shift 2 ;;
    -d) DEST="$2"; shift 2 ;;
    -m) MEDIA="$2"; shift 2 ;;
    --no-templates) TEMPLATES=0; shift ;;
    --down) DOWN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$HOST" ]] || { echo "usage: $0 -H <unraid-host> [-u root] [-d dest] [-m media-path]"; exit 1; }

SSH="ssh ${USER_}@${HOST}"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [[ "$DOWN" == 1 ]]; then
  $SSH "cd '$DEST' && docker compose down"
  echo "stack stopped."
  exit 0
fi

echo "==> 1/6 sync project to ${HOST}:${DEST}"
$SSH "mkdir -p '$DEST'"
rsync -az --delete \
  --exclude .git --exclude node_modules --exclude .venv --exclude dist \
  --exclude __pycache__ --exclude config --exclude .env \
  "$SRC/" "${USER_}@${HOST}:${DEST}/"

if [[ "$TEMPLATES" == 1 ]]; then
  echo "==> 2/6 stage Unraid templates (Docker tab -> Add Container -> template dropdown)"
  $SSH "cp '$DEST'/unraid/*.xml /boot/config/plugins/dockerMan/templates-user/ 2>/dev/null || true"
else
  echo "==> 2/6 skipped (--no-templates)"
fi

echo "==> 3/6 ensure 'filearr' docker network"
$SSH "docker network inspect filearr >/dev/null 2>&1 || docker network create filearr"

echo "==> 4/6 .env (generated with random secrets on first deploy)"
$SSH "cd '$DEST' && if [[ ! -f .env ]]; then
  PG_PW=\$(openssl rand -hex 16)
  MEILI_KEY=\$(openssl rand -hex 16)
  {
    echo \"POSTGRES_PASSWORD=\$PG_PW\"
    echo \"MEILI_MASTER_KEY=\$MEILI_KEY\"
    echo \"FILEARR_DATABASE_URL=postgresql+psycopg://filearr:\$PG_PW@postgres:5432/filearr\"
    echo \"FILEARR_PROCRASTINATE_DSN=postgresql://filearr:\$PG_PW@postgres:5432/filearr\"
    echo \"FILEARR_MEILI_URL=http://meilisearch:7700\"
    echo \"FILEARR_MEILI_MASTER_KEY=\$MEILI_KEY\"
    echo \"FILEARR_AUTH_ENABLED=false\"   # test mode: no API keys needed on LAN
    echo \"MEDIA_PATH=$MEDIA\"
    echo \"PUID=99\"; echo \"PGID=100\"; echo \"TZ=\$(cat /etc/timezone 2>/dev/null || echo UTC)\"
  } > .env
  echo '    .env created'
else echo '    .env exists, keeping it'; fi"

echo "==> 5/6 build + start stack (first build takes a few minutes)"
$SSH "cd '$DEST' && docker compose up -d --build"

echo "==> 6/6 bootstrap database / job queue / search index (idempotent)"
$SSH "cd '$DEST' && docker compose run --rm app python scripts/init_db.py"

echo
echo "done. UI: http://${HOST}:8484  ·  API docs: http://${HOST}:8484/api/docs"
echo "add a library:  curl -X POST http://${HOST}:8484/api/v1/libraries \\"
echo "  -H 'Content-Type: application/json' \\"
echo "  -d '{\"name\":\"media\",\"root_path\":\"/data/media\"}'"
echo "then trigger a scan from the response id: POST /api/v1/libraries/{id}/scan"
echo
echo "teardown: $0 -H $HOST --down"
