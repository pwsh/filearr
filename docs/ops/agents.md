# Distributed agents — enrollment runbook (Phase 5, P5-T1)

Filearr's distributed-agent architecture (roadmap §1, v3) lets remote machines
run a local scanner + offline index and replicate their catalog to central. It
is **opt-in and off by default** — a single-node deploy is entirely unaffected
and never starts the CA.

**P5-T1 delivers the central-side trust root only:** the `agents` /
`enrollment_tokens` tables, the token-mint + register-first enrollment API, and
the Admin → Agents console panel. The Go agent binary, actual cert issuance, and
replication are later tasks (P5-T2..T8). This runbook covers standing up the CA
and minting an enrollment token; it flags what is not yet wired.

## 1. Turn the feature on

> **Proxmox deploys: this is now automated.** `proxmox/deploy-proxmox.sh`
> prompts once ("Enable distributed agents?"; persisted as `AGENTS_ENABLED` /
> `AGENTS_CA_URL` in `deploy.conf`) and then, on every deploy: starts step-ca
> (compose `agents` profile, either TLS mode), writes
> `FILEARR_AGENTS_ENABLED/CA_URL/CA_PROVISIONER` into the CT `.env`, pins the
> CA root fingerprint (`FILEARR_CA_FINGERPRINT`), patches the provisioner
> claims (§7.1, once), and extracts the provisioner private JWK (§7.2) into
> `FILEARR_CA_PROVISIONER_JWK` — the JWK is a SECRET and lands in the CT
> `.env` only, never in `deploy.conf`, never echoed. A post-deploy summary
> prints the enroll endpoints + the per-device commands. The sections below
> remain the manual/reference path for non-Proxmox deployments and recovery.

```bash
# .env
FILEARR_AGENTS_ENABLED=true
FILEARR_ENROLLMENT_TOKEN_TTL_MINUTES=60      # minutes-to-hours, NOT days
FILEARR_CA_URL=https://ca.filearr.lan:9000   # your step-ca (see §2)
FILEARR_CA_FINGERPRINT=<root-fingerprint>    # public pin, printed on CA init
FILEARR_CA_PROVISIONER=filearr-agents
FILEARR_AGENT_CERT_TTL_HOURS=48              # 24–72h band (short blast radius)
# P5-T2: the provisioner's DECRYPTED private JWK (JSON, one line, EC P-256/ES256).
# SECRET — treat like FILEARR_SECRET_KEY; never commit it. When UNSET the register
# response's `ca_ott` is null (agents enroll but cannot fetch certs — see §7).
FILEARR_CA_PROVISIONER_JWK='{"kty":"EC","crv":"P-256","kid":"...","x":"...","y":"...","d":"..."}'
FILEARR_CA_OTT_TTL_SECONDS=300               # OTT lifetime (short, single-use)
```

With `FILEARR_AGENTS_ENABLED=false` (default) the `/api/v1/agents` surface
returns 404 and the Admin → Agents panel stays hidden. The tables still exist
(empty) so enabling later needs **no migration**.

## 2. Stand up step-ca (optional compose profile)

step-ca (smallstep, Apache-2.0) runs **only** under the `agents` compose
profile, never in the default stack:

```bash
# .env (CA init — consumed once on first boot)
STEPCA_NAME="Filearr Agents CA"
STEPCA_DNS=ca.filearr.lan,step-ca,localhost

docker compose --profile agents up -d step-ca
docker compose logs step-ca | grep -i fingerprint   # -> FILEARR_CA_FINGERPRINT
```

The image auto-initialises a fresh CA on first boot and prints the **root
fingerprint** (public pinning material — not a secret). Copy it into
`FILEARR_CA_FINGERPRINT`. Pin: `smallstep/step-ca:0.30.2` (the version the
phase-5 research names). **On any bump, re-verify the exact patch AND the full
smallstep CVE list** — same discipline as the Meili/Caddy pins.

> **OpenBao PKI** is the documented drop-in alternative (MPL-2.0) for operators
> who already centralise PKI in Vault/OpenBao — see
> `docs/research/phase-5-distributed-agents.md` §1.2. step-ca is the default.

## 3. Mint an enrollment token and enroll a machine

Admin → Agents → **Mint token** (or `POST /api/v1/agents/enrollment-tokens`,
admin scope). The raw token is shown **once** and never stored — only its
sha256 is persisted. Hand it to the new machine out-of-band (copy/paste into the
agent installer, or a QR/deep-link).

The enrollment handshake (R3 — **register precedes cert**):

1. **Agent → `POST /api/v1/agents/register`** `{token, hostname, platform,
   name?}` — the server validates + **consumes** the token (single-use), assigns
   the authoritative `agent_id`, and returns it plus CA bootstrap info
   (`ca.url` / `ca.fingerprint` / `ca.provisioner`) and a one-time
   `enroll_secret`. The agent is now **pending** (no cert yet). The response also
   carries a short-lived, single-use **`ca_ott`** (the step-ca token for step 2;
   null if the provisioner JWK is unconfigured — §7).
2. **Agent → step-ca** — generates a keypair + CSR with the returned `agent_id`
   in the cert CN/SAN and, using the register response's `ca_ott` (a scoped,
   single-use step-ca JWK provisioner token central minted, §7), calls step-ca's
   `POST /1.0/sign` directly to obtain a short-lived client cert. Keys never
   leave the agent; central never proxies the CSR. *(Agent-side; built in P5-T2
   against the API the P5-T2a spike selected — smallstep/certificates `ca` v0.30.2.)*
3. **Agent → `POST /api/v1/agents/{id}/certificate`** `{enroll_secret,
   cert_fingerprint}` — binds the issued fingerprint (pending → **active**). The
   one-time secret closes the window where a guessed pending-agent UUID could be
   hijacked; P5-T2 further hardens this behind the freshly-minted mTLS cert.

A second redemption of the same token is rejected (single-use); an expired token
is rejected. Both are enforced server-side and audited.

## 4. Revocation (kill switch)

Admin → Agents → **revoke** (or `DELETE /api/v1/agents/{id}`) stamps
`revoked_at`. This is an **application-layer denylist** (research §1.4): the
agent is refused on every replication/config request regardless of whether its
short-lived cert is still cryptographically valid. It is **not** a hard delete —
the row and its replication history are retained. Combined with the 24–72h cert
TTL + passive (refuse-to-renew) revocation, this bounds a stolen-cert blast
radius without operating CRL/OCSP infrastructure.

**Hard delete** (Admin → Agents → **delete**, or `DELETE
/api/v1/agents/{id}?purge=true`) removes the row entirely — the cleanup path for
failed-enrollment `pending` rows and decommissioned machines with no data
footprint. Refused (409) while any library or item still references the agent
(replicated data keeps its provenance; revoke those, or delete their libraries
first). Cascades remove the agent's commands/transfers/ledger/reconcile rows;
`libraries.source_agent_id` and `enrollment_tokens.consumed_by` go NULL. Audited
as `agent_deleted`.

**Enrollment tokens**: an unconsumed token deletes freely (`DELETE
/agents/enrollment-tokens/{hash}`); a consumed token's row carries the
`consumed_by` link and needs `?force=true` — the audit event records that link
before the row goes, so the trail survives the cleanup.

## 5. Audit trail

Every mutation writes a `security_events` row (Admin → Audit): `agent_token_minted`,
`agent_token_revoked`, `agent_registered` (ok/rejected + reason),
`agent_cert_bound`, `agent_revoked`. Raw tokens/secrets never appear in the log.

## 6. Agent commands (on-demand instructions, P10-T1)

Separate from the policy/replication channels, the `agent_commands` table is the
queue through which central asks an agent to do ONE thing on demand:

| kind | meaning | who enqueues |
|---|---|---|
| `stat_check` | cheap existence/freshness `stat()` of an agent-hosted item | verify UX (P10-T3) |
| `rehash_check` | strong verify: quick/content hash re-read | verify UX (P10-T3) |
| `stage_upload` | start an agent→central retrieve staging upload | retrieve API (P10-T13) |

**Lifecycle.** `pending` → (agent poll delivers) `picked_up` → (agent reports)
`done` / `failed`. A per-minute maintenance sweep flips a stale unpicked
`pending` row or a lease-lapsed `picked_up` row to **`expired`** (kept, not
deleted, so the UI can say "the agent never came back") and re-queues an
unacked delivery back to `pending` (at-least-once), bounded by
`FILEARR_AGENT_COMMAND_MAX_ATTEMPTS`. An admin may **cancel** any pre-terminal
command. `done` / `failed` / `expired` / `cancelled` are terminal + immutable.

**Two auth planes** (both behind `FILEARR_AGENTS_ENABLED`):

- *Operator* — `POST /api/v1/agents/{id}/commands` (enqueue, `write`),
  `GET /api/v1/agent-commands` (list, keyset, filter by agent/state/kind),
  `GET /api/v1/agent-commands/{id}`, `POST /api/v1/agent-commands/{id}/cancel`
  (`write`). Enqueue + cancel are audited (`agent_command_enqueued` /
  `agent_command_cancelled`). **Wave 4 (P6-T4)** swaps the coarse `write` gate on
  enqueue for the path-scoped RBAC `download` action, evaluated *before* the row
  is created.
- *Agent* — `POST /api/v1/agents/{id}/commands/poll` (drain up to `max`
  pending), `.../commands/{cid}/ack` (in-flight lease heartbeat),
  `.../commands/{cid}/complete` (report `{ok, result}`). A poll also refreshes
  `agents.last_seen_at`. This is a PLAIN poll — the held-open long-poll rides
  P5-T4. Per-poll traffic is NOT audited (noise).

**Agent-plane auth — `FILEARR_AGENT_AUTH_MODE` (P5-T6, shipped 2026-07-17).**
Every agent-plane endpoint (commands, replication, reconcile, policy) routes
through `_authenticate_agent`. Three modes:

- `fingerprint` (default) — the **interim** scheme: the agent's bound
  `cert_fingerprint` as a bearer token (the only durable per-agent secret before
  mTLS). **Caveat:** the fingerprint rotates on cert renewal while central still
  holds the enrollment fingerprint, so a renewed agent can 401 until it re-pins
  `FILEARR_AGENT_AUTH_FINGERPRINT` — the reason to migrate to mTLS.
- `mtls-header` — trust the Caddy `agents.<domain>` site's **already-verified**
  client identity (see `docs/ops/tls.md`): `X-Filearr-Proxy-Auth` must match
  `FILEARR_PROXY_SHARED_SECRET` and `X-Filearr-Agent-San == str(agent_id)`.
  Identity is the **SAN**, so **the renewal-drift caveat above DIES** — the SAN
  survives cert rotation. The bearer is refused. Requires the shared secret
  (fails closed when unset).
- `both` — transition: mtls-header when the proxy header is present (hard-fails
  on a bad secret/SAN), else bearer. Used during the flip.

**Flip sequence (zero downtime):** set `both` → migrate each agent to
`https://agents.<domain>` (the Go client presents its enrolled cert
automatically) → set `mtls-header`. Full runbook in `docs/ops/tls.md`.

**Tunables** (`FILEARR_AGENT_COMMAND_*`): `TTL_SECONDS` (default 3600, "hours not
minutes"; per-kind defaults are P10-T7), `TTL_MAX_SECONDS` (enqueue override
clamp), `LEASE_SECONDS` (unacked-delivery redelivery window, default 300),
`MAX_ATTEMPTS` (5), `POLL_MAX` (50), `PAYLOAD_MAX_BYTES` / `RESULT_MAX_BYTES`
(size caps — a hostile/buggy caller cannot bloat a row). The UI surfaces an
agent's commands via Admin → Agents → **commands** (state chips + cancel).

## 7. step-ca provisioner claims + the JWK secret + the CA proxy (P5-T2)

P5-T2 (central half) mints the `ca_ott` on register. Three operator steps make
it work end to end.

### 7.1 Provisioner claims

> **Remote management changes everything here (live finding 2026-07-18).** Our
> compose sets `DOCKER_STEPCA_INIT_REMOTE_MANAGEMENT=true`, and in that mode
> step-ca keeps provisioners in its **admin database** — `authority.provisioners`
> in `ca.json` is absent, so editing `ca.json` (the pre-2026-07-18 instruction
> below) does nothing. Set the claims through the admin API instead (auto-init's
> initial admin is subject `step` on the init provisioner; password = the CA
> password file):
>
> ```bash
> docker compose exec -T step-ca step ca provisioner update filearr-agents \
>   --x509-min-dur=24h --x509-default-dur=48h --x509-max-dur=72h \
>   --allow-renewal-after-expiry \
>   --admin-subject=step --admin-provisioner=filearr-agents \
>   --admin-password-file=/home/step/secrets/password \
>   --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt
> ```
>
> The Proxmox deploy script does this automatically. The `ca.json` shape below
> is kept for reference / non-remote-management CAs only.

The bare provisioner the compose `DOCKER_STEPCA_INIT_*` env creates issues certs
but does not encode the spike's TLS-lifetime ruling. Set its claims to:

```jsonc
// step-ca config/ca.json -> authority.provisioners[JWK "filearr-agents"]
{
  "type": "JWK",
  "name": "filearr-agents",
  "key": { /* public JWK — auto-populated */ },
  "encryptedKey": "...",              // the private JWK, JWE-encrypted (see 7.2)
  "claims": {
    "minTLSCertDuration": "24h",
    "defaultTLSCertDuration": "48h",
    "maxTLSCertDuration": "72h",
    "allowRenewalAfterExpiry": true   // BOUNDED grace for long-offline agents
  }
}
```

`allowRenewalAfterExpiry` lets a long-offline agent renew a just-expired cert
over mTLS instead of re-enrolling; it is the CA-side half of the re-enrollment
gap the re-issue endpoint (7.3) covers on the central side. Edit `ca.json` in
the `stepca_data` volume and restart step-ca.

### 7.2 Extract the provisioner private JWK -> `FILEARR_CA_PROVISIONER_JWK`

Central signs the OTT with the provisioner's **decrypted private** JWK. step-ca
stores it JWE-encrypted (`encryptedKey`) under the provisioner password.

**Where `encryptedKey` lives depends on remote management** (see §7.1): with it
on (our compose default) it is NOT in `ca.json` — fetch it from the CA's public
`/provisioners` endpoint (serving the JWE publicly is by design; that is how
`step ca token` works client-side — only the password can open it):

```bash
# in the CT (the Proxmox deploy script automates exactly this)
ENC=$(curl -sk https://localhost:9000/provisioners \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
      print(next(p["encryptedKey"] for p in d.get("provisioners", d if isinstance(d,list) else []) \
      if p.get("name")=="filearr-agents" and p.get("type")=="JWK"), end="")')
printf '%s' "$ENC" | docker compose exec -T step-ca \
  step crypto jwe decrypt --password-file /home/step/secrets/password
# -> {"kty":"EC","crv":"P-256","kid":"...","x":"...","y":"...","d":"..."}
```

**Which password?** (live finding 2026-07-18): with remote management the
provisioner key is encrypted under the CA **administrative password** — printed
ONCE in the first-boot log (`docker compose logs step-ca | grep -i "password
is"`), and NOT the same as `secrets/password` (the CA-key password). The deploy
script tries `secrets/password`, then `secrets/admin_password`, then recovers
the log-printed password — persisting it as `secrets/admin_password` (0600, in
the CA volume) so nothing ever depends on container-log retention again. If the
first-boot log is gone AND `admin_password` was never persisted, the password
is unrecoverable — rotate the provisioner key (create a new keypair, `step ca
provisioner update filearr-agents --private-key=...`, put the new plaintext
private JWK in `.env`).

Paste the result into `FILEARR_CA_PROVISIONER_JWK` (single-operator model: env is
acceptable; **rotate** by generating a new provisioner key, updating `ca.json`,
and replacing the env value). It is a **secret** — same class as
`FILEARR_SECRET_KEY`; Filearr never logs it (only *that* it is unset/malformed),
and validates its shape (EC P-256, private) on first use.

**Fail-safe.** If `FILEARR_CA_PROVISIONER_JWK` is unset OR malformed, registration
still succeeds but `ca_ott` is **null** — a bad key never takes enrollment down;
agents simply cannot obtain certs until it is fixed, then use the re-issue
endpoint (7.3) to hand a fresh OTT to already-registered agents. Central emits
`agent_ca_ott_minted` (agent_id + `jti` only — never the token) on every mint.

### 7.3 Re-issue endpoint (re-enrollment recovery)

`POST /api/v1/agents/{id}/ca-ott` (admin scope) mints a **fresh** OTT for an
existing **pending or active** agent — the operator-driven recovery path for an
agent long offline past its cert TTL, or one that registered before the JWK was
plumbed. A **revoked** agent is refused (409); an unknown id is 404; if the
provisioner JWK is unconfigured the endpoint returns 503 (its only job is to
mint). Audited by `jti`.

### 7.4 CA proxy: SNI / L4 passthrough (do NOT L7-terminate)

**The CA must NOT be L7-proxied.** step-ca's `/renew` authenticates via the
client cert on the direct TLS connection; an L7 terminator silently breaks it
(the `/1.0/sign` enroll call would survive L7, but renewal will not). Route a
dedicated hostname (e.g. `ca.<domain>`) as **SNI-based L4/TCP passthrough** to
step-ca:9000, never through an L7 reverse proxy.

> **P5-T6 update (2026-07-17): this is now solved IN-CT — no external proxy
> needed.** In `acme-dns` TLS mode (`docker/caddy/Caddyfile.acme`, see
> `docs/ops/tls.md`) the CT's own Caddy carries a caddy-l4 **listener wrapper** on
> 443 that peeks the TLS ClientHello and raw-TCP-proxies `ca.<domain>` straight to
> `step-ca:9000` (the CA keeps its own TLS), while every other SNI falls through to
> the HTTP app. The external nginx front is out of the path entirely; there is no
> upstream `stream {}` block to maintain. The constraint above still holds — it is
> now enforced by the l4 route rather than by hand.

### 7.5 Go agent implementation facts (P5-T2 shipped, 2026-07-16)

The Go half lives in `agent/` (`cmd/filearr-agent` + `internal/enroll`,
module `github.com/filearr/filearr/agent`, Go 1.26, sole production dep
`github.com/smallstep/certificates v0.30.2`). `filearr-agent enroll` runs the
full §3 handshake; `filearr-agent run` starts the renewal daemon (2/3-TTL +
±10% jitter, capped exponential backoff, mTLS `/renew`, best-effort root
refresh after every renewal + an out-of-band `RefreshRoots` hook for the
future CA-rotation signal). Facts verified against a real in-process step-ca
0.30.2 authority (recorded here so nobody re-litigates them):

- **`cert_fingerprint` = lowercase hex SHA-256 of the leaf DER.** Central
  stores it verbatim (no format enforcement — `agentsync.bind_agent_certificate`
  equality-compares only), and this matches step-ca's own root-fingerprint
  convention, so `step certificate fingerprint` output is directly comparable.
- **CSR↔OTT SAN enforcement is REAL:** step-ca's JWK provisioner rejects (403)
  any CSR whose SANs differ from the OTT's `sans`. The agent uses
  `ca.CreateSignRequest(ott)` which derives CN/SANs from the OTT itself, so
  CSR==OTT holds by construction. A bare-UUID agent_id classifies as a **DNS
  SAN** (`x509util.SplitSANs`).
- **Clock skew: ±1 minute leeway** on OTT validation (`ValidateWithLeeway`);
  +30 s future `nbf`/`iat` accepted, +2 min rejected.
- **OTT replay rejected by `jti`** ("token already used", 401). Audience
  matching is port-insensitive (central's port-bearing `aud` matches).
- Windows caveat: the key file's 0600 mode doesn't map to an ACL — effective
  protection is the data dir's inherited ACL (hardening item, tracked in
  `certstore.go`).

## 8. Agent self-update: signed manifest + staged rollout (P5-T7)

Agents self-update from an operator-signed manifest. The signing **private key
lives only on your signing machine** (never on central, never in the repo — see
`agent/README.md` for keygen + the `-ldflags` public-key pin), so a compromised
central cannot push a wrongly-signed binary (research §8: central is untrusted
for update integrity — the agent verifies the Ed25519 signature against its
build-time pinned key).

**Production public key** (generated 2026-07-17 on the dev host; private key in
`~/.filearr-signing`, vault-backed; PUBLIC material — safe to publish). Every
production agent build must pin it:

```bash
go build -ldflags "-X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=j0hTanu7jdT44h7pwPzb5vv0juSDmJMCAyi8tV33/wQ=" ./cmd/filearr-agent
```

A binary built WITHOUT the pin refuses all updates (fail-closed) — fine for dev,
wrong for the fleet. If the key is ever regenerated (`keygen --force`), every
deployed agent must be rebuilt/redeployed with the new pin before it will accept
another update.

### 8.1 Settings

```bash
# .env
# Where uploaded artifact BINARIES live (manifests go in the agent_releases table).
# Default: {config_dir}/agent-releases  (i.e. /config/agent-releases in the LXC).
FILEARR_AGENT_RELEASES_DIR=/config/agent-releases
# The rollout_group whose agents receive a canary release before you promote it.
FILEARR_AGENT_CANARY_GROUP=canary          # default
# Hard ceiling on one uploaded artifact (bytes).
FILEARR_AGENT_UPDATE_MAX_ARTIFACT_BYTES=536870912   # 512 MiB default
```

Assign a machine to the canary wave by setting its `agents.rollout_group` to the
canary group (R5 — this text column migrates to phase-6 machine groups later;
never two parallel grouping authorities). Everyone else stays `default`.

### 8.2 Operator flow

1. **Build + sign** three platforms and produce `manifest.json` (see
   `agent/README.md` — `filearr-release keygen` once, then build with the pinned
   public key, then `filearr-release sign`).
2. **Register** the signed manifest (admin scope) — lands as `stage=canary`:
   `POST /api/v1/agent-releases` (body = `manifest.json`).
3. **Upload** each artifact binary (admin scope), verified against the manifest
   sha256/size: `PUT /api/v1/agent-releases/{version}/artifacts/{filename}` with
   the raw file as the body. A release is only offered / promotable once **every**
   manifest artifact is present.
4. **Watch the canary group confirm.** `GET /api/v1/agent-releases` returns each
   release's `confirmed_count` and a per-agent `agent_version` rollup — the §6.3
   "which version has each agent confirmed" query. An agent reports its running
   version on every manifest poll; that report IS the confirmed-version signal
   (a polling agent has, by definition, booted + passed its 60s health window).
5. **Promote** canary→general once the canary wave is healthy (the R5 / §6.3
   operator-confirmation gate): `POST /api/v1/agent-releases/{version}/promote`.
   The whole fleet then sees it on its next poll (default 6h;
   `FILEARR_AGENT_UPDATE_POLL_INTERVAL`).

### 8.3 Rollback semantics

A newly-swapped binary is on trial: the agent writes a boot-counter, then on each
launch runs a 60s health window (local index opens + a central contact succeeds
or fails cleanly, no panic). On pass it clears the counter, deletes the `.old`
binary, and confirms its version. If the new binary crashes through **3 launch
attempts** without ever passing, the next launch **automatically restores the
previous binary** and re-execs it (systemd-boot boot-assessment pattern, research
§5.3). Run the agent under a service manager with restart-on-failure (systemd
`Restart=on-failure` + `StartLimitBurst`, a Windows Service failure action, or
launchd `KeepAlive`) so a crashed agent is relaunched to trigger the rollback.

A `sha256` mismatch on download, an invalid signature, or an unpinned build all
**refuse the update** (fail-closed) rather than swapping.

## Not yet wired (later phase-5 tasks)

- **P5-T4/T5**: replication (outbox → `replication-batch`) + reconciliation.
- ~~**P5-T6**: mTLS enforcement on the agent-plane endpoints.~~ **Shipped
  2026-07-17** — `FILEARR_AGENT_AUTH_MODE=mtls-header|both` (§6) + the
  `agents.<domain>` Caddy mTLS site + in-CT CA L4 passthrough (§7.4,
  `docs/ops/tls.md`). The interim cert-fingerprint bearer remains the default
  (`fingerprint` mode) until an operator flips the fleet over.
- **P10-T3/T4/T6/T13**: the consumers of `agent_commands` — the verify flow,
  the tus staging upload, the central download + SSE, and the RBAC transfer
  API. P10-T1 (this section) builds only the queue + its central surface.
