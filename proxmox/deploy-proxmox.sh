#!/usr/bin/env bash
# deploy-proxmox.sh — deploy the filearr stack into a Docker-enabled LXC on Proxmox VE,
# with remote storage (SMB/CIFS, FTP, SFTP, WebDAV, NFS, local) mounted INSIDE the LXC
# so the deployment does not depend on host-side mounts.
#
# Run on the PROXMOX HOST shell (as root), from inside the filearr project folder:
#     bash proxmox/deploy-proxmox.sh                first run -> wizard, then deploy;
#                                                   later runs -> redeploy with saved defaults
#     bash proxmox/deploy-proxmox.sh --reconfigure  re-run the wizard
#     bash proxmox/deploy-proxmox.sh --storages     re-run only the storage definitions
#     bash proxmox/deploy-proxmox.sh --status       CT + mounts + stack status
#     bash proxmox/deploy-proxmox.sh --destroy      stop & delete the container
#
# Storage design (all inside the CT):
#   smb/cifs, ftp, sftp, webdav  -> rclone FUSE mounts (userspace — works in an
#                                   UNPRIVILEGED CT with the fuse=1 feature)
#   nfs                          -> kernel mount; requires a PRIVILEGED CT
#                                   (script switches automatically and warns)
#   local                        -> host path bind-mounted via pct (only type
#                                   that touches the host)
#   Each storage mounts read-only at /data/media/<name> inside the CT; docker
#   compose binds /data/media into app+worker. systemd units mount storages at
#   boot, ordered Before=docker.service so containers always see them.
#
# Persistence: answers -> ~/.config/filearr/deploy.conf ; storage definitions
# (incl. credentials) -> ~/.config/filearr/storages.env (chmod 600). Both are
# re-applied on every redeploy, so the CT is fully disposable.

set -euo pipefail
set -E  # ERR trap inherits into functions/subshells

# Any command failing under `set -e` used to kill the script SILENTLY —
# several "successful-looking" no-op deploys were exactly this. Now every
# unexpected exit names the line and command, and major steps are banners.
trap 'rc=$?; echo; echo "✗ DEPLOY FAILED (exit $rc) at line $LINENO: $BASH_COMMAND" >&2; echo "  Rerun with:  bash -x $0 ...  for a full trace." >&2' ERR

step() { echo; echo "═══ STEP: $* ═══"; }

CONF_DIR="${HOME}/.config/filearr"
CONF="${CONF_DIR}/deploy.conf"
# set -u hardening: the Cloudflare token is only ever read interactively (and
# deliberately never persisted to deploy.conf), so on non-interactive or
# keep-current redeploys it must still exist as a (possibly empty) variable.
CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}"

# Dirty-tree guard (live incident 2026-07-17): this script deploys whatever is
# in the source tree — including half-finished, unverified work if a deploy
# races an in-flight change. When the tree is a git repo and has uncommitted
# changes, demand explicit confirmation (skippable via FILEARR_DEPLOY_ALLOW_DIRTY=1
# for deliberate hotfix pushes).
warn_dirty_tree() {
  local project_dir="$1"
  command -v git >/dev/null 2>&1 || return 0
  git -C "$project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  local dirty
  dirty="$(git -C "$project_dir" status --porcelain 2>/dev/null | head -20)"
  [ -z "$dirty" ] && return 0
  [ "${FILEARR_DEPLOY_ALLOW_DIRTY:-0}" = "1" ] && { echo "[deploy] WARNING: deploying a DIRTY tree (FILEARR_DEPLOY_ALLOW_DIRTY=1)"; return 0; }
  echo
  echo "!! The source tree has UNCOMMITTED changes — you may be deploying"
  echo "!! half-finished, unverified work:"
  echo "$dirty" | sed 's/^/!!   /'
  if [ -t 0 ]; then
    read -r -p "Deploy the dirty tree anyway? [y/N] " _ans
    [[ "$_ans" == "y" || "$_ans" == "Y" ]] || die "aborted: commit first, or set FILEARR_DEPLOY_ALLOW_DIRTY=1"
  else
    die "refusing non-interactive dirty-tree deploy (set FILEARR_DEPLOY_ALLOW_DIRTY=1 to override)"
  fi
}
STORAGES_ENV="${CONF_DIR}/storages.env"   # NAME|TYPE|HOST|SHARE_OR_PATH|USER|PASS|PORT|DOMAIN
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CT_APP_DIR="/opt/filearr"
CT_MEDIA_ROOT="/data/media"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found — run this on the Proxmox host"; }
ask() { local p="$1" d="${2:-}"; if [[ -n "$d" ]]; then read -r -p "$p [$d]: " REPLY; REPLY="${REPLY:-$d}"; else read -r -p "$p: " REPLY; fi }

# OPS-T7: emit a value as a JSON string (escape backslash + doublequote; share
# names may carry spaces which need no escaping inside a JSON string).
json_str() {
  local s="$1"
  s="${s//\\/\\\\}"   # \ -> \\
  s="${s//\"/\\\"}"     # " -> \"
  printf '"%s"' "$s"
}

save_conf() {
  mkdir -p "$CONF_DIR"
  cat > "$CONF" <<EOF
VMID_START=$VMID_START
VMID=$VMID
HOSTNAME_=$HOSTNAME_
BRIDGE=$BRIDGE
IP_MODE=$IP_MODE
IP_CIDR=$IP_CIDR
GATEWAY=$GATEWAY
STORAGE=$STORAGE
DISK_GB=$DISK_GB
CORES=$CORES
MEMORY_MB=$MEMORY_MB
WEB_PORT=$WEB_PORT
WEB_TLS_PORT=$WEB_TLS_PORT
PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-}
TLS_MODE=${TLS_MODE:-internal}
TLS_DOMAIN=${TLS_DOMAIN:-}
ACME_EMAIL=${ACME_EMAIL:-}
AGENTS_ENABLED=${AGENTS_ENABLED:-no}
AGENTS_CA_URL=${AGENTS_CA_URL:-}
THUMBS_STORAGE=${THUMBS_STORAGE:-}
THUMBS_SIZE_GB=${THUMBS_SIZE_GB:-64}
TEMPLATE=$TEMPLATE
PRIVILEGED=$PRIVILEGED
EOF
  chmod 600 "$CONF"
  echo "saved defaults -> $CONF"
}

# ---------- public base URL (export/report link prefix) ----------
# Persisted in deploy.conf as PUBLIC_BASE_URL and written to the CT .env as
# FILEARR_PUBLIC_BASE_URL. We distinguish "never asked" (key ABSENT from the
# conf -> shell var UNSET -> prompt once) from "answered blank" (key PRESENT
# but empty -> var SET to "" -> never prompt again, relative links). A saved
# entry (even blank) is never re-asked, incl. on --reconfigure / non-interactive.
persist_public_base_url() {
  mkdir -p "$CONF_DIR"; touch "$CONF"
  local tmp; tmp="$(mktemp)"
  grep -v '^PUBLIC_BASE_URL=' "$CONF" 2>/dev/null > "$tmp" || true
  printf 'PUBLIC_BASE_URL=%s\n' "$PUBLIC_BASE_URL" >> "$tmp"
  mv "$tmp" "$CONF"
  chmod 600 "$CONF"
}

ensure_public_base_url() {
  # already answered (even if blank) -> never re-ask
  [[ -n "${PUBLIC_BASE_URL+set}" ]] && return 0
  if [[ ! -t 0 ]]; then
    # no controlling terminal to prompt (CI/piped): default blank + persist so
    # the value is stable and the wizard never blocks. Edit $CONF to change it.
    PUBLIC_BASE_URL=""
    echo "note: FILEARR_PUBLIC_BASE_URL unset and no TTY - defaulting to blank (relative links)."
    persist_public_base_url
    return 0
  fi
  local ans=""
  while true; do
    # This runs on the HOST before any pct-exec section, so fd 3 is untouched.
    read -r -p "Public base URL for export/report links, e.g. https://filearr.lan:8443 - leave blank for relative links: " ans || ans=""
    if [[ -z "$ans" || "$ans" =~ ^https?:// ]]; then
      PUBLIC_BASE_URL="$ans"; break
    fi
    echo "  must start with http:// or https:// (or leave blank) - try again"
  done
  persist_public_base_url
}

# ---------- dedicated thumbnail volume (filesystem crash isolation) ----------
# Persisted in deploy.conf as THUMBS_STORAGE / THUMBS_SIZE_GB and applied at CT
# CREATE as a pct mount point on ${CT_APP_DIR}/config/thumbnails. Motivation: a
# LIVE INCIDENT (thumbnail generation filled /config and crashed Postgres —
# backend FIX-11) plus the same isolation later hand-applied to a live CT. The
# mount is CT config, NOT compose state: the existing ./config:/config bind
# carries the submount into every container, the source push preserves config/,
# and deploy_stack's override regeneration never touches it. Blank
# THUMBS_STORAGE = share the rootfs (previous behaviour). Prompt-once contract
# mirrors ensure_public_base_url; to change later edit $CONF — a change applies
# only at CT CREATE (for a live CT: stack down, mv thumbnails aside,
# `pct set <vmid> -mp<N> <storage>:<gb>,mp=<path>,backup=0`, reboot, copy back).
# Sizing note: grid thumbs cap at 20 KB/item — a 1M-item catalog can reach
# ~20 GB, and the disk monitor WARNs below 20 GB free per filesystem, so the
# 64 GB default is deliberate; smaller volumes boot straight into WARN.
persist_thumbs_config() {
  mkdir -p "$CONF_DIR"; touch "$CONF"
  local tmp; tmp="$(mktemp)"
  grep -vE '^(THUMBS_STORAGE|THUMBS_SIZE_GB)=' "$CONF" 2>/dev/null > "$tmp" || true
  printf 'THUMBS_STORAGE=%s\n' "${THUMBS_STORAGE:-}" >> "$tmp"
  printf 'THUMBS_SIZE_GB=%s\n' "${THUMBS_SIZE_GB:-64}" >> "$tmp"
  mv "$tmp" "$CONF"; chmod 600 "$CONF"
}

ensure_thumbs_config() {
  # already answered (even if blank = share rootfs) -> never re-ask
  [[ -n "${THUMBS_STORAGE+set}" ]] && return 0
  if [[ ! -t 0 ]]; then
    THUMBS_STORAGE=""; THUMBS_SIZE_GB="${THUMBS_SIZE_GB:-64}"
    echo "note: THUMBS_STORAGE unset and no TTY - thumbnails share the rootfs (edit $CONF to isolate)."
    persist_thumbs_config
    return 0
  fi
  echo
  echo "── thumbnail volume ──"
  echo "  A dedicated volume mounted at ${CT_APP_DIR}/config/thumbnails: a full"
  echo "  thumbnail cache then stops thumbnail writes WITHOUT starving Postgres/"
  echo "  Meilisearch on the rootfs (this exact failure crashed a live deploy)."
  echo "Available storage:"; pvesm status | awk 'NR>1{print "  - "$1" ("$2")"}'
  while true; do
    ask "Thumbnail volume storage (blank = share the rootfs)" "${THUMBS_STORAGE:-}"
    THUMBS_STORAGE="$REPLY"
    [[ -z "$THUMBS_STORAGE" ]] && break
    pvesm status 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$THUMBS_STORAGE" && break
    echo "  '$THUMBS_STORAGE' is not a storage pvesm knows - try again (or blank to skip)"
  done
  if [[ -n "$THUMBS_STORAGE" ]]; then
    while true; do
      ask "  thumbnail volume size GB" "${THUMBS_SIZE_GB:-64}"
      [[ "$REPLY" =~ ^[0-9]+$ ]] && { THUMBS_SIZE_GB="$REPLY"; break; }
      echo "  must be a whole number of GB"
    done
  fi
  persist_thumbs_config
}

# ---------- TLS mode (internal CA vs Let's Encrypt DNS-01 wildcard) ----------
# Persisted in deploy.conf as TLS_MODE / TLS_DOMAIN / ACME_EMAIL. The Cloudflare
# API token and the auto-generated proxy shared secret are SECRETS — they live in
# the CT .env only (never in deploy.conf, never echoed). Prompt-once semantics
# mirror ensure_public_base_url: an ABSENT key => ask; a PRESENT (even blank) key
# => keep. On a redeploy the token prompt accepts blank = "keep the CT's existing
# token", so the secret is entered once and survives disposable-CT rebuilds.
persist_tls_config() {
  mkdir -p "$CONF_DIR"; touch "$CONF"
  local tmp; tmp="$(mktemp)"
  grep -vE '^(TLS_MODE|TLS_DOMAIN|ACME_EMAIL)=' "$CONF" 2>/dev/null > "$tmp" || true
  printf 'TLS_MODE=%s\n' "${TLS_MODE:-internal}" >> "$tmp"
  printf 'TLS_DOMAIN=%s\n' "${TLS_DOMAIN:-}" >> "$tmp"
  printf 'ACME_EMAIL=%s\n' "${ACME_EMAIL:-}" >> "$tmp"
  mv "$tmp" "$CONF"; chmod 600 "$CONF"
}

ensure_tls_config() {
  # Fully answered already (mode set; for acme the domain+email captured too) ->
  # skip, so the two call sites (wizard + main deploy flow) never double-prompt.
  # Same "answered once, not re-asked (even on --reconfigure)" contract as
  # ensure_public_base_url; change TLS settings by editing deploy.conf / the CT .env.
  if [[ -n "${TLS_MODE+set}" ]]; then
    if [[ "$TLS_MODE" != "acme-dns" || ( -n "${TLS_DOMAIN:-}" && -n "${ACME_EMAIL:-}" ) ]]; then
      return 0
    fi
  fi
  # already answered (key present in conf -> var SET) -> never re-ask the MODE
  if [[ -z "${TLS_MODE+set}" ]]; then
    if [[ ! -t 0 ]]; then
      TLS_MODE="internal"
      echo "note: TLS_MODE unset and no TTY - defaulting to 'internal' (self-signed LAN CA)."
      persist_tls_config; return 0
    fi
    echo
    echo "── TLS mode ──"
    echo "  internal  = self-signed LAN/homelab CA in the CT (no public DNS; today's default)"
    echo "  acme-dns  = Let's Encrypt WILDCARD *.<domain> via Cloudflare DNS-01; the CT"
    echo "              terminates public TLS itself (no external nginx) and also fronts the"
    echo "              agent mTLS plane + step-ca SNI passthrough. Needs a Cloudflare token."
    while true; do
      ask "TLS mode (internal/acme-dns)" "${TLS_MODE:-internal}"; TLS_MODE="$REPLY"
      [[ "$TLS_MODE" == "internal" || "$TLS_MODE" == "acme-dns" ]] && break
      echo "  must be 'internal' or 'acme-dns'"
    done
  fi

  if [[ "$TLS_MODE" == "acme-dns" ]]; then
    if [[ -t 0 ]]; then
      ask "  apex domain (e.g. example.com) — cert is *.<domain>" "${TLS_DOMAIN:-}"; TLS_DOMAIN="$REPLY"
      ask "  ACME account email (LE expiry notices)" "${ACME_EMAIL:-}"; ACME_EMAIL="$REPLY"
      echo "  Cloudflare API token — scope Zone:DNS:Edit on the ${TLS_DOMAIN:-<domain>} zone."
      echo "  (stored in the CT .env only, never echoed; leave blank on a redeploy to keep the current one)"
      read -r -s -p "  Cloudflare API token: " CLOUDFLARE_API_TOKEN; echo
    fi
    [[ -n "${TLS_DOMAIN:-}" ]] || die "acme-dns mode requires an apex domain (TLS_DOMAIN)"
    [[ -n "${ACME_EMAIL:-}" ]] || die "acme-dns mode requires an ACME email (ACME_EMAIL)"
  fi
  persist_tls_config
}

# ---------- Cloudflare API token presence (acme-dns) ----------
# The token is a SECRET kept ONLY in the CT .env (never deploy.conf). ensure_tls_config
# prompts for it exactly once — when acme-dns is first configured — then early-returns on
# every later run. That left a live gap (2026-07-19): recreating the CT regenerates a
# fresh .env, so the token silently vanished and the stack came up CERTLESS behind only a
# buried WARNING (caddy: "missing API token" at Caddyfile.acme:51). These two helpers close
# it. ct_has_cf_token reports whether the CT that will ACTUALLY be deployed already carries
# a non-empty token; handle_cloudflare_token prompts when appropriate (host-side; the value
# flows into .env via deploy_stack, which overwrites only when a new value was entered —
# blank always means "keep the CT's current token").
ct_has_cf_token() {
  [[ -n "${VMID:-}" ]] || return 1
  pct status "$VMID" >/dev/null 2>&1 || return 1
  pct exec "$VMID" -- bash -c "grep -q '^CLOUDFLARE_API_TOKEN=..*' '${CT_APP_DIR}/.env' 2>/dev/null"
}

# mode "replace" (wizard / --reconfigure): always offer the prompt so a rotated/expired
#   token can be swapped in — blank keeps the current one, or enters one if the CT has none.
# mode "ensure" (deploy safety net): prompt ONLY when the target CT has no token, so an
#   ordinary redeploy is never nagged but a recreated/destroyed CT can't silently come up
#   certless. Non-interactive runs degrade to the same loud warning deploy_stack already emits.
handle_cloudflare_token() {
  local mode="$1"
  [[ "${TLS_MODE:-internal}" == "acme-dns" ]] || return 0
  [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]] && return 0     # captured earlier this run
  [[ "${CF_TOKEN_HANDLED:-0}" == 1 ]] && return 0       # already prompted this run
  local present="no"; ct_has_cf_token && present="yes"
  [[ "$mode" == "ensure" && "$present" == "yes" ]] && return 0
  if [[ ! -t 0 ]]; then
    [[ "$present" == "yes" ]] || echo "note: acme-dns but no Cloudflare API token in the target CT .env and none supplied — LE issuance will FAIL until CLOUDFLARE_API_TOKEN is set in the CT .env."
    return 0
  fi
  echo
  echo "── Cloudflare API token (acme-dns) ──"
  if [[ "$present" == "yes" ]]; then
    echo "  A token is already set in CT ${VMID}. Leave blank to KEEP it, or paste a new"
    echo "  one to REPLACE it (e.g. rotated/expired)."
  else
    echo "  No token is set in the target CT — a recreated container needs it re-entered,"
    echo "  or LE issuance stays broken (blank leaves it broken for now)."
  fi
  echo "  Scope: Zone:DNS:Edit on the ${TLS_DOMAIN:-<domain>} zone (stored in the CT .env only, never echoed)."
  read -r -s -p "  Cloudflare API token: " CLOUDFLARE_API_TOKEN; echo
  CF_TOKEN_HANDLED=1
}

# ---------- distributed agents (P5 platform: step-ca + enrollment endpoints) ----------
# Persisted in deploy.conf as AGENTS_ENABLED / AGENTS_CA_URL (both non-secret).
# The provisioner private JWK (FILEARR_CA_PROVISIONER_JWK) is a SECRET — it is
# extracted IN-CT from step-ca's own volume and written to the CT .env only,
# never to deploy.conf, never echoed (same class as CLOUDFLARE_API_TOKEN /
# FILEARR_SECRET_KEY). Prompt-once semantics mirror ensure_tls_config.
persist_agents_config() {
  mkdir -p "$CONF_DIR"; touch "$CONF"
  local tmp; tmp="$(mktemp)"
  grep -vE '^(AGENTS_ENABLED|AGENTS_CA_URL)=' "$CONF" 2>/dev/null > "$tmp" || true
  printf 'AGENTS_ENABLED=%s\n' "${AGENTS_ENABLED:-no}" >> "$tmp"
  printf 'AGENTS_CA_URL=%s\n' "${AGENTS_CA_URL:-}" >> "$tmp"
  mv "$tmp" "$CONF"; chmod 600 "$CONF"
}

ensure_agents_config() {
  # already answered (key present in conf -> var SET) -> never re-ask
  if [[ -n "${AGENTS_ENABLED+set}" ]]; then return 0; fi
  if [[ ! -t 0 ]]; then
    AGENTS_ENABLED="no"
    echo "note: AGENTS_ENABLED unset and no TTY - defaulting to 'no' (edit $CONF to change)."
    persist_agents_config; return 0
  fi
  echo
  echo "── Distributed agents ──"
  echo "  Filearr agents index media on OTHER machines and replicate into this server"
  echo "  (search, verify, retrieve). Enabling starts step-ca (the agents' certificate"
  echo "  authority, compose profile 'agents') and turns on the enrollment endpoints."
  echo "  Safe to enable now and enroll machines later; 'no' can be flipped by rerunning"
  echo "  after removing AGENTS_ENABLED from $CONF."
  while true; do
    ask "Enable distributed agents? (yes/no)" "${AGENTS_ENABLED:-no}"; AGENTS_ENABLED="$REPLY"
    [[ "$AGENTS_ENABLED" == "yes" || "$AGENTS_ENABLED" == "no" ]] && break
    echo "  must be 'yes' or 'no'"
  done
  if [[ "$AGENTS_ENABLED" == "yes" ]]; then
    local ca_default=""
    [[ "${TLS_MODE:-internal}" == "acme-dns" && -n "${TLS_DOMAIN:-}" ]] && ca_default="https://ca.${TLS_DOMAIN}"
    echo "  CA URL = where AGENT MACHINES reach step-ca."
    if [[ -n "$ca_default" ]]; then
      echo "  Your acme-dns Caddy already passes ca.${TLS_DOMAIN} straight through to step-ca"
      echo "  (SNI/L4, never L7-terminated) — the default is right unless you know otherwise."
      echo "  Remember the LAN DNS overrides: ca.${TLS_DOMAIN} AND agents.${TLS_DOMAIN} -> this CT."
    else
      echo "  internal TLS mode: leave blank to auto-use https://<CT-IP>:9000 at deploy time"
      echo "  (step-ca publishes :9000; agents pin the root fingerprint, so IP URLs are fine —"
      echo "  but give the CT a DHCP reservation/static IP or the pinned URL goes stale)."
    fi
    ask "  CA URL for agents (blank = auto)" "${AGENTS_CA_URL:-$ca_default}"; AGENTS_CA_URL="$REPLY"
  fi
  persist_agents_config
}

# Runs AFTER the stack is up (step-ca must be healthy): flips the agent platform
# on in the CT .env and automates docs/ops/agents.md §7 — root-fingerprint pin,
# provisioner cert-lifetime claims, and the provisioner private-JWK extraction
# (secret: .env only, never echoed). Idempotent: the JWK is extracted only when
# absent; the claims are patched only when absent; URL/fingerprint upserts are
# deterministic. app+worker are recreated at the end so the new env applies.
configure_agents() {
  [[ "${AGENTS_ENABLED:-no}" == "yes" ]] || return 0
  echo "==> configuring distributed agents (step-ca pin + provisioner JWK)"
  local ca_url="${AGENTS_CA_URL:-}"
  if [[ -z "$ca_url" ]]; then
    ca_url="https://$(ct_ip):9000"
    echo "    CA URL (auto): $ca_url"
  fi
  pct exec "$VMID" -- bash -c "set -euo pipefail; cd $CT_APP_DIR
_upsert() { grep -v \"^\$1=\" .env > .env._t 2>/dev/null || true; echo \"\$1=\$2\" >> .env._t; mv .env._t .env; }
_upsert FILEARR_AGENTS_ENABLED true
_upsert FILEARR_CA_URL '${ca_url}'
_upsert FILEARR_CA_PROVISIONER filearr-agents

# step-ca must be answering before we can pin/extract anything.
for i in \$(seq 1 30); do
  docker compose exec -T step-ca step ca health --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt >/dev/null 2>&1 && break
  [[ \$i == 30 ]] && { echo '[agents] step-ca never became healthy — check: docker compose logs step-ca'; exit 1; }
  sleep 2
done

# Root fingerprint (PUBLIC pinning material — printed once in the CA log, but
# always recomputable from the root cert itself).
FP=\$(docker compose exec -T step-ca step certificate fingerprint /home/step/certs/root_ca.crt 2>/dev/null | tr -d '\r\n' || true)
[[ -n \"\$FP\" ]] || { echo '[agents] could not read the step-ca root fingerprint'; exit 1; }
_upsert FILEARR_CA_FINGERPRINT \"\$FP\"
echo \"[agents] CA root fingerprint pinned: \$FP\"

# Provisioner private JWK -> FILEARR_CA_PROVISIONER_JWK (SECRET; agents.md §7.2).
# REMOTE-MANAGEMENT REALITY (live finding 2026-07-18): our compose enables
# DOCKER_STEPCA_INIT_REMOTE_MANAGEMENT, and in that mode step-ca stores
# provisioners in its ADMIN DB — authority.provisioners in ca.json is absent.
# The provisioner list (incl. the JWE-encrypted private key — safe to serve,
# that is how 'step ca token' works client-side) comes from the CA's public
# /provisioners endpoint instead. -k is acceptable: a MITM on the CT's own
# localhost:9000 could only feed a wrong JWE, which fails decrypt — the
# password never leaves the CA volume. Decrypt runs IN the CA container; the
# plaintext lands in .env ONLY, never echoed. Extracted only when absent.
if ! grep -q '^FILEARR_CA_PROVISIONER_JWK=..*' .env 2>/dev/null; then
  ENC=\$(curl -sk --max-time 10 https://localhost:9000/provisioners \
    | docker compose run --rm -T --no-deps app python -c '
import json,sys
d=json.load(sys.stdin)
provs=d.get(\"provisioners\") or (d if isinstance(d,list) else [])
for p in provs:
    if p.get(\"name\")==\"filearr-agents\" and p.get(\"type\")==\"JWK\":
        sys.stdout.write(p.get(\"encryptedKey\",\"\"))
' || true)
  if [[ -n \"\$ENC\" ]]; then
    # The JWE password varies by init mode (live finding 2026-07-18): with
    # remote management the provisioner key is encrypted under the CA
    # ADMINISTRATIVE password (printed once in the first-boot log), NOT the
    # secrets/password file. Try, in order: the classic password file, a
    # previously-persisted admin_password, then the log-printed password —
    # persisting the winner as secrets/admin_password so later runs never
    # depend on container-log retention.
    JWK=''
    for pwf in /home/step/secrets/password /home/step/secrets/admin_password; do
      JWK=\$(printf '%s' \"\$ENC\" | docker compose exec -T step-ca step crypto jwe decrypt --password-file \"\$pwf\" 2>/dev/null || true)
      [[ \"\$JWK\" == '{'*'\"d\"'*'}' ]] && break || JWK=''
    done
    if [[ -z \"\$JWK\" ]]; then
      APW=\$(docker compose logs step-ca 2>/dev/null | grep -i 'password is' | tail -1 | sed 's/.*password is: //' | tr -d '\r\n ' || true)
      if [[ -n \"\$APW\" ]]; then
        printf '%s' \"\$APW\" | docker compose exec -T step-ca sh -c 'cat > /tmp/adminpw && chmod 600 /tmp/adminpw'
        JWK=\$(printf '%s' \"\$ENC\" | docker compose exec -T step-ca step crypto jwe decrypt --password-file /tmp/adminpw 2>/dev/null || true)
        if [[ \"\$JWK\" == '{'*'\"d\"'*'}' ]]; then
          printf '%s' \"\$APW\" | docker compose exec -T step-ca sh -c 'cat > /home/step/secrets/admin_password && chmod 600 /home/step/secrets/admin_password'
          echo '[agents] admin password recovered from the CA log and persisted to secrets/admin_password'
        else
          JWK=''
        fi
        docker compose exec -T step-ca rm -f /tmp/adminpw 2>/dev/null || true
      fi
    fi
    if [[ -n \"\$JWK\" ]]; then
      _upsert FILEARR_CA_PROVISIONER_JWK \"\$JWK\"
      echo '[agents] provisioner private JWK extracted -> .env (enrollment OTTs enabled)'
    else
      echo '[agents] WARNING: JWK decrypt failed with every known password (agents.md §7.2)'
    fi
  else
    echo '[agents] WARNING: no filearr-agents JWK provisioner served by the CA (agents.md §7.2)'
  fi
fi

# Provisioner claims (agents.md §7.1): 24h/48h/72h TLS lifetimes +
# allowRenewalAfterExpiry for long-offline agents. Remote management means the
# ca.json patch documented pre-2026-07-18 is DEAD — claims are set through the
# admin API. Auto-init's initial admin is subject 'step' on the init
# provisioner, password = the CA password file. Best-effort: a failure here
# never blocks enrollment (default claims still issue certs).
if ! curl -sk --max-time 10 https://localhost:9000/provisioners | grep -q minTLSCertDuration; then
  # Admin auth uses the ADMIN password (secrets/admin_password once persisted
  # above; secrets/password on CAs whose init shared one password).
  APWF=/home/step/secrets/password
  docker compose exec -T step-ca test -f /home/step/secrets/admin_password 2>/dev/null && APWF=/home/step/secrets/admin_password
  if docker compose exec -T step-ca step ca provisioner update filearr-agents \
      --x509-min-dur=24h --x509-default-dur=48h --x509-max-dur=72h \
      --allow-renewal-after-expiry \
      --admin-subject=step --admin-provisioner=filearr-agents \
      --admin-password-file=\"\$APWF\" \
      --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt >/dev/null 2>&1; then
    echo '[agents] provisioner claims set (24h/48h/72h + allowRenewalAfterExpiry)'
  else
    echo '[agents] WARNING: claims update via admin API failed — certs still issue with defaults; see docs/ops/agents.md §7.1'
  fi
fi

# Apply the new env to the API/worker (compose recreates on env change).
docker compose --profile agents up -d app worker >/dev/null

# HARD final check: without the JWK, register succeeds but every enroll dies on
# a null ca_ott — say so unmissably instead of burying a warning mid-log.
if ! grep -q '^FILEARR_CA_PROVISIONER_JWK=..*' .env 2>/dev/null; then
  echo ''
  echo '[agents] ******************************************************************'
  echo '[agents] ** FILEARR_CA_PROVISIONER_JWK IS STILL UNSET — AGENT ENROLLMENT **'
  echo '[agents] ** WILL FAIL (null ca_ott). Re-run this deploy to retry the     **'
  echo '[agents] ** automatic extraction, or follow docs/ops/agents.md §7.2.     **'
  echo '[agents] ******************************************************************'
fi"
  echo "    ✓ agent platform configured"
}

next_free_vmid() { local id=$1; while pct status "$id" >/dev/null 2>&1 || qm status "$id" >/dev/null 2>&1; do id=$((id+1)); done; echo "$id"; }
detect_bridges() { ls /sys/class/net 2>/dev/null | grep -E '^vmbr' | tr '\n' ' '; }

ensure_template() {
  pveam update >/dev/null 2>&1 || true
  local avail cached
  avail=$(pveam available --section system 2>/dev/null | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
  [[ -n "$avail" ]] || die "no debian-12-standard template available via pveam"
  cached=$(pveam list local 2>/dev/null | awk '{print $1}' | grep -F "$(basename "$avail")" || true)
  [[ -n "$cached" ]] || { echo "==> downloading LXC template $avail"; pveam download local "$avail"; }
  TEMPLATE="local:vztmpl/$(basename "$avail")"
}

# ---------- storage wizard ----------
storages_need_privileged() { grep -qs '|nfs|' "$STORAGES_ENV" 2>/dev/null; }
storages_have_local() { grep -qs '|local|' "$STORAGES_ENV" 2>/dev/null; }

wizard_storages() {
  mkdir -p "$CONF_DIR"
  echo "── storage definitions (each mounts read-only at ${CT_MEDIA_ROOT}/<name> inside the CT) ──"
  if [[ -s "$STORAGES_ENV" ]]; then
    echo "current storages:"; awk -F'|' '{printf "  - %s (%s) %s/%s\n",$1,$2,$3,$4}' "$STORAGES_ENV"
    ask "Keep these and add more (add) / redefine from scratch (new) / keep as-is (keep)" "keep"
    case "$REPLY" in
      new) : > "$STORAGES_ENV" ;;
      keep) return 0 ;;
    esac
  else
    : > "$STORAGES_ENV"
  fi
  while true; do
    echo
    ask "Add a storage? (yes/no)" "$( [[ -s "$STORAGES_ENV" ]] && echo no || echo yes )"
    [[ "$REPLY" == "yes" ]] || break
    ask "  name (mount folder name, e.g. media, music)" ""; local name=$REPLY
    ask "  type (smb/cifs/ftp/sftp/webdav/nfs/local)" "smb"; local type=$REPLY
    [[ "$type" == "cifs" ]] && type="smb"
    local host="" share="" user="" pass="" port="" domain=""
    case "$type" in
      smb)    ask "  server (host/IP)" ""; host=$REPLY
              ask "  username (bare account name — NO domain prefix)" ""; user=$REPLY
              read -r -s -p "  password: " pass; echo
              ask "  AD domain / workgroup (empty = WORKGROUP)" ""; domain=$REPLY
              # UI-T7a: SMB credentials are collected ONCE per host. Add as many
              # shares on this host as wanted, all reusing host/user/pass/domain
              # (setup_storages then builds ONE rclone remote per host, mounted at
              # each :share). The first share uses the name entered above; the
              # PORT field is left empty (NAME|smb|HOST|SHARE|USER|PASS||DOMAIN).
              ask "  share name" ""; share=$REPLY
              echo "${name}|smb|${host}|${share}|${user}|${pass}||${domain:-}" >> "$STORAGES_ENV"
              echo "  added: $name (smb) -> ${host}/${share}"
              while true; do
                ask "  Add another share on ${host}? (yes/no)" "no"
                [[ "$REPLY" == "yes" ]] || break
                ask "    name (mount folder name)" ""; local sname=$REPLY
                ask "    share name" ""; local sshare=$REPLY
                [[ -n "$sname" && -n "$sshare" ]] || { echo "    name and share required — skipped"; continue; }
                echo "${sname}|smb|${host}|${sshare}|${user}|${pass}||${domain:-}" >> "$STORAGES_ENV"
                echo "    added: $sname (smb) -> ${host}/${sshare}"
              done
              continue ;;
      ftp|sftp) ask "  server (host/IP)" ""; host=$REPLY
              ask "  remote path" "/"; share=$REPLY
              ask "  username" ""; user=$REPLY
              read -r -s -p "  password (empty = key/anonymous): " pass; echo
              ask "  port" "$( [[ $type == ftp ]] && echo 21 || echo 22 )"; port=$REPLY ;;
      webdav) ask "  URL (https://host/dav)" ""; host=$REPLY
              ask "  username" ""; user=$REPLY
              read -r -s -p "  password: " pass; echo ;;
      nfs)    ask "  server (host/IP)" ""; host=$REPLY
              ask "  export path (e.g. /export/media)" ""; share=$REPLY
              echo "  NOTE: nfs requires a PRIVILEGED container — the script will switch automatically." ;;
      local)  ask "  host path to bind-mount" ""; share=$REPLY ;;
      *)      echo "  unknown type '$type', skipped"; continue ;;
    esac
    echo "${name}|${type}|${host}|${share}|${user}|${pass}|${port}|${domain:-}" >> "$STORAGES_ENV"
    echo "  added: $name ($type)"
  done
  chmod 600 "$STORAGES_ENV"
  [[ -s "$STORAGES_ENV" ]] || die "at least one storage is required"
  echo "saved storages -> $STORAGES_ENV"
}

# ---------- main wizard ----------
wizard() {
  echo "── filearr Proxmox deploy wizard (answers saved to $CONF) ──"
  ask "Container starting number (first free VMID >= this is used)" "${VMID_START:-200}"; VMID_START=$REPLY
  ask "Container hostname" "${HOSTNAME_:-filearr}"; HOSTNAME_=$REPLY
  echo "Detected bridges: $(detect_bridges)"
  ask "Network bridge" "${BRIDGE:-vmbr0}"; BRIDGE=$REPLY
  ask "IP mode (dhcp/static)" "${IP_MODE:-dhcp}"; IP_MODE=$REPLY
  IP_CIDR="${IP_CIDR:-}"; GATEWAY="${GATEWAY:-}"
  if [[ "$IP_MODE" == "static" ]]; then
    ask "Static IP (CIDR, e.g. 192.168.1.60/24)" "$IP_CIDR"; IP_CIDR=$REPLY
    ask "Gateway" "$GATEWAY"; GATEWAY=$REPLY
  fi
  echo "Available storage:"; pvesm status | awk 'NR>1{print "  - "$1" ("$2")"}'
  ask "Rootfs storage" "${STORAGE:-local-lvm}"; STORAGE=$REPLY
  ask "Disk size GB" "${DISK_GB:-16}"; DISK_GB=$REPLY
  ensure_thumbs_config
  ask "CPU cores" "${CORES:-4}"; CORES=$REPLY
  ask "Memory MB" "${MEMORY_MB:-4096}"; MEMORY_MB=$REPLY
  ask "Web UI port (on the container's IP)" "${WEB_PORT:-8484}"; WEB_PORT=$REPLY
  ask "HTTPS port (Caddy TLS sidecar, on the container's IP)" "${WEB_TLS_PORT:-8443}"; WEB_TLS_PORT=$REPLY
  ensure_public_base_url
  ensure_tls_config
  ensure_agents_config

  wizard_storages

  PRIVILEGED=0
  if storages_need_privileged; then
    echo "!! an NFS storage is defined -> container will be PRIVILEGED (kernel NFS mounts)"
    PRIVILEGED=1
  fi

  # BUG FIX (live, 2026-07-13): --reconfigure used to unconditionally take the
  # NEXT FREE VMID — the saved CT (e.g. 300) is occupied, so it silently
  # switched to a new id, and the redeploy check then created a SECOND LXC.
  # Now: when the previously saved VMID still hosts a container, offer to keep
  # reconfiguring it (default) instead of deploying a new one.
  if [[ -n "${VMID:-}" ]] && pct status "$VMID" >/dev/null 2>&1; then
    ask "Existing filearr CT $VMID found — keep it (yes) or deploy a NEW container (no)?" "yes"
    if [[ "$REPLY" =~ ^[Yy] ]]; then
      echo "── keeping existing CT $VMID (settings will apply on next deploy) ──"
    else
      VMID=$(next_free_vmid "$VMID_START")
      echo "── will create NEW CT at VMID $VMID (first free >= $VMID_START) ──"
    fi
  else
    VMID=$(next_free_vmid "$VMID_START")
    echo "── will use VMID $VMID (first free >= $VMID_START) ──"
  fi
  ensure_template
  save_conf
  # acme-dns: offer to enter/replace the Cloudflare token (blank = keep the CT's
  # current one). VMID is final here, so the prompt is correct whether we are
  # reconfiguring the existing CT or deploying a brand-new one.
  handle_cloudflare_token replace
}

# ---------- container lifecycle ----------
# OPS-T7 / P12 (optional iGPU passthrough for hardware video thumbnails) —
# DELIBERATELY NOT wired in automatically: it is opt-in and host-specific, and
# the thumbnail pipeline degrades cleanly to software when /dev/dri is absent
# (FILEARR_THUMB_ACCEL=auto). The worker compose service already carries the
# `devices: /dev/dri` + `group_add: render` mapping; to let it reach the host
# iGPU (Intel Arc/UHD), add the CT-side passthrough AFTER first deploy and then
# `pct reboot`:
#   RENDER_GID=$(stat -c '%g' /dev/dri/renderD128)          # host render group id
#   cat >>/etc/pve/lxc/<VMID>.conf <<EOF
#   lxc.cgroup2.devices.allow: c 226:* rwm                  # DRI major 226
#   lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
#   EOF
#   # unprivileged CT: map the render gid so renderD128 is group-accessible
#   #   (add a lxc.idmap pair for that gid, or run the CT privileged for iGPU).
# Verify inside the CT: `ls -l /dev/dri` then `docker compose exec worker ffmpeg -hwaccels`.
create_ct() {
  local net="name=eth0,bridge=${BRIDGE}"
  if [[ "$IP_MODE" == "static" ]]; then net+=",ip=${IP_CIDR},gw=${GATEWAY}"; else net+=",ip=dhcp"; fi

  local args=(--hostname "$HOSTNAME_" --cores "$CORES" --memory "$MEMORY_MB" --swap 512
              --rootfs "${STORAGE}:${DISK_GB}" --net0 "$net" --onboot 1 --tags filearr)
  if [[ "$PRIVILEGED" == 1 ]]; then
    args+=(--unprivileged 0 --features "nesting=1,fuse=1,mount=nfs;cifs")
  else
    args+=(--unprivileged 1 --features "nesting=1,keyctl=1,fuse=1")
  fi

  # 'local' storages are the only host dependency: pct bind mounts
  local idx=0
  # fd 3 feeds these loops: `pct exec` inherits stdin and EATS the rest of
  # storages.env otherwise, then dies SIGHUP (exit 129) when stdin runs dry —
  # THE silent-redeploy killer (caught live by the ERR trap, 2026-07-10).
  while IFS='|' read -r -u3 name type host share user pass port domain; do
    [[ "$type" == "local" ]] || continue
    args+=(--mp${idx} "${share},mp=${CT_MEDIA_ROOT}/${name},ro=1")
    idx=$((idx+1))
  done 3< "$STORAGES_ENV"

  # Dedicated thumbnail volume (ensure_thumbs_config): its own filesystem, so a
  # full cache stops thumbnail writes without starving Postgres on the rootfs.
  # Takes the next mp index AFTER the local-storage binds above. backup=0: the
  # cache is a disposable projection — vzdump must not balloon on it. LXC
  # auto-creates the target path, and the ./config:/config compose bind carries
  # the submount into the containers (submounts present at container start are
  # included in the rbind).
  if [[ -n "${THUMBS_STORAGE:-}" ]]; then
    args+=(--mp${idx} "${THUMBS_STORAGE}:${THUMBS_SIZE_GB:-64},mp=${CT_APP_DIR}/config/thumbnails,backup=0")
    idx=$((idx+1))
    echo "==> thumbnails on dedicated ${THUMBS_STORAGE}:${THUMBS_SIZE_GB:-64}G volume"
  fi

  echo "==> creating CT $VMID ($HOSTNAME_, privileged=$PRIVILEGED) on $BRIDGE"
  pct create "$VMID" "$TEMPLATE" "${args[@]}"
  pct start "$VMID"
  echo "==> waiting for network in CT..."
  for _ in $(seq 1 30); do
    pct exec "$VMID" -- ping -c1 -W1 deb.debian.org >/dev/null 2>&1 && break
    sleep 2
  done
  echo "==> installing Docker + rclone + mount tooling inside CT"
  pct exec "$VMID" -- bash -c "export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null
    apt-get install -y -qq locales curl ca-certificates rclone fuse3 nfs-common >/dev/null
    sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen >/dev/null
    update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
    sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
    curl -fsSL https://get.docker.com | sh >/dev/null"
}

# ---------- OPS-T7: deploy-time share map (auto library share_prefix) ----------
# The deploy KNOWS the true network URL behind every mount it configures, so it
# writes a credential-free map to the container's /config/share-map.json. The app
# reads it read-only and auto-populates each library's user-facing share_prefix
# from the mount that covers its root (and keeps it correct across remounts/
# redeploys). Regenerated on EVERY deploy/reconfigure so it stays relevant.
#   entry: {container_prefix, share_url, storage_type, host, unc?}
#   smb    -> smb://host/share(/sub)     + unc \\host\share(\sub)
#   ftp    -> ftp://host/path            sftp -> sftp://host/path
#   webdav -> the configured URL         nfs  -> host:/export
#   local  -> omitted (not a network share)
write_share_map() {
  echo "==> writing deploy share map -> ${CT_APP_DIR}/config/share-map.json (auto share_prefix)"
  local tmp; tmp=$(mktemp /tmp/filearr-sharemap.XXXX.json)
  local count=0
  {
    printf '[\n'
    local first=1
    while IFS='|' read -r -u3 name type host share user pass port domain; do
      [[ -n "$name" ]] || continue
      local cprefix="${CT_MEDIA_ROOT}/${name}"
      local url="" unc="" sh
      case "$type" in
        smb)
          sh="${share#/}"                       # drop a leading slash if present
          url="smb://${host}/${sh}"
          unc="\\\\${host}\\${sh//\//\\}"   # \\host\share (/-> \ in subpath)
          ;;
        ftp)    url="ftp://${host}/${share#/}" ;;
        sftp)   url="sftp://${host}/${share#/}" ;;
        webdav) url="${host}" ;;                # host field already holds the URL
        nfs)    url="${host}:${share}" ;;       # classic NFS reference host:/export
        local)  continue ;;                     # bind mount, not a network share
        *)      continue ;;
      esac
      [[ -n "$url" ]] || continue
      [[ $first == 1 ]] || printf ',\n'
      first=0
      printf '  {"container_prefix": %s, "share_url": %s, "storage_type": %s, "host": %s' \
        "$(json_str "$cprefix")" "$(json_str "$url")" "$(json_str "$type")" "$(json_str "$host")"
      [[ -n "$unc" ]] && printf ', "unc": %s' "$(json_str "$unc")"
      printf '}'
      count=$((count+1))
    done 3< "$STORAGES_ENV"
    printf '\n]\n'
  } > "$tmp"
  pct exec "$VMID" -- mkdir -p "${CT_APP_DIR}/config"
  pct push "$VMID" "$tmp" "${CT_APP_DIR}/config/share-map.json"
  rm -f "$tmp"
  echo "    ${count} network-share mapping(s) written"
}

# ---------- storage mounts inside the CT ----------
setup_storages() {
  echo "==> configuring storage mounts inside CT (systemd units, Before=docker.service)"
  while IFS='|' read -r -u3 name type host share user pass port domain; do
    [[ -n "$name" ]] || continue
    local mnt="${CT_MEDIA_ROOT}/${name}"
    pct exec "$VMID" -- mkdir -p "$mnt"

    case "$type" in
      local) continue ;;  # handled via pct bind mount at create time

      nfs)
        pct exec "$VMID" -- bash -c "grep -qsF '${host}:${share} ${mnt}' /etc/fstab || \
          echo '${host}:${share} ${mnt} nfs ro,_netdev,x-systemd.automount 0 0' >> /etc/fstab
          systemctl daemon-reload && mount '$mnt' 2>/dev/null || mount -t nfs -o ro '${host}:${share}' '$mnt'"
        ;;

      smb|ftp|sftp|webdav)
        # UI-T7a: SMB uses ONE rclone remote per host (a host-slug remote name)
        # so multiple shares on the same NAS reuse a single credential/remote,
        # each mounted at a different :share path. Non-SMB remotes stay 1:1 with
        # the storage name. Re-running config create for an already-defined remote
        # just overwrites it with identical params (idempotent) — harmless when
        # several shares on one host each iterate here.
        local remote
        if [[ "$type" == "smb" ]]; then
          remote="smb_${host//[^A-Za-z0-9]/_}"
        else
          remote="$name"
        fi
        # rclone remote (config create obscures the password itself)
        local create="rclone config create '$remote'"
        case "$type" in
          smb)    create+=" smb host '$host' user '$user'"
                  [[ -n "$pass" ]] && create+=" pass '$pass'"
                  [[ -n "$domain" ]] && create+=" domain '$domain'" ;;
          ftp)    create+=" ftp host '$host' user '$user' port '${port:-21}'"; [[ -n "$pass" ]] && create+=" pass '$pass'" ;;
          sftp)   create+=" sftp host '$host' user '$user' port '${port:-22}'"; [[ -n "$pass" ]] && create+=" pass '$pass'" ;;
          webdav) create+=" webdav url '$host' vendor other user '$user'"; [[ -n "$pass" ]] && create+=" pass '$pass'" ;;
        esac
        local remote_path=""
        [[ "$type" == "smb" ]] && remote_path="$share"
        [[ "$type" == "ftp" || "$type" == "sftp" ]] && remote_path="${share#/}"

        pct exec "$VMID" -- bash -c "$create >/dev/null 2>&1 || true
cat > /etc/systemd/system/filearr-mount-${name}.service <<UNIT
[Unit]
Description=filearr rclone mount: ${name} (${type})
After=network-online.target
Wants=network-online.target
Before=docker.service

[Service]
Type=notify
# dir-cache 5m (was 12h): NAS-side changes (e.g. corrected mtimes) must be
# visible to scans promptly; 12h served stale attributes to a full rescan
# (live finding 2026-07-12). 5m balances SMB chatter vs freshness.
ExecStart=/usr/bin/rclone mount ${remote}:${remote_path} ${mnt} \
  --read-only --allow-other --vfs-cache-mode minimal \
  --dir-cache-time 5m --attr-timeout 10s --umask 022
ExecStop=/bin/fusermount3 -u ${mnt}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now filearr-mount-${name}.service"
        ;;
    esac
  done 3< "$STORAGES_ENV"

  echo "==> verifying mounts (read test — a dead FUSE endpoint passes mountpoint checks)"
  sleep 3
  while IFS='|' read -r -u3 name type _; do
    [[ -n "$name" && "$type" != "local" ]] || continue
    local mnt="${CT_MEDIA_ROOT}/${name}"
    if pct exec "$VMID" -- timeout 15 ls "$mnt" >/dev/null 2>&1; then
      echo "    ${name}: mounted and readable"
    else
      echo "    ${name}: unreadable/stale — restarting mount unit"
      pct exec "$VMID" -- bash -c "fusermount3 -uz '$mnt' 2>/dev/null || umount -l '$mnt' 2>/dev/null || true
        systemctl restart filearr-mount-${name}.service 2>/dev/null || mount '$mnt' 2>/dev/null || true"
      sleep 3
      if pct exec "$VMID" -- timeout 15 ls "$mnt" >/dev/null 2>&1; then
        echo "    ${name}: recovered"
      else
        echo "    ${name}: STILL FAILING — check: pct exec $VMID -- journalctl -u filearr-mount-${name} -n 20"
      fi
    fi
  done 3< "$STORAGES_ENV"

  write_share_map
}

# ---------- app deploy ----------
push_source() {
  echo "==> pushing project source into CT:$CT_APP_DIR"
  warn_dirty_tree "$PROJECT_DIR"
  local tarball; tarball=$(mktemp /tmp/filearr-src.XXXX.tar.gz)
  tar -C "$PROJECT_DIR" -czf "$tarball" \
    --exclude .git --exclude node_modules --exclude .venv --exclude dist \
    --exclude __pycache__ --exclude config --exclude '*.bundle*' \
    --exclude 'RECOVERY-*' .
  # Content stamp: proves WHICH source this deploy pushed, end to end. The
  # stamp is baked into the image (backend/.build-stamp -> /app/.build-stamp)
  # and verified after the stack starts (see deploy_stack).
  BUILD_STAMP="$(sha256sum "$tarball" | cut -c1-12)-$(date -u +%Y%m%dT%H%M%SZ)"
  echo "    source stamp: $BUILD_STAMP"
  pct exec "$VMID" -- mkdir -p "$CT_APP_DIR"
  pct push "$VMID" "$tarball" /tmp/filearr-src.tar.gz
  # CLEAN extract: stale files from previous pushes must not survive
  # (renamed/deleted modules would otherwise linger in the build context).
  # Preserve runtime state: .env, config/, docker-compose.override.yml.
  pct exec "$VMID" -- bash -c "cd $CT_APP_DIR && \
    find . -mindepth 1 -maxdepth 1 \
      ! -name .env ! -name config ! -name docker-compose.override.yml \
      -exec rm -rf {} + && \
    tar -xzf /tmp/filearr-src.tar.gz -C $CT_APP_DIR && rm /tmp/filearr-src.tar.gz && \
    printf '%s\n' '$BUILD_STAMP' > $CT_APP_DIR/backend/.build-stamp"
  rm -f "$tarball"
}

deploy_stack() {
  echo "==> generating .env (first deploy only) and starting stack"
  # TLS-mode knobs (host-side; interpolated into the CT commands below).
  # acme-dns: caddy owns 443 (filearr/agents/ca via SNI), needs the agents profile
  # so step-ca's root exists for the mTLS trust_pool. internal: today's behaviour.
  local caddy_ports profile_args caddyfile
  if [[ "${TLS_MODE:-internal}" == "acme-dns" ]]; then
    caddy_ports="443:443"; profile_args="--profile agents"; caddyfile="Caddyfile.acme"
  else
    caddy_ports="${WEB_TLS_PORT}:443"; profile_args=""; caddyfile="Caddyfile.internal"
  fi
  # agents opted in (ensure_agents_config): step-ca must run in EITHER TLS mode.
  # (acme-dns already carries the profile for the mTLS trust_pool root.)
  if [[ "${AGENTS_ENABLED:-no}" == "yes" && -z "$profile_args" ]]; then
    profile_args="--profile agents"
  fi
  pct exec "$VMID" -- bash -c "cd $CT_APP_DIR && if [[ ! -f .env ]]; then
    PG_PW=\$(openssl rand -hex 16); MEILI_KEY=\$(openssl rand -hex 16)
    {
      echo POSTGRES_PASSWORD=\$PG_PW
      echo MEILI_MASTER_KEY=\$MEILI_KEY
      echo FILEARR_DATABASE_URL=postgresql+psycopg://filearr:\$PG_PW@postgres:5432/filearr
      echo FILEARR_PROCRASTINATE_DSN=postgresql://filearr:\$PG_PW@postgres:5432/filearr
      echo FILEARR_MEILI_URL=http://meilisearch:7700
      echo FILEARR_MEILI_MASTER_KEY=\$MEILI_KEY
      echo FILEARR_AUTH_ENABLED=false
      echo MEDIA_PATH=${CT_MEDIA_ROOT}
      echo PUID=0; echo PGID=0; echo TZ=\$(cat /etc/timezone 2>/dev/null || echo UTC)
    } > .env; fi"
  pct exec "$VMID" -- bash -c "cd $CT_APP_DIR && cat > docker-compose.override.yml <<EOF
services:
  app:
    ports: !override
      - \"${WEB_PORT}:8000\"
  caddy:
    ports: !override
      - \"${caddy_ports}\"
EOF
# detect the render group's numeric GID for the worker's /dev/dri access
# (group_add must be numeric — container image has no 'render' name)
RGID=\$(getent group render | cut -d: -f3); RGID=\${RGID:-104}
grep -q '^RENDER_GID=' .env && sed -i \"s/^RENDER_GID=.*/RENDER_GID=\$RGID/\" .env || echo \"RENDER_GID=\$RGID\" >> .env
# public base URL for export/report links (blank -> site-relative links).
# Re-applied every deploy so a later --reconfigure change takes effect without
# recreating .env. grep -v + append keeps it metachar-safe and single-valued.
grep -v '^FILEARR_PUBLIC_BASE_URL=' .env > .env.pburl 2>/dev/null || true
echo \"FILEARR_PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-}\" >> .env.pburl
mv .env.pburl .env
# FILEARR_SECRET_KEY: required for alert-channel secret encryption (AES-GCM;
# channels cannot be created without it). Generated ONCE, in-CT, and NEVER
# rotated automatically — rotating would orphan already-encrypted channel
# secrets. No python needed: openssl preferred, /dev/urandom+od fallback.
if ! grep -q '^FILEARR_SECRET_KEY=..*' .env 2>/dev/null; then
  if command -v openssl >/dev/null 2>&1; then
    SK=\$(openssl rand -hex 32)
  else
    SK=\$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  fi
  grep -v '^FILEARR_SECRET_KEY=' .env > .env.sk 2>/dev/null || true
  echo \"FILEARR_SECRET_KEY=\$SK\" >> .env.sk
  mv .env.sk .env
  echo \"[deploy] generated FILEARR_SECRET_KEY (alert-channel encryption enabled)\"
fi
# --- TLS mode (OPS acme-dns rework / P5-T6 agent mTLS) ---
# Select the Caddyfile the caddy service runs; in acme-dns also plumb the LE +
# Cloudflare + agent-mTLS settings. Upsert keeps each key single-valued.
_upsert() { grep -v \"^\$1=\" .env > .env._t 2>/dev/null || true; echo \"\$1=\$2\" >> .env._t; mv .env._t .env; }
_upsert FILEARR_CADDYFILE ${caddyfile}
if [ \"${TLS_MODE:-internal}\" = \"acme-dns\" ]; then
  _upsert FILEARR_TLS_DOMAIN \"${TLS_DOMAIN}\"
  _upsert FILEARR_ACME_EMAIL \"${ACME_EMAIL}\"
  _upsert STEPCA_DNS \"ca.${TLS_DOMAIN},step-ca,localhost\"
  # Cloudflare token: overwrite only when a new one was entered this run (blank on
  # a redeploy keeps the CT's existing token). SECRET — never echoed.
  if [ -n \"${CLOUDFLARE_API_TOKEN:-}\" ]; then _upsert CLOUDFLARE_API_TOKEN \"${CLOUDFLARE_API_TOKEN}\"; fi
  grep -q '^CLOUDFLARE_API_TOKEN=..*' .env 2>/dev/null || echo '[deploy] WARNING: acme-dns but CLOUDFLARE_API_TOKEN unset — LE issuance fails until it is set in .env'
  # FILEARR_PROXY_SHARED_SECRET: generate ONCE (caddy mTLS site <-> backend trust);
  # never auto-rotated. Same discipline as FILEARR_SECRET_KEY.
  if ! grep -q '^FILEARR_PROXY_SHARED_SECRET=..*' .env 2>/dev/null; then
    if command -v openssl >/dev/null 2>&1; then PS=\$(openssl rand -hex 32); else PS=\$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'); fi
    _upsert FILEARR_PROXY_SHARED_SECRET \"\$PS\"
    echo '[deploy] generated FILEARR_PROXY_SHARED_SECRET (agent mTLS proxy trust)'
  fi
fi
# iGPU: map /dev/dri into the worker ONLY when the CT actually has it
# (docker hard-fails on missing device paths — live failure 2026-07-13)
if [ -e /dev/dri/renderD128 ]; then
  cat >> docker-compose.override.yml <<EOF
  worker:
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - \"\$RGID\"
EOF
  echo 'iGPU detected: /dev/dri mapped into worker (QSV available)'
else
  echo 'no /dev/dri in CT: video thumbs use software decode (see deploy-proxmox.sh iGPU note to enable QSV)'
fi
docker compose pull postgres meilisearch --quiet || true
docker compose ${profile_args} build --pull ${FORCE_REBUILD:+--no-cache}
docker compose ${profile_args} up -d --remove-orphans
# Bound the buildkit cache (2026-07-21 audit: repeated deploys had accreted
# 11 GB, 8.8 GB reclaimable, on the CT rootfs). Runs AFTER build+up so the
# layers just built are the most-recently-used and are what --keep-storage
# retains — the next redeploy stays incremental. NEVER a full prune: that
# would force every redeploy into a cold multi-minute rebuild. || true: cache
# hygiene must not fail a deploy.
docker builder prune -f --keep-storage 6GB 2>/dev/null | tail -n1 || true"
  configure_agents
  echo "==> bootstrap DB / queue / index (idempotent)"
  pct exec "$VMID" -- bash -c "cd $CT_APP_DIR && docker compose run --rm app python scripts/init_db.py"
  verify_deploy
  agents_summary
}

# Post-deploy cheat sheet for the agent endpoints — printed only when the
# platform is on. No secrets: the CA fingerprint is public pinning material.
agents_summary() {
  [[ "${AGENTS_ENABLED:-no}" == "yes" ]] || return 0
  local ip; ip="$(ct_ip)"
  local central ca_url
  if [[ "${TLS_MODE:-internal}" == "acme-dns" && -n "${TLS_DOMAIN:-}" ]]; then
    central="https://filearr.${TLS_DOMAIN}"
  else
    central="http://${ip}:${WEB_PORT}"
  fi
  ca_url="${AGENTS_CA_URL:-https://${ip}:9000}"
  echo
  echo "── Distributed agents: ready ──"
  echo "  central (enroll/API):  ${central}"
  echo "  step-ca (agent CA):    ${ca_url}"
  if [[ "${TLS_MODE:-internal}" == "acme-dns" && -n "${TLS_DOMAIN:-}" ]]; then
    echo "  mTLS agent plane:      https://agents.${TLS_DOMAIN}  (for the later auth-mode flip)"
    echo "  LAN DNS overrides needed: ca.${TLS_DOMAIN} + agents.${TLS_DOMAIN} -> ${ip}"
  fi
  echo "  Enroll a machine:"
  echo "    1. Admin -> Agents -> Mint token (shown once, single-use, short TTL)"
  echo "    2. on the device:  filearr-agent enroll -central ${central} -token <paste> -name <name>"
  echo "    3.                 filearr-agent scan --root <media path>   (repeatable)"
  echo "    4.                 filearr-agent run                        (daemon; wrap in a service)"
  echo "  Full guide: docs/ops/agents.md"
}

# Fails loudly when the running containers were not built from the source
# that push_source just pushed (stale build cache, wrong PROJECT_DIR, or a
# partially-failed build would all be caught here).
# ---------- graceful job quiesce/resume around deploys ----------
# Before replacing containers: gracefully STOP running scans (keeps all
# progress; UI-T13 semantics — no tombstoning, wrap-up runs) and remember
# their libraries. After the new stack verifies, re-trigger those scans.
# Best-effort by design: if the app is unreachable (first deploy, crashed
# stack) we skip — container restarts are already crash-safe.
QUIESCED_LIBS_FILE="/tmp/filearr-quiesced-libs.$$"

api() {  # api <method> <path>  -> body (curl inside the CT against localhost)
  pct exec "$VMID" -- curl -s -X "$1" "http://localhost:${WEB_PORT}/api/v1$2" </dev/null 2>/dev/null
}

quiesce_scans() {
  echo "==> quiescing running scans (graceful stop, progress kept)"
  local scans; scans=$(api GET "/scans?limit=50") || true
  if [[ -z "$scans" || "$scans" == *"Connection refused"* ]]; then
    echo "    app not reachable — nothing to quiesce"; return 0
  fi
  # collect running/stopping run ids + their libraries
  local ids libs
  ids=$(printf '%s' "$scans" | pct exec "$VMID" -- docker exec -i filearr-app-1 python -c "
import json,sys
runs=[r for r in json.load(sys.stdin) if r.get('status') in ('running','stopping')]
print(' '.join(r['id'] for r in runs)); print(' '.join(r['library_id'] for r in runs))" </dev/null 2>/dev/null) || true
  local run_ids lib_ids
  run_ids=$(printf '%s\n' "$ids" | sed -n 1p); lib_ids=$(printf '%s\n' "$ids" | sed -n 2p)
  if [[ -z "$run_ids" ]]; then echo "    no running scans"; : > "$QUIESCED_LIBS_FILE"; return 0; fi
  printf '%s\n' $lib_ids | sort -u > "$QUIESCED_LIBS_FILE"
  for rid in $run_ids; do
    api POST "/scans/${rid}/stop" >/dev/null || true
    echo "    stop requested: scan $rid"
  done
  # wait (bounded) for wrap-up to finish so the worker isn't killed mid-write
  local waited=0
  while (( waited < 180 )); do
    local still; still=$(api GET "/scans?limit=50" | grep -c '"status": *"stopping"\|"status":"stopping"' || true)
    [[ "$still" == "0" || -z "$still" ]] && { echo "    scans stopped cleanly"; return 0; }
    sleep 5; waited=$((waited+5))
  done
  echo "    ⚠ some scans still wrapping up after 180s — proceeding (crash-safe)"
}

resume_scans() {
  [[ -s "$QUIESCED_LIBS_FILE" ]] || { rm -f "$QUIESCED_LIBS_FILE"; return 0; }
  echo "==> resuming scans stopped for the deploy"
  while read -r lib; do
    [[ -n "$lib" ]] || continue
    api POST "/libraries/${lib}/scan" >/dev/null \
      && echo "    re-triggered scan: library $lib" \
      || echo "    ⚠ could not re-trigger library $lib — start it from Admin"
  done < "$QUIESCED_LIBS_FILE"
  rm -f "$QUIESCED_LIBS_FILE"
}

verify_deploy() {
  echo "==> verifying deployed image matches pushed source"
  local pushed deployed
  pushed="${BUILD_STAMP:-unknown}"
  deployed=$(pct exec "$VMID" -- bash -c \
    "cd $CT_APP_DIR && docker compose exec -T app cat /app/.build-stamp 2>/dev/null" || echo MISSING)
  echo "    pushed:   $pushed"
  echo "    deployed: $deployed"
  if [[ "$deployed" != "$pushed" || "$pushed" == "unknown" ]]; then
    echo "    ✗ STAMP MISMATCH — the running app was NOT built from the source just pushed."
    echo "      Retry with a forced clean build:  FORCE_REBUILD=1 $0 <same args>"
    exit 1
  fi
  echo "    ✓ deployed image was built from this push"
  verify_live
}

# OPS-T2: functional post-deploy smoke — the stamp check proves the RIGHT code
# is running; this proves it actually WORKS end to end (DB/Meili/search) and that
# TLS terminates. Runs inside the CT against the published ports. Any failure
# exits non-zero -> the ERR trap fails the whole deploy (loudly, named check).
verify_live() {
  echo "==> post-deploy smoke (OPS-T2): http://localhost:${WEB_PORT}"
  # </dev/null so pct exec does not swallow the caller's stdin (the T10 fix).
  pct exec "$VMID" -- bash "${CT_APP_DIR}/scripts/smoke.sh" "http://localhost:${WEB_PORT}" </dev/null

  if [[ "${TLS_MODE:-internal}" == "acme-dns" ]]; then
    # LE issuance is async and depends on the Cloudflare DNS-01 challenge + the
    # public A/AAAA records the operator still has to create — so there is no
    # cert to smoke-test in-CT yet. The plain-HTTP smoke above already proved the
    # app works; verify TLS externally once DNS + issuance settle.
    echo "==> acme-dns mode: skipping in-CT TLS smoke (LE wildcard issues async via Cloudflare DNS-01)."
    echo "    After DNS + issuance settle, verify:  curl -I https://filearr.${TLS_DOMAIN}/api/v1/health"
    return 0
  fi

  echo "==> TLS reachability (OPS-T1): https://localhost:${WEB_TLS_PORT} (self-signed CA, curl -k)"
  local code
  code=$(pct exec "$VMID" -- curl -sk -o /dev/null -w '%{http_code}' \
    --max-time 15 "https://localhost:${WEB_TLS_PORT}/api/v1/health" </dev/null || echo 000)
  if [[ "$code" != "200" ]]; then
    echo "    ✗ HTTPS via Caddy returned ${code} (expected 200) — TLS sidecar not serving."
    echo "      Check:  pct exec $VMID -- bash -c 'cd ${CT_APP_DIR} && docker compose logs caddy'"
    exit 1
  fi
  echo "    ✓ HTTPS reachable through the Caddy sidecar (200)"
}

update_system() {
  echo "==> updating CT OS packages, Docker engine and tooling (full-upgrade)"
  pct exec "$VMID" -- bash -c "export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null
    apt-get -y -qq full-upgrade >/dev/null
    apt-get -y -qq autoremove --purge >/dev/null
    apt-get -y -qq clean" || echo "    WARN: apt upgrade reported issues (continuing)"
}

ct_ip() { pct exec "$VMID" -- hostname -I 2>/dev/null | awk '{print $1}'; }

# ---------- main ----------
need pct; need pveam; need pvesm
MODE="${1:-deploy}"
# legacy migration: project was renamed catalarr -> filearr
if [[ ! -f "$CONF" && -f "$HOME/.config/catalarr/deploy.conf" ]]; then
  mkdir -p "$CONF_DIR"
  cp "$HOME/.config/catalarr/"* "$CONF_DIR"/ 2>/dev/null || true
  echo "migrated saved settings from ~/.config/catalarr -> $CONF_DIR"
fi
[[ -f "$CONF" ]] && source "$CONF"
# defaults / legacy-key compatibility (conf files written before this fix used HOSTNAME=)
HOSTNAME_="${HOSTNAME_:-${HOSTNAME:-filearr}}"
PRIVILEGED="${PRIVILEGED:-0}"
IP_MODE="${IP_MODE:-dhcp}"
WEB_PORT="${WEB_PORT:-8484}"
WEB_TLS_PORT="${WEB_TLS_PORT:-8443}"

case "$MODE" in
  --reconfigure) wizard ;;
  --storages)
    wizard_storages
    if storages_need_privileged && [[ "${PRIVILEGED:-0}" != 1 ]]; then
      echo "!! NFS storage added but existing CT is unprivileged — destroy & redeploy to apply (--destroy, then deploy)."
    fi
    ;;
  --status)
    [[ -n "${VMID:-}" ]] || die "no saved deployment ($CONF missing)"
    pct status "$VMID" || true
    while IFS='|' read -r -u3 name type _; do
      [[ -n "$name" ]] || continue
      pct exec "$VMID" -- mountpoint -q "${CT_MEDIA_ROOT}/${name}" 2>/dev/null \
        && echo "storage ${name} (${type}): mounted" || echo "storage ${name} (${type}): NOT mounted"
    done 3< "$STORAGES_ENV"
    pct exec "$VMID" -- bash -c "cd $CT_APP_DIR && docker compose ps" || true
    exit 0
    ;;
  --destroy)
    [[ -n "${VMID:-}" ]] || die "no saved deployment ($CONF missing)"
    read -r -p "Destroy CT $VMID and its data? (yes/no): " ok
    [[ "$ok" == "yes" ]] || exit 0
    pct stop "$VMID" >/dev/null 2>&1 || true
    pct destroy "$VMID"
    echo "CT $VMID destroyed. Defaults + storage definitions kept in $CONF_DIR for redeploy."
    exit 0
    ;;
  deploy) ;;
  *) die "unknown option: $MODE" ;;
esac

[[ -f "$CONF" && -s "$STORAGES_ENV" ]] || wizard
# Ask once for the public base URL on any deploy/reconfigure/storages run whose
# saved conf predates this feature (idempotent: no-op once the wizard set it).
ensure_public_base_url
# Same prompt-once discipline for the TLS mode (internal vs acme-dns).
ensure_tls_config
# Safety net: in acme-dns mode the Cloudflare token must exist in the CT that is
# about to be deployed. Prompts ONLY when it is actually missing (a recreated or
# destroyed CT), so ordinary redeploys are never nagged; deduped against the
# wizard's "replace" prompt above via CF_TOKEN_HANDLED / a token already entered.
handle_cloudflare_token ensure
# Same prompt-once discipline for the distributed-agent platform (step-ca +
# enrollment endpoints); the JWK secret itself is handled in-CT at deploy time.
ensure_agents_config
# Same prompt-once discipline for the dedicated thumbnail volume (applied at CT
# create; drift-checked against an existing CT in the redeploy branch below).
ensure_thumbs_config

if [[ -n "${VMID:-}" ]] && pct status "$VMID" >/dev/null 2>&1; then
  echo "── redeploying to existing CT $VMID with saved defaults ──"
  pct start "$VMID" >/dev/null 2>&1 || true
  # Thumbs-volume drift check: the conf promises dedicated storage but this CT
  # has no mount on the thumbnails path (conf answered after the CT was created,
  # or the mp was removed by hand). A pct mp cannot be hot-added mid-redeploy —
  # it needs the stack down and a CT reboot — so warn LOUDLY with the exact
  # hand-apply sequence instead of silently reverting to shared-rootfs behaviour.
  if [[ -n "${THUMBS_STORAGE:-}" ]] && \
     ! pct exec "$VMID" -- mountpoint -q "${CT_APP_DIR}/config/thumbnails" 2>/dev/null; then
    echo "!! THUMBS_STORAGE=${THUMBS_STORAGE} is saved, but ${CT_APP_DIR}/config/thumbnails"
    echo "!! is NOT a mountpoint in CT $VMID — thumbnails are sharing the rootfs."
    echo "!! To apply the dedicated volume to this existing CT:"
    echo "!!   pct exec $VMID -- bash -c 'cd ${CT_APP_DIR} && docker compose --profile agents down'"
    echo "!!   pct exec $VMID -- mv ${CT_APP_DIR}/config/thumbnails ${CT_APP_DIR}/config/thumbnails.pre-move"
    echo "!!   pct set $VMID -mp<free-N> ${THUMBS_STORAGE}:${THUMBS_SIZE_GB:-64},mp=${CT_APP_DIR}/config/thumbnails,backup=0"
    echo "!!   pct reboot $VMID    # then copy thumbnails.pre-move/. back in and rerun this deploy"
    echo "!! (find a free mp index with: pct config $VMID | grep ^mp)"
  fi
  # legacy migration (rename): stop old catalarr stack, disable old mount units,
  # carry the old .env across with renamed keys (keeps DB credentials working)
  pct exec "$VMID" -- bash -c '
    if [[ -d /opt/catalarr ]]; then
      cd /opt/catalarr && docker compose -p catalarr down 2>/dev/null || true
      systemctl disable --now "catalarr-mount-*" 2>/dev/null || true
      for u in /etc/systemd/system/catalarr-mount-*.service; do
        [[ -f "$u" ]] && systemctl disable --now "$(basename "$u")" 2>/dev/null || true
      done
      mkdir -p /opt/filearr
      if [[ -f /opt/catalarr/.env && ! -f /opt/filearr/.env ]]; then
        sed "s/CATALARR_/FILEARR_/g" /opt/catalarr/.env > /opt/filearr/.env
        echo "migrated .env (old DB credentials preserved)"
      fi
      mv /opt/catalarr /opt/catalarr.old 2>/dev/null || true
    fi'
  step "quiesce jobs";      quiesce_scans
  step "update system";     update_system
  step "storage mounts";    setup_storages          # re-applies storage defs
  step "push source";       push_source
  step "build + start stack"; deploy_stack
  step "resume jobs";       resume_scans
  step "deploy complete"
else
  VMID=$(next_free_vmid "${VMID_START:-200}")
  echo "── first deploy: CT $VMID ──"
  [[ -n "${TEMPLATE:-}" ]] || ensure_template
  save_conf
  create_ct
  step "update system";     update_system
  step "storage mounts";    setup_storages
  step "push source";       push_source
  step "build + start stack"; deploy_stack
  step "deploy complete"
fi

IP=$(ct_ip)
echo
echo "════════════════════════════ DEPLOYMENT SUMMARY ════════════════════════════"
echo
echo "  Container"
echo "    VMID:        $VMID  (hostname: $HOSTNAME_, $( [[ "$PRIVILEGED" == 1 ]] && echo privileged || echo unprivileged ))"
echo "    Bridge/IP:   $BRIDGE / ${IP:-unknown} ($IP_MODE)"
echo "    Resources:   ${CORES} cores, ${MEMORY_MB} MB RAM, ${DISK_GB} GB disk on ${STORAGE}$( [[ -n "${THUMBS_STORAGE:-}" ]] && echo ", thumbs ${THUMBS_SIZE_GB:-64} GB on ${THUMBS_STORAGE}" )"
echo
echo "  Services (docker compose in CT:$CT_APP_DIR)"
pct exec "$VMID" -- bash -c "cd $CT_APP_DIR && docker compose ps --format '    {{.Service}}: {{.State}} ({{.Status}})'" 2>/dev/null || true
echo
echo "  Storages (read-only inside the CT)"
while IFS='|' read -r -u3 name type _; do
  [[ -n "$name" ]] || continue
  state="NOT MOUNTED"
  pct exec "$VMID" -- mountpoint -q "${CT_MEDIA_ROOT}/${name}" 2>/dev/null && state="mounted"
  echo "    ${CT_MEDIA_ROOT}/${name}  (${type}) — ${state}"
done 3< "$STORAGES_ENV"
echo
echo "  Saved settings"
echo "    Defaults:    $CONF"
echo "    Storages:    $STORAGES_ENV"
echo
echo "─────────────────────────────── WHERE TO GO ────────────────────────────────"
echo
if [[ "${TLS_MODE:-internal}" == "acme-dns" ]]; then
  echo "  TLS mode: acme-dns — the CT terminates public TLS itself (NO external nginx)."
  echo "  A Let's Encrypt WILDCARD *.${TLS_DOMAIN} is issued via Cloudflare DNS-01."
  echo
  echo "  1. CREATE THESE DNS RECORDS (in Cloudflare, pointing at the CT / your"
  echo "     port-forward — all three share port 443 via SNI):"
  echo "       filearr.${TLS_DOMAIN}   A/AAAA  -> ${IP:-<ct-ip-or-public-ip>}   (web UI/API)"
  echo "       agents.${TLS_DOMAIN}    A/AAAA  -> ${IP:-<ct-ip-or-public-ip>}   (agent mTLS plane)"
  echo "       ca.${TLS_DOMAIN}        A/AAAA  -> ${IP:-<ct-ip-or-public-ip>}   (step-ca SNI passthrough)"
  echo "     The Cloudflare API TOKEN needs scope: Zone:DNS:Edit on the ${TLS_DOMAIN} zone."
  echo "     (DNS-01 needs NO inbound :80 — issuance works behind NAT.)"
  echo
  echo "  2. Open the web UI (once DNS + issuance settle):"
  echo "       https://filearr.${TLS_DOMAIN}/                (real LE cert — no warning)"
  echo "       http://${IP:-<ct-ip>}:${WEB_PORT}             (plain HTTP — API/compat)"
  echo "     API docs: https://filearr.${TLS_DOMAIN}/api/docs"
  echo
  echo "  3. Agent mTLS plane (docs/ops/agents.md + docs/ops/tls.md):"
  echo "     A FILEARR_PROXY_SHARED_SECRET was generated in the CT .env, and the"
  echo "     agents.${TLS_DOMAIN} site REQUIRES client certs (step-ca root). To cut"
  echo "     the fleet over from the interim bearer to mTLS (zero-downtime):"
  echo "       a) set FILEARR_AGENT_AUTH_MODE=both in the CT .env, redeploy"
  echo "       b) point each agent at https://agents.${TLS_DOMAIN} (it presents its cert)"
  echo "       c) once all agents are on mTLS, set FILEARR_AGENT_AUTH_MODE=mtls-header"
  echo "     Ensure step-ca is configured (provisioner claims + JWK) per agents.md §7."
else
  echo "  1. Open the web UI:"
  echo "       https://${IP:-<ct-ip>}:${WEB_TLS_PORT}   (TLS via Caddy — recommended)"
  echo "       http://${IP:-<ct-ip>}:${WEB_PORT}    (plain HTTP — API/compat)"
  echo "     The HTTPS cert is signed by Caddy's LAN-internal CA. To remove the"
  echo "     browser warning, trust the root CA on your client once:"
  echo "       pct exec ${VMID} -- bash -c 'cd ${CT_APP_DIR} && docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt /tmp/filearr-root-ca.crt'"
  echo "       pct pull ${VMID} /tmp/filearr-root-ca.crt ./filearr-root-ca.crt"
  echo "     then import filearr-root-ca.crt into your OS/browser trust store."
  echo "     (Wave 4 login will REQUIRE https — plain HTTP refuses secure cookies.)"
  echo "     Switch to a public Let's Encrypt wildcard anytime:  bash proxmox/deploy-proxmox.sh --reconfigure  (choose acme-dns)."
  echo "  2. API docs (interactive): http://${IP:-<ct-ip>}:${WEB_PORT}/api/docs"
fi
echo
echo "  3. Register your first library (point it at a mounted storage):"
echo "       curl -X POST http://${IP:-<ct-ip>}:${WEB_PORT}/api/v1/libraries \\"
echo "         -H 'Content-Type: application/json' \\"
echo "         -d '{\"name\":\"media\",\"root_path\":\"${CT_MEDIA_ROOT}/$(awk -F'|' 'NR==1{print $1}' "$STORAGES_ENV")\"}'"
echo
echo "  4. Start a scan with the id returned above:"
echo "       curl -X POST http://${IP:-<ct-ip>}:${WEB_PORT}/api/v1/libraries/<id>/scan"
echo "     then watch results appear in the web UI search."
echo
echo "  Management:"
echo "    status                bash proxmox/deploy-proxmox.sh --status"
echo "    add/change storages   bash proxmox/deploy-proxmox.sh --storages  (then redeploy)"
echo "    change settings       bash proxmox/deploy-proxmox.sh --reconfigure"
echo "    redeploy after edits  bash proxmox/deploy-proxmox.sh"
echo "    remove everything     bash proxmox/deploy-proxmox.sh --destroy"
echo
echo "═════════════════════════════════════════════════════════════════════════════"
