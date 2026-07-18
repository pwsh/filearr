# Research Brief — Future Roadmap Item 2: Local Query Access

Scope: `docs/future-roadmap.md` §2. Local CLI (`filearr query ...`) +
optional local web UI against the v3 Go agent's local SQLite FTS5 index
(`docs/research/phase-5-distributed-agents.md`), answering "where did I put
that file" fully offline. Policy-controlled from the central console:
enabled/disabled per machine group, optionally auth-required, always
read-only. Interacts with:

- **§1 (agents)**: rides the Go agent — TA-3's SQLite FTS5 index, TA-6's
  ETag-poll + opportunistic-SSE config channel, TA-7's rollout groups.
  Phase-5 reserves this feature as **TA-8**; this brief concretizes it.
- **§3 (RBAC)**: no phase-6 doc exists yet. Assumes roadmap §3's model
  (global roles + machine-group/path-scope ACLs, Meilisearch tenant tokens
  centrally) and asks how a **disconnected** agent enforces a **cached
  copy**, since tenant-token verification is unreachable offline.

Priority order for every tradeoff: **security > integrity > reliability >
speed > compatibility > scalability**. AGPL-3.0-or-later-compatible OSS
only. Hub-and-spoke only — a local CLI/UI talks only to its own machine's
agent, never another agent.

Research current 2026-07-07, via parallel deep-research passes (web search +
adversarial verification). Claims sourced from search snippets rather than a
rendered fetch are flagged "unverified."

---

## 1. CLI UX design

### 1.1 Prior art

Two convergent clusters:

- **TTY-gated color, decoupled from output shape.** ripgrep's
  `--color=auto|always|never|ansi` colors only on a real tty, respects
  `NO_COLOR`/`TERM=dumb` (ripgrep FAQ.md/GUIDE.md). fd mirrors this. No tool
  auto-switches to JSON based on isatty.
- **Explicit opt-in structured output, decoupled from reshaping.** ripgrep
  `--json` emits **NDJSON** (types `begin/match/context/end/summary`;
  non-UTF8 as base64). GitHub CLI `--json` requires an explicit field
  allow-list → JSON array, reshaped via bundled `--jq` or `--template` (Go
  templates+Sprig), both require `--json` set (cli.github.com/manual/
  gh_pr_list). Docker `--format` = same idea (Go templates,
  `--format json`→JSON Lines). **voidtools `es.exe`** (closest analog): 
  `-csv/-tsv/-json/-efu/-txt`, `-no-header`, `-utf8-bom`
  (voidtools.com/support/everything/command_line_interface/).
- **fd/fzf are outliers**: no structured mode — delimiter conventions only.
- **plocate**: substrings by default, glob on metachars, `--regexp`/
  `--regex` for BRE/ERE; multiple patterns AND by default.
- **clig.dev** codifies this directly: "If human-readable output breaks
  machine-readable output, use `--plain`... formatted JSON if `--json`
  passed"; color off if not tty or `NO_COLOR` set (clig.dev "Output").

### 1.2 CLI framework: urfave/cli v3, not cobra

- **urfave/cli v3**: v3.10.0 (Jun 2026), MIT, zero non-stdlib deps
  (confirmed via go.mod). Recommended line (v2 maintenance-only); brisk
  cadence, one active maintainer. Full bash/zsh/fish/PowerShell completion
  gated behind `EnableShellCompletion: true`.
- **cobra**: v1.10.2 (Dec 2025), Apache-2.0, ~44k stars, but pulls
  `go-md2man`/`mousetrap`/`pflag`/yaml as real deps. Cobra's creator
  (spf13.com/p/the-maintainers-dilemma/, 2026-05-20) states 243 open
  issues/118 open PRs, "hasn't meaningfully reviewed a PR in months" —
  conservatively maintained due to downstream criticality (kubectl/Hugo/gh),
  not active evolution.
- **Verdict**: urfave/cli v3 fits this project's low-dependency bias.
  Cobra defensible only if kubectl/gh-style ecosystem familiarity outweighs
  dependency weight.

### 1.3 Table rendering: lipgloss/table, not bubbletea

`lipgloss` core (MIT) depends only on `charmbracelet/x/ansi` + stdlib — no
bubbletea. `bubbletea` (Elm-architecture TUI runtime) is overkill for a
one-shot render-and-exit tool. `text/tabwriter` (stdlib, zero deps,
"frozen") is a legitimate minimal fallback. **Recommendation**:
lipgloss/table for colorized TTY output, tabwriter for piped/`--plain`,
`--json` bypasses rendering entirely. No documented "lipgloss vs tabwriter"
debate found — this is reasoned, not confirmed consensus.

### 1.4 Filter/query DSL design

- **Everything**: space=AND, `|`=OR, `!`=NOT, `< >` grouping, wildcards,
  `regex:` escape; consistent `function:value|<value|<=value|>value|
  start..end` shape (`size:>1mb`, `dm:today`, `dm:1/8/2014..31/8/2014`),
  named size buckets, `ext:pdf;doc` semicolon lists.
- **Gmail**: implicit AND, explicit `AND`/`OR`/`{ }`, `-` negation, `( )`
  grouping, relative dates (`older_than:7d`).
- **GitHub code search** rebuilt as PEG grammar → AST → Elasticsearch bool
  query, nesting capped at 5 levels (github.blog, 2025-05-13) — good
  precedent for deliberately bounding nesting.
- **Recommendation**: hand-rolled recursive descent (Pike's lexer/parser
  split) over `alecthomas/participle` (struct-tag grammar lib) — the
  grammar is small (flat `key:op value` + implicit AND + explicit OR/NOT),
  so a hand-written parser is lower-dependency and easier to audit. Reserve
  participle if parenthesized grouping grows. Reference (don't adopt)
  `thedustin/go-gmail-query-parser`, `lrstanley/go-queryparser`,
  `timsolov/rest-query-parser`.

**Concrete operator set**: `kind:<media_type>`, `ext:<ext>[;<ext>]`,
`size:>1G`/`<500M`/`1M..10M`, `modified:<7d`/`>2026-01-01`, `path:<glob>`,
`sidecar:false` (mirrors T3's `is_sidecar`), bare tokens = implicit-AND
substring, `-`/`!` negation, quoted phrases. Document in `--help` that this
is **not** typo-tolerant (§4).

---

## 2. Local web UI: embedding, security

### 2.1 Embedding

`http.FileServerFS(fsys fs.FS)` (Go 1.22+) + `fs.Sub` for `//go:embed`.
**Bind explicitly to `127.0.0.1:PORT`, never `0.0.0.0`** — several
browsers/OSes treat `0.0.0.0` as loopback-equivalent, exploited in the
"0.0.0.0-day" incident class (Oligo Security, 2024). **MIME gotcha**:
`mime.TypeByExtension` reads the Windows registry, can serve `.js` as
`text/plain` (golang/go#32350) — fix via `mime.AddExtensionType` in
`init()`. **embed.FS modtime gotcha**: embedded files report zero
`time.Time{}`, so `ServeContent` never emits 304s (golang/go#44854) — wrap
with a build-time ModTime or ETag. No native SPA fallback; `ServeFile`
doesn't sanitize `..` — add middleware. **Precedent**: Tailscale's Quad100
UI (`http://100.100.100.100/`), on-device only, read-only unless device
owner signed in (tailscale.com/kb/1381); Syncthing GUI via `//go:embed`.

### 2.2 DNS rebinding

Attacker DNS returns short-TTL record to their own server (passes
same-origin check), then re-resolves to an internal/loopback IP;
same-origin JS now reaches the internal target (Jackson et al., ACM ToWeb
3(1), 2009). **Bind address is irrelevant** — same-origin policy is
hostname-based, not IP-based. **Directly on-point**: CVE-2025-49596 (CVSS
9.4), Anthropic MCP Inspector — no auth between web UI and proxy; exploited
via exactly this pattern for RCE, fixed via session token + Origin/Host
allow-list (2025-06-27). Recurs: CVE-2025-66414 (MCP TS SDK); older
CVE-2018-14732 (webpack-dev-server). **Mitigation — Syncthing**: since
v0.14.6, validates `Host` header on loopback binds, rejects unless matching
`localhost`/`127.0.0.1`/`[::1]`. **Mitigation — Tailscale LocalAPI**:
requires `Sec-Tailscale: localapi` header (not settable via ordinary
fetch/XHR) + credential; prefers a Unix domain socket, sidestepping
rebinding entirely. **Recommendation**: Syncthing-style strict Host-header
allow-list, no skip-check escape hatch by default.

### 2.3 CSRF on loopback

**Still a real risk** — browsers gate reading cross-origin responses, not
sending requests; for state-changing endpoints the request itself is the
attack (Oligo, 2024-08-07). **Direct proof**: CVE-2022-21703 (Grafana) —
`SameSite=Lax` + content-type sniffing forged authenticated POSTs, no
response-reading needed. **Syncthing's mechanism** (verified from
`lib/api/api_csrf.go`): 1h token lifetime, cookie set on `/`, `/rest/...`
requires matching `X-CSRF-Token-<unique>` header unless authenticated via
`X-API-Key`. **SameSite alone is insufficient** per OWASP's CSRF cheat
sheet (synchronizer tokens are primary defense). Go 1.25 added
`net/http.CrossOriginProtection` (Fetch Metadata + Origin/Referer
fallback) — usable on the Go agent's HTTP server. **Recommendation**:
Syncthing-style cookie+header token, or Go 1.25's `CrossOriginProtection`.

### 2.4 Token-in-URL vs. cookie auth

**URL-token risks**: Referer leakage (partially mitigated by Chrome's
`strict-origin-when-cross-origin` default); proxy/access logs capture full
query strings by default; browser history persistence; OS process-listing
exposure (CWE-214). HTTPS does **not** mitigate this class (CWE-598).
**Jupyter's proven pattern**: bootstrap token (printed to stdout/log)
exchanged on first use for a session cookie, token then discarded.
**Cookie tradeoffs**: `HttpOnly` blocks JS reads but not CSRF (still needs
§2.3); `SameSite=Strict` is right for a single-machine local UI.
**Recommendation**: one-time bootstrap token → `HttpOnly`+`SameSite=Strict`
session cookie, exactly Jupyter's pattern — never a persistent bookmarked
`?token=` URL. **Cautionary counter-example**: Ollama binds `127.0.0.1`
with zero built-in auth; users reconfiguring to `0.0.0.0` removed the only
protection with no compensating auth (CVE-2024-37032 "Probllama," RCE) —
argues for building bootstrap-token/cookie auth in from day one.

---

## 3. Local API surface: transport, auth, read-only enforcement

### 3.1 Unix domain socket vs. localhost TCP

Go's `net.Listen`/`Dial` support `"unix"` natively. **No mode parameter on
`Listen("unix", path)`** — set permissions via `syscall.Umask(0077)` before
Listen (no race window) or `os.Chmod` after (race window exists;
golang/go#11822). Permission checks happen at `connect()` like any
filesystem object. **Docker**: `docker.sock` defaults `root:docker` `0660`;
Docker's own guidance: "access to the Docker API is effectively root
access," never expose unauthenticated over TCP. **Postgres**:
`unix_socket_permissions` GUC sets socket mode directly, no umask trick
needed. **Recommendation**: Unix domain socket (Linux/macOS) as the default
CLI↔daemon transport — sidesteps DNS rebinding entirely, OS-native
permission control for free. Reserve localhost TCP for the optional web UI
only (browsers can't dial a UDS), locked down per §2.

### 3.2 Windows named pipes

**github.com/Microsoft/go-winio**: MIT, Microsoft-maintained, not archived
(67 open issues/43 open PRs active). Last tag v0.6.2 (Apr 2024) but
development continues via pseudo-versioned commits (latest indexed Jan
2026). Used throughout Moby/Docker/containerd. Implements
`net.Listener`/`net.Conn`, modeled on Go's own `net` package. **Permission
model**: SDDL-string security descriptor at pipe-creation time (no
file-mode-bits equivalent); restricting to current-user needs an SDDL DACL
with the caller's SID — exact syntax needs verification at implementation
time. **No actively-maintained cross-platform abstraction found**:
`natefinch/npipe` is Windows-only, zero tags; `james-barrow/golang-ipc` is
the one genuine cross-platform match but last tagged Jul 2023 (~3 years
stale, soft-abandonment signal). **Recommendation**: follow Docker/
containerd's approach — go-winio (Windows) + stdlib `net.Listen("unix",..)`
(Linux/macOS) behind a build-tag-gated wrapper, not a third-party shim.

### 3.3 Same-user authentication (peer credentials)

**Linux**: `getsockopt(fd, SOL_SOCKET, SO_PEERCRED)` on `AF_UNIX` returns
`{pid,uid,gid}` snapshotted at connect. Go: `golang.org/x/sys/unix.
GetsockoptUcred` (no stdlib support — confirmed gap, golang/go#41659,
open). **macOS/BSD**: `getpeereid(int s, uid_t*, gid_t*)`, FreeBSD origin.
**Windows**: `GetNamedPipeClientProcessId` + `ImpersonateNamedPipeClient` +
compare SIDs — composite recipe from several Win32 APIs, not one canonical
page. **Divergent real-world practice**: Docker deliberately does **not**
use SO_PEERCRED — explicit maintainer rejection on record (moby/moby#35711)
relying purely on filesystem+group-membership gating (documented as
root-equivalent). **systemd's sd-bus DOES** use SO_PEERCRED, treating
kernel-reported UID as authoritative over client claims. **Postgres `peer`
auth** is the closest spec match: obtains the client's OS user name and
checks it matches, local connections only. **Recommendation**: adopt
Postgres's `peer`-auth model — verify connecting process's OS UID matches
the daemon's own running UID, reject otherwise (the systemd/Postgres
pattern, not Docker's weaker group-membership-only model — appropriate
given this project's security-first priority order). Same-user-only is a
correct, sufficient invariant for v1; shared multi-user machines are
deferred (§8).

### 3.4 Read-only enforcement (defense in depth)

Strongest confirmed prior art: **GNOME Tracker/LocalSearch** — "readonly
queries happen on readonly database connections. It is essentially not
possible to perform any data change from the query APIs"; query and update
are separate parser entry points all the way to the public API. Don't rely
on "endpoints just happen to only implement GET." Layered recommendation:
(1) **API-layer**: Go 1.22+ method-scoped `http.ServeMux` — non-GET/HEAD
gets 404 from the stdlib router itself, since no mutating handler is ever
registered; (2) **middleware backstop**: global reject-non-GET/HEAD so
future routes inherit the restriction; (3) **DB-layer**: open the local
SQLite connection with `SQLITE_OPEN_READONLY` — independent enforcement
below the HTTP layer, matching RocksDB's own recommendation of pairing
API-level with storage-level read-only; (4) never reuse the read-only
connection/handler path for the agent's own write operations (scan ingest,
outbox) — two fully separate connections.

---

## 4. Query semantics parity, with the honest gap documented

### 4.1 FTS5 has no fuzzy/typo tolerance — reconfirmed

FTS5's `trigram` tokenizer matches *substrings*, not fuzzy/edit-distance —
a query token must still appear literally. Corroborated by **sist2**
(closest spiritual precedent, offers both Elasticsearch and SQLite/FTS5
backends): its README comparison table checks "Fuzzy search" for
Elasticsearch, leaves it **explicitly blank for SQLite/FTS5**. **spellfix1**
remains the only first-party fuzzy extension, but references FTS4 not
FTS5, ships unbundled, no meaningful changelog activity since 2016 —
effectively dormant. No official successor found.

### 4.2 Standard workaround: two-stage candidate + verify

Practitioner pattern: generate trigrams from the query, use FTS5 trigram
MATCH to pull a bounded candidate set, then compute exact Levenshtein/
edit-distance in application code only over that candidate set — the same
n-gram-prefilter-then-verify pattern used by pg_trgm. A community
extension, `streetwriters/sqlite-better-trigram`, patches the trigram
tokenizer's blind spot on short (<3 char) tokens. **Recommendation**:
trigram FTS5 MATCH for candidate generation (bounded, e.g. top 200 rows),
then in-process edit-distance re-rank (mature MIT-licensed Go Levenshtein
lib) when the query looks like it might contain a typo (heuristic: zero
exact/substring hits, or explicit `~` marker). Narrows the gap without
pretending to fully match Meilisearch.

### 4.3 Document the gap honestly

Per phase-5's own conclusion (§3.1): local SQLite's job is "find this
file's exact/near-exact name/path offline," **not** "typo-tolerant ranked
search" — that's Meilisearch's job centrally (invariant 1). This brief's
candidate+verify addition narrows but doesn't close the gap — state this
explicitly in `--help` and local-UI search-box copy, so a zero-result local
search isn't mistaken for "file doesn't exist."

### 4.4 Path-scope ACL filtering (ties to roadmap §3)

Roadmap §3's path-scope ACLs compile into Meilisearch tenant tokens
centrally; a disconnected agent can't mint/verify a tenant token, so local
enforcement must be a locally-cached, agent-enforced filter: cached policy
(via TA-6's ETag channel) carries the effective path-scope for the local
user (v1: effectively "the machine's own user"); enforce as a `WHERE
rel_path GLOB/LIKE ...` predicate applied to every local result set, never
trusting a client-supplied scope param — same "verify, don't trust the
client" posture as the central design. If cached policy is stale beyond
its TTL (§5.2), apply the **most restrictive last-known scope**, never
fall back to unrestricted.

---

## 5. Policy control model

### 5.1 Delivery: reuses TA-6, no new channel

TA-6 already specifies ETag-polled `GET /api/agents/{id}/policy` +
opportunistic SSE. This feature's payload (`local_access_enabled`,
`web_ui_enabled`, `auth_required`, `read_only: true` always, path-scope) is
additional fields on the existing `policy_versions.policy` JSONB blob — no
separate config-push mechanism needed.

### 5.2 Offline grace behavior: fail-closed, with justification

| System | Cache/grace behavior | Fails |
|---|---|---|
| Tailscale | Per-device key-expiry (not a blanket TTL); ACLs cached and enforced locally until that device's own key expires | **open** |
| Chrome Enterprise | Reloads ~5s while online; no confirmed numeric offline-grace-period found | effectively **open** |
| Fleet/osquery | Online-liveness window = `min(distributed_interval, config_tls_refresh)+60s` — a liveness metric, not a policy fail-open/closed case | n/a |
| Intune | Default **30-day** compliance validity — no check-in flips compliant→noncompliant; separate 0–365-day admin grace delays enforcement | **closed** |

Pattern: connectivity-first tools (Tailscale, Chrome) lean fail-open;
compliance-first tools (Intune) lean fail-closed on staleness. Filearr's
priority order places this feature in the Intune camp — a local search/UI
surface is smaller blast-radius than "should this device keep talking to
the mesh," which argues for conservatism here.

**Recommendation: fail-closed with a bounded 24h grace window, not
indefinite fail-open.** Cache last-fetched policy; continue honoring it for
**24 hours** if unreachable (reusing phase-5's existing "offline >24h"
full-reconciliation threshold, not a new constant). **Past 24h, fail
closed**: disable the web UI (CLI degrades to most-restrictive-cached-scope,
clearly labeled stale). **Explicit CLI/web-UI asymmetry**: CLI same-user
access (§3.3's peer-credential check is itself offline-capable) is
**always allowed** regardless of staleness, unless the central console
explicitly pushed a disable (which persists indefinitely once received —
only default-enabled+stale-cache degrades). The **web UI**, carrying
browser attack surface (§2), fails closed on the stricter rule: no fresh
policy confirmation within 24h → web UI shuts down entirely, no
degraded-open middle state. Justification: the CLI's risk surface requires
an attacker to already have code execution as that OS user; the web UI's
risk surface additionally includes any malicious webpage the browser
visits — a materially larger, more remotely-triggerable class. A
never-contacted agent defaults the web UI to **disabled** and the CLI to
same-user-only with no scope restriction beyond "this machine's own local
index."

### 5.3 Auth-required flag

Composes with §2.4: `auth_required` gates whether the bootstrap token is
needed at all. Never affects the CLI's same-user peer-credential check
(§3.3), which is always-on and not policy-gated.

---

## 6. Local query history / frecency (optional)

**Firefox Places frecency**: score = sum over sampled visits of
`(visit-type weight) × (recency-bucket weight)` — buckets roughly last 4
days→100, 14d→70, 31d→50, 90d→30, older→10; typed-URL visits weighted far
higher (~2000) than link visits (~100). **zoxide** ("inspired by Firefox's
URL bar ranking"): score starts at 1, **+1 per visit**; when total DB score
exceeds `_ZO_MAXAGE`, scores divided down to ~90% of ceiling; stale entries
pruned after 90 days. **Recommendation**: a zoxide-style frequency+recency
counter (not Firefox's full weighted-visit-type model, since Filearr
queries lack a meaningful typed/clicked distinction) — increment per query/
clicked-result, periodically decay, prune below a floor.

**Privacy — strictly local, never replicated**: must never ride the
outbox/replication path (phase-5 §4) — categorically different from
file-metadata replication. Keep architecturally incapable of leaving the
machine (separate local-only table, never touched by the outbox writer),
not merely policy-gated. Precedent: zoxide/autojump ship with no networking
code in their scoring path at all. Firefox's own model is a *lower* bar
(history sync bundles into Sync as soon as a user signs in) — Filearr
should be stricter, since local search terms are a more sensitive signal
than browser URL history. Explicit copy: "search history stays on this
machine and is never sent to the central server."

---

## 7. Task breakdown

Continuing phase-5's numbering (reserved as **TA-8**, size M). Expanded
into sequenced sub-tasks, CLI-first then web UI:

**TA-8.1 — Query DSL + local read-only query engine (M)**
Recursive-descent parser (§1.4) for `kind:/ext:/size:/modified:/path:` +
implicit AND/explicit OR/negation; FTS5 trigram-candidate + edit-distance
fuzzy layer (§4.2); dedicated `SQLITE_OPEN_READONLY` connection (§3.4).
*Accept*: `kind:video size:>1G modified:<7d` returns correct results
against a seeded index; adversarial test confirms the read-only connection
cannot be coerced into a write via any query input.

**TA-8.2 — Unix socket / named pipe daemon API + peer-credential auth (M)**
`net.Listen("unix",...)` with pre-listen `umask(0077)` (Linux/macOS), or
go-winio `ListenPipe` with current-user-restricted SDDL (Windows);
SO_PEERCRED/`getpeereid` same-UID check per connection (§3.3), Windows via
`GetNamedPipeClientProcessId`+SID compare.
*Accept*: a different-OS-user process cannot open a query connection
(cross-user test Linux/macOS + Windows CI); socket/pipe never has a
world-accessible window (permissions asserted immediately post-listen).

**TA-8.3 — CLI (`filearr query`) against the local daemon API (S)**
urfave/cli v3 wiring; `--json` (NDJSON) + lipgloss/table default, tty-gated
color, `NO_COLOR` respected; shell-completion via urfave/cli v3's built-in
support; `--help` documents the typo-tolerance gap (§4.3).
*Accept*: `filearr query --json 'kind:video size:>1G'` produces valid
NDJSON parseable by `jq`; piping produces no ANSI codes without `--plain`;
fully offline still returns correct results.

**TA-8.4 — Policy fields + fail-closed enforcement (M)**
Extend `policy_versions.policy` JSONB (no new table) with
`local_access_enabled`/`web_ui_enabled`/`auth_required`/path-scope;
agent-side asymmetric CLI-vs-web-UI fail-closed enforcement (§5.2); 24h
grace window reusing phase-5's existing constant.
*Accept*: a centrally-pushed "disable web UI" is honored within one poll
interval and **persists** through a subsequent offline period; a
never-contacted agent starts with web UI disabled, CLI enabled by default;
an agent whose last-fetched policy was "enabled" but unrefreshed >24h
auto-disables the web UI without a central push.

**TA-8.5 — Local web UI: minimal search box + results, no admin (M)**
`embed.FS`+`http.FileServerFS`, Go 1.22+ method-scoped GET/HEAD-only
routing, Host-header allow-list (§2.2), CSRF synchronizer token (§2.3),
bootstrap-token→`HttpOnly`+`SameSite=Strict` cookie exchange (§2.4) gated
by `auth_required`. Scope: search box + results (path/size/modified/kind,
open-containing-folder/copy-path per roadmap §5 P0) — no settings, no
admin, no write path, ever.
*Accept*: forged/missing Host header gets 403 pre-handler; DNS-rebinding
adversarial test (resolve test domain to 127.0.0.1 post-load) fails to
reach the API; disabling policy takes the UI down within TA-8.4's window.

**TA-8.6 — Local query history / frecency (S, optional/deferrable)**
zoxide-style frequency+recency local-only table (§6), architecturally
isolated from the outbox path, periodic decay/prune.
*Accept*: repeated searches rank higher over time; the outbox table shows
zero rows attributable to search history after heavy local use; wiping the
agent's local DB is documented as also wiping search history.

---

## 8. Open questions

1. **Multi-user machine scope.** Assumes CLI/UI user = machine's primary OS
   user (§3.3). A shared workstation needing per-OS-user path-scopes is
   out of scope — flagged, not resolved.
2. **Exact SDDL string for Windows named-pipe user-restriction** (§3.2) —
   mechanism correct, exact syntax needs verification at implementation time.
3. **24h grace window (§5.2) is a reasoned default, not empirically
   tuned** — reused from phase-5 for constant-consistency. Revisit if real
   deployments show it's too aggressive or too lax.
4. **Fuzzy-match trigger heuristic (§4.2)** — "zero exact hits or explicit
   `~`" is a starting point; validate against real query logs once shipped.
5. **Path-scope cache format (§4.4)** assumes a simple glob/prefix
   predicate. If roadmap §3's eventual RBAC model produces a richer scope
   grammar, flatten it to a simple predicate at policy-push time
   (recommended) rather than growing a local rule-evaluator.
6. **Does the local web UI need a "you're seeing a restricted view"
   affordance** when scope is active, vs. silent filtering (which risks a
   user thinking a file is missing)? Not resolved here.

---

## Sources cited (by section)

- **S1 (CLI UX)**: github.com/BurntSushi/ripgrep (GUIDE.md, FAQ.md), rg.1
  man page, cli.github.com/manual/gh_pr_list, github.com/sharkdp/fd,
  github.com/junegunn/fzf, plocate(1) man page, voidtools.com/support/
  everything/{command_line_interface,command_line_options,searching}/,
  docs.docker.com/engine/cli/formatting/, kubernetes.io/docs/reference/
  kubectl/, clig.dev, github.com/charmbracelet/{lipgloss,bubbletea},
  pkg.go.dev/text/tabwriter, github.com/spf13/cobra,
  spf13.com/p/the-maintainers-dilemma/ (2026-05-20), github.com/urfave/cli,
  cli.urfave.org/v3/examples/completions/, github.com/alecthomas/participle,
  go.dev/talks/2011/lex.slide, github.blog (2025-05-13),
  support.google.com/mail/answer/7190, github.com/thedustin/
  go-gmail-query-parser, github.com/lrstanley/go-queryparser,
  github.com/timsolov/rest-query-parser
- **S2 (web UI security)**: go.dev/doc/go1.22, pkg.go.dev/io/fs#Sub,
  golang/go#32350 #68591 #47912 #44854 #45445 #46935 #9876 #18837,
  tailscale.com/kb/1381, docs.syncthing.net/{dev/web.html,users/faq.html},
  github.com/syncthing/syncthing#4815 #4819, github.com/syncthing/
  syncthing/blob/main/lib/api/api_csrf.go, go-gitea/gitea#17352,
  adambarth.com/papers/2009/..., github.blog/security/application-security/
  dns-rebinding-attacks-explained/, oligo.security/blog/0-0-0-0-day-...,
  oligo.security/blog/critical-rce-...-cve-2025-49596 (2025-06-27),
  GHSA-7f8r-222p-6f5g, GHSA-w48q-cv73-mx4w, jub0bs.com/posts/
  2022-02-08-cve-2022-21703-writeup/, cheatsheetseries.owasp.org/cheatsheets/
  Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html, CWE-214, CWE-598,
  developer.chrome.com/blog/referrer-policy-new-chrome-default,
  jupyter-server.readthedocs.io/operators/security.html, CVE-2024-37032
- **S3 (transport/auth)**: pkg.go.dev/net, golang/go#11822,
  man7.org/linux/man-pages/man7/unix.7.html, cheatsheetseries.owasp.org/
  cheatsheets/Docker_Security_Cheat_Sheet.html, postgresql.org/docs/current/
  {auth-pg-hba-conf.html,auth-peer.html}, github.com/microsoft/go-winio,
  github.com/natefinch/npipe, github.com/james-barrow/golang-ipc,
  golang.org/x/sys/unix, golang/go#41659, man.freebsd.org, learn.microsoft.com/
  windows/win32/api/{winbase/nf-winbase-getnamedpipeclientprocessid,
  namedpipeapi/nf-namedpipeapi-impersonatenamedpipeclient}, moby/moby#35711
  #9976, github.com/systemd/systemd (bus-socket.c), gnome.pages.gitlab.gnome.org/
  tracker/docs/developer/security.html, matthewsetter.com/blog/item/
  restrict-allowed-route-methods-go-122 (2024-04-11), github.com/facebook/
  rocksdb/wiki/Read-only-and-Secondary-instances
- **S4 (query semantics)**: sqlite.org/{fts5.html,spellfix1.html},
  github.com/sist2app/sist2, tdom.dev/sqlite-fuzzy-search.html,
  github.com/streetwriters/sqlite-better-trigram
- **S5 (policy control)**: tailscale.com/kb/1091, chromium-discuss group
  (AsyncPolicyLoader), fleetdm.com/{vitals/osquery-flags,docs/using-fleet/faq},
  learn.microsoft.com/intune/device-security/compliance/monitor-policy,
  ninjaone.com/blog/use-intune-compliance-grace-period-effectively/
- **S6 (frecency/privacy)**: firefox-source-docs.mozilla.org/browser/
  urlbar/ranking.html, mozilla.github.io/application-services (frecency.rs),
  github.com/ajeetdsouza/zoxide/wiki/Algorithm, support.mozilla.org/kb/sync,
  hacks.mozilla.org/2018/11/firefox-sync-privacy (2018)
- **Internal**: docs/future-roadmap.md §1/§2/§3/§5, docs/research/
  phase-5-distributed-agents.md §3/§4/§6/§10, CLAUDE.md (invariants 1-7,
  priority order, AGPL license)

**Honest gaps** (spot-check before relying on): exact SDDL syntax for a
current-user-only named pipe; containerd's exact go-winio pinned version;
Fleet's exact default `config_refresh` seconds; current-version Firefox
frecency constants (drift across sources); zoxide's precise query-time
recency-weighting formula (only storage-side aging confirmed); Chrome
Enterprise's numeric offline-grace-period (only the 5s online-reload
interval confirmed); sqlite-better-trigram's 2026 maintenance recency;
several cited Docker CVEs sourced from third-party trackers, not
NVD/MITRE-cross-checked.
