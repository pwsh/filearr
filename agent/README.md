# filearr-agent (Phase 5 — Distributed Agent Architecture, v3)

> **Status: buildable — P5-T2 (enrollment + renewal) shipped 2026-07-16.**
> Module `github.com/filearr/filearr/agent`, **Go 1.26** (the go.mod pin),
> production dependency `github.com/smallstep/certificates v0.30.2` only.
> `cmd/filearr-agent` + `internal/enroll` exist and are tested (including an
> in-process step-ca authority — no Docker required); the remaining packages
> land with their tasks in
> [`docs/tasks/phase-5-distributed-agents-tasks.md`](../docs/tasks/phase-5-distributed-agents-tasks.md);
> rationale and citations live in
> [`docs/research/phase-5-distributed-agents.md`](../docs/research/phase-5-distributed-agents.md).
> Dev-host notes (Go path, test commands): [`docs/dev-windows.md`](../docs/dev-windows.md).

The agent is a **per-machine** companion to the central Filearr server. It scans
one host's *local* disks, keeps a local SQLite/FTS5 index that is **fully usable
offline**, and replicates lightweight file-change events up to central Postgres
over mTLS. Central remains the single source of truth (CLAUDE.md invariant 1);
the local index is disposable and rebuildable from a filesystem walk, exactly as
Meilisearch is one level up.

## What the agent is (and is not)

- **Is:** an offline-first local catalog + a reliable, at-least-once,
  idempotent replication client. Local search answers *"where did I put that
  file"* (path / size / mtime / hashes / filename-derived title — **Architect
  ruling R1**).
- **Is not:** a full metadata extractor. Heavy/exotic per-type extraction (3D
  mesh, structured document properties) stays **central and post-replication**.
  The local index carries only the R1 field set until central enriches the item.

## Language / packaging (brief §2.6 — decided)

- **Go**, single static binary, cross-compiled for Windows/macOS/Linux.
- `modernc.org/sqlite` (pure-Go, FTS5-capable, **no cgo**) for the local index.
- `fsnotify` for watch mode (reusing the shipped scanner's debounce-into-full-
  rescan decision from T5; inotify unreliable over SMB/NFS — watch is local-disk
  only, same gotcha as central).
- `zeebo/xxh3` (MIT, no cgo) for the quick/content hash tiering ported from the
  Python scanner.
- **No embedded Python interpreter.** Porting the scanner is a *logic* port, not
  a code embed (brief §2.6 reuse strategy). Extraction that has good non-Python
  CLIs (ffprobe/exiftool) may be shelled out; everything else defers to central.
- Gitignore/preset matching: **`github.com/git-pkgs/gitignore` v1.2.0** (MIT),
  ruled by the **P5-T3a** spike (sole 44/44 pass against the `pathspec` ground
  truth; exact pin + permanent vector gate + vendor contingency — see
  `docs/research/phase-5-t3a-gitignore-spike.md`).

## Build prerequisites

- Go toolchain ≥ 1.26 (pinned in `go.mod`).
- No cgo / no C compiler required (pure-Go SQLite is a deliberate constraint).
- Cross-compilation via `GOOS`/`GOARCH` — no per-OS CI runner fleet.
- A running central Filearr server + a step-ca (or OpenBao PKI) instance for
  enrollment integration tests (**P5-T1**/**P5-T2**).

## Trust / enrollment model (brief §7.1, Architect ruling R3)

Registration precedes certification. The operator mints a **single-use,
short-TTL enrollment token** centrally; the agent registers with that token
**first**; the server assigns the authoritative `agent_id`; the agent then
generates its keypair + CSR **embedding that `agent_id` in the cert CN/SAN**;
the CA signs. **No certificate is ever issued before registration.** mTLS is the
only integrity layer on the config-push channel for v3 — payload signing beyond
mTLS is explicitly **rejected** for the single-operator trust model (**R4**),
revisited only when phase-6 RBAC introduces multiple policy authors.

## CLI framework + branded packaging (P7-T3)

Top-level dispatch is **urfave/cli v3** (MIT, low-dependency — the P7-T3 CLI
framework ruling). The pre-existing subcommands keep byte-compatible stdlib-flag
parsing (they run under `SkipFlagParsing` and delegate to their original
handlers); only the new `query` command defines native urfave flags, so
`bash/zsh/fish/pwsh` shell completion (built into urfave v3) covers its flags.

The user-facing verb **`filearr query`** is this binary's `query` subcommand.
Ship a `filearr` **symlink/alias → `filearr-agent`** (single static binary, no
second artifact) to give the branded UX:

```sh
ln -s filearr-agent /usr/local/bin/filearr      # linux/macOS
# Windows: a filearr.exe hardlink, or a `doskey`/PowerShell `Set-Alias`
filearr query 'kind:video size:>1G modified:<7d'
```

`query` dials the P7-T2 same-user socket/pipe (the supported offline surface);
the older `search` subcommand opens the index file directly and is retained only
for local debugging.

## Local web UI (P7-T5)

The `run` daemon can also serve a **minimal, read-only browser search surface** —
a search box + results table (path / size / modified / kind, per-row copy-path),
debounced live queries against the same offline query engine the CLI uses. It is
**not** an admin console: there is no settings page and no write path, ever. Its
JS issues only `GET` requests.

- **Bind address:** loopback TCP only — default **`127.0.0.1:8686`**. Override
  with `-web-addr host:port` on `run`, or `FILEARR_AGENT_WEBUI_ADDR` (flag wins).
  A non-loopback bind (e.g. `0.0.0.0`) is **refused** (the "0.0.0.0-day" class);
  the web UI is loopback-only by design. The socket/named-pipe query transport
  (P7-T2) is a separate listener and is unaffected by the web UI's state.
- **Policy-gated (fail closed):** the listener serves **only** while the central
  policy has `web_ui_enabled = true` **and** the cached policy is fresh within
  `offline_grace_seconds` (default 24h). A never-contacted agent starts with the
  web UI **off**. Centrally disabling it — or the policy going stale past grace —
  takes the UI down within one poll interval with **no central push** (R4
  asymmetry); the CLI/socket query path keeps answering same-user queries.
- **Auth (Jupyter bootstrap-token pattern), gated by `auth_required` (default
  on):** at each listener start the daemon prints a one-time tokenized URL to its
  log/stdout:

  ```text
  filearr local web UI: open http://127.0.0.1:8686/?token=<hex>
  ```

  Opening it exchanges the token (constant-time compared) for an `HttpOnly` +
  `SameSite=Strict` session cookie and redirects to strip the token from the URL;
  all page + API routes then require the cookie. With `auth_required = false` the
  UI serves without a token (still loopback-bound + Host-checked). The token
  rotates every time the listener (re)starts.
- **Hardening:** a strict **Host-header allow-list** (only `localhost` /
  `127.0.0.1` / `[::1]`, `403` pre-handler — the DNS-rebinding defence, no
  skip-check escape hatch); **GET/HEAD-only** method-scoped routing plus a global
  non-GET/HEAD `405` backstop; stdlib **`net/http.CrossOriginProtection`** (CSRF)
  wrapping the mux; a self-only `Content-Security-Policy`. Embedded assets
  (`embed.FS` + `http.FileServerFS`) carry content-hash **ETags** so conditional
  requests return `304`.
- **R3 restricted view:** when the cached policy carries path-scope predicates the
  page shows a visible "restricted view" banner (and a stale-policy banner when the
  cache is past grace). The search box copy documents that local search is
  trigram/substring based and **not** typo-tolerant like central search — a
  zero-result local search does not mean the file is missing.

## Module layout

See [`docs/layout.md`](docs/layout.md) for the intended module tree
(`cmd/filearr-agent` + `internal/{enroll,scan,index,outbox,config,update,localapi}`),
with the key types and responsibilities of each package.

## Self-update: release signing + rollout (P5-T7)

The agent self-updates from a **minisign-style signed manifest** (full TUF
explicitly rejected, research §5.1). Central stores and serves the manifest +
artifacts but is **untrusted for update integrity** (research §8): it cannot
re-sign a manifest, so a compromised central cannot push a wrongly-signed
binary. The signing **private key lives only on the operator's signing machine**
(default `~/.filearr-signing` / `%USERPROFILE%\.filearr-signing`), is backed up
to a vault, and is **never committed** (`.gitignore`-guarded) and never reaches
central. The matching **public key is pinned into the agent binary at build
time**.

### One-time: generate the signing keypair

```bash
go run ./cmd/filearr-release keygen
# -> writes ~/.filearr-signing/filearr-release.{key,pub} (key is 0600)
# -> prints the base64 PUBLIC key + the exact -ldflags to pin it
```

`keygen` refuses to overwrite an existing key (regenerating invalidates every
already-pinned agent). Store the private key in your vault.

### Build the agent with the public key pinned

```bash
PUB=$(cat ~/.filearr-signing/filearr-release.pub)
for t in linux/amd64 windows/amd64 darwin/arm64; do
  GOOS=${t%/*} GOARCH=${t#*/} CGO_ENABLED=0 go build \
    -ldflags "-X main.Version=1.4.0 -X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=$PUB" \
    -o dist/filearr-agent-${t%/*}-${t#*/}$([ ${t%/*} = windows ] && echo .exe) ./cmd/filearr-agent
done
```

An agent built **without** the `-X ...update.PublicKeyBase64=` pin cannot verify
any manifest and **refuses every update** (fail-closed, logged once at startup).

### Sign the built artifacts

```bash
go run ./cmd/filearr-release sign -version 1.4.0 -out manifest.json \
    linux/amd64=dist/filearr-agent-linux-amd64 \
    windows/amd64=dist/filearr-agent-windows-amd64.exe \
    darwin/arm64=dist/filearr-agent-darwin-arm64
```

This computes each artifact's sha256/size, emits a **canonical** manifest JSON,
and embeds an Ed25519 signature over the canonical bytes (also written to
`manifest.json.sig`). **Canonicalization** (so any re-implementation matches
byte-for-byte): top-level key order `version, created_at, artifacts` (the
`signature` field is excluded from the signed bytes); artifacts sorted by
`(platform, arch, url)`; each artifact's keys ordered `platform, arch, sha256,
size, url`; compact JSON, HTML-escaping disabled, UTF-8, no trailing newline.
The agent re-derives these bytes from the parsed manifest, so central storing
the manifest as JSONB (re-serializing it) is harmless.

### Upload → canary → promote

Upload is two-phase (admin scope; see `docs/ops/agents.md §8`):

```bash
# 1. register the signed manifest (lands as stage=canary)
curl -H "Authorization: Bearer <admin-key>" -H 'Content-Type: application/json' \
     --data @manifest.json https://filearr.example.com/api/v1/agent-releases
# 2. stream each artifact (sha256 verified against the manifest)
curl -H "Authorization: Bearer <admin-key>" --upload-file dist/filearr-agent-linux-amd64 \
     https://filearr.example.com/api/v1/agent-releases/1.4.0/artifacts/filearr-agent-linux-amd64
# ... (windows + darwin) ...
# 3. after canary agents confirm healthy, promote to the whole fleet
curl -X POST -H "Authorization: Bearer <admin-key>" \
     https://filearr.example.com/api/v1/agent-releases/1.4.0/promote
```

Only agents whose `rollout_group` is the canary group (`FILEARR_AGENT_CANARY_GROUP`,
default `canary`) see a `canary` release; **promote** flips it to `general` for
everyone. Confirm canary health via `GET /api/v1/agent-releases` (the per-agent
`agent_version` rollup — "which version has each agent confirmed").

### A/B swap + crash-loop rollback (research §5.2/§5.3)

Each `run` daemon polls `GET /agents/{id}/update-manifest` on a long interval
(`FILEARR_AGENT_UPDATE_POLL_INTERVAL`, default 6h), verifies the signature,
downloads + sha256-verifies the matching artifact, then swaps:

- **Windows**: rename the running `.exe` aside to `.old.exe`, move the new
  binary into place, re-exec (Tailscale/rclone pattern).
- **Linux/macOS**: `rename(2)` swap — the running inode stays valid — then
  re-exec. macOS note: we ship a **bare binary**, not a signed `.app` bundle, so
  the Sparkle whole-bundle rule (Apple TN3126) does not apply; if we ever ship a
  notarized bundle the swap must become a whole-bundle swap.

Before swapping, the updater writes a boot-counter (`update-state.json`:
`{new_version, previous_binary_path, attempts, max_attempts:3}`). On each boot
of the new binary it increments the counter and runs a 60s health window (index
opens; central contact succeeds or fails cleanly with no panic); on pass it
clears the state, deletes the `.old` binary, and reports the running version
(the confirmed-version signal). If the binary crashes through **3 launch
attempts** without a healthy window, the next boot **restores the previous
binary and re-execs it** (systemd-boot "Automatic Boot Assessment" pattern).
Pair this with the OS service manager's own restart-on-failure policy so a
crashed agent gets relaunched to trigger the rollback.

`filearr-agent update [--check]` runs one check+apply now (or, with `--check`,
just prints the available version).

## Relationship to the central scaffold

The central side of the replication contract is scaffolded in Python at
[`backend/filearr/agentsync.py`](../backend/filearr/agentsync.py) — the wire
models (`AgentEvent`/`ReplicationBatch`), the `seq_no` continuation guard
(`check_batch`), batch collapse (`plan_upserts`), and the reconciliation
manifest digest (`manifest_digest`) are pure and tested there today. This Go
agent produces those events and consumes those verdicts.
