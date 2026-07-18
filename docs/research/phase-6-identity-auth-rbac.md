# Research Brief — Future Roadmap Item 3: Identity, Auth & RBAC (Central Console)

Scope: `docs/future-roadmap.md` §3 (Identity, auth & RBAC, v3 central console).
This brief pressure-tests and concretizes the already-decided shape — it does
not relitigate it. Fixed decisions carried in as constraints:

- **Auth providers**: local accounts, LDAP (`python-ldap`), SAML (`pysaml2`),
  OIDC/SSO (`Authlib`). `fastapi-users` avoided (maintenance mode).
- **RBAC, two layers** (Grafana/Portainer precedent): global roles
  (Admin/User/Viewer) + group-based resource ACLs on machine groups and file
  locations (path scopes), with inheritance + override.
- **Enforcement in search**: ACLs compile into Meilisearch tenant tokens
  (per-session signed JWT with embedded filter) — row-level security without
  trusting the client.
- Meilisearch has no native RBAC (roadmap-only) — the app layer owns the
  permission model permanently, not as an interim measure.

Interacts with: `docs/research/phase-5-distributed-agents.md` (machine groups
are the RBAC scoping unit for agent-originated data; that brief's §11 flags
"RBAC machine-groups model" as a cross-cutting dependency on this one), and
`docs/future-roadmap.md` §8 (Meilisearch adoption plan already commits to
tenant tokens + pinning ≥1.48.2 for the CVEs discussed below). Existing v1
code this extends: `backend/filearr/security.py` (Bearer API-key auth,
`read`/`write`/`admin` scopes, sha256-hashed keys, no KDF because keys are
high-entropy CSPRNG output) and `backend/filearr/models.py`'s `ApiKey` table.

Constraint order for every tradeoff in this document: **security > integrity
> reliability > speed > compatibility > scalability**. AGPL-3.0-or-later
-compatible OSS dependencies only (per `docs/future-roadmap.md` §9's license
decision). Research current as of **2026-07-07**.

---

## 1. Findings

### 1.1 Federation / identity-provider libraries

**python-ldap** (PSF-2.0-equivalent/Python-style license, effectively
MIT-compatible) is the correct choice over `ldap3`. `ldap3`'s last real
stable release is **2.9.1 (2021-07-18)**; every tag since — up through
2.10.2rc4 seen as late as April 2026 — is an unpromoted release candidate,
meaning the project has been functionally stalled for roughly five years
despite still appearing "active" in shallow searches (tags exist, they just
never leave RC status). `python-ldap` continues normal tagged releases and is
the library actually used by production LDAP-auth integrations in
comparable self-hosted tools. Verdict: **keep python-ldap, avoid ldap3**,
matching the prior decision.
(https://pypi.org/project/ldap3/, https://github.com/python-ldap/python-ldap/releases)

**pysaml2** (Apache-2.0) remains the pragmatic SAML SP library for Python.
The key implementation decision is signature verification: pysaml2 shells
out to the **xmlsec1 CLI as a subprocess** rather than linking `python3-saml`'s
in-process C extension (`lxml` + libxmlsec1 bindings called directly from the
Python process). For a security-sensitive parsing path handling
attacker-influenced XML, subprocess isolation is the safer default — a
memory-safety bug in the XML-security C extension crashes/exploits a
disposable child process, not the API server's own process space. This is a
defense-in-depth win, not a performance one; SAML login is a low-QPS path
(interactive human login, not a per-request hot path), so the subprocess
overhead is immaterial. Verdict: **keep pysaml2**, and **specifically
lean into the xmlsec1-subprocess architecture** as a stated security
property, not an incidental implementation detail.

**Authlib** (BSD-3-Clause) is the right OIDC/OAuth2 client/server toolkit.
It has an active CVE history that must inform the version pin — five
GitHub Security Advisories were found spanning 2024-2026:

| CVE ID | Affected range | Fixed version | Issue |
|---|---|---|---|
| CVE-2024-37568 | < 1.3.1 | 1.3.1 | Algorithm-confusion: HMAC key accepted where an asymmetric key was expected |
| CVE-2025-61920 | < 1.6.5 | 1.6.5 | DoS via oversized JOSE segments (unbounded parse) |
| CVE-2025-68158 | 1.0.0-1.6.5 | 1.6.6 | 1-click account takeover via unbound CSRF `state` parameter |
| CVE-2026-27962 (GHSA-wvwj-cvrp-7pv5) | <= 1.6.8 | 1.6.9 | JWS JWK header injection -- attacker-supplied `jwk`/`jku` header can smuggle a verification key, bypassing signature checks |
| CVE-2026-28498 (GHSA-m344-f55w-2m6j) | < 1.6.9 | 1.6.9 | Fail-open OIDC `at_hash`/`c_hash` binding -- ID token can be replayed against a different access/auth code without detection |

The prior pass's placeholder pin of "≥1.7.2" is **corrected here**: the
actual highest "fixed in" version across all five advisories is **1.6.9**
(no 1.7.x release has shipped a security fix beyond that as of this
research date, and pinning to a specific patched minor is safer than betting
on an unreleased 1.7.2). **Recommended pin: `authlib>=1.6.9`.** The GitHub
advisories page for `authlib/authlib` also lists several further 2026
issues (`alg:none` bypass, JWE Bleichenbacher padding-oracle, cache-based
CSRF, two open-redirect advisories) not yet triaged into this table —
**re-check the advisory list immediately before implementation**, since this
is an actively-patched library and the safe floor moves.
(https://github.com/authlib/authlib/security/advisories)

**fastapi-users** confirmed in maintenance mode: security/dependency patches
continue but no new features, and the maintainer team has stated they are
building a successor toolkit rather than extending this one. Last release
2026-03-27. Verdict: **avoid** for new RBAC/session work — building on a
toolkit whose own maintainers are walking away from it inverts the
maintenance-burden goal of using a library at all.
(https://github.com/fastapi-users/fastapi-users)

**argon2-cffi** for password hashing (local-account provider only — LDAP/SAML/
OIDC delegate credential verification entirely to the external IdP and never
touch a Filearr-side password). `passlib`, the once-standard wrapper, is
unmaintained (no PyPI release addressing modern Python/bcrypt-backend
breakage); argon2-cffi is maintained, implements the Argon2id KDF (2015 PHC
winner, resistant to GPU/ASIC cracking unlike bcrypt/scrypt at equivalent
tuning effort), and is the library import guides currently recommend
directly. Note this is a **different trust model from `ApiKey`**:
`security.py`'s existing comment ("high entropy, so no slow KDF needed") is
correct for CSPRNG-generated API keys, but a **human-chosen password has far
lower entropy** and specifically needs a slow, memory-hard KDF — the two
mechanisms are not interchangeable, and local-account passwords must never
reuse the API-key sha256-hash-at-rest pattern.

### 1.2 Passkeys / WebAuthn

**Deferred**, matching Immich's precedent: Immich (the closest architectural
sibling — self-hosted media catalog, AGPL, Postgres+search-index shape) also
punted native WebAuthn/passkey support and instead delegates to OIDC when an
operator wants passwordless/hardware-key login (register a passkey at the
IdP level — Authentik, Keycloak, Authelia, or a cloud IdP — and let OIDC
delegation carry it through). Building first-class WebAuthn ceremony
handling (attestation, credential storage, resident-key vs non-resident
flows, platform vs roaming authenticator UX) is a substantial standalone
effort with a narrow near-term payoff given OIDC delegation already covers
the use case. Recommend the same deferral for Filearr; revisit only if
OIDC-delegated passkey UX proves too clunky for the self-hosted single-user
case (no IdP running).

### 1.3 Session management

**HttpOnly + Secure + SameSite=Strict cookies, Postgres-backed, not
stateless JWT.** The decisive argument against stateless JWT sessions is
**instant revocation**: a signed JWT with no server-side record cannot be
invalidated before its `exp` claim without either a blocklist (which is
itself server-side state, defeating the "stateless" premise) or very short
TTLs with painful refresh churn. A Postgres-backed session table gives O(1)
revocation ("log out everywhere," "kill this session from the admin
console," forced logout on role/scope change) at the cost of one indexed
lookup per authenticated request — an acceptable cost matching invariant-style
"integrity > speed" ordering, and Postgres is already the system of record
so no new infrastructure is introduced (no Redis, matching the project's
existing no-Redis stance from Procrastinate).

**Timeout numbers — Grafana's actual defaults** (re-verified against
Grafana's own configuration docs, since the prior pass's numbers were
close but not exact):
- `login_maximum_inactive_lifetime_duration` = **7 days** (session dies if
  unused for 7 days)
- `login_maximum_lifetime_duration` = **30 days** (hard absolute cap
  regardless of activity)
- `token_rotation_interval_minutes` = **10 minutes** (fixed default, not a
  10-15 range as the unverified prior note suggested; configurable). Each
  rotation resets the inactivity timer.

(https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-authentication/)

Recommend adopting these exact defaults (7d inactivity / 30d absolute / 10min
rotation) as Filearr's starting configuration, operator-tunable via
`FILEARR_*` env vars matching the existing config pattern. **Immich's ~400-day
cookie lifetime is confirmed as the anti-pattern to avoid** — a
year-plus-lived session with no rotation is a standing credential-theft
target with no time-bounded blast radius; it optimizes for "never re-login
on a home server" convenience at a real security cost, and is exactly the
kind of tradeoff this project's stated priority order (security first)
rejects.

**API keys remain for automation**, unchanged in shape from today's
`ApiKey` model, but re-homed under the new `service_accounts` principal type
(§3 below) rather than living as a parallel, disconnected identity concept.
Grafana's own split between interactive user sessions and **service
accounts** (a first-class non-human principal type, distinct from "a user's
personal API key") is the direct model: service accounts are auditable,
individually revocable, and scopable exactly like human principals, whereas
today's bare `ApiKey` rows have no owning identity at all. This is the
cleanest way to unify "the automation actor that scans/writes via REST"
and "the human who logs into the console" under one permission model
without forcing automation through interactive-session mechanics (cookies
make no sense for a cron job or another *arr app's API client).

### 1.4 RBAC engine

**Hand-rolled Postgres tables using the native `ltree` extension for path
hierarchy**, not a third-party policy engine. Alternatives rejected:
- **Casbin** (Apache-2.0, real and maintained) — its generic matcher-rule
  policy model (RBAC/ABAC/domain-RBAC via arbitrary `.conf`/CSV rules) is
  overhead for what's actually an "NTFS-style permission inheritance"
  problem; Postgres+ltree expresses that more directly and stays
  query-planner-visible (`EXPLAIN` works on an ltree ancestor query; a
  Casbin matcher expression is a black box to it).
- **Oso** — Osohq discontinued the OSS policy-engine line in 2025-26 in
  favor of hosted Oso Cloud; confirmed deprecated for this use case, same
  maintenance-burden trap as `fastapi-users`.
- **SpiceDB / OpenFGA** — Zanzibar-model relationship-based authorization,
  built for planet-scale multi-tenant SaaS (millions of objects, complex
  relationship graphs). Running a separate authorization microservice (own
  gRPC API, own datastore) for a self-hosted single-tenant-per-install
  catalog is the wrong scale of tool, and would introduce a second
  source-of-truth the project's invariant #1 ("everything rebuildable from
  Postgres") would then have to account for.

Postgres's `ltree` extension (built-in `contrib`, no external dependency)
natively models materialized-path hierarchies with fast ancestor/descendant
queries (`@>`, `<@`, GiST/B-tree indexes) — exactly the shape of "does a
grant at `media.movies` cover a file at `media.movies.action.2020`." This
keeps the whole permission model queryable in plain SQL, joinable against
`items`/`libraries` in the same transaction, and consistent with the
project's existing bias toward Postgres-native features (`uuidv7()`, GIN on
JSONB, now `ltree`) over adding new services.

---

## 2. Recommended architecture

### 2.1 Auth provider abstraction

A single internal `AuthProvider` protocol with one implementation per
mechanism, all resolving to the same downstream artifact: a `principal_id`
plus a set of asserted group memberships (from LDAP group DNs, SAML
attribute assertions, or OIDC ID-token claims) that the RBAC layer maps to
Filearr groups.

```
AuthProvider.authenticate(credentials) -> AuthResult(principal_id, external_groups, session_hint)
```

- **local**: username/password against `users.password_hash` (argon2-cffi),
  or an existing session cookie.
- **ldap**: bind against configured LDAP server (python-ldap), map
  `memberOf`/configured group-DN filters to Filearr groups on each login
  (LDAP is the source of truth for group membership; Filearr does not let
  an admin manually edit an LDAP-sourced user's groups — matches the
  "external IdP owns identity" model every comparable tool uses).
- **saml**: SP-initiated flow via pysaml2 (xmlsec1 subprocess for signature
  verification, §1.1); attribute assertions mapped to Filearr groups by a
  configurable attribute-name.
- **oidc**: Authlib as the RP; ID-token claims (`groups` claim if the IdP
  emits one, otherwise a configurable claim name) mapped identically.

All four converge on the same `principals` -> `principal_groups` -> RBAC
tables (§2.3), so the RBAC evaluation code has **zero knowledge of which
auth provider produced the principal** — this is the load-bearing design
property that keeps LDAP/SAML/OIDC additive rather than requiring RBAC
rework per provider.

### 2.2 Session + API-key coexistence

Two parallel principal-presentation mechanisms, one evaluation path:

| | Interactive session | API key |
|---|---|---|
| Carrier | HttpOnly+Secure+SameSite=Strict cookie | `Authorization: Bearer` header |
| Backing store | `sessions` table (Postgres) | `api_keys` table (existing, extended) |
| Lifetime | 7d inactivity / 30d absolute / 10min rotation | `expires_at` (existing), no rotation (machine credential) |
| Revocation | Delete session row -> next request 401s | Delete/disable key row (existing) |
| Owning principal | `users.id` (human) | `service_accounts.id` (machine) |
| Used by | Svelte SPA browser session | *arr-style integrations, scripts, other agents |

Both resolve, in `security.py`'s successor, to the same
`Principal(id, kind, scopes, group_ids)` object before hitting any
route handler — route-level dependency injection (`require_scope`,
extended to `require_permission`) doesn't need to know which carrier
authenticated the request.

### 2.3 RBAC data model (DDL)

```sql
-- Every authenticatable actor, human or machine, is a principal.
CREATE TABLE principals (
    id          UUID PRIMARY KEY DEFAULT uuidv7(),
    kind        TEXT NOT NULL CHECK (kind IN ('user', 'service_account')),
    global_role TEXT NOT NULL DEFAULT 'viewer'
                CHECK (global_role IN ('admin', 'user', 'viewer')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at TIMESTAMPTZ  -- soft-disable without deleting audit history
);

CREATE TABLE users (
    principal_id    UUID PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
    username        TEXT UNIQUE NOT NULL,
    email           TEXT,
    password_hash   TEXT,           -- NULL for LDAP/SAML/OIDC-only principals
    auth_provider   TEXT NOT NULL DEFAULT 'local',  -- 'local'|'ldap'|'saml'|'oidc'
    external_subject TEXT,          -- IdP-side stable subject/DN, for re-binding on login
    last_login_at   TIMESTAMPTZ
);

-- ApiKey (existing table) becomes a child of service_accounts, NOT of principals
-- directly, preserving the existing api_keys schema/rows unchanged.
CREATE TABLE service_accounts (
    principal_id UUID PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
    name         TEXT NOT NULL
);
-- api_keys.service_account_id FK added (see migration §3); existing
-- api_keys.scopes (read/write/admin) is preserved as a coarse fallback
-- until/unless an operator opts a key into path-scoped grants.

CREATE TABLE sessions (
    id              UUID PRIMARY KEY DEFAULT uuidv7(),
    principal_id    UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    session_hash    TEXT NOT NULL UNIQUE,  -- sha256 of cookie value, never store raw
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_absolute TIMESTAMPTZ NOT NULL, -- created_at + 30d, fixed at creation
    rotated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip_address      INET,
    user_agent      TEXT
);
CREATE INDEX ix_sessions_principal ON sessions (principal_id);
CREATE INDEX ix_sessions_expiry ON sessions (last_seen_at, expires_absolute);

-- Groups: the RBAC grouping unit, shared by human users and (per phase-5)
-- machine groups. A group is either a plain permission group or tagged as
-- representing a fleet machine-group (mutually referencing phase-5's
-- agent_groups table by id, not duplicating it).
CREATE TABLE principal_groups (
    id          UUID PRIMARY KEY DEFAULT uuidv7(),
    name        TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local','ldap','saml','oidc')),
    external_ref TEXT  -- LDAP group DN / SAML or OIDC group claim value, for sync
);

CREATE TABLE principal_group_members (
    principal_id UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    group_id     UUID NOT NULL REFERENCES principal_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (principal_id, group_id)
);

-- Path-scoped ACL grants. path_key uses ltree, mirroring library/rel_path
-- hierarchy: a library "movies" with rel_path "Action/2020/Film.mkv" encodes
-- as ltree 'movies.Action.2020' (see §2.4 for the encoding function).
CREATE EXTENSION IF NOT EXISTS ltree;

CREATE TABLE path_grants (
    id            UUID PRIMARY KEY DEFAULT uuidv7(),
    group_id      UUID NOT NULL REFERENCES principal_groups(id) ON DELETE CASCADE,
    path_key      LTREE NOT NULL,       -- e.g. 'movies.Action.2020'
    is_deny       BOOLEAN NOT NULL DEFAULT false,  -- explicit deny vs allow
    actions       TEXT[] NOT NULL,      -- subset of the grantable-actions enum
    machine_group_id UUID,              -- optional: scope grant to an agent group (phase-5)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    UUID REFERENCES principals(id)
);
CREATE INDEX ix_path_grants_path_gist ON path_grants USING GIST (path_key);
CREATE INDEX ix_path_grants_group ON path_grants (group_id);

-- Grantable actions (documented as a CHECK constraint or app-level enum,
-- matching future-roadmap.md §3's list):
-- search_metadata, search_content, download, upload, modify, delete,
-- edit_metadata, manage_alerts
```

Indexing notes: the GiST index on `path_key` is what makes "find all grants
whose path is an ancestor of (or equal to) this item's path" a fast indexed
query (`path_grants.path_key @> item_path` or the reverse, depending on
grant-direction convention — Postgres ltree's `@>` reads "is ancestor of").
`ix_sessions_expiry` supports the periodic sweep job that deletes rows past
`expires_absolute` (paired with an application-level check on every request
for the 7-day inactivity rule, since that can't be a single static index
condition against a moving "now").

### 2.4 ltree path encoding

Item identity is `(library_id, rel_path)` (invariant #3) — `rel_path` is an
OS path with arbitrary characters (spaces, dots, non-ASCII), while `ltree`
labels are restricted to `[A-Za-z0-9_]` (with `.` reserved as the level
separator). The encoding function must:
1. Prefix every path with the library's own identifier as the top label
   (`lib_<uuid_no_dashes>`), since grants are always library-scoped first —
   this also sidesteps needing to encode the library name itself.
2. Percent-decode-safe transform each path segment: replace every character
   outside `[A-Za-z0-9_]` with a fixed escape encoding (e.g. hex-pair with a
   sentinel prefix, `_XX`) so segment boundaries can't collide — **this is
   flagged as an open question (§5)**: a naive lossy transform (e.g.
   collapsing all non-alphanumerics to `_`) can make two distinct real
   directories collide into the same ltree label, which would incorrectly
   merge or leak their ACL scope. The encoding must be **collision-free and
   reversible only in the sense of being deterministic** (does not need to
   decode back to the original path string; the canonical `rel_path` already
   lives on `items`/`libraries` for display — the ltree value is purely an
   index key).
3. Truncate/hash long segments (ltree has no hard documented label-count
   limit relevant here, but individual label length and total path length
   have practical ceilings) — deep directory trees or long filenames used
   as intermediate segments should hash to a fixed-width label above a
   threshold, with the mapping stored in a lookup table only if reversibility
   is ever needed for debugging (it should not be needed for the permission
   check itself, which only needs ancestor-matching, not decoding).

### 2.5 Effective-permission evaluation algorithm

Given `(principal_id, item_id, action)`:

1. Resolve `principal.global_role`. If `admin`, short-circuit: **ceiling
   model bounds path grants, but admin's ceiling is "everything"** — no
   path-grant lookup needed. (This is the "ceiling" side of "ceiling model +
   longest-prefix-wins": the global role sets the maximum possible scope;
   path grants can only narrow within it, never exceed it. A `viewer`
   global role can never be handed a path grant that includes `modify` —
   that grant is either rejected at write-time or simply has no effect,
   enforced at grant-creation time, not just at evaluation time, so the
   two checks agree.)
2. Compute the item's `path_key` (library + rel_path, per §2.4).
3. Fetch all `path_grants` rows where `path_key` is an ancestor-or-self of
   the item's path key (`item.path_key <@ path_grants.path_key`), joined
   through `principal_group_members` for the requesting principal (directly
   or via any group the principal belongs to), optionally filtered by
   `machine_group_id` if the item's owning agent falls in that group.
4. Among the matching grants, select the **most specific** (longest
   matching `path_key`, i.e. the one with the most labels — NTFS-style
   "closest applicable ACE wins").
5. At that most-specific level, if **both an allow and an explicit deny**
   exist for the same action, **deny wins** (AWS-style "explicit deny always
   overrides allow at equal specificity" — this is the corrective callout
   against Nextcloud's documented anti-pattern, where a broader "allow"
   rule was observed to override a narrower "deny," which is backwards from
   what operators expect and has caused real access-control surprises in
   that project's issue tracker).
6. If no path grant matches at all, fall back to the global-role default
   (e.g. `viewer` -> `search_metadata` only, no `download`/`modify`).

**Caching strategy**: this evaluation is a hot path (every search result
row, potentially every item-detail fetch) and must not be a fresh multi-table
join per row. Two-tier cache:
- **Per-request memoization**: within a single API request, cache
  `(principal_id, path_key_prefix) -> resolved grant set` since a single
  search response evaluates many items that likely share ancestor path
  prefixes.
- **Short-TTL process-local cache** (30-60s) keyed on
  `(principal_id, group_membership_version)`, invalidated eagerly whenever
  a grant, group membership, or global role changes (bump a version counter
  on write) — this avoids a distributed-cache dependency (no Redis,
  matching project stance) while still bounding staleness to under a
  minute, which is an acceptable window for a permission change to
  propagate given the operator explicitly triggered it and isn't expecting
  instantaneous global effect across every open session.
- The actual **search-time enforcement doesn't re-run this evaluation per
  result at all** — it's compiled once per session into a Meilisearch
  tenant-token filter (§2.6), so Meilisearch itself does the row-level
  filtering at query time using its own (much faster, C++-native) filter
  evaluation. The Postgres-side evaluation algorithm above is what
  *produces* that compiled filter, and is also used directly for
  non-search paths (item detail fetch, PATCH, download) where there's no
  Meilisearch document to filter.

### 2.6 Tenant-token compilation pipeline

On session creation (login) or API-key-scoped search request:

1. Resolve the principal's full set of `path_grants` (allow paths, minus
   deny paths, minus anything above the global-role ceiling) into a list of
   **allowed library/path prefixes** for the `search_metadata` and
   `search_content` actions specifically (tenant tokens gate search
   visibility, not download/modify — those remain Postgres-side checks on
   the actual PATCH/download endpoints).
2. Compile that into a Meilisearch **search rule** — a per-index filter
   expression, e.g. `library_id IN [...] AND (path_prefix STARTS WITH "..."
   OR path_prefix STARTS WITH "...")` — matching the existing plan (§8 of
   future-roadmap.md) to add a filterable `path_prefix`/`acl_groups`
   attribute to indexed documents at index time.
3. Sign a **tenant token** (Meilisearch's native mechanism: a JWT-like
   token signed with a **per-user parent API key**, embedding the compiled
   search rule and an expiry) via `meilisearch-python-sdk`.
4. **Critical constraint, confirmed**: Meilisearch tenant tokens have **no
   per-token revocation** — the only way to invalidate an already-issued
   tenant token before its embedded expiry is to **delete/rotate the parent
   key** that signed it, which invalidates *every* token that parent key
   ever signed. This is why **per-user (or per-session) parent signing
   keys are required**, not one shared parent key for the whole
   application: if all users' tenant tokens were signed by one global
   parent key, revoking any single compromised token (or responding to any
   single permission change) would force-invalidate every other user's
   still-valid session simultaneously — an unacceptable blast radius.
   Practical shape: mint one Meilisearch API key per Filearr principal
   (or per Filearr session, if per-session revocation granularity is
   wanted at the cost of more Meilisearch-side key bookkeeping — per-user is
   the recommended default; per-session is the escalation path if a
   customer needs finer revocation), store its Meilisearch key ID against
   the principal, and rotate/delete that specific parent key on permission
   change, session logout, or account disable.
5. Tenant token TTL should be **short** (minutes, matching session rotation
   cadence in §1.3) and re-minted transparently alongside each session
   rotation — the frontend never sees or stores the Meilisearch parent key,
   only the short-lived tenant token, which is safe to hand to the browser
   for direct Meilisearch queries if that architecture is desired later
   (bypassing the API server for search reads), or simply used server-side
   if the API server remains the sole Meilisearch caller (the safer,
   recommended default for v1 of this feature — direct-from-browser
   Meilisearch calls are a larger blast-radius surface to add later, not a
   default to ship with).
6. **Open question on embedded-filter size**: Meilisearch tenant tokens
   embed the filter expression directly in the signed JWT payload; a
   principal with a very large number of narrow, non-contiguous path
   grants (many individually-granted subdirectories rather than one broad
   grant) could produce a filter expression long enough to bump into
   practical JWT-size/URL-header-size ceilings or Meilisearch's own
   filter-expression complexity limits. Flagged in §5 as needing a
   concrete measurement (how many discrete `path_prefix` clauses before
   this becomes a problem) rather than an assumed-fine answer.

---

## 3. Migration path from current `ApiKey`

**Goal: zero downtime, zero behavior change until an operator opts in.**

1. **Schema migration (additive only)**: create `principals`,
   `service_accounts`, `users`, `sessions`, `principal_groups`,
   `principal_group_members`, `path_grants` (all new tables — no existing
   table is altered destructively). Add `api_keys.service_account_id UUID
   REFERENCES service_accounts(principal_id)`, nullable initially.
2. **Backfill**: for every existing row in `api_keys`, create one
   `principals` row (`kind='service_account'`, `global_role` derived from
   the key's existing `scopes` array — a key with `admin` in `scopes` maps
   to global `admin`; `write`-only maps to `user`; `read`-only maps to
   `viewer`), one matching `service_accounts` row (`name` = the key's
   existing `name` column), and set `api_keys.service_account_id`
   accordingly. This is a pure data-migration script, no manual operator
   action required, and preserves **today's exact authorization behavior**
   — a key that could do everything before can still do everything after,
   because it inherited the `admin` global role which is the ceiling that
   makes path-grant checks moot (§2.5 step 1).
3. **`security.py`'s `_verify_credentials` extends, not replaces**: the
   sha256-hash lookup against `api_keys.key_hash` stays byte-for-byte
   identical; what changes is that the resolved `ApiKey` row now also
   carries a `principal_id` (via `service_account_id`) that the new
   `require_permission` dependency can use for path-scoped checks, while
   `require_scope` (today's coarse read/write/admin check) **keeps working
   unmodified** for any endpoint not yet migrated to path-aware permissions.
   Both dependencies can coexist on different routes during a gradual
   endpoint-by-endpoint migration.
4. **Opt-in path grants**: only once an operator explicitly creates a
   `principal_groups` row and populates `path_grants` does any principal's
   effective permission set narrow below "whatever their global role
   allows everywhere" — the default post-migration state for every
   pre-existing key is **unchanged behavior**, matching invariant-style
   "don't break existing deployments" discipline used elsewhere in this
   project (e.g. T9's `init_db` stamping pre-Alembic DBs instead of forcing
   a rebuild).
5. **New human-user flows (local accounts, LDAP, SAML, OIDC) are additive
   features** layered on the same `principals` table — they do not require
   touching a single existing `api_keys` row.
6. **Sequencing consequence**: this migration path is precisely why the
   task breakdown (§6) puts "local accounts + RBAC core" before "OIDC"
   before "LDAP" before "SAML" — each subsequent provider is pure addition
   to an already-stable `principals`/`path_grants` foundation, never a
   rework of it.

---

## 4. Security notes

- **Secret storage.** LDAP bind-password, SAML SP private key, OIDC client
  secret, and Meilisearch parent-key material are all operator-supplied
  long-lived secrets distinct in kind from user passwords (§1.1) and from
  API keys (existing sha256-at-rest pattern) — they must live in
  environment variables / a secrets file consistent with the existing
  `FILEARR_*` config pattern, **never** in the `principal_groups` or any
  other application-data table, and never logged (the existing
  `security.py` comment "never logged or echoed" for bearer tokens should
  extend explicitly to these IdP secrets in code review).
- **Token signing keys** (session cookies, if any signed component is
  added beyond the opaque `session_hash` design above; and Meilisearch
  per-user parent keys) must be generated with a CSPRNG, never derived from
  predictable material, and the Meilisearch parent-key rotation path (§2.6)
  must be exercised and tested specifically for the "revoke one user, verify
  every *other* user's search still works uninterrupted" property — this
  is the single most likely regression to introduce silently, since a
  shared-parent-key implementation would pass every functional test right
  up until the first real revocation.
- **SSRF in OIDC/SAML metadata fetch.** OIDC discovery
  (`/.well-known/openid-configuration`) and SAML IdP metadata are commonly
  configured by URL, fetched server-side. Textbook SSRF vector if that URL
  is ever editable by a lower-privilege principal. Mitigations: (a)
  metadata-URL config is `admin`-only, never exposed to `user`/`viewer`;
  (b) if self-service IdP registration ever ships, validate against a
  resolved-IP allow/denylist — but note a blanket private-IP block would
  break the **common case** here (Filearr's IdP is often on the same LAN,
  e.g. Authentik/Keycloak on the same Proxmox host), so "admin-only config"
  is the real mitigation, not "block RFC 1918"; (c) Authlib's/pysaml2's
  metadata fetch calls need explicit timeouts and response-size caps
  against a slow/misconfigured IdP.
- **SAML XML-signature pitfalls.** XML Signature Wrapping (XSW) is the
  classic SAML attack: attacker takes a validly-signed response, injects a
  forged unsigned `Assertion` alongside the signed one, and exploits a
  parser that validates the signature against one element but reads claims
  from another. Mitigation is architectural, not library-trust: verify the
  signed `<SignedInfo>` `Reference` points at the *exact* assertion element
  claims are read from (by ID, not document position), and reject responses
  with multiple `Assertion` elements unless explicitly expected. Needs a
  dedicated hand-crafted-XSW-payload test case, not just "trust pysaml2."
- **xmlsec1 subprocess isolation** (§1.1): run with minimal/no passthrough
  env vars and a short hard timeout — subprocess isolation stops
  memory-corruption escape but not CPU/memory exhaustion from an XML-bomb
  payload, so the timeout still matters. Confirm pysaml2's parser uses an
  entity-expansion-safe configuration, same posture as the `defusedxml`
  pattern already adopted for Kodi NFO parsing in T3 (reuse, don't
  reinvent, the "untrusted XML" threat model).
- **Rate limiting / brute-force lockout**: `slowapi` (in-memory or
  Postgres-backed, no Redis — matches project stance) on login and any
  LDAP-bind path. **Authelia's defaults**: 3 failed attempts / 2-minute
  find window / 5-minute ban. Track **per-username and per-source-IP
  independently** — IP-only tracking misses a distributed brute force
  against one username; username-only tracking lets an attacker lock out a
  legitimate user via failed-login spam from elsewhere.
- **Audit logging for reads is off by default.** Immich, Paperless-ngx, and
  Nextcloud all log write/admin actions (login, permission change, delete,
  metadata edit) but not every read/search/view — full read-audit volume
  is high, value is low outside true multi-tenant SaaS (not Filearr's
  shape). Extend the existing `item_versions` pattern to permission-grant
  changes, login/logout, and account lifecycle events; leave read/search
  logging behind an opt-in `FILEARR_AUDIT_READS=true` flag (default off).

---

## 5. Open questions

1. **ltree path-encoding collision safety** (§2.4): the exact escape
   scheme for mapping arbitrary `rel_path` segments (any Unicode, spaces,
   dots) into valid ltree labels needs a concrete, tested specification
   before implementation — a naive lossy encoding risks two distinct real
   directories mapping to the same ltree label, silently merging their ACL
   scope. Needs a worked design doc with test vectors (Unicode filenames,
   segments that are pure digits, segments containing literal `.`)
   before T21 (RBAC core, §6) is implemented.
2. **Tenant-token embedded-filter size ceiling** (§2.6 step 6): unmeasured.
   Needs a concrete test — synthesize a principal with N discrete,
   non-contiguous path grants and find where the compiled Meilisearch
   filter expression or signed-token size becomes impractical, to decide
   whether "many small grants" needs a different compilation strategy
   (e.g. collapsing to a coarser common ancestor with a documented
   precision loss) above some threshold.
3. **CVE year-prefix discrepancy** — **resolved during this research
   pass, not actually a discrepancy**: CVE-2026-57823 and CVE-2026-57824
   are correctly year-prefixed as originally stated; the suspicion that
   they should resolve to a 2025 identifier was unfounded (CVE reservation
   commonly precedes disclosure, and a 2026-numbered CVE patched within
   2026 is unremarkable, not a red flag). No further action needed beyond
   what's already reflected in `docs/future-roadmap.md` §8's pin
   recommendation (Meilisearch >=1.48.2, both CVEs patched there and
   backported to 1.47.1).
4. **Authlib pin currency**: this brief corrects the pin to `>=1.6.9`
   (from the prior placeholder `>=1.7.2`, which named a version that
   does not appear to have shipped a security fix as of this research
   date). Authlib's advisory list is actively growing (several further
   2026 GHSA entries were seen but not triaged in §1.1) — **re-verify the
   pin against the live advisory list at implementation time**, not
   against this document, since this is exactly the kind of fast-moving
   fact that goes stale between research and coding.
5. **Group-membership sync cadence for LDAP/SAML/OIDC-sourced groups**:
   should Filearr re-resolve a principal's external group memberships on
   every login only (simple, but a group change made server-side at the
   IdP doesn't take effect until next login — could be hours/days for an
   infrequently-logging-in user), or also on a periodic background
   refresh (adds complexity and an LDAP-polling job, but closes that gap)?
   No decision made; flagged for whoever implements T24/T25 (§6) to choose
   based on how urgently permission *revocations* specifically need to
   propagate (permission *grants* propagating late is low-risk; a
   revoked user retaining old group access until next login is the
   higher-risk direction and argues for at least a periodic refresh for
   `admin`-tier group memberships specifically, even if regular groups
   stay login-time-only).
6. **Per-session vs per-user Meilisearch parent keys** (§2.6 step 4):
   this brief recommends per-user as the default with per-session as an
   escalation path, but doesn't resolve which one ships first. Per-session
   gives strictly finer revocation (log out one browser tab's session
   without touching the user's other active sessions) at the cost of N
   Meilisearch API keys per active user instead of 1 — worth revisiting
   once real Meilisearch API-key-count operational limits (if any) are
   understood at the scale this project actually expects to run at.
7. **Machine-group / path-grant interaction with phase-5's `agent_groups`**:
   `path_grants.machine_group_id` (§2.3) is sketched as a plain nullable FK
   reference, but phase-5's brief (§11 there) flags this exact
   cross-cutting dependency as unresolved from its side too — the two
   research passes agree a dependency exists but neither has pinned the
   exact join/ownership semantics (does a path grant scoped to a machine
   group mean "this grant only applies to items whose source agent is in
   that group," and if an item's source agent changes group membership
   after the fact, does the grant apply retroactively or only prospectively).
   Needs a joint design pass with whoever implements the agent-groups
   table.

---

## 6. Task breakdown (T20-T31)

Sequenced local-accounts -> RBAC core -> tenant tokens -> OIDC -> LDAP -> SAML,
per the project's stated preference to ship the foundation and the
highest-value/lowest-complexity federation option before the two
XML/directory-heavy integrations.

**T20 (M) — Principals/users/sessions schema + local-account auth**
Create `principals`, `users`, `service_accounts`, `sessions` tables (§2.3).
Migrate existing `api_keys` rows into `service_accounts`/`principals`
per §3 steps 1-2. Implement local username/password login
(argon2-cffi hashing), session-cookie issuance
(HttpOnly/Secure/SameSite=Strict), and the 7d/30d/10min lifecycle (§1.3).
*Accept*: existing API-key-authenticated requests behave identically
post-migration (regression suite passes unmodified); a new local user can
log in, get a session cookie, and have it expire per the stated policy in
an integration test that fast-forwards time; passwords are never
recoverable from the `users` table (only argon2 hash present).

**T21 (L) — RBAC core: groups, path_grants, ltree, evaluation algorithm**
Create `principal_groups`, `principal_group_members`, `path_grants`
tables + `ltree` extension (§2.3). Implement the path-encoding function
(§2.4, resolving open question #1 with a concrete tested scheme) and the
effective-permission evaluation algorithm (§2.5) including the
per-request-memoization and short-TTL caching layers. Build an admin-console
UI (or API-only for v1 if UI is deferred) for creating groups and granting
paths.
*Accept*: a principal in a group with a `path_grants` row scoped to
`movies.Action` can access items under that path and is denied items
under `movies.Comedy`; an explicit deny at `movies.Action.2020` overrides
an allow at `movies.Action` (equal-specificity deny-wins test per §2.5
step 5); a `viewer` global-role principal cannot be granted `modify` via
path grant (ceiling enforced at grant-creation time).

**T22 (M) — Meilisearch tenant-token compilation pipeline**
Add `path_prefix`/`acl_groups` filterable attributes to indexed documents
(coordinate with `index_sync` task). Implement per-user Meilisearch
parent-key minting/storage, the grant->filter compilation (§2.6 steps 1-2),
and tenant-token issuance/rotation tied to session rotation cadence.
*Accept*: a user's search results are correctly filtered to their granted
paths purely via the Meilisearch-side tenant-token filter (verified by
issuing a raw request with the tenant token directly against Meilisearch,
bypassing the API server, to confirm enforcement doesn't rely on
API-server-side post-filtering as a crutch); revoking one user's parent
key does not affect any other user's active tenant token (the specific
regression named in §4).

**T23 (S) — `require_permission` FastAPI dependency + endpoint migration**
Build the route-level dependency that wraps §2.5's evaluation algorithm,
coexisting with today's `require_scope` (§3 step 3). Migrate item-detail,
PATCH, and download endpoints from `require_scope` to `require_permission`
one at a time.
*Accept*: each migrated endpoint has a test proving both the coarse
(global-role-only, no path grants configured) and fine-grained
(path-grant-restricted) cases behave correctly; unmigrated endpoints
continue passing their existing `require_scope` tests unmodified.

**T24 (M) — OIDC provider (Authlib)**
Implement RP-side OIDC flow: discovery-document fetch (with SSRF
mitigations per §4 — admin-only config, timeout/size caps), authorization-
code flow, ID-token validation (explicitly test the `at_hash`/`c_hash`
binding correctly, since CVE-2026-28498 was exactly a fail-open bug in
this check), and claim->group mapping. Pin `authlib>=1.6.9` (§1.1).
*Accept*: login via a real test IdP (e.g. a local Keycloak/Authentik
container) succeeds and correctly maps IdP groups to Filearr
`principal_groups`; a forged/replayed ID token with mismatched
`at_hash` is rejected (regression test for the specific CVE class).

**T25 (M) — LDAP provider (python-ldap)**
Implement bind-based authentication against a configured LDAP server,
group-DN mapping to `principal_groups`, and the group-membership
sync-cadence decision from open question #5.
*Accept*: login against a real test LDAP server (e.g. OpenLDAP test
container) succeeds; a user removed from an LDAP group loses the
corresponding Filearr group membership within the chosen sync cadence,
verified in an integration test.

**T26 (L) — SAML provider (pysaml2 + xmlsec1 subprocess)**
Implement SP-initiated SAML login, xmlsec1-subprocess signature
verification with the restrictive execution environment from §4
(timeout, minimal env passthrough), attribute-assertion->group mapping,
and the XSW-specific test case from §4 (hand-crafted multi-Assertion
payload must be rejected).
*Accept*: login against a real test IdP (e.g. a local SimpleSAMLphp or
Keycloak-as-SAML-IdP container) succeeds; the XSW payload test fails
closed (login rejected, not silently authenticated as the wrong
identity); xmlsec1 subprocess timeout is verified against a
deliberately slow/oversized malicious response.

**T27 (S) — Rate limiting + lockout (slowapi + Authelia-pattern defaults)**
Apply slowapi to login and any credential-check paths; implement the
3-attempt/2-minute-window/5-minute-ban defaults (§4), tracked
per-username and per-source-IP independently.
*Accept*: 4 failed logins within 2 minutes for one username trigger a
5-minute lockout for that username regardless of source IP; a
distributed brute force (many IPs, one username) is still caught by the
per-username tracking even though no single IP crosses its own threshold.

**T28 (S) — Audit logging extension**
Extend the existing `item_versions` audit pattern to permission-grant
changes, login/logout events, and account lifecycle events (create/
disable/role-change). Add the `FILEARR_AUDIT_READS` opt-in flag (default
off) for read/search-level audit logging (§4).
*Accept*: a permission-grant change, a login, and a role change each
produce an inspectable audit row with actor/timestamp/action; with
`FILEARR_AUDIT_READS=false` (default) a search request produces no audit
row; with it set `true`, it does.

**T29 (S) — Service-account management UI/API**
Admin-console CRUD for `service_accounts` (rename, disable, view
associated `api_keys`, create new keys under an explicit service account
rather than today's bare unowned-key creation flow).
*Accept*: a new API key can only be created under an explicit service
account (no more orphan keys); an existing pre-migration key correctly
displays its backfilled service-account owner (§3 step 2).

**T30 (M) — Session management UI ("active sessions," remote logout)**
Admin/self-service UI listing a principal's active sessions
(IP/user-agent/last-seen) with per-session and "log out everywhere"
revocation, exercising the instant-revocation property that was the core
argument against stateless JWT (§1.3).
*Accept*: revoking a session from the UI invalidates it on the very next
request from that session's cookie (no waiting for expiry); "log out
everywhere" invalidates all of a principal's sessions but not other
principals'.

**T31 (S) — Documentation + operator migration guide**
Document the new auth-provider configuration surface
(`FILEARR_AUTH_*` env vars per provider), the zero-downtime migration
behavior (§3) so existing operators understand nothing changes until they
opt in, and a worked example of setting up one path-scoped group grant
end-to-end.
*Accept*: a fresh operator following only the doc can configure OIDC
login and one path-scoped group without needing to read this research
brief or the source code.

---

## Sources cited

- Meilisearch security advisories (CVE-2026-57823, CVE-2026-57824,
  patched v1.48.2, backported v1.47.1): https://github.com/meilisearch/meilisearch/security
- fastapi-users maintenance-mode status, last release 2026-03-27:
  https://github.com/fastapi-users/fastapi-users
- ldap3 stalled-since-2021 status (last stable 2.9.1, 2021-07-18):
  https://pypi.org/project/ldap3/
- python-ldap active-release status: https://github.com/python-ldap/python-ldap/releases
- Authlib security advisories (CVE-2024-37568, CVE-2025-61920,
  CVE-2025-68158, CVE-2026-27962, CVE-2026-28498):
  https://github.com/authlib/authlib/security/advisories
- Grafana session/token defaults (`login_maximum_inactive_lifetime_duration`
  =7d, `login_maximum_lifetime_duration`=30d,
  `token_rotation_interval_minutes`=10):
  https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-authentication/
- Internal: `docs/future-roadmap.md` §3, §8, §9; `backend/filearr/security.py`;
  `backend/filearr/models.py`; `docs/research/phase-5-distributed-agents.md`
  §11 (machine-groups/RBAC cross-dependency).
