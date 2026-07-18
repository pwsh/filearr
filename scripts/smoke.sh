#!/usr/bin/env bash
# smoke.sh — post-deploy sanity check for a running Filearr stack (OPS-T2).
#
# Hits the four load-bearing read endpoints and asserts a healthy shape. Prints a
# clear PASS/FAIL line per check and exits NON-ZERO if any check fails, so it
# doubles as a deploy gate (deploy-proxmox.sh runs it inside the CT after the
# build-stamp verification) and a manual "is it actually up?" tool.
#
# Usage:
#   scripts/smoke.sh [BASE_URL]
#     BASE_URL   default http://localhost:8000  (e.g. http://<ct-ip>:8484,
#                or an https URL — TLS cert is validated loosely, see -k below)
#
# Env:
#   FILEARR_SMOKE_TOKEN   optional Bearer token; sent when auth is enabled
#                         (/stats and /search require the `read` scope). Not
#                         needed when FILEARR_AUTH_ENABLED=false (deploy default).
#   SMOKE_TIMEOUT         per-request timeout seconds (default 15)
#
# HTTPS note: curl runs with -k (insecure) so the check works against Caddy's
# self-signed internal-CA cert without needing the LAN CA trusted on the box
# running the smoke test. It validates REACHABILITY + response shape, not the
# cert chain (trust verification is a separate manual step, see docs/ops).

set -uo pipefail

BASE_URL="${1:-http://localhost:8000}"
BASE_URL="${BASE_URL%/}"   # strip trailing slash
TIMEOUT="${SMOKE_TIMEOUT:-15}"
TOKEN="${FILEARR_SMOKE_TOKEN:-}"

pass=0 fail=0
green() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
red()   { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
ok()   { green "$*"; pass=$((pass+1)); }
bad()  { red   "$*"; fail=$((fail+1)); }

# fetch <path> -> sets HTTP_CODE and BODY globals
fetch() {
  local path="$1"; local hdr=()
  [[ -n "$TOKEN" ]] && hdr=(-H "Authorization: Bearer ${TOKEN}")
  local raw rc
  raw=$(curl -sk --max-time "$TIMEOUT" "${hdr[@]}" -w $'\n%{http_code}' "${BASE_URL}${path}" 2>/dev/null)
  rc=$?
  HTTP_CODE="${raw##*$'\n'}"
  BODY="${raw%$'\n'*}"
  [[ "$rc" -eq 0 ]] || HTTP_CODE="000"
  return 0
}

echo "── Filearr smoke test → ${BASE_URL} ──"

# 1) /health — 200 + status ok
fetch "/api/v1/health"
if [[ "$HTTP_CODE" == "200" ]] && grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$BODY"; then
  ok "/api/v1/health — 200, status ok"
else
  bad "/api/v1/health — HTTP ${HTTP_CODE}, body: ${BODY:0:120}"
fi

# 2) /version — 200 + non-null build_stamp (echo it)
fetch "/api/v1/version"
STAMP=$(sed -n 's/.*"build_stamp"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"$BODY")
if [[ "$HTTP_CODE" == "200" && -n "$STAMP" ]]; then
  ok "/api/v1/version — 200, build_stamp=${STAMP}"
elif [[ "$HTTP_CODE" == "200" ]]; then
  bad "/api/v1/version — 200 but build_stamp is null (dev checkout, not a deployed image?)"
else
  bad "/api/v1/version — HTTP ${HTTP_CODE}, body: ${BODY:0:120}"
fi

# 3) /stats — 200 + meili.healthy true
fetch "/api/v1/stats"
if [[ "$HTTP_CODE" == "200" ]] && grep -Eq '"healthy"[[:space:]]*:[[:space:]]*true' <<<"$BODY"; then
  ok "/api/v1/stats — 200, meili.healthy true"
else
  bad "/api/v1/stats — HTTP ${HTTP_CODE}, meili not healthy. body: ${BODY:0:160}"
fi

# 4) /search — 200 for a trivial query
fetch "/api/v1/search?q=smoke&limit=1"
if [[ "$HTTP_CODE" == "200" ]]; then
  ok "/api/v1/search?q=smoke — 200"
else
  bad "/api/v1/search?q=smoke — HTTP ${HTTP_CODE}, body: ${BODY:0:120}"
fi

echo "── ${pass} passed, ${fail} failed ──"
[[ "$fail" -eq 0 ]] || { echo "SMOKE FAILED" >&2; exit 1; }
echo "SMOKE PASSED"
