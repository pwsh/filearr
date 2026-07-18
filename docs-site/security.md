# Security features

This page describes Filearr's security surface: the auth model, RBAC, the search
tenant-scoping, the agent-plane trust model, audit logging, rate limiting, signed
agent updates, the data-safety (recycle-bin) model, and secrets posture.

## Authentication model

Filearr supports two credential carriers, side by side:

- **API keys (Bearer tokens).** Prefixed, high-entropy CSPRNG tokens, stored
  **sha256-hashed at rest** (high entropy, so no slow KDF needed). Each key has
  one or more scopes: **`read`**, **`write`**, **`admin`** (admin implies the
  others). Use them for `*arr`-style integrations and scripts:

    ```http
    Authorization: Bearer <api-key>
    ```

- **Interactive sessions.** Postgres-backed, opaque session cookies (not
  stateless JWTs — so revocation is O(1): delete the row). The cookie is
  `HttpOnly` and `SameSite` (lax by default, so OIDC callbacks work while
  state-changing requests stay CSRF-safe), and `Secure` whenever the request
  arrived over HTTPS. Lifetimes are tunable: a 30-day absolute cap, a 7-day
  idle window, and 10-minute opaque-token rotation by default.

Local passwords use **argon2id** (a slow, memory-hard KDF appropriate for
low-entropy human secrets) — a deliberately different trust model from the
sha256-at-rest API keys.

**Auth is off by default** (`FILEARR_AUTH_ENABLED=false`) for a frictionless
first look. Turning it on is additive and zero-downtime; existing API keys keep
working. The **first admin is always a local account**, which is your break-glass
path if a federated provider locks everyone out. See
[Operations → enabling auth](operations.md#enabling-authentication).

### Federated login (optional)

- **OIDC SSO** — Authlib relying party (authorization-code + PKCE, JWKS ID-token
  validation). Env-configured; role mapping evaluated at every login; JIT
  provisioning and optional group sync. Email-based account linking is **off by
  default** (an account-takeover surface) and only ever links on an IdP-asserted
  `email_verified=true` exact match.
- **LDAP / Active Directory** — bind auth via ldap3, TLS-first (StartTLS upgrade
  for non-loopback `ldap://`, refused without TLS unless an operator explicitly
  allows plaintext). Direct-bind or search-then-bind, group→role mapping, and
  optional group sync.

Both fail **closed**: a half-configured provider's endpoints 404 rather than
500ing, and an unmapped user is refused when you leave the default role empty.

## RBAC: groups, path grants, and ltree scoping

Beyond the coarse read/write/admin scopes, Filearr has **path-scoped RBAC**:

- **Principals** are users or service accounts; **groups** collect principals.
- A **path grant** ties a principal or group to a path scope and an action set.
  The item's scope is encoded as an `ltree` value derived from its
  `(library, relative path)`, and grants are matched by ltree ancestry — so a
  grant on a folder covers everything beneath it.
- A non-admin principal with no grants sees **nothing** (fail-closed); an admin
  bypasses scoping entirely.

!!! note "ltree columns are real extension types"
    The scope columns are backed by the Postgres `ltree` extension type (with a
    GiST ancestor index), with a text fallback where the extension is
    unavailable. This matters operationally — see the
    [ltree bind-cast class](operations.md#the-ltree-bind-cast-42804-error-class)
    in the runbook.

## Tenant-scoped search

RBAC extends into Meilisearch. A scoped (non-admin) principal's search is
constrained by a **server-side proxy filter**: the API injects the principal's
compiled scope filter into the Meilisearch query body (a principal with no grants
compiles to a filter that matches nothing). Because enforcement is server-side,
there is no client-visible tenant token to leak, and there is a configurable
ceiling on the compiled filter length — an over-large grant set is **refused**
(the admin must consolidate grants) rather than silently coarsened.

Note the Meilisearch tenant-token CVE pin discussed in
[Setup requirements](setup.md#dependency-versions).

## Agent-plane authentication

The agent plane (replication, reconcile, policy, commands) has its own auth,
selected by `FILEARR_AGENT_AUTH_MODE`:

| Mode | How identity is proven | Notes |
|---|---|---|
| `fingerprint` *(default, interim)* | The agent's bound cert fingerprint as a bearer token | The only durable per-agent secret before mTLS. **Caveat:** the fingerprint rotates on cert renewal, so a long-lived fleet must re-pin or migrate to mTLS. |
| `mtls-header` | A TLS-terminating proxy verifies the client cert and forwards the verified SAN | Identity is the SAN (`== agent_id`), which survives cert rotation — the drift caveat dies. Requires the proxy shared secret; the bearer path is refused. |
| `both` | Proxy header when present (hard-fails on a bad secret/SAN), else bearer | The zero-downtime transition mode. |

The mTLS path relies on a TLS-terminating proxy (Caddy's `agents.<domain>` site)
that verifies the client cert against the step-ca root and forwards a **verified**
identity as trusted headers, guarded by `FILEARR_PROXY_SHARED_SECRET`. The
mTLS-header modes fail **closed** when that secret is unset.

**Flip sequence (zero downtime):** set `both` → migrate each agent to the mTLS
site (the Go client presents its enrolled cert automatically) → set
`mtls-header`.

### Enrollment tokens

Enrollment tokens are **single-use** and **short-TTL** (default 60 minutes) —
the token is the single human-copy-paste weak link, so its blast-radius window is
deliberately small. The raw token is shown once and only its hash is stored.

## Audit log

Login, logout, session lifecycle, grant changes, and every agent mutation
(`agent_token_minted`, `agent_registered`, `agent_cert_bound`, `agent_revoked`,
`agent_ca_ott_minted`, …) are **always** recorded to a security-events table
(Admin → Audit, or `GET /api/v1/audit`, admin scope). Raw tokens and secrets
never appear in the log.

High-volume **read** auditing (a per-query event) is **off by default** (low
value outside multi-tenant SaaS) and toggled with `FILEARR_AUDIT_READS`. Retention
is split: noisy login-failure rows purge sooner than higher-value events.

!!! info "Download / export / verify are audited unconditionally"
    Actions that move data out of Filearr — downloads, exports, and verify — are
    recorded regardless of the read-audit toggle, so there is always a trail of
    what left the system.

## Rate limiting (brute-force lockout)

Login is protected by a **Postgres-backed** limiter (survives restarts, shared
across workers). It tracks **two independent buckets** per attempt: the submitted
username (catches a distributed brute force — many IPs, one account) and the
source IP. Either bucket crossing the threshold locks it, and the lock is checked
*before* the slow argon2 verify runs. Defaults: 3 failures / 120-second window →
300-second lock, returning `429 + Retry-After`.

!!! warning "Only trust `X-Forwarded-For` behind a trusted proxy"
    Leave `FILEARR_AUTH_RATELIMIT_TRUST_FORWARDED_FOR=false` unless a trusted
    proxy sets the header — otherwise a client can spoof it to dodge the per-IP
    bucket. The per-username bucket is unspoofable regardless.

## Signed agent updates

Agent self-update integrity does **not** trust central. Releases are Ed25519-signed
with a key that lives only on your signing machine; the public key is pinned into
each agent binary at build time; a mismatched sha256, an invalid signature, or an
unpinned build all refuse the update. Rollout is a **canary → promote** gate, and
a crash-looping new binary is **automatically rolled back** by boot counting. See
[Agents → self-update](agents.md#self-update-with-signed-releases).

## Data-safety model (recycle bin / tombstones)

Scans **never hard-delete** (architecture invariant 4). A file gone from disk is
tombstoned `missing`; a user-deleted item becomes `trashed` and waits for a
scheduled recycle-bin purge (retention `FILEARR_RECYCLE_RETENTION_DAYS`, default
30 days). Only `active` items appear in search and browse. A `missing` item
returns to `active` automatically if a later scan sees the file again — identity
is `(library, relative path)`, so re-appearance re-attaches. See
[Operations → recycle-bin recovery](operations.md#recycle-bin-tombstone-recovery).

## Secrets management posture

- **What lives where.** Secrets (`FILEARR_SECRET_KEY`, `MEILI_MASTER_KEY`,
  `POSTGRES_PASSWORD`, `FILEARR_CA_PROVISIONER_JWK`, `FILEARR_PROXY_SHARED_SECRET`,
  the Cloudflare token) live in the container `.env` / compose `env_file` only —
  never in a committed compose file, and on Proxmox never in the (non-secret)
  `deploy.conf`.
- **Channel secrets are encrypted at rest.** Alert-channel credentials (SMTP
  password, webhook HMAC secret) are AES-GCM encrypted with an envelope key
  derived from `FILEARR_SECRET_KEY`, which is held **outside** Postgres — so a
  stolen database dump exposes no channel credentials. When the key is unset the
  alert-channels API returns 503 rather than storing plaintext.
- **Signing keys never touch the server.** The agent-release signing private key
  lives only on your signing machine; central only ever holds public/pinned
  material.
- **What is never logged.** The CA provisioner JWK, the proxy shared secret,
  raw enrollment tokens, agent enroll secrets, channel secrets, and OTTs are
  never written to logs — only *that* a key is missing/malformed, or a token's
  `jti`, is recorded.
- **`FILEARR_SECRET_KEY` is never auto-rotated** — rotating it would orphan
  already-encrypted channel secrets.
