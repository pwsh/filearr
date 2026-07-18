# Research Brief — Future Roadmap Item 1: Distributed Agent Architecture (v3)

Scope: `docs/future-roadmap.md` §1 (Distributed agent architecture, v3). This
brief pressure-tests and concretizes the already-decided shape — it does not
relitigate it. Fixed decisions carried in as constraints:

- **Enrollment**: one-time token -> agent CSR -> central signs a **short-lived
  client cert** (step-ca pattern). Certificate = machine identity (Syncthing
  cert-hash-as-device-ID precedent). All agent<->server traffic is **mutual
  TLS**.
- **Local index**: agents scan to a **local SQLite index (FTS5)**, fully
  functional offline.
- **Replication**: transactional **outbox + per-agent monotonic seq_no**,
  batched upserts to central Postgres keyed on `(agent_id, seq_no)` for
  idempotency. **No CRDTs/vector clocks** (hub-and-spoke, not p2p -- cr-sqlite/
  Litestream ruled out). Tombstones replicate; periodic full-reconciliation
  sweep as safety net.
- **Updates**: signed packages, central version pinning, staged rollout
  groups (Fleet/Wazuh precedent).
- **Config push**: per-agent/per-group policy, versioned and auditable.

Interacts with: §2 (local query/CLI reads the same SQLite index), §4
(indexing controls -- `docs/research/phase-2-indexing-controls.md`'s preset
bundles / `scan_paths` become the policy payload agents pull and apply
locally, including v3-scoped Stage C in that brief). Constraint order for
every tradeoff in this document: **security > integrity > reliability >
speed > compatibility > scalability**. Modern OS only (Windows 10/11 22H2+,
macOS 13+, current-LTS Linux). AGPL-3.0-or-later-compatible OSS dependencies
only. Hub-and-spoke only -- no P2P.

Research current as of **2026-07-07**. Where a claim was sourced via search
snippets rather than a fully rendered fetch (the research sessions hit
web-fetch rate limits repeatedly), it is flagged inline as unverified --
treat those as "best available, spot-check before depending on them,"
not as settled fact.

---

## 1. Enrollment / Certificate Authority

### 1.1 step-ca (smallstep) -- current state

- **License: Apache-2.0, unchanged.** No BSL/dual-license change found for
  the `smallstep/certificates` (step-ca) repo itself. Smallstep sells an
  adjacent **commercial** product ("Step-CA Pro") and a hosted "Smallstep
  Platform" SaaS, but these are separate products layered on top of the OSS
  CA, not a relicense of it. `step` CLI is also Apache-2.0.
  (github.com/smallstep/certificates, smallstep.com/open-source/,
  smallstep.com/product/step-ca-pro/)
- Current stable version reported as **v0.30.2** (2026-03-23) -- sourced from
  search snippets, not re-verified against the live releases page; treat the
  exact patch number as approximate, the "still actively released, still
  Apache-2.0" conclusion as solid.
- **ACME support**: full ACMEv2 (RFC 8555) -- http-01/dns-01/tls-alpn-01/
  device-attest-01 challenges (smallstep.com/docs/step-ca/acme-basics/).
- **Native renewal**: a separate, non-ACME protocol via `step ca renew
  --daemon`, which auto-renews at roughly 2/3 of certificate lifetime
  elapsed (jittered, configurable), with `--exec`/`--signal` hooks to reload
  a dependent service on renewal (smallstep.com/docs/step-ca/renewal/).
- **Enrollment flow** (from smallstep docs, adapted to Filearr's use case):
  1. Operator runs `step ca provisioner add` (or equivalent) to create a
     JWK/OIDC/ACME/X5C provisioner appropriate for agent enrollment.
  2. Admin (via Filearr's central console) mints a short-lived, single-use
     **enrollment token** and hands it to the user out-of-band (copy/paste
     into the agent installer, or a QR/deep-link).
  3. Agent runs `step ca bootstrap` once (pins the CA root by fingerprint),
     generates a local keypair + CSR, and calls `step ca certificate
     <machine-id> <crt> <key> --token=<token>` (or the equivalent Go/Python
     SDK call, since Filearr's agent won't shell out to the CLI in
     production -- see §2 for language choice). The CA validates the token
     (single-use, short TTL) and returns a signed **short-lived client
     cert**.
  4. Thereafter, the agent renews via `step ca renew --daemon` (or the
     equivalent library call on a timer) -- no token re-use, no repeated
     enrollment step.
  This maps directly onto Filearr's decided flow; step-ca is a credible,
  low-risk implementation vehicle for it (either shelling out to `step`, or
  -- cleaner for a non-Python agent -- using step-ca's HTTPS/ACME API
  directly from the agent's own TLS/crypto library, avoiding a CLI
  subprocess dependency).

### 1.2 Alternatives (if step-ca isn't suitable)

- **HashiCorp Vault PKI**: Vault itself is still **BSL 1.1** (confirmed via
  HashiCorp's Aug 2023 announcement; no reversal found post the Feb 2025
  IBM acquisition close -- this is inferred from absence of contrary news,
  not a direct re-statement, flagged as such). BSL's Additional Use Grant
  restricts specifically **offering Vault as a competing hosted/managed
  service**; it does not prohibit a self-hosted OSS project's *users*
  from running Vault themselves as an optional integration. The
  AGPL-compatibility question is really "would Filearr bundle/redistribute
  Vault," which it would not (Vault would run as a separate, user-operated
  process, same relationship Filearr already has with Postgres/
  Meilisearch) -- so BSL is not a blocker for treating Vault PKI as an
  optional pluggable CA backend, but it does mean Vault **cannot ship in
  the default docker-compose stack** the way Postgres/Meilisearch do
  (those are permissively licensed; Vault is not, and depending on it as
  a hard requirement would materially change the project's license
  posture). Recommendation: don't make Vault the default; document it as
  an optional CA backend for operators who already run Vault.
- **OpenBao**: **MPL-2.0**, Linux Foundation fork of Vault (forked from
  Vault 1.14.0, the last MPL-licensed release). Current version ~v2.5.5
  (reported 2026-06-17, not independently re-verified). PKI secrets engine
  documented as feature-comparable to Vault OSS's PKI engine for standard
  issuance/renewal/revocation; gaps are Enterprise-only features (FIPS
  140-3 build pending, KMIP client-only) that don't matter for Filearr's
  use case. Governance: Linux Foundation TSC, OpenSSF member project,
  roughly 4-6 week release cadence. This is the **cleaner drop-in
  alternative** to step-ca if Vault-style dynamic-secrets-adjacent PKI
  (short-lived leases, broader secrets-management integration) is ever
  wanted -- genuinely AGPL-compatible (MPL-2.0 is a permissive-enough weak
  copyleft, same category as `pathspec` already accepted elsewhere in this
  project per `docs/research/phase-2-indexing-controls.md` §2).
- **Recommendation**: **step-ca remains the right default.** It is
  purpose-built for exactly this (device/agent enrollment CA, not a
  general secrets manager), Apache-2.0 with zero license friction, and is
  already the pattern the roadmap cites (Fleet/osquery-style fleets use
  it or something functionally identical). OpenBao PKI is a legitimate
  fallback to document for operators who want to centralize Filearr's CA
  inside an existing Vault/OpenBao deployment, not a reason to switch the
  default.

### 1.3 Short-lived renewal automation patterns

- step-ca's own daemon story (`step ca renew --daemon`) is purpose-built
  for unattended renewal and is the natural fit for an agent process that
  is already long-running.
- **Teleport Machine ID (`tbot`)** is a useful comparison point: a
  long-running daemon renews on a fixed interval well inside the
  credential TTL (example cited: `renewal_interval: 20m` against a 30m
  TTL) and additionally watches for CA-rotation events to trigger an
  out-of-cycle renewal. Filearr's agent should adopt the same two
  triggers: (a) a fixed fraction-of-lifetime timer (step-ca's own
  approach), and (b) an explicit "CA rotated, renew now" push signalled
  over the same config-push channel used for policy (§6), so a
  server-initiated CA rotation doesn't have to wait for the next
  scheduled renewal window on every agent.
- Concrete recommendation: agent-side cert TTL in the **24-72h range**
  (short enough that a stolen cert has a small blast-radius window, long
  enough that a brief central-server outage doesn't strand agents),
  renewal attempted at the 60% mark, with retry/backoff if the central
  server is unreachable (an agent that can't renew keeps operating
  locally -- offline-first per the decided architecture -- but should
  surface a "certificate expiring, reconnect soon" warning locally well
  before the hard expiry).

### 1.4 Revocation: CRL/OCSP vs short-lived-only

- **step-ca's own OSS posture**: only **passive revocation** -- `step ca
  revoke` marks a certificate as revoked in the CA's own database, which
  blocks *future renewal*, but does not cryptographically invalidate an
  already-issued, still-unexpired certificate. Active CRL/OCSP
  distribution is documented as a **Step-CA Pro (commercial)** feature,
  not present in the OSS CA server (smallstep.com/docs/step-ca/revocation/).
- **Smallstep's own argument for this design** (Mike Malone, smallstep
  CEO, "Good certificates die young: what's passive revocation and how is
  it implemented?", smallstep.com/blog/passive-revocation/, 2025-05-25):
  CRL/OCSP infrastructure is operationally brittle at scale (stale CRLs,
  OCSP-responder availability becoming a new single point of failure,
  soft-fail behavior in most TLS stacks meaning a down OCSP responder
  often means revocation is silently ignored anyway) -- the argument is
  that short TTLs + automated renewal + passive revocation (refuse to
  renew) gives most of the practical security benefit of active
  revocation without the operational fragility.
- **Recommendation for Filearr, reasoned against this project's specific
  threat model** (a stolen agent laptop, not a large PKI with thousands of
  relying parties needing instant revocation): **adopt short-lived-only +
  passive revocation as the default, primary mechanism**, consistent with
  step-ca's own OSS capabilities and smallstep's stated rationale. This is
  directly reinforced by the project's own priority order (security >
  integrity > reliability > speed > ...): a 24-72h cert lifetime bounds
  the exposure window for a stolen cert far more predictably than
  standing up and operating CRL/OCSP infrastructure that could itself
  fail open. **Additionally** maintain a simple central-side **revoked
  agent_id denylist** checked on every replication/config-push request
  (a Postgres table, `revoked_agents(agent_id, revoked_at, reason)`,
  checked in the API middleware after mTLS handshake succeeds) -- this is
  cheap, gives an **immediate** kill-switch for the replication/config
  API paths specifically (the actual attack surface that matters, since a
  stolen cert is only dangerous insofar as it can talk to Filearr's API),
  and doesn't require implementing X.509 CRL/OCSP at all. This denylist
  check is *not* revocation in the PKI sense (the cert stays
  cryptographically valid until expiry) -- it's an application-layer
  authorization check layered on top of mTLS authentication, and should
  be documented as such so the distinction is never confused in an audit.

---

## 2. Agent runtime: language and packaging

### 2.1 Constraints specific to Filearr

The agent must: walk a local filesystem (conceptually reusing the existing
Python `backend/filearr/tasks/scan.py` walk/diff/tombstone/extract logic),
write to a local SQLite FTS5 index, hold a persistent mTLS client
connection, watch for filesystem changes, and be distributable/auto-
updatable to **non-technical end users** on Windows/macOS/Linux as a single
installable artifact.

### 2.2 Python (PyInstaller / Briefcase)

- **PyInstaller** (~6.21.0, 2026): full Python 3.13 support, but **no
  cross-compilation at all** -- a Windows binary must be built on a Windows
  CI runner, macOS on macOS, Linux on Linux (three native build hosts,
  unavoidable). `--codesign-identity` handles macOS ad-hoc signing but not
  notarization (needs a separate `notarytool` step); **no Windows
  Authenticode support at all** -- signing is a fully separate manual
  `signtool.exe` invocation outside PyInstaller's scope. Onefile-mode
  antivirus false-positive risk is a long-documented, structurally-rooted
  issue (bootloader-packed binaries trip heuristic AV scanners) -- a real
  UX cost specifically for the non-technical end users this agent targets
  who won't know how to whitelist a flagged installer. Onefile mode also
  re-extracts to a temp directory on every launch, adding startup latency
  (no hard benchmark found, but architecturally unavoidable with that
  packaging mode).
- **Briefcase** (BeeWare), 0.3.25 (Nov 2025), still pre-1.0. Also **no
  cross-compilation** (needs Xcode on macOS, MSBuild/WiX on Windows -- same
  three-native-hosts requirement). Signing is materially better integrated
  than PyInstaller's: automatically signs+notarizes macOS builds by
  default, including resuming an interrupted notarization submission.
- **Verdict**: neither tool solves the cross-compilation problem (three
  native CI runners required regardless), and PyInstaller in particular
  carries a real AV-false-positive UX tax on the exact non-technical
  audience this feature targets.

### 2.3 Go

- Static binaries by default (`CGO_ENABLED=0`); true single-host
  cross-compilation via `GOOS`/`GOARCH` is normal, default, and
  well-documented for pure-Go code -- this is the single biggest practical
  advantage over Python and Rust for Filearr's "build once, ship to three
  OSes" requirement. CGO reintroduces cross-toolchain pain (needs `zig cc`
  or mingw/osxcross), so the recommendation below deliberately avoids CGO.
- `fsnotify/fsnotify`: BSD-3-Clause, actively maintained (v1.10.1 May 2026
  / v1.9.0 Apr 2026), covers inotify (Linux)/kqueue (macOS/BSD)/
  ReadDirectoryChangesW (Windows) behind one API; used in production by
  Kubernetes and Viper -- a mature, unglamorous, well-trodden dependency.
- SQLite drivers: **mattn/go-sqlite3** (MIT, cgo-based) needs
  `CGO_ENABLED=1` + `-tags sqlite_fts5` for FTS5 support, reintroducing the
  cross-compile toolchain problem this recommendation is trying to avoid.
  **modernc.org/sqlite** is a pure-Go transpilation of SQLite (no cgo) and
  lists FTS5 as a built-in compiled feature -- this is the driver that
  preserves the single-host cross-compile story end to end. A concrete
  precedent: Gogs (`gogs/gogs#7882`) explicitly cited mattn/go-sqlite3's
  cgo requirement as blocking easy cross-compiled releases and moved
  toward modernc.org/sqlite for exactly this reason.
- Idle memory footprint: hard numbers are scarce and inconsistent across
  sources (see §2.5). GoReleaser's own docs make dual-OS code-signing
  (Windows Authenticode + macOS notarization) more turnkey than either
  Python packager or Rust's `cargo-dist`.

### 2.4 Rust

- Cross-compilation is comparatively harder, specifically for **macOS
  targets from non-Mac hosts** (needs the license-restricted Apple SDK
  plus osxcross, or `cargo-zigbuild`; the `cross` tool v0.2.5 doesn't
  support macOS targets at all). Windows-from-Linux works reasonably via
  mingw-w64 for `-gnu` targets; `-msvc` is harder. Practical community
  guidance found: just use a native macOS CI runner rather than fight
  cross-compilation -- which reintroduces the same three-native-hosts cost
  Go was chosen specifically to avoid.
- `notify` crate: mature (v8.2.0, CC0-1.0 core), widely depended on
  (alacritty, deno, rust-analyzer, watchexec) -- comparable maturity to Go's
  `fsnotify`.
- `rusqlite` FTS5: no dedicated Cargo feature flag; FTS5 ships bundled
  automatically via the `bundled` feature (build.rs compiles SQLite with
  `-DSQLITE_ENABLE_FTS5`) -- confirm exact feature-flag naming directly on
  docs.rs before finalizing a `Cargo.toml`, since this was not verbatim
  re-confirmed in research.
- Code-signing tooling lags Go's: `cargo-dist` has working Windows
  Authenticode signing (via SSL.com eSigner) but **macOS notarization
  support is an open, unresolved GitHub issue**
  (axodotdev/cargo-dist#1121, opened mid-2024, still open as of this
  research pass) -- a real, currently-unsolved gap for exactly the
  packaging problem Filearr needs solved.

### 2.5 Memory footprint comparison

Only one number in this table is directly citable from a primary source;
the rest are noted with their confidence level:

| Agent | Language | Reported idle footprint | Confidence |
|---|---|---|---|
| Wazuh agent | C/C++ | **~35MB average** | High -- stated directly in official docs |
| Tailscale `tailscaled` | Go | 17MB-849MB across various GitHub issues | Low -- inconsistent, complaint-biased sample |
| osquery `osqueryd` | C++ | No idle baseline found; watchdog ceiling 200-400MB | N/A |
| Fleet `fleetd`/Orbit | Go | Not published | N/A |

Directionally, Go and Rust agents are expected to sit in the low tens-of-MB
range for an idle background process (consistent with the one solid data
point, Wazuh's C/C++ agent at ~35MB, and generally-understood Go/Rust
runtime overhead being comparable to or lower than a C agent with a
similar feature set); a Python agent bundled via PyInstaller/Briefcase
would carry the CPython interpreter + bundled stdlib regardless of actual
workload, typically pushing idle RSS meaningfully higher (no clean
benchmark found for this specific comparison -- flagged as reasoned
inference, not a cited number).

### 2.6 Recommendation: Go, with an explicit reuse strategy for the Python scanner

**Recommend Go**, using `modernc.org/sqlite` (pure-Go, FTS5-capable, no
cgo) and `fsnotify` for file watching. Justification, weighed against the
project's stated priority order:

- **Security**: a static, small, auditable binary with no bundled
  interpreter reduces attack surface relative to a PyInstaller bundle
  (which ships an entire CPython runtime + stdlib, a larger and less
  auditable artifact to sign and distribute to non-technical users'
  machines).
- **Integrity/reliability**: true single-host cross-compilation
  eliminates an entire class of "works on the build machine that produced
  it, breaks on a different OS's build" risk, and removes the
  three-native-CI-runner operational burden this project's small team
  would otherwise have to maintain indefinitely.
- **Compatibility**: `modernc.org/sqlite` avoids cgo entirely, so the
  binary has zero runtime C-library dependency surprises across
  Windows/macOS/Linux.
- **Cost paid**: this is **not** a Python codebase, so the existing
  `backend/filearr/tasks/scan.py` walk/diff/tombstone/extract logic
  cannot be imported or called directly -- it must be **conceptually
  reimplemented**, not embedded.

**Reuse strategy** (since a full rewrite forfeits real value in the
existing, battle-tested scanner):

1. **Do not embed a Python interpreter in the agent.** Embedding
   CPython (or shipping a full Python runtime alongside a Go binary)
   reintroduces exactly the packaging/signing/AV-false-positive problems
   §2.2 identified, defeating the reason Go was chosen. Rule this out
   explicitly rather than leaving it as an implicit fallback.
2. **Port the *logic*, not the code.** The scan pipeline's actual
   hard-won knowledge -- mtime+size first-pass diff, quick_hash (xxh3
   first+last 64KiB) then content_hash tiering, move detection via
   `(quick_hash, size)` candidate matching with `content_hash`
   disambiguation, tombstone-not-delete, sidecar classification rules,
   batched-commit-then-defer-extract ordering (CLAUDE.md invariant 5) --
   is a specification, not Python-specific code. All of it is
   straightforwardly expressible in Go: xxh3 has mature Go
   implementations (`zeebo/xxh3` -- MIT, no cgo), `fnmatch`/gitignore-glob
   matching has a Go equivalent story (`go-git/gitignore`, or reimplement
   the pathspec-equivalent semantics chosen in
   `docs/research/phase-2-indexing-controls.md` §2 -- that brief's
   `pathspec` recommendation was Python-specific; the Go agent needs its
   own gitignore-semantics library, e.g. `denormal/go-gitignore` or
   `sabhiram/go-gitignore` -- evaluate at implementation time, flagged as
   an open question below).
3. **Extraction (per-type metadata) is the one piece worth keeping in
   Python, via a narrow boundary, not embedding.** ffprobe/pymediainfo/
   trimesh/pypdf/python-docx/openpyxl extraction logic (T1/T6) is
   substantial, tested, and has no obvious Go equivalent library
   ecosystem of the same maturity (trimesh especially -- no comparable Go
   3D-mesh library was researched or is likely to exist). Two honest
   options, to be decided at implementation time rather than presumed
   here:
   - **(a) Agent shells out to `ffprobe`/`exiftool`-class external
     binaries directly** (same subprocess-isolation discipline the
     central server already uses per the roadmap's ffprobe/CAD-kernel
     sandboxing notes) for the extraction types that have good non-Python
     CLI tools (ffprobe, exiftool). This covers video/audio/image
     technical metadata without needing Python at all.
   - **(b) For extraction types with no good non-Python CLI equivalent**
     (3D mesh stats via trimesh, structured document properties via
     pypdf/python-docx/openpyxl), the agent uploads the raw file (or a
     bounded, policy-gated subset of files) to the **central server**
     for extraction using the existing Python pipeline unchanged -- i.e.,
     don't reimplement trimesh in Go; let heavy/exotic extraction remain
     a central-server-side job against replicated file content when a
     library's policy allows content upload, and have the local agent
     index carry only the property-light fields (path, size, mtime,
     hashes, filename-derived title/year guess) until a richer
     extraction pass completes centrally. This keeps the local SQLite
     index useful and fast to build without requiring full extraction
     parity, consistent with local-index being a fast-lookup cache, not
     a complete mirror of central's extracted metadata.
   This is flagged as an **open question requiring a product decision**
   (how much extraction fidelity should the *local* index have before
   the item syncs?), not a settled design -- see §11.

---

## 3. SQLite local index

### 3.1 FTS5 configuration: trigram vs unicode61, and the typo-tolerance gap

- `unicode61` (SQLite's default FTS5 tokenizer) does whole-token matching;
  the **trigram tokenizer** (sqlite.org/fts5.html §4.3.4) explicitly
  supports *substring* matching -- "a query or phrase token may match any
  sequence of characters within a row, not just a complete token" -- with
  a `case_sensitive` option (default off) and `remove_diacritics`.
- **FTS5 has no built-in edit-distance/typo tolerance.** This is an
  inference from the complete absence of "typo"/"fuzzy"/"edit distance"
  anywhere in SQLite's own FTS5 documentation, not an explicit disclaimer
  -- but it is corroborated by direct real-world evidence: **sist2**
  (the closest spiritual precedent to Filearr -- a local file indexer
  offering both an Elasticsearch backend and a SQLite/FTS5 backend) has a
  backend-comparison table in its own README where "Fuzzy search" is
  checked for Elasticsearch and **explicitly blank for SQLite/FTS5** --
  direct confirmation from a project running both stacks side by side
  that FTS5 alone does not deliver fuzzy/typo-tolerant matching.
- The dedicated SQLite fix, **spellfix1** (sqlite.org/spellfix1.html),
  provides real Levenshtein/edit-distance functions
  (`spellfix1_editdist`/`editdist3`), but its own docs reference **FTS4,
  not FTS5**, and show no changelog activity since SQLite 3.11.0 (2016) --
  it ships but reads as effectively dormant/unmaintained for an FTS5-based
  design.
- **Conclusion, and how it interacts with Filearr's existing
  architecture**: the local agent SQLite index should use `unicode61` (or
  `trigram` if substring "contains" matching on filenames is specifically
  wanted -- e.g. matching `report` inside `Q3_report_final.pdf`) for fast,
  correct local lookup, but should **not** be expected to deliver the
  typo-tolerant fuzzy search that is Filearr's headline feature
  (CLAUDE.md: "typo-tolerant instant search" -- that's Meilisearch's job,
  running centrally). This is not a regression or a gap to fix -- it's
  the correct division of labor and directly consistent with invariant 1
  (Meilisearch is the disposable *search* projection; local SQLite is a
  disposable *offline-availability* projection with a narrower job:
  "can I find this file's exact/near-exact name/path when the central
  server is unreachable," not "typo-tolerant ranked search"). Document
  this distinction explicitly in the local CLI's `--help` text and any
  local-web-UI copy so users don't expect Meilisearch-grade fuzziness
  from the offline path.

### 3.2 WAL mode

Confirmed appropriate. SQLite's own docs (sqlite.org/wal.html): WAL's
writer "merely appends... writers and readers can run at the same time"
via snapshot isolation -- exactly the single-agent-writer +
local-CLI/local-web-UI-reader topology this feature needs. Default
checkpoint trigger is WAL file size >1000 pages
(`PRAGMA wal_autocheckpoint`); a long-lived open reader (e.g., a local
web UI kept open in a browser tab) can delay full checkpoint/truncation
but does not block the writer -- acceptable, monitor WAL file size in
practice rather than pre-optimizing.

The documented "WAL does not work over a network filesystem" caveat
(shared-memory requirement) is **confirmed not applicable** here -- this
is a purely local, single-machine agent database, never opened over
SMB/NFS.

Batch sizing: no SQLite-authored numeric rule exists; community
consensus (forum/blog sources, not authoritative docs) converges around
~1000 rows per transaction for insert-heavy workloads. Filearr's existing
convention of batched commits every 250 files (central scanner,
CLAUDE.md invariant 5) is more conservative than that consensus,
favoring shorter lock-hold and more frequent progress/cancellation
checks over raw throughput -- **recommend the local agent reuse the same
250-file batch-commit cadence** for consistency with the rest of the
system's tested tradeoff, not re-tune it independently without cause.

### 3.3 Schema mirroring and corruption recovery

**Schema**: mirror the central `items` table's shape narrowly -- not a
full 1:1 column copy, but the subset needed for local search + outbox
replication: `id` (locally-generated UUID, becomes the item's identity
once replicated -- see §4.2 on how this interacts with server-assigned
IDs), `rel_path`, `filename`, `extension`, `size`, `mtime`, `quick_hash`,
`content_hash`, a narrow `metadata` JSON blob (whatever fields the local
extraction tier in §2.6 produces), `status` (active/missing/trashed,
mirroring `ItemStatus`), plus a local-only `synced_at`/`local_seq_no`
pair used by the outbox (§4). An FTS5 virtual table
(`items_fts`) indexes `filename`/`rel_path`/searchable metadata text,
kept in sync via SQLite triggers on the base table (standard FTS5
external-content-table pattern, documented at sqlite.org/fts5.html
§4.4).

**Corruption recovery**: no tool surveyed does true incremental in-place
*repair* of a corrupted local index -- the universal pattern across
Spotlight (`mdutil -E`, full erase+rebuild), Recoll (`-z` full rebuild
vs. `-Z` in-place reset without dropping the index -- the one tool
offering a genuine middle ground, but still source-driven), and sist2
(`--incremental` reuses prior state but has no documented
corruption-triggered auto-rebuild) is **detect, then rebuild from
source**. SQLite's own tooling agrees: `PRAGMA integrity_check`/
`quick_check` detect structural corruption but have no auto-repair path;
official recovery is `.recover` (explicitly "not guaranteed perfect," may
lose content) or dump-and-reload into a fresh file.

**Recommendation**: treat the local SQLite index exactly as Filearr
already treats Meilisearch under invariant 1 -- fully disposable and
rebuildable from a filesystem walk. Concretely: agent runs
`PRAGMA integrity_check` on startup (cheap, bounded); on any failure,
delete and recreate the SQLite file, then run a full local walk to
repopulate it (same code path as a first-run enrollment scan), and flag
to the central server (next successful replication) that a local rebuild
occurred (for observability -- an agent that rebuilds its index
repeatedly is a signal of failing storage, worth surfacing as an
operational alert per roadmap §6). No incremental-repair code path is
needed or recommended -- this mirrors the existing Meilisearch philosophy
one level down the stack and avoids building bespoke repair logic for a
problem that's already solved by "just re-walk the filesystem," which
this feature needs to be fast at anyway (it's a local disk walk, not an
SMB-mount walk).

---

## 4. Replication protocol

### 4.1 Outbox schema

Adapting the standard transactional-outbox shape (Debezium's Outbox
Event Router documentation: `id`, `aggregatetype`, `aggregateid`,
`type`, `payload` -- renameable via config) to Filearr's polling variant
(SQLite has no CDC/WAL-tailing connector the way Postgres does for
Debezium, so this must be the poll-and-drain variant, not log-tailing):

```sql
-- Local agent SQLite DB
CREATE TABLE outbox (
    seq_no      INTEGER PRIMARY KEY,      -- per-agent monotonic; SQLite
                                            -- AUTOINCREMENT-free INTEGER PK
                                            -- is already monotonic per
                                            -- SQLite's rowid semantics,
                                            -- but use explicit AUTOINCREMENT
                                            -- to guarantee no reuse after
                                            -- a delete (defensive, since
                                            -- gap-detection logic below
                                            -- depends on seq_no never
                                            -- being reused)
    item_id     TEXT NOT NULL,              -- local item UUID
    op          TEXT NOT NULL,              -- 'upsert' | 'tombstone'
    payload     TEXT NOT NULL,              -- JSON snapshot of the item
                                            -- row at write time (not a
                                            -- diff -- simpler, and items
                                            -- are small)
    written_at  TEXT NOT NULL,              -- ISO8601, local clock
    sent_at     TEXT,                       -- NULL until server ACKs
    batch_id    TEXT                        -- set when included in an
                                            -- in-flight POST, cleared on
                                            -- ACK or timeout-retry
);
CREATE INDEX ix_outbox_unsent ON outbox(seq_no) WHERE sent_at IS NULL;
```

The agent writes to its local `items` table and this `outbox` table **in
the same SQLite transaction** (standard outbox guarantee: the local
write and the "something changed, needs shipping" fact can never
diverge). A separate replicator goroutine/thread drains
`WHERE sent_at IS NULL ORDER BY seq_no`, batches, POSTs, and marks
`sent_at` only after a server-side ACK naming the exact `seq_no` range
accepted.

**Explicit honesty note**: this composition (SQLite outbox -> batched
HTTPS POST -> Postgres-side idempotent upsert keyed on
`(agent_id, seq_no)`) is a sound synthesis of individually
well-documented patterns (transactional outbox, idempotency keys,
`ON CONFLICT DO NOTHING`) -- it is **not** itself a named, independently
attested architecture found in any single primary source during
research. State it in any external documentation as "built from standard
patterns," not as "the industry-standard approach."

### 4.2 seq_no semantics, gap handling, idempotent upsert

**Kafka's idempotent-producer design (KIP-98)** is the closest analog:
broker assigns a producer ID; sequence numbers increase monotonically
per (producer, partition); the broker accepts a produce request only if
its sequence is exactly last-committed+1, treats a *lower* sequence as a
harmless already-applied replay, and anything else (a *gap*, i.e., a
sequence higher than expected) as an error requiring the producer to
restart its session. **Critical limitation directly relevant to
Filearr**: Kafka's exactly-once guarantee holds only within one
producer session, not durably across restarts -- the sequence counter is
snapshotted for broker-side leader failover, but is not designed to
survive an arbitrary producer-side crash/restart on its own. This means
**Filearr's agent-side `seq_no` must be durably persisted in the
agent's own SQLite DB** (which it already is, as the outbox table's
primary key) rather than held only in process memory -- a crash mid-batch
must not lose the sequence anchor, and since it's already the SQLite
table's PK this requirement is satisfied by construction, not by
additional work.

**Central (server-side) idempotent upsert** -- solidly confirmed,
standard pattern: `INSERT ... ON CONFLICT (agent_id, seq_no) DO NOTHING`
against a `replication_log` table (or the dedup check folded directly
into the upsert transaction against `items`), directly analogous to
Stripe-style idempotency keys (Brandur Leach's widely cited
"Implementing Stripe-like Idempotency Keys in Postgres" pattern:
store-first-response keyed on a client-supplied idempotency key -- here,
`(agent_id, seq_no)` is that composite key).

**Gap detection**: the server tracks, per agent, the **highest
contiguous `seq_no` it has accepted** (`agents.last_contiguous_seq_no`).
On receiving a batch, if the lowest `seq_no` in the batch is greater
than `last_contiguous_seq_no + 1`, there is a gap -- the server should
**not** advance `last_contiguous_seq_no` past the gap (store the
higher-numbered rows as received-but-not-contiguous, or simply reject
the batch and ask the agent to resend from `last_contiguous_seq_no + 1`
-- rejecting and re-requesting is simpler and recommended, since the
agent already has every unsent row durably in its own outbox by
`seq_no` and can trivially re-send the correct range; don't build
out-of-order buffering on the server for a gap case that should be
rare and cheap to recover from by re-asking).

Borrowing the **Postgres logical replication** watermark model
conceptually (`confirmed_flush_lsn` in `pg_replication_slots` -- the
subscriber-acknowledged point below which WAL is safely reclaimable, and
the documented behavior that slots don't rewind -- a subscriber
requesting replay from before the watermark gets fast-forwarded, not
errored): Filearr's server should track a per-agent "last confirmed
seq_no" watermark and, if an agent's local outbox has already
purged/compacted below the seq_no the server is asking to resume from
(e.g., after a local index rebuild per §3.3, which resets local seq_no
tracking), fall back to a **full reconciliation sweep** (§4.4) rather
than erroring -- this is the direct architectural link between "local
index got rebuilt" and "replication needs a resync," and should be an
explicit, tested code path, not an incidental edge case.

### 4.3 Batch size / backpressure

Concrete starting-point defaults, adapted from log-shipping agents
(the closest architectural cousins -- batching local writes for network
shipment with retry/backpressure):

- **Flush trigger**: size-OR-age, not count alone (Vector.dev's sink
  pattern: `batch.max_bytes` ~10MB, `batch.timeout_secs` ~1s -- flush on
  whichever comes first). Recommend an analogous **flush at 500 rows OR
  5 seconds OR 2MB serialized payload**, whichever comes first -- tuned
  down from Vector's numbers since Filearr's payloads (item metadata
  rows) are smaller and less latency-sensitive than log lines, and a
  slower flush cadence is fine given this is filesystem metadata, not
  real-time telemetry.
- **Local buffer overflow policy**: bounded local buffer (the outbox
  table itself, capped by available disk, which is effectively
  unbounded for this use case relative to expected row counts) --
  recommend **block** (agent simply accumulates unsent outbox rows
  indefinitely while offline, per the offline-first design goal) over
  **drop**, since dropping replication rows would silently desync the
  local and central catalogs, violating the integrity-over-speed
  priority order. An agent offline for weeks should still fully
  reconcile once reconnected, not have silently dropped changes.
- **Backoff**: exponential, reset on success (Filebeat's model: 1s->60s
  doubling, resets on success) rather than a fixed retry ceiling
  (Fluent Bit's `retry_limit=1` default is explicitly flagged as the
  wrong model here -- an agent that's offline for days must keep trying
  indefinitely, not give up after one retry).

### 4.4 Transport, and full-reconciliation sweep design

**Transport**: confirmed HTTPS+JSON+mTLS batch POST is a reasonable,
precedented choice for the agent->server (replication) direction -- Fleet's
fleetd/osquery does exactly this (`/api/v1/osquery/log`-style endpoints,
node-key + TLS auth). **Important calibration**: Wazuh and Tailscale,
also surveyed, actually use *more custom* protocols for their primary
channels (Wazuh: a custom AES-framed protocol over dedicated ports, not
HTTPS/JSON; Tailscale: Noise IK tunneled inside TLS/HTTP2, not plain
JSON) -- so the honest framing is "at least one major fleet-management
tool (Fleet) validates plain HTTPS+JSON+mTLS for this exact use case,"
not "every comparable tool does this." Given Filearr's stated bias
toward simplicity and the roadmap's own explicit preference, **plain
HTTPS+mTLS+JSON batch POST for replication remains the right choice** --
it is operationally simplest to implement, debug, and reason about, and
Fleet's precedent is a real, on-point validation even if not universal.

**Full-reconciliation sweep**: researched three tiers of design
(Merkle-tree anti-entropy per Cassandra/Dynamo; lighter sketch-based
reconciliation like Range-Based Set Reconciliation/Negentropy or IBLTs;
dumb full-manifest diff). Cassandra's own documentation calls Merkle
tree validation compaction "resource intensive" **at multi-terabyte,
continuous-multi-replica-repair scale** -- and RBSR/IBLT-class approaches
(Negentropy, used by Nostr relays syncing tens of millions of elements)
earn their complexity specifically for **low-bandwidth/high-round-trip
links** (open-internet relay gossip), not a LAN/VPN-adjacent
hub-and-spoke topology. At Filearr's actual expected per-agent scale
(10K-1M files), a `(rel_path, mtime, size, hash)` manifest for even 1M
files is on the order of ~150MB uncompressed, single-digit MB
compressed -- trivially sendable in seconds over any reasonable link, with
a straightforward server-side anti-join against Postgres to find
discrepancies. **Recommendation: a periodic full-manifest diff, not
Merkle/RBSR/IBLT**, at current and reasonably foreseeable scale. Treat
"agent corpora regularly exceed ~10M files" or "agents connect over
bandwidth-constrained/high-latency links" as the explicit re-evaluation
trigger for revisiting this, rather than building sketch-based
reconciliation pre-emptively.

Cadence: run the full-reconciliation sweep on a slow fixed interval
(e.g., daily) **and** opportunistically whenever an agent reconnects
after being offline past some threshold (e.g., >24h), since that's
exactly when incremental outbox replication is most likely to have
diverged from a local index rebuild (§3.3) or a missed tombstone.

### 4.5 Tombstone replication and purge coordination

Postgres logical replication itself has no tombstone concept (DELETEs
replay directly via WAL decoding -- no accumulating-tombstone problem).
Cassandra's `gc_grace_seconds` (default 10 days) exists specifically
because of N-way multi-master replica coordination -- a tombstone can't
be safely purged until *every* replica has compacted past it, or a
stale replica can resurrect deleted data. **Filearr's topology is
structurally simpler than Cassandra's**: one authoritative central
Postgres, not N-way gossip -- so it does not inherit Cassandra's
"wait for all replicas" uncertainty. It is structurally closer to
**Syncthing's model** (tombstone + retention window; Syncthing 2.0
auto-expires deleted-file records after 6 months by default, at real
risk of resurrection for a device offline longer than that).

**Recommendation**: tombstone rows (agent-local `status='missing'` or
`'trashed'`, mirroring central `ItemStatus`) replicate through the
**same outbox/seq_no path** as any other row change -- no separate
tombstone-replication mechanism needed. Purge (permanent removal from
both local and central storage) should require **both**: (a) the
existing central recycle-bin retention window already governing central
purge (CLAUDE.md invariant 4, "scheduled recycle-bin purge, retention
configurable" -- reuse this unchanged, don't invent a second retention
policy), **and** (b) confirmation that the specific originating agent's
last full-reconciliation sweep (§4.4) has been processed by the server
-- since there is only one central authority, this is a single watermark
check per agent, not an N-replica coordination problem the way
Cassandra's `gc_grace_seconds` is. An agent offline longer than the
recycle-bin retention window is a real, explicit edge case (central
might purge a file whose delete the agent hasn't even reported yet, or
vice versa) -- flagged as an open question in §11 rather than silently
resolved here, since it's a product policy decision (what happens when
an offline-for-months agent reconnects and its local index disagrees
with an already-purged central item) as much as an engineering one.

---

## 5. Update / rollout mechanism

### 5.1 TUF vs simpler signed-manifest

**python-tuf / TUF generally**: CNCF-graduated spec, genuinely mature
(Datadog's agent uses TUF+in-toto via `DataDog/go-tuf`; Fleet's Orbit
uses `go-tuf`). But the **operational burden is real and well-documented,
not merely theoretical**: four roles (root/targets/snapshot/timestamp),
offline threshold-signed root+targets keys requiring genuine key-ceremony
events (PyPI's 2020 ceremony was livestreamed; Sigstore's involved 5
keyholders across 4 organizations with hardware tokens; Fleet's own
engineering handbook documents 3 offline root keys held by named
individual executives, annual rotation, air-gapped signing via USB, and
a timestamp file that must be **re-signed every 2 weeks** as an ongoing
operational cost). **Direct, on-record judgment from a TUF/Sigstore
co-founder** (Dan Lorenc, blog.sigstore.dev, 2021-05-18): *"It's probably
not a good idea for a single developer with a cron job... most OSS
development is WAY closer to the Raspberry Pi model... I can't really
recommend this part in good faith for most OSS software."* No
small/solo-team OSS project was found running a scoped-down TUF
deployment as a positive case study.

**Recommendation: do not adopt full TUF.** Filearr is not Fleet-scale
(Fleet's TUF investment buys survivable-key-compromise guarantees that
justify its cost at Fleet's fleet size and threat model) and does not
have a team resourced for offline root-key ceremonies and biweekly
timestamp re-signing as an ongoing maintenance burden -- that operational
cost would compete directly with actual feature work, and the roadmap's
own priority order (security > integrity > reliability > **speed**...)
doesn't mean "adopt the most secure possible mechanism regardless of
cost," it means "don't trade security for speed" -- TUF here would trade
*maintainer time* for a security margin disproportionate to Filearr's
actual threat model (a compromised release pipeline, not a compromised
CDN with thousands of relying parties).

**Recommended alternative: minisign-style signed manifest**, following
the **Tauri** desktop-app-updater pattern directly (Tauri: sign release
artifacts in CI with a long-lived Ed25519 keypair, publish a
`latest.json`-style manifest referencing version + download URL +
signature, agent verifies the manifest signature against a pinned public
key before applying an update). minisign itself: ISC license, Ed25519 +
BLAKE2b pre-hash for large files, current version 0.12
(reported 2026-01-15), single maintainer (Frank Denis/jedisct1) but
long-track-record and low-complexity-by-design (this is a feature, not a
risk, for a tool whose entire purpose is "small, auditable, does one
thing"). Concrete design: CI signs `{version, platform, sha256,
download_url}` manifest entries with a minisign keypair whose private
key **never touches Filearr's build servers in plaintext** (stored in
CI secrets, ideally an HSM-backed signer for the release job only);
agent fetches the manifest over the same mTLS channel used for config
push, verifies the signature against a public key baked into the agent
binary at build time, downloads the referenced binary, verifies its
sha256, and only then proceeds to the OS-specific swap (§5.2).

**cosign** was also evaluated as a candidate: confirmed usable for
arbitrary blobs (`sign-blob`/`verify-blob`, not just OCI images), and
long-lived-keypair signing without Fulcio/Rekor is possible
(`--tlog-upload=false` / `--insecure-ignore-tlog=true`) -- but `cosign
initialize` still fetches/caches Sigstore's own TUF root even in
key-based mode, and true fully-offline verification isn't explicitly
guaranteed by any doc found (an open cosign feature request,
sigstore/cosign#2255, underscores this gap). **minisign is the lower-
complexity, lower-dependency choice** given this residual TUF-bootstrap
baggage in cosign's key-based mode; cosign is a legitimate fallback if
the project later wants Sigstore-ecosystem integration for other reasons.

### 5.2 A/B binary swap per OS

Per-OS mechanics, all directly confirmed against real implementations:

- **Windows**: cannot delete/overwrite a running `.exe`. Standard
  pattern (rclone's own `selfupdate` docs, Tailscale's `clientupdate`
  package): download the new binary to a temp path, rename the
  *currently running* binary aside (e.g. to `.old.exe` -- Windows
  permits renaming a running executable, just not deleting/overwriting
  it in place), install the new binary at the original path, and either
  re-exec (Tailscale's approach: the updater copies itself, re-executes
  the new binary, which then handles finishing the swap) or require a
  restart on next launch. Recommend Tailscale's re-exec pattern --
  cleaner for a background service that shouldn't wait for a user to
  manually relaunch it.
- **macOS**: replace the **entire signed app bundle atomically**, never
  patch individual files in place -- macOS code-signing hashes every
  bundle resource (`_CodeSignature/CodeResources`), so any post-signing
  file modification breaks the signature and triggers Gatekeeper
  rejection on next launch (Apple TN3126). This is the Sparkle
  (sparkle-project.org) approach and should be followed exactly: ship
  updates as a new, fully-signed-and-notarized bundle, swap the whole
  bundle directory via a small helper process, never modify files
  inside an already-signed bundle.
- **Linux**: safe by `unlink(2)`/`rename(2)` semantics -- a running
  process holds an open file descriptor to the old inode; renaming the
  path only rewrites the directory entry, so the running process keeps
  executing the old code in memory while new process launches
  immediately resolve to the new binary at that path. No watchdog
  process needed for the swap itself (though a supervisor is still
  useful for the crash-loop detection in §5.3).

### 5.3 Crash-loop rollback safety

No tool surveyed (including Chrome/Omaha) confirms fully autonomous,
crash-telemetry-triggered rollback of a *binary version* -- Chrome's
confirmed rollback mechanisms are **admin-policy-driven**
(`RollbackToTargetVersion` enterprise policy) or **feature-flag-level**
(Finch/Variations rolling back a *config*, not the binary, server-side)
-- don't cite "Chrome auto-rolls-back on crash" as an established
pattern; it isn't, per the primary sources found.

The best directly-portable pattern found is **boot-counting**, as used
by systemd-boot's "Automatic Boot Assessment" (tries-left/tries-done
counter on boot entries, cleared by a "boot succeeded" service once
healthy) and U-Boot's `bootcount`/`bootlimit`/`altbootcmd` (exceeding
the limit triggers an automatic fallback). **Recommendation: implement
the same idea at the agent level**, not the OS boot level:

1. Before applying an update, the updater writes a small local state
   file recording `{new_version, previous_binary_path, attempts: 0,
   max_attempts: 3}`.
2. On each launch of the new version, increment `attempts` and start a
   **health-check timer** (e.g., 60 seconds: local SQLite opens cleanly,
   mTLS handshake with central succeeds or fails gracefully without
   crashing, no unhandled panic/exception in that window).
3. If the health check passes within the window, clear the state file
   (update is confirmed good) -- this is the systemd-boot
   "boot succeeded" equivalent.
4. If the process crashes or fails the health check 3 times in a row
   (`attempts >= max_attempts` without ever clearing the state file),
   the *next* launch attempt (triggered by the OS service manager's own
   restart policy -- see below) instead restores `previous_binary_path`
   over the current binary and clears the state file, effectively
   rolling back automatically.
5. Pair this with the OS's own process-supervision restart policy so a
   crashed agent actually gets relaunched to trigger step 4: systemd
   `Restart=on-failure` + `StartLimitBurst=`/`StartLimitIntervalSec=` on
   Linux, a Windows Service with configured failure actions, and
   launchd's `KeepAlive`/`ThrottleInterval` on macOS -- all directly
   support "restart on crash, with a burst limit," which is exactly the
   trigger this design needs.

This is a small, self-contained state machine (a handful of fields in a
local file, no external dependency), directly modeled on a decades-proven
embedded/boot pattern rather than invented from scratch.

---

## 6. Config push

### 6.1 What comparable tools actually do (corrected against initial assumption)

The roadmap leans toward "long-poll/SSE for config push." Research
confirms this is defensible but should be calibrated against what
real comparable tools do, which is **more poll-oriented than the
roadmap's phrasing might suggest**:

- **Fleet/osquery**: **no true push exists.** osquery's `config_refresh`
  flag defaults to **0 (disabled)** -- config loads once at startup
  unless explicitly set to refresh on an interval. Fleet's own team
  stated directly (fleetdm/fleet#519): "Fleet notifies the host that it
  wants new information, then waits for the host to respond... on the
  interval it checks into Fleet." Fleet's "Refetch" mechanism is a flag
  consumed at the host's *next self-initiated* poll, not an out-of-band
  push -- an offline or slow-polling host gets nothing until it
  reconnects. This is a clean, validated precedent for a **poll +
  flag-triggered early check-in** model, not literal server push.
- **Wazuh**: described in its own docs as config being "pushed from the
  manager to agents," but delivery in practice rides on the agent's
  periodic keepalive/checksum check-in (`notify_time`/`time-reconnect`
  both default 60s) -- a hybrid: push *semantics*, poll-driven
  *delivery*, same pattern one layer up at the cluster level (workers
  initiate the connection to the master, master pushes down that
  worker-initiated channel).
- **osquery's `distributed_interval`** (default 60s, disabled by default
  via `disable_distributed=true`) is a genuinely **separate** interval
  from `config_refresh`, governing ad hoc distributed-query pull
  (the mechanism behind Fleet's live-query feature) -- worth noting as a
  precedent for Filearr keeping "policy config" and "on-demand command"
  as two conceptually separate channels even if they share transport.
- **Tailscale** is the one surveyed tool with genuine persistent-stream
  push: a client sends one `MapRequest{Stream: true}`, the server holds
  the HTTP response open and streams `MapResponse` updates (full map,
  then deltas: `PeersChanged`, `PacketFilter` for ACL changes) as they
  happen -- confirmed by reading Headscale's (open-source control-plane
  reimplementation) `serveLongPoll()` code directly. This is **not SSE
  framing** (no `text/event-stream` content type) -- length-prefixed JSON
  over chunked HTTP/2 riding on an already-encrypted Noise/TLS
  connection, with a ~50s keepalive ticker.

### 6.2 Recommendation: SSE remains sound, but calibrate expectations

FastAPI's native SSE support (`EventSourceResponse`, already used
elsewhere in Filearr per CLAUDE.md -- `/scans/{id}/events`) is a
reasonable, low-effort mechanism to reuse for config push, and it is
**architecturally sound** -- but the research does **not** find it
independently validated as "what everyone does" for this specific
use case; it's closer to "a reasonable FastAPI-native choice, most
directly comparable to Tailscale's persistent-stream approach, while
Fleet/osquery/Wazuh actually use plain interval polling with a
nudge-flag." Given that distinction, **recommend a layered design
rather than picking one exclusively**:

1. **Primary: periodic pull with ETag** (agent polls
   `GET /api/agents/{id}/policy` on a base interval -- e.g. every 5
   minutes -- sending `If-None-Match` with its last-seen policy version
   hash; server returns `304` cheaply if unchanged, full policy body +
   new ETag if changed). This is the Fleet/osquery-precedented,
   trivially-scalable, corporate-proxy-friendly baseline that works
   even for agents behind restrictive networks that kill long-lived
   connections -- a real, cited tradeoff (SSE/long-poll connections can
   be silently dropped by aggressive corporate proxies, a known
   operational headache for exactly this class of software).
2. **Secondary, opportunistic: SSE for "check in now"**, reusing the
   existing `EventSourceResponse` pattern, held open only while the
   agent's own local UI/CLI is actively in use (not as a permanent
   always-open background connection for every agent) -- this gives the
   "admin changes a policy and wants it applied immediately" UX win
   without paying Tailscale-scale persistent-connection costs for every
   idle agent, all the time. An agent with no local UI open falls back
   to the ETag poll cadence, which is an acceptable latency tradeoff for
   background policy changes (a scan-path exclusion doesn't need
   sub-second propagation).
3. This explicitly avoids over-committing to "every agent holds a
   permanent SSE connection to the central server," which doesn't have
   real precedent at Filearr's target deployment scale (self-hosted,
   likely tens to low-hundreds of agents per deployment, not
   Tailscale's global-scale control plane) and reintroduces a real
   connection-scaling and corporate-firewall cost for a marginal latency
   win.

### 6.3 Versioned policy audit trail

Convergent pattern across every tool surveyed (Fleet's Activities feed:
`id`, `created_at`, `actor_full_name/id/email`, `type`, `details{}`,
independent of its git-based content history; PuppetDB's catalog
records: `certname`, `version`, `hash`, `transaction_uuid`,
`producer_timestamp` -- the strongest native "which policy version ran
where, when" queryable API found) -- but **no single named
cross-tool standard**. A notable, directly relevant gap: **Kubernetes
Deployments and Helm releases have no built-in actor field** -- audit
attribution has to be bolted on via separate audit logs, which is a
worse position than Fleet/PuppetDB's first-class-actor-column approach.

**Recommendation**: make `policy_versions` (central Postgres) a
first-class table, not a reconstructed-from-logs afterthought:

```sql
CREATE TABLE policy_versions (
    id           UUID PRIMARY KEY DEFAULT uuidv7(),
    scope_type   TEXT NOT NULL,   -- 'agent' | 'agent_group' | 'global'
    scope_id     UUID,            -- NULL for 'global'
    version      INTEGER NOT NULL,-- monotonic per (scope_type, scope_id)
    policy       JSONB NOT NULL,  -- the effective policy payload (preset
                                  -- bundles, scan_paths overrides, etc.
                                  -- per phase-2-indexing-controls.md)
    actor        TEXT NOT NULL,   -- API key name / user, never nullable
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_type, scope_id, version)
);
```
Agents report back the policy `version` they have applied on every
replication batch (a cheap extra field on the existing outbox POST, not
a separate endpoint), giving the central console a live "which agents
are on the current policy vs. lagging" view for free -- directly
reusable for the staged-rollout-group tracking in §5 (an agent group's
rollout progress is the same "which version has each agent confirmed"
query, applied to binary versions instead of policy versions).

---

## 7. Concrete component designs

### 7.1 Enrollment flow sequence

```
Operator (central console)              Agent (new machine)              step-ca
  |                                          |                               |
  |-- mint one-time enrollment token ------->|                               |
  |   (out-of-band: copy/paste, QR, deep-link)                              |
  |                                          |-- generate local keypair --->|
  |                                          |-- step ca bootstrap -------->|
  |                                          |   (pin CA root fingerprint)  |
  |                                          |-- CSR + enrollment token --->|
  |                                          |                               |
  |                                          |<-- signed short-lived cert --|
  |                                          |   (24-72h TTL; subject CN =  |
  |                                          |    machine-generated agent_id)|
  |                                          |
  |                                          |-- POST /agents/register ---->| (central API,
  |                                          |   {agent_id, hostname, os,   |  mTLS now live)
  |                                          |    platform_defaults} -------|
  |                                          |
  |<---------------------------------------- agent now visible in console,
  |                                           default policy (per-platform
  |                                           default search locations,
  |                                           phase-2-indexing-controls.md
  |                                           S1.10) applied
  |
  [background, indefinitely]
  |                                          |-- step ca renew (60% of ---->| (renews without
  |                                          |    lifetime elapsed)         |  re-enrollment)
```

Key properties: the enrollment token is single-use and short-TTL (should
expire in minutes-to-hours, not days, since it's the one step with a
human-copy-paste weak link); the certificate's Common Name or SAN
encodes the agent's stable identity (a server-issued or
locally-generated-then-confirmed UUID -- recommend **server-assigned**
at the `/agents/register` step, not client-generated, so the central
Postgres `agents.id` is authoritative and never collides -- the
Syncthing "cert-hash-as-device-ID" precedent from the roadmap is
adapted here to "cert CN carries a server-confirmed ID," not
"cert hash alone is the ID," since Filearr needs a human-friendly,
revocable-by-ID entity in its own Postgres schema, not just a
cryptographic identifier).

### 7.2 Outbox DDL

Already specified in full in §4.1. Central-side companion table:

```sql
CREATE TABLE agents (
    id                     UUID PRIMARY KEY DEFAULT uuidv7(),
    hostname               TEXT NOT NULL,
    platform               TEXT NOT NULL,   -- 'windows'|'macos'|'linux'
    cert_fingerprint       TEXT NOT NULL UNIQUE,
    last_contiguous_seq_no BIGINT NOT NULL DEFAULT 0,
    last_seen_at           TIMESTAMPTZ,
    agent_version          TEXT,
    policy_version_applied INTEGER,
    revoked_at             TIMESTAMPTZ,      -- application-layer kill
                                              -- switch, see S1.4
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_replication_log (
    agent_id   UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    seq_no     BIGINT NOT NULL,
    item_id    UUID NOT NULL,
    op         TEXT NOT NULL,       -- 'upsert' | 'tombstone'
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, seq_no)  -- the idempotency key
);
```

`agent_replication_log` is intentionally a thin append-only ledger, not
the `items` table itself -- the actual upsert into central `items`
happens in the same transaction as the `agent_replication_log` insert,
and `ON CONFLICT (agent_id, seq_no) DO NOTHING` on this table is what
makes a retried batch (agent didn't see the ACK, resends) safe: if the
`(agent_id, seq_no)` pair already exists, the whole transaction is a
no-op, so the `items` upsert doesn't get re-applied either (wrap both in
one transaction gated by the ledger insert's conflict check).

### 7.3 Replication endpoint contract

```
POST /api/agents/{agent_id}/replication-batch
Headers: mTLS client cert (agent_id must match cert-derived identity)

Body:
{
  "entries": [
    {"seq_no": 1042, "item_id": "...", "op": "upsert", "payload": {...}},
    {"seq_no": 1043, "item_id": "...", "op": "tombstone", "payload": {...}}
  ]
}

Response 200:
{"accepted_from": 1042, "accepted_to": 1043, "last_contiguous_seq_no": 1043}

Response 409 (gap detected):
{"error": "gap", "expected_seq_no": 1040, "resend_from": 1040}
```

Idempotency: safe to POST the same batch twice (server-side `ON
CONFLICT DO NOTHING` per §7.2); safe to POST overlapping batches (a
retry that includes already-ACKed entries just no-ops those specific
rows via the same conflict check, individual entries within a batch are
not all-or-nothing at the transaction level -- process entries in
`seq_no` order within the batch, stopping at the first gap rather than
rejecting the whole batch, so a batch that's *mostly* new but restates
one already-seen row doesn't have to be discarded wholesale).

### 7.4 Agent state machine (offline -> sync)

```
                    +-------------+
        startup     |   OFFLINE   |<-------------+
       -----------> | (local scan  |             |
                    |  + local FTS  |             | mTLS handshake
                    |  fully usable)|             | fails / cert
                    +------+------+             | expired & unrenewable
                           | mTLS handshake            |
                           | succeeds                  |
                           v                            |
                    +-------------+                    |
                    |  CONNECTED   |--------------------+
                    | (renew cert  |
                    |  if due)     |
                    +------+------+
                           |
                           v
                    +-------------+
                    |  DRAINING    |  outbox rows with seq_no >
                    |  OUTBOX      |  last_contiguous_seq_no
                    +------+------+  sent in batches (S4.3)
                           |
                    all caught up
                           |
                           v
                    +-------------+
                    |   STEADY     |  incremental outbox drain as
                    |   STATE      |  local scans produce new rows;
                    |              |  config poll on ETag interval
                    +------+------+  (S6.2)
                           |
              time since last full
              reconciliation sweep
              exceeds threshold, OR
              agent was offline > 24h
                           |
                           v
                    +-------------+
                    |    FULL      |  manifest diff (S4.4) against
                    | RECONCILE    |  central; resolves any drift
                    +------+------+  from missed tombstones etc.
                           |
                           +----------> back to STEADY STATE
```

The critical property: **every state except CONNECTED/DRAINING/STEADY
is fully functional for local search** (OFFLINE is not a degraded
mode for the agent's own user -- it's the expected, designed-for normal
condition when disconnected, per the roadmap's explicit "fully
functional offline" requirement). Only replication/config-sync
functionality depends on connectivity.

---

## 8. Threat model sketch

| Threat | Mitigation |
|---|---|
| **Stolen agent certificate/private key** (e.g. stolen laptop) | Short cert TTL (24-72h, §1.3) bounds exposure window; passive revocation (refuse renewal, §1.4) stops the thief from indefinitely renewing; application-layer `agents.revoked_at` denylist (§1.4) gives an immediate kill-switch on the replication/config API specifically, independent of cert validity, checked on every request post-handshake. Residual risk: a stolen cert remains valid for up to its TTL even after the operator revokes/denylists it if the central server can't be reached to check the denylist -- mitigate by keeping the denylist check on the *server* side only (never trust an agent's own claim about its revocation status) and keeping TTLs short enough that this window is operationally acceptable. |
| **MITM on agent<->server traffic** | mTLS (both directions authenticated, not just server-authenticated TLS) -- an attacker without a valid signed client cert cannot complete the handshake at all, and without the CA's private key cannot forge one. Certificate pinning at bootstrap (`step ca bootstrap`'s root-fingerprint pinning) prevents a MITM from substituting a different CA during initial enrollment specifically, the one point before mTLS is fully established. |
| **Malicious/compromised central server** | Out of scope for a hub-and-spoke design to fully defend against (the central server is the trust root by construction -- this is an accepted architectural tradeoff, not an oversight). Partial mitigations worth having: agent-side signature verification on update manifests (§5.1) means a compromised central server *cannot* push an unsigned/wrongly-signed malicious binary update even if it fully controls the replication/config channels -- the signing key should be kept separate from the server's own operational credentials (ideally on a separate, rarely-used signing machine/HSM) specifically so "central server compromised" doesn't automatically imply "attacker can push malicious agent updates," which would otherwise be the single most severe failure mode in this whole design. |
| **Malicious/compromised agent** | The central server should not blindly trust agent-reported data beyond what mTLS authenticates (i.e., "this data really came from agent X") -- it does not, and should not, imply "this data is truthful" (a compromised agent could report false file metadata). This is accepted for v3 (matching invariant 6's "read-only until write-back" caution) -- replicated data lands in central Postgres as agent-attributed, and any future write-back/RBAC work (roadmap §3) should treat agent-originated data with the same "verify, don't just trust" posture already implied by the app-layer ACL design. |
| **Replay of an old replication batch** | `(agent_id, seq_no)` idempotent upsert (§4.2/§7.2) makes a replayed batch a safe no-op rather than a duplicate/corruption risk -- this is the primary replay defense and it's already load-bearing for the ordinary retry case, not just the adversarial one. |
| **Config-push tampering / forged policy** | Policy is delivered over the same mTLS channel (server-authenticated to the agent, agent-authenticated to the server) -- an on-path attacker without a valid cert on either side cannot inject a forged policy. Consider signing policy payloads themselves (not just transport-layer protection) if a future threat model specifically includes "attacker who has compromised a reverse proxy in front of the central API but not the API server itself" -- flagged as an open question (§11), likely over-engineering for v3's initial scope given mTLS already covers the more probable threat. |

---

## 9. Language/runtime recommendation summary

**Go**, using `modernc.org/sqlite` (pure-Go, FTS5-capable) and
`fsnotify`. Justification recap: only option that delivers true
single-host cross-compilation to Windows/macOS/Linux (avoiding a
three-native-CI-runner operational burden), smaller/more auditable
artifact than a PyInstaller/Briefcase bundle (no embedded interpreter),
comparable or better idle memory footprint expectation, and a more
turnkey dual-OS code-signing story (GoReleaser) than Rust's currently-
gapped `cargo-dist` macOS notarization support.

**Reuse plan for the existing Python scanner**: reimplement the
walk/diff/tombstone *logic* (not code) in Go -- it's a well-specified
algorithm (mtime+size filter, quick_hash/content_hash tiering, move
detection, tombstone-not-delete), directly portable. For per-type
extraction: shell out to non-Python CLI tools (ffprobe, exiftool) where
good ones exist; for extraction types with no non-Python equivalent
(3D mesh via trimesh, structured document properties), defer full
extraction fidelity to the central server (agent replicates
lightweight metadata immediately; central server extracts richer
properties from replicated file content when policy allows), rather than
either reimplementing those parsers in Go or embedding a Python runtime
in the agent (which would defeat the reasons Go was chosen). This
fidelity gap is an explicit, flagged open product question (§11), not
silently resolved.

---

## 10. Task breakdown

Sequenced enroll -> scan-local -> replicate -> config-push -> update, per
project T-numbering convention (continuing from existing T-series;
recommend prefixing this roadmap item's tasks `TA-` for "agent" to avoid
collision with Phase-1's `T1-T11` and Phase-2's `T4-N`).

**TA-1 -- step-ca deployment + enrollment token flow (size M)**
Stand up step-ca (or document OpenBao PKI as the pluggable alternative,
§1.2) as an optional compose service; central API endpoint to mint
single-use, short-TTL enrollment tokens; `agents` table (§7.2).
*Accept*: an operator can mint a token via the API, and a manually-run
`step ca certificate` call using that token receives a valid short-lived
client cert scoped to a server-confirmed `agent_id`.

**TA-2 -- Go agent skeleton: enrollment + cert renewal (size M)**
New `agent/` module (Go). Implements the enrollment flow (§7.1) against
step-ca's HTTPS/ACME-adjacent API directly (no CLI subprocess
dependency in production -- evaluate step-ca's Go client library vs.
hand-rolled HTTP calls against its API at implementation time).
Background renewal daemon per §1.3 (fixed-fraction timer + "CA rotated"
push trigger).
*Accept*: agent binary enrolls against a running step-ca using a
minted token, persists its cert+key locally, and successfully renews
before expiry without human intervention in a soak test spanning
multiple renewal cycles.

**TA-3 -- Local SQLite FTS5 index + filesystem walk (size L)**
Port walk/diff/tombstone logic (§9) to Go; `modernc.org/sqlite`
schema per §3.3; `unicode61`/`trigram` FTS5 virtual table + sync
triggers; `PRAGMA integrity_check` startup guard + rebuild-from-walk
recovery path (§3.3); `fsnotify`-based watch mode reusing the existing
project's debounce-into-full-rescan decision (T5 precedent) rather than
inventing a new incremental model.
*Accept*: a fresh agent, pointed at a local directory, produces a
searchable local FTS5 index matching a reference walk's file count;
deliberately corrupting the SQLite file and restarting the agent
triggers a clean full rebuild without manual intervention; disabled
network connectivity throughout has no effect on local search
functionality (offline-first requirement).

**TA-4 -- Outbox + replication client (size L)**
Outbox table (§4.1) written in the same local transaction as item
upserts/tombstones; drain loop with size/age-triggered batching and
backoff-with-reset (§4.3); replication endpoint + idempotent upsert on
the central side (§7.2/§7.3).
*Accept*: killing the agent process mid-batch and restarting it results
in zero data loss and zero duplicate central rows (verify via the
`(agent_id, seq_no)` idempotency key surviving a forced retry of an
already-partially-ACKed batch); an agent disconnected for a simulated
72 hours fully catches up on reconnection without manual intervention.

**TA-5 -- Full-reconciliation sweep (size M)**
Manifest-diff design per §4.4; triggered on a fixed interval and on
reconnection-after-threshold; server-side anti-join against Postgres.
*Accept*: a deliberately-desynced agent (e.g., central row manually
edited to diverge from what the agent would report) is corrected by
the next reconciliation sweep without requiring a full local index
rebuild.

**TA-6 -- Config poll (ETag) + opportunistic SSE (size M)**
`policy_versions` table (§6.3); `GET /api/agents/{id}/policy` with
ETag/If-None-Match support; agent-side poll loop; SSE endpoint reused
from the existing scan-events pattern, held open only while a local
agent UI/CLI is active (§6.2).
*Accept*: a policy change (e.g., a new preset bundle toggled centrally)
is applied by a background-only agent within one poll interval, and
within seconds by an agent with its local UI open; `agents.
policy_version_applied` accurately reflects the last-applied version
for both paths.

**TA-7 -- Signed update manifest + updater (size L)**
minisign-based signing pipeline in CI (§5.1); agent-side manifest
fetch/verify/download/swap per §5.2's OS-specific mechanics; crash-loop
boot-counting rollback state machine (§5.3); staged rollout groups
(agent-group-scoped manifest visibility, reusing the `agents` table's
grouping -- exact grouping mechanism shared with roadmap §3's "machine
groups" RBAC work, flagged as a cross-cutting dependency in §11).
*Accept*: a signed update rolls out to a designated canary group first
and only reaches the full fleet after an operator confirms the canary
group is healthy (via the same "which version has each agent
confirmed" query pattern from §6.3); a deliberately-broken binary
(crashes on startup) triggers automatic rollback to the previous
version within 3 launch attempts without manual intervention.

**TA-8 -- Local CLI/web UI against the local index (size M, roadmap §2)**
`filearr query ...` CLI and optional local web UI reading the local
SQLite FTS5 index directly; policy-controlled enable/disable from the
central console (roadmap §2's explicit requirement) -- this needs
TA-6's config channel to carry a "local access enabled/disabled,
auth-required" flag.
*Accept*: local search works with the agent fully disconnected from
central; a centrally-set "disable local access" policy is honored
within one config poll interval.

---

## 11. Open questions

1. **Local extraction fidelity gap.** §2.6/§9 flags that some
   per-type extraction (3D mesh via trimesh, structured document
   properties) has no good Go-native equivalent and would need to
   either ship as central-server-side post-replication extraction or be
   left out of the local index entirely. This is a real product
   decision (does the local index need full metadata parity with
   central, or is "path/size/mtime/hash/filename-derived title" enough
   for offline "where did I put that file" search) that should be made
   explicitly before TA-3, not discovered mid-implementation.
2. **Gitignore-semantics library for Go.** `docs/research/phase-2-
   indexing-controls.md` recommends Python's `pathspec` for the central
   scanner's preset-bundle matching; the Go agent needs its own
   equivalent for local `scan_paths`/preset evaluation (candidates:
   `denormal/go-gitignore`, `sabhiram/go-gitignore` -- neither
   independently vetted in this research pass for gitignore-negation
   correctness the way `pathspec` was). Needs its own short evaluation
   pass before TA-3, mirroring the rigor `phase-2-indexing-controls.md`
   §2 applied to the Python side.
3. **Offline-agent-vs-recycle-bin-purge race** (§4.5). An agent offline
   longer than central's recycle-bin retention window could have its
   pending tombstone/delete reports arrive after central has already
   permanently purged the item via an unrelated path (e.g., a different
   agent or the central scanner touching the same library). Needs an
   explicit resolution policy (likely: central purge always wins,
   agent's late tombstone report becomes a no-op against an
   already-gone row) -- flagged, not resolved, here.
4. **Server-assigned vs agent-generated identity at enrollment**
   (§7.1). Recommended server-assigned `agent_id` for Postgres-side
   authority, but this creates a brief window where the agent has a
   valid cert (CN may need to embed a server-confirmed ID) before or
   during the `/agents/register` call -- exact ordering (does the CA
   issue the cert before or after registration confirms the ID?) needs
   a precise sequence decision, not just the high-level flow sketched
   in §7.1.
5. **Policy-payload signing, beyond transport-layer mTLS** (§8's config-
   push tampering row). Flagged as likely over-engineering for v3's
   initial threat model, but worth an explicit accept/reject decision
   rather than silent omission, especially once RBAC/machine-groups
   (roadmap §3) introduce more actors who can author policy centrally.
6. **Staged-rollout-group definition dependency on roadmap §3.**
   TA-7's canary/staged-rollout groups need *some* grouping concept;
   roadmap §3 defines "machine groups" for RBAC purposes at a later
   phase. Decide whether TA-7 needs its own lightweight ad hoc grouping
   now (e.g., a simple `agents.rollout_group` text column) or should
   explicitly wait for/share the RBAC machine-groups model -- building
   two incompatible grouping concepts would be a real, avoidable design
   debt.
7. **Exact step-ca client integration** (native Go/Python SDK call vs.
   shelling out to the `step` CLI from the agent binary). Shelling out
   reintroduces a bundled-external-binary packaging problem (defeating
   part of the single-static-binary rationale for choosing Go);
   calling step-ca's HTTP/ACME API directly is cleaner but needs a
   concrete implementation spike to confirm feasibility before TA-2 is
   sized with confidence.

---

## Sources cited (by section)

- **S1**: github.com/smallstep/certificates, smallstep.com/open-source/,
  smallstep.com/product/step-ca-pro/, smallstep.com/docs/step-ca/
  acme-basics/, smallstep.com/docs/step-ca/renewal/, smallstep.com/docs/
  step-ca/revocation/, smallstep.com/blog/passive-revocation/ (Mike
  Malone, 2025-05-25), HashiCorp Vault BSL announcement (Aug 2023),
  OpenBao project (Linux Foundation, MPL-2.0)
- **S2**: PyInstaller docs/GitHub issues (#6754, #8164), Briefcase/
  BeeWare docs (0.3.25), Go docs on GOOS/GOARCH cross-compilation,
  fsnotify/fsnotify GitHub (BSD-3-Clause), modernc.org/sqlite docs,
  mattn/go-sqlite3 GitHub, gogs/gogs#7882, `cross` tool (v0.2.5) docs,
  notify crate (CC0-1.0) GitHub, rusqlite/docs.rs, axodotdev/
  cargo-dist#1121, Wazuh agent sizing docs
- **S3**: sqlite.org/fts5.html, sqlite.org/spellfix1.html,
  sqlite.org/wal.html, sqlite.org SQLite FAQ (commit throughput),
  github.com/sist2app/sist2 README (backend comparison table), Recoll
  usermanual (`-z`/`-Z`), macOS `mdutil` docs
- **S4**: Debezium Outbox Event Router docs, Kafka KIP-98 (idempotent
  producers), Postgres logical replication docs
  (`pg_replication_slots`, `confirmed_flush_lsn`), Brandur Leach
  "Implementing Stripe-like Idempotency Keys in Postgres", Vector.dev
  sink/batching docs, Fluent Bit docs (`Mem_Buf_Limit`, `retry_limit`),
  Filebeat docs (`bulk_max_size`, backoff), Cassandra `nodetool repair`/
  Merkle tree docs, Dynamo paper (Merkle trees), Range-Based Set
  Reconciliation (arXiv:2212.13567), Negentropy (Nostr), IBLT
  (Eppstein et al., SIGCOMM 2011), Syncthing 2.0 tombstone retention
  (PR #10023)
- **S5**: python-tuf/GitHub releases, CNCF TUF graduation, Docker
  Content Trust/Notary retirement (blog.docker, 2025-07-29), Datadog
  `DataDog/go-tuf`, PyPI PEP 458/480, Fleet engineering handbook (TUF/
  Orbit key ceremony, fleetdm.com/handbook/engineering/tuf), Dan Lorenc
  blog.sigstore.dev (2021-05-18), minisign (jedisct1), Tauri updater
  docs, sigstore/cosign docs + #2255, rclone `selfupdate` docs, Tailscale
  `clientupdate` (pkg.go.dev), Sparkle (sparkle-project.org), Apple
  TN3126, man7.org `unlink(2)`, systemd.service(5)/systemd.unit(5),
  systemd-boot Automatic Boot Assessment, U-Boot bootcount docs,
  Chrome/Omaha `RollbackToTargetVersion` enterprise policy docs
- **S6**: osquery CLI flags docs (`config_refresh`, `distributed_interval`),
  fleetdm/fleet#519, Wazuh architecture/centralized-configuration docs,
  Headscale `serveLongPoll()` source, RFC 9110 (ETag/If-None-Match),
  Consul blocking queries docs, Fleet Activities API docs, PuppetDB
  catalog/report API docs, Kubernetes Deployment revision/ConfigMap
  docs, Helm release-secret storage docs

**Honest gap summary** (do not present these as settled facts without a
direct spot-check): exact current step-ca patch version; OpenBao GitHub
star count / production-adopter list; exact `rusqlite` FTS5 feature-flag
name; Tailscale/Wazuh exact keepalive-interval and propagation-latency
figures; Fleet's exact per-component update-channel flag names; systemd's
exact default `StartLimitIntervalSec`/`StartLimitBurst` values for
recent versions; whether `cosign verify-blob` in key-only mode makes
zero network calls. All of these were sourced via search-snippet
extraction during a rate-limited research session rather than a fully
rendered primary-source fetch.
