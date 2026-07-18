# Enabling authentication (Phase 6, P6-T1)

Filearr ships **auth OFF by default** (`FILEARR_AUTH_ENABLED=false`) for a
trusted LAN: no login wall, existing Bearer API keys and their read/write/admin
scopes behave exactly as before. Turning auth on is a deliberate, additive,
zero-downtime opt-in — nothing about the API-key path changes.

## What "auth on" gives you

- Interactive **username/password login** for the web UI, backed by
  **Postgres-stored sessions** (an HttpOnly `filearr_session` cookie). Sessions
  are instantly revocable (delete the row) — no stateless-JWT blind spot.
- A global role per user (`admin` / `user` / `viewer`) mapped onto the same
  scope vocabulary the API keys use: `admin` → admin+write+read, `user` →
  write+read, `viewer` → read only.
- **API keys keep working unchanged**, in parallel with sessions. A request is
  accepted if it carries EITHER a valid Bearer key OR a valid session cookie.

Path-scoped ACLs (per-folder grants), tenant-token search filtering, and
OIDC/LDAP/SAML federation are later Phase-6 tasks (P6-T2..T7); this round is the
identity + login foundation.

## One-time enable (set flag → bootstrap → login)

1. **Serve over TLS first.** The session cookie is marked `Secure` only when the
   request is https (honouring `X-Forwarded-Proto` from the Caddy front, OPS-T1).
   Use `https://<host>:8443`. Plain http still works temporarily, but the login
   page will warn that credentials are unprotected in transit. If you run behind
   the Caddy proxy, start uvicorn with `--proxy-headers` so the request scheme is
   trusted.

2. **Set the flag** and restart the app container:

   ```
   FILEARR_AUTH_ENABLED=true
   ```

3. **Bootstrap the first admin.** While zero users exist the UI shows a
   *first-run* form (or POST directly):

   ```
   curl -X POST https://<host>:8443/api/v1/auth/bootstrap \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"a-strong-passphrase"}'
   ```

   Bootstrap is **once-only** — it returns `409` after any user exists. The first
   account is always `admin` and cannot be deleted while it is the last admin
   (so you can never lock yourself out).

4. **Log in.** Visit `https://<host>:8443/`, sign in, and manage further users
   from the admin surface (`/api/v1/auth/users`). Passwords are hashed with
   Argon2id (`argon2-cffi`); the plaintext is never stored or recoverable.

## Session lifecycle (tunable)

| Setting | Default | Meaning |
|---|---|---|
| `FILEARR_SESSION_TTL_HOURS` | `720` (30d) | absolute cap, fixed at login |
| `FILEARR_SESSION_INACTIVITY_HOURS` | `168` (7d) | idle window (sliding) |
| `FILEARR_SESSION_ROTATION_MINUTES` | `10` | opaque-token rotation cadence |
| `FILEARR_SESSION_COOKIE_NAME` | `filearr_session` | cookie name |

A role change, password change, or account disable **revokes all of that
principal's sessions immediately** (they must re-authenticate).

## Turning it back off

Set `FILEARR_AUTH_ENABLED=false` and restart. The gate becomes a no-op again;
user rows and sessions remain in the database (harmless) and re-activate if you
flip the flag back on. No migration is involved either way.

## Search scoping (P6-T3) — post-deploy rebuild REQUIRED

RBAC now scopes **search results** to each user's granted paths. Enforcement is
server-side: the API compiles the caller's grants (allow-prefixes minus
deny-prefixes, deny-wins, exactly matching `rbac.evaluate`) into a Meilisearch
filter and injects it into every search query — `/search`, `/search/tags`, and
`/items/{id}/similar`. The browser never talks to Meilisearch directly, so no
tenant tokens / per-user parent keys are issued (that stays a phase-9 escalation
for a future browser-direct path).

**Who gets scoped:** interactive **session** users with a non-admin global role.
Unaffected (no filter injected, byte-identical to before): `admin` users, Bearer
**API keys** (trusted integrations — per-key path scoping is future
service-account work), and everything when `FILEARR_AUTH_ENABLED=false`.

**REQUIRED after deploying this version:** run a full index rebuild so existing
documents gain the new `path_scope` attribute the filter matches on:

```bash
# enqueue the rebuild task (shadow-index swap; live search stays up)
docker compose run --rm app python -c \
  "import asyncio; from filearr.tasks.index_sync import rebuild_index; asyncio.run(rebuild_index())"
```

Until the rebuild completes, scope-filtered (non-admin) users will see **empty**
results (fail-closed — never over-share); admins and API keys are unaffected.
New/rescanned items get `path_scope` automatically. Items scanned before P6-T2
(no `path_scope` stamped) are invisible to scoped users until re-scanned.

**Grant changes take effect on the next request** — the filter is recompiled
from live DB grants every query (no cache yet; the P6-T4 grant cache will add
invalidation). A role change also revokes the user's sessions immediately.

**Too many narrow grants?** If a user's compiled filter exceeds
`FILEARR_MEILI_SCOPE_FILTER_CEILING` (default 4096 chars) the search returns
**HTTP 422 "consolidate grants"** rather than silently widening or narrowing the
scope — fold the many narrow grants into fewer, broader path scopes.

## Endpoint enforcement (P6-T4) — path-scoped RBAC on data routes

P6-T3 scoped **search**; P6-T4 extends the same grant model to the rest of the
API. A route-level dependency, `require_permission(action)`, sits ON TOP of the
coarse read/write/admin scope gate and refines it per RESOURCE via the pure
`rbac.evaluate` engine over each item's `path_scope`.

**Who is refined:** interactive **session** users with a non-admin global role.
**Unaffected (fast path, byte-identical to before):** `admin` sessions, Bearer
**API keys** (trusted integrations — per-key path scoping is future
service-account work), and everything when `FILEARR_AUTH_ENABLED=false`.

**What is enforced now**

| Surface | Action | Behaviour for a scoped user |
|---|---|---|
| `GET /items/{id}` (+ `/thumb`, `/copies`, `/copy-counts`, `/similar`) | `search_metadata` | item outside read scope → **404** (existence never leaked); copies/thumbs of un-granted items omitted |
| `PATCH /items/{id}`, `POST /items/batch` | `edit_metadata` | readable but edit-denied → **403**; unreadable → **404**; per-item in a batch (one denial doesn't fail the batch) |
| `POST /items/{id}/transfer`, transfer download | `download` (coarse `write`) | RBAC gate BEFORE any side effect; 404/403 per the ruling |
| `GET /reports/{id}`, `GET /custom-reports/{id}/run` (json + csv/ndjson/xml) | `search_metadata` | rows the user can't read are absent from the page AND every export format |
| `GET /libraries/{id}/tree` (browse), `GET /stats/timeline` | `search_metadata` | folders/files/histogram counts limited to readable items |

**404 vs 403 (deliberate):** an *unreadable* item returns **404** so its very
existence isn't disclosed to a user outside its scope; **403** is used only when
the item IS readable but the specific (write/download) action is denied.

**Collection filtering** is a single SQL predicate over `items.path_scope`
(`rbac_sql.scope_where_clause`) that reproduces `rbac.evaluate` exactly
(longest-prefix-wins, explicit-deny-wins, ceiling clamp, default-deny). In
production it uses the native ltree `<@` descendant test; the test sandbox's
`text` column uses an equivalent `starts_with` prefix match. A NULL `path_scope`
(pre-P6-T2 item, not yet backfilled) matches nothing → invisible to scoped users
(fail-closed).

**Grant freshness / cache.** Grants are resolved once per request (memoized) and
cached in-process for 30s. ANY grant/group/membership change — and any role
change/disable — **immediately** invalidates the cache (a generation counter),
so an edit takes effect on the caller's next request with no staleness window;
the TTL is only a backstop. (A role change also revokes the principal's sessions,
as before.)

**NFC/NFD grant advisory.** Creating a path grant whose folder is a Unicode
NFC/NFD sibling of an existing grant (same on-screen name, different bytes →
separate ltree scopes) returns a non-blocking `warnings[]` note so an admin can
reconcile — path encoding stays byte-exact (no silent normalization, R7).

**Admin/config surfaces** (libraries, scans, system/jobs, RBAC admin, alert
channels, custom-field defs) remain admin/write-scope only — unchanged.

---

# OIDC single sign-on (Phase 6, P6-T5)

OIDC/OpenID-Connect SSO lets users sign in through an external identity provider
(Authelia, Keycloak, Authentik, Pocket-ID, or any spec-compliant IdP) instead of
a local password. It is a **pure addition**: with `FILEARR_OIDC_ENABLED=false`
(the default) the login page is byte-for-byte the local form and nothing here
runs. SSO logins **mint the exact same Postgres session** as a local login — there
is no parallel auth path, so revocation, rotation, and the read/write/admin scope
mapping all behave identically.

## Flow (what happens)

1. The user clicks **"Sign in with SSO"** → `GET /api/v1/auth/oidc/login`.
2. Filearr generates a single-use `state`, a `nonce`, and a **PKCE** (S256)
   verifier, stores them server-side (TTL `FILEARR_OIDC_LOGIN_STATE_TTL_MINUTES`,
   default 10), and 302-redirects to the IdP authorization endpoint.
3. The IdP authenticates the user and redirects back to
   `GET /api/v1/auth/oidc/callback?code=…&state=…`.
4. Filearr validates `state` (single-use), exchanges the code (PKCE) for tokens,
   and **validates the ID token**: signature via the IdP JWKS, `iss`/`aud`/`exp`,
   `nonce`, and the `at_hash` binding to the access token. Signature algorithms
   are allow-listed to asymmetric families (`none`/HMAC are never accepted).
5. It **JIT-provisions or links** the user, applies role + group mapping, mints
   the `filearr_session` cookie, and 302s to the app.

Any failure redirects to `/` with a generic `?sso_error=<reason>` flag — never a
stack trace, never a partial session (fail-closed).

## Cookie SameSite ruling

The session cookie SameSite default **changed from `strict` (P6-T1) to `lax`**
(`FILEARR_SESSION_COOKIE_SAMESITE`). An OIDC callback is a top-level *cross-site*
navigation from the IdP; under `SameSite=Strict` the freshly-minted cookie is
withheld on the callback's redirect to `/`, so the user lands logged-out. `lax`
sends the cookie on top-level GET navigations (the SSO return) while STILL
withholding it on cross-site POST/PATCH/DELETE and sub-resource requests — every
state-changing Filearr endpoint is a non-GET JSON API, so CSRF protection is
preserved. Operators who never use SSO may set it back to `strict`.

## Configuration (env only)

| Variable | Default | Meaning |
|---|---|---|
| `FILEARR_OIDC_ENABLED` | `false` | Master switch. |
| `FILEARR_OIDC_ISSUER` | — | IdP issuer URL (https, or loopback for dev). Discovery is `{issuer}/.well-known/openid-configuration`. |
| `FILEARR_OIDC_CLIENT_ID` | — | Registered client id. |
| `FILEARR_OIDC_CLIENT_SECRET` | — | Client secret. Omit for a public (PKCE-only) client. |
| `FILEARR_OIDC_SCOPES` | `openid profile email` | Requested scopes (`openid` is force-added). |
| `FILEARR_OIDC_REDIRECT_URI` | derived | Override when the public URL can't be inferred from the request. Must match the IdP client registration. |
| `FILEARR_OIDC_ROLE_CLAIM` | — | ID-token claim carrying role/group values. |
| `FILEARR_OIDC_ROLE_MAP` | — | `claimval:role,claimval:role` (e.g. `filearr-admins:admin,staff:user`). Highest-privilege match wins. |
| `FILEARR_OIDC_DEFAULT_ROLE` | `viewer` | Role when nothing maps. **Set EMPTY to REFUSE unmapped users** (fail-closed). |
| `FILEARR_OIDC_AUTO_PROVISION` | `true` | Create a local account on first login (no password — SSO-only). |
| `FILEARR_OIDC_USERNAME_CLAIM` | `preferred_username` | Username source (falls back to email local-part, then `sub`); numeric suffix on collision. |
| `FILEARR_OIDC_LINK_BY_EMAIL` | `false` | Link an SSO login to an existing LOCAL account by exact, IdP-verified email. **Account-takeover surface — leave off unless the IdP's email is trustworthy.** |
| `FILEARR_OIDC_GROUP_CLAIM` | — | Claim whose values map, BY NAME, to existing `principal_groups` (never auto-created). |
| `FILEARR_OIDC_LOGIN_STATE_TTL_MINUTES` | `10` | Single-use state/nonce/PKCE lifetime. |

### Role & group mapping (applied at every login)

Role and group membership are re-evaluated on **every** login, so an IdP-side
change takes effect the next time the user signs in (and the RBAC grant cache is
invalidated so it applies immediately). Group sync matches the group claim values
by name to existing Filearr groups: a `source='oidc'` group is fully IdP-managed
(joined AND left as the claim changes); a `source='local'` group name-match is
**add-only** so an admin's manual membership is never clobbered.

## Worked example — Authelia

```yaml
# authelia configuration.yml (identity_providers.oidc.clients)
- client_id: filearr
  client_secret: '<hashed-secret>'
  public: false
  authorization_policy: two_factor
  redirect_uris:
    - https://filearr.example.com/api/v1/auth/oidc/callback
  scopes: [openid, profile, email, groups]
```
```bash
FILEARR_OIDC_ENABLED=true
FILEARR_OIDC_ISSUER=https://auth.example.com
FILEARR_OIDC_CLIENT_ID=filearr
FILEARR_OIDC_CLIENT_SECRET=<plaintext-secret>
FILEARR_OIDC_SCOPES="openid profile email groups"
FILEARR_OIDC_ROLE_CLAIM=groups
FILEARR_OIDC_ROLE_MAP="filearr-admins:admin,filearr-users:user"
FILEARR_OIDC_DEFAULT_ROLE=          # empty => refuse anyone not in a mapped group
FILEARR_OIDC_GROUP_CLAIM=groups
```

**Keycloak:** issuer `https://kc.example.com/realms/<realm>`; add a
*Group Membership* mapper (unset "Full group path") to a `groups` claim; set the
redirect URI as above. **Pocket-ID / Authentik:** same shape — point the issuer
at the provider's discovery base and register the callback URI.

## Break-glass / lockout prevention

The **first admin is always created locally** (`POST /auth/bootstrap`), and SSO
never provisions an admin unless your `ROLE_MAP` says so. If the IdP is down or a
role mapping locks everyone out, **disable OIDC** (`FILEARR_OIDC_ENABLED=false`)
and log in with the local bootstrap admin — the local password path is always
available. SSO-only accounts (no password hash) cannot use the local form; a
password login for them is rejected with a clear error.

## RP-initiated (IdP) logout — deferred

Filearr does not persist the raw ID token, so it cannot send an `id_token_hint`
to the IdP `end_session_endpoint`. Logout therefore revokes the **local** Filearr
session immediately (the instant-revocation property) but does not sign the user
out at the IdP. Close the browser or log out at the IdP directly if a full
federated logout is required. (A best-effort RP-initiated logout can be added
later if the ID token is stored alongside the session.)

## Authlib version pin (R5)

The Authlib floor was **re-verified against the live GitHub advisory list at
implementation (2026-07-13)**. The newest advisory at that time
(GHSA-w8p2-r796-3vmq, 2026-06-08) is patched only in 1.6.10/1.7.1, so the pin
moved **up** from the brief's `>=1.6.9` to **`authlib>=1.7.1`** (resolves to
1.7.2). Re-check the advisory list again on any future bump.

# LDAP / Active Directory sign-in (Phase 6, P6-T6)

LDAP lets users sign in with their existing directory (OpenLDAP, Active
Directory, FreeIPA, etc.) credentials. Like OIDC it is a **pure addition**: with
`FILEARR_LDAP_ENABLED=false` (default) nothing engages and `/auth/login` is the
plain local form. There is **no new UI** — the same username/password form posts
to the same `/auth/login`; the directory verifies the password via a real bind.

## Flow (local-first, then LDAP)

1. `/auth/login` first tries the **local** password path. A local account always
   wins — a same-named local admin stays local, and a wrong local password never
   falls through to the directory.
2. If local auth does not succeed **and** the username is unknown or already
   ldap-sourced **and** LDAP is configured, the request falls through to LDAP:
   * **Search-then-bind** (default): a service account (or an anonymous bind when
     none is set) searches `FILEARR_LDAP_USER_BASE` for
     `FILEARR_LDAP_USER_FILTER` (with `{username}` **LDAP-escaped**), then binds
     as the found DN with the **presented password** — the only place the
     password is ever checked (never a filter/attribute compare).
   * **Direct-bind**: set `FILEARR_LDAP_USER_DN_TEMPLATE` (e.g.
     `uid={username},ou=people,dc=ex,dc=com` or AD `{username}@corp.example.com`)
     to bind straight to a derived DN with no search. `{username}` is DN-escaped.
3. On a successful bind the user is JIT-provisioned as an **SSO-only** account
   (`password_hash` NULL — the local form is refused for them), keyed by the
   stable identity `(ldap, <server host>, <subject>)`. The subject prefers an
   immutable operational attribute (`entryUUID` / AD `objectGUID`) and falls back
   to the DN with a logged warning (a rename would then orphan the account).
4. The group→role map is evaluated on **every** login; optional group sync
   reconciles `principal_groups`. A role/group change bumps the grant cache.

## Transport security (never silent plaintext)

* `ldaps://…` → implicit TLS from the first byte.
* `ldap://…` to a **non-loopback** host → **StartTLS is required** by default
  (`FILEARR_LDAP_START_TLS=true`). Filearr **refuses** to bind plaintext to a
  remote host unless you *explicitly* set `FILEARR_LDAP_ALLOW_PLAINTEXT=true`
  (logged loudly on every login). `ldap://localhost` is allowed plaintext for
  local dev only.
* Server-cert verification is **on** by default
  (`FILEARR_LDAP_TLS_VERIFY=true`); point `FILEARR_LDAP_TLS_CA_CERT_FILE` at your
  CA bundle for a private CA. Setting verify off logs a warning — do not run that
  way in production.
* Referrals are disabled (a hostile server cannot bounce the bind elsewhere),
  connections are read-only, and connect+receive timeouts are enforced
  (`FILEARR_LDAP_TIMEOUT`).

## Injection posture

Every user-supplied value is escaped: `escape_filter_chars` into search filters
(the `{username}` and `{user_dn}` placeholders) and RFC-4514 DN-escaping into the
`USER_DN_TEMPLATE`. An **empty password is rejected before any socket is opened**
(RFC 4513 unauthenticated-/anonymous-bind class — otherwise a blank password
could "succeed" as an anonymous bind and impersonate). A search that returns zero
or more than one entry is refused (unknown / ambiguous). The full injection
matrix (`)(*\/`, NUL, `*` wildcard) is covered by `tests/test_ldap_p6t6.py`.

## Configuration (env only)

| Env | Default | Notes |
|-----|---------|-------|
| `FILEARR_LDAP_ENABLED` | `false` | Master switch. |
| `FILEARR_LDAP_SERVER` | — | `ldaps://dc.example.com` or `ldap://…`. |
| `FILEARR_LDAP_START_TLS` | `true` | Upgrade an `ldap://` remote via StartTLS. |
| `FILEARR_LDAP_ALLOW_PLAINTEXT` | `false` | Escape hatch: plaintext `ldap://` to a remote host (warns). |
| `FILEARR_LDAP_TLS_VERIFY` | `true` | Verify the server certificate. |
| `FILEARR_LDAP_TLS_CA_CERT_FILE` | — | CA bundle for a private CA. |
| `FILEARR_LDAP_TIMEOUT` | `10` | Connect + receive timeout (seconds). |
| `FILEARR_LDAP_BIND_DN` | — | Service account for search/group reads. Empty ⇒ anonymous search bind. |
| `FILEARR_LDAP_BIND_PASSWORD` | — | Service-account password. |
| `FILEARR_LDAP_USER_DN_TEMPLATE` | — | Direct-bind template (`{username}` DN-escaped). Set this OR the search pair below. |
| `FILEARR_LDAP_USER_BASE` | — | Search base for search-then-bind. |
| `FILEARR_LDAP_USER_FILTER` | `(uid={username})` | AD: `(sAMAccountName={username})`. `{username}` is filter-escaped. |
| `FILEARR_LDAP_ATTR_USERNAME` | `uid` | AD: `sAMAccountName`. |
| `FILEARR_LDAP_ATTR_EMAIL` | `mail` | |
| `FILEARR_LDAP_ATTR_UID` | `entryUUID` | Stable subject attribute. AD: `objectGUID`. Falls back to the DN. |
| `FILEARR_LDAP_USE_MEMBEROF` | `false` | Read groups from the user's `memberOf` (AD default) instead of a group search. |
| `FILEARR_LDAP_ATTR_MEMBEROF` | `memberOf` | |
| `FILEARR_LDAP_GROUP_BASE` | — | Group search base (group-search mode). |
| `FILEARR_LDAP_GROUP_FILTER` | `(member={user_dn})` | `{user_dn}` is filter-escaped. |
| `FILEARR_LDAP_ROLE_MAP` | — | `groupDN=>role;groupDN=>role`. DNs contain commas, so pairs are `;`-separated and the DN/role delimiter is `=>`. Matched case-insensitively; highest-privilege wins. |
| `FILEARR_LDAP_DEFAULT_ROLE` | — (empty) | Role when nothing maps. **Empty ⇒ REFUSE unmapped users** (fail-closed). |
| `FILEARR_LDAP_AUTO_PROVISION` | `true` | JIT-create an SSO-only account on first successful bind. |
| `FILEARR_LDAP_GROUP_SYNC` | `false` | Sync LDAP groups → existing `principal_groups` matched BY NAME (the group CN), `source='ldap'` (add+remove for ldap-sourced only; a `source='local'` name-match is add-only). |

## Worked example — Active Directory (search-then-bind, LDAPS)

```env
FILEARR_LDAP_ENABLED=true
FILEARR_LDAP_SERVER=ldaps://dc01.corp.example.com
FILEARR_LDAP_BIND_DN=CN=filearr-svc,OU=Service,DC=corp,DC=example,DC=com
FILEARR_LDAP_BIND_PASSWORD=<service-account-password>
FILEARR_LDAP_USER_BASE=DC=corp,DC=example,DC=com
FILEARR_LDAP_USER_FILTER=(sAMAccountName={username})
FILEARR_LDAP_ATTR_USERNAME=sAMAccountName
FILEARR_LDAP_ATTR_UID=objectGUID
FILEARR_LDAP_USE_MEMBEROF=true
FILEARR_LDAP_ROLE_MAP=CN=Filearr Admins,OU=Groups,DC=corp,DC=example,DC=com=>admin;CN=Filearr Users,OU=Groups,DC=corp,DC=example,DC=com=>user
FILEARR_LDAP_DEFAULT_ROLE=            # empty => refuse anyone not in a mapped group
FILEARR_LDAP_GROUP_SYNC=true
```

## Worked example — OpenLDAP (StartTLS, group search)

```env
FILEARR_LDAP_ENABLED=true
FILEARR_LDAP_SERVER=ldap://ldap.example.com     # upgraded via StartTLS (default)
FILEARR_LDAP_START_TLS=true
FILEARR_LDAP_TLS_CA_CERT_FILE=/etc/filearr/ca.pem
FILEARR_LDAP_BIND_DN=cn=readonly,dc=example,dc=com
FILEARR_LDAP_BIND_PASSWORD=<readonly-password>
FILEARR_LDAP_USER_BASE=ou=people,dc=example,dc=com
FILEARR_LDAP_USER_FILTER=(uid={username})
FILEARR_LDAP_ATTR_UID=entryUUID
FILEARR_LDAP_GROUP_BASE=ou=groups,dc=example,dc=com
FILEARR_LDAP_GROUP_FILTER=(member={user_dn})
FILEARR_LDAP_ROLE_MAP=cn=admins,ou=groups,dc=example,dc=com=>admin;cn=staff,ou=groups,dc=example,dc=com=>user
FILEARR_LDAP_DEFAULT_ROLE=
```

## LDAP library choice — ldap3 (override of the research doc)

The research doc (`docs/research/phase-6-identity-auth-rbac.md` §1.1) recommended
**python-ldap**. P6-T6 ships **ldap3** instead, a reasoned override:

* **Offline testability (decisive).** ldap3 ships a `MOCK_SYNC` strategy that
  runs the whole bind/search path in memory, so the security-critical injection /
  empty-password / group-mapping matrix runs **network-free** in CI. python-ldap
  has no offline harness — it needs a live directory, which the sandbox cannot
  provide, so those tests could not exist at all.
* **Clean install.** ldap3 is pure-Python (no libldap/SASL C headers or compiler);
  python-ldap builds a C extension.
* **No known CVEs.** GitHub Advisory DB + PyPI checked live 2026-07-13 — ldap3
  has none. Its only knock is maintenance staleness (last release 2.9.1, 2021);
  since LDAPv3 bind/search is a **frozen protocol**, a stable-but-unmaintained
  client is a maintenance risk, not a vulnerability risk.
* **License:** LGPL-3.0 — AGPL-3.0-compatible.

All directory I/O is isolated behind `filearr.ldap_auth.connect`, so a future
swap to python-ldap (or bonsai) is a localized change.

# SAML sign-in (Phase 6, P6-T7) — DEFERRED

SAML SP support is **deferred, not shipped**. Ruling (2026-07-13; priority order
security > integrity > reliability > speed):

* The research-chosen library **pysaml2** (latest 7.5.4, Oct 2025) hard-pins
  `pyopenssl<24.3.0`, which transitively requires `cryptography<44`. Filearr
  deliberately pins **`cryptography==48.0.0`** (the AES-GCM envelope encryption of
  alert-channel secrets, P8-T4). Adopting pysaml2 would **force-downgrade the
  crypto stack** below 44 — a security regression to satisfy an SP library. That
  fails the security > integrity ordering.
* The only mainstream alternative, **python3-saml**, links the **in-process
  libxmlsec1 C extension** — exactly the architecture the research doc rejected in
  favour of pysaml2's **xmlsec1-subprocess** isolation for attacker-influenced
  XML. Swapping to it to dodge the pin would discard the stated defense-in-depth
  posture.

Shipping a compromised or weak SAML SP is explicitly out of scope, so T7 waits
until either pysaml2 relaxes its `pyopenssl` ceiling to allow cryptography ≥48,
or a cryptography-48-compatible, subprocess-isolated signature path exists. When
revisited, the mandatory controls are unchanged: signed-assertion **required**,
audience/recipient/`NotOnOrAfter` enforced, replay cache on the assertion ID, the
**XSW** defense (reject multi-`Assertion` responses; bind claims to the signed
element by ID), an entity-expansion-safe (`defusedxml`) parser, and the xmlsec1
subprocess under a hard timeout. The `authx.SAMLProvider` stub is retained as the
typed placeholder. **Interim recommendation:** most SAML IdPs (Keycloak,
Authelia, Authentik, Okta, Entra ID) also speak **OIDC** — use the shipped OIDC
provider (P6-T5) instead.


# Rate limiting & lockout (Phase 6, P6-T8)

Filearr protects the credential-check paths (login — local *and* the LDAP
fall-through — plus bootstrap and password-change) with a Postgres-backed
brute-force limiter. No slowapi, no Redis: the state lives in `auth_rate_limits`
so it survives a restart and is shared across every worker.

Two **independent** buckets are tracked per attempt:

* **per-username** — keyed on the *submitted* username (lower-cased). This is
  what catches a **distributed** brute force: many source IPs hammering one
  account never trip any single per-IP counter, but they share the username
  bucket.
* **per-source-IP** — keyed on the client address, catching one host trying many
  usernames.

Either bucket reaching the failure threshold inside the window locks *that*
bucket. A locked attempt is refused with **HTTP 429 + `Retry-After`** *before*
the slow argon2 verify runs. A wrong username and a wrong password are
byte-identical (same 401, same eventual 429) so the limiter never becomes an
account-enumeration oracle. A successful login clears the username bucket; the IP
bucket decays on its own window (it may be shared behind a NAT).

| Setting | Default | Meaning |
|---|---|---|
| `FILEARR_AUTH_RATELIMIT_ENABLED` | `true` | master switch (false → no-op) |
| `FILEARR_AUTH_RATELIMIT_MAX_ATTEMPTS` | `3` | failures within the window before a lock |
| `FILEARR_AUTH_RATELIMIT_WINDOW_SECONDS` | `120` | the find window |
| `FILEARR_AUTH_RATELIMIT_LOCK_SECONDS` | `300` | how long a locked bucket stays locked |
| `FILEARR_AUTH_RATELIMIT_TRUST_FORWARDED_FOR` | `false` | trust the leftmost `X-Forwarded-For` |

**Proxy note:** set `TRUST_FORWARDED_FOR=true` **only** when a trusted reverse
proxy (the Caddy TLS sidecar, OPS-T1) is in front and strips/sets the header —
otherwise a client can spoof it to dodge the per-IP bucket. The per-username
bucket is unspoofable either way.

The OIDC callback is deliberately **not** rate-limited: its 256-bit single-use
`state` (consumed server-side) already defeats replay/brute force, and there is
no password to lock.


# Security audit log (Phase 6, P6-T9)

Auth-relevant actions are recorded in `security_events` (append-only): login /
logout (local, LDAP, OIDC), failed logins and lockouts, account lifecycle
(create / disable / enable / role-change / delete / password-change / bootstrap),
grant & group changes, and session revocations. Each row carries the event type,
the acting/subject principal (nullable — a failed login for a nonexistent user
has none), the attempted username, source IP, a truncated user-agent, a
timestamp, and a small secret-scrubbed `details` bag. Writes happen in their own
transaction and any failure is logged-and-swallowed — auditing never breaks the
auth path it observes, and a password/token can never land in a row.

Read the feed at **`GET /api/v1/audit`** (admin scope) or the Admin → Security
audit panel. It is keyset-paginated (`ts DESC, id DESC`) with `event_type`,
`principal_id`, `since`, and `until` filters.

Read/search auditing is **opt-in** — high volume, low value outside multi-tenant
SaaS. Set `FILEARR_AUDIT_READS=true` to also record a `search` event per query.

Retention (a daily maintenance purge, no retry):

| Setting | Default | Applies to |
|---|---|---|
| `FILEARR_SECURITY_AUDIT_FAILURE_RETENTION_DAYS` | `90` | `login_failure` rows |
| `FILEARR_SECURITY_AUDIT_RETENTION_DAYS` | `365` | every other event |

> These are distinct from `FILEARR_AUDIT_RETENTION_DAYS`, which governs the
> unrelated ItemVersion metadata-change audit (P4-T9) — a different table and
> trust model. They are not interchangeable.


# Active sessions & remote logout (Phase 6, P6-T11)

Because sessions are Postgres-backed (not stateless JWT), revocation is instant —
deleting the row invalidates the cookie on its very next request. The UI exposes
this:

* **`GET /api/v1/auth/sessions`** — your own active sessions (IP / user-agent /
  last-seen), the current one flagged.
* **`DELETE /api/v1/auth/sessions/{id}`** — revoke one of your sessions (a 404 if
  it is not yours — another user's session is never revealed).
* **`POST /api/v1/auth/sessions/revoke-all`** — "log out everywhere" (kills all
  of your sessions, including the current one).
* **`GET`/`DELETE /api/v1/auth/users/{id}/sessions`** — admin: list or force-log-
  out any principal (only that principal's sessions).

A role change, disable, password change, or account delete already revokes every
session for the affected principal automatically (a change of authority takes
effect immediately). All of these surfaces live under Admin → Active sessions.
