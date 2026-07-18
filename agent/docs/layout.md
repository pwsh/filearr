# filearr-agent — intended module tree

**Design of record only — no Go source exists yet** (see `agent/README.md` for
why). This file describes the module tree the Phase-5 tasks will build, so an
implementer starts from an agreed structure rather than inventing one. Package
boundaries follow the concrete component designs in
`docs/research/phase-5-distributed-agents.md` §7 and the agent state machine
(§7.4: `OFFLINE → CONNECTED → DRAINING → STEADY → (FULL RECONCILE) → STEADY`).

```
agent/
  go.mod                      # added by P5-T2 (first Go task)
  cmd/
    filearr-agent/            # main package: flag/config parse, daemon wiring,
                              #   supervises the goroutines below, signal handling
  internal/
    enroll/                   # enrollment + certificate lifecycle
    scan/                     # local filesystem walk/diff/tombstone (logic port)
    index/                    # local SQLite + FTS5 store
    outbox/                   # transactional outbox + replication client
    config/                   # policy poll (ETag) + opportunistic SSE
    update/                   # signed-manifest self-update + rollback
    localapi/                 # local CLI/UI query surface over the local index
    history/                  # local-only query frecency (P7-T6), a SEPARATE db
```

## Packages

### `cmd/filearr-agent`
Entry point and process supervisor. Parses local config (which libraries/
`scan_paths` to watch, central URL, cert paths), constructs the `index`,
`outbox`, `scan`, `config`, and `update` components, and runs them as
long-lived goroutines coordinated by the §7.4 state machine. Owns graceful
shutdown so an in-flight batch is either fully ACKed or left durably unsent in
the outbox (never half-applied). Key types: `Agent` (top-level supervisor),
`Config` (parsed local settings).

### `internal/enroll`
Implements the enrollment sequence (§7.1, **R3**): present the one-time token to
the central `/agents/register` endpoint **first**, receive the server-assigned
`agent_id`, generate the local keypair, build a CSR embedding `agent_id` in the
CN/SAN, obtain the signed short-lived cert from step-ca, and persist cert+key
locally. Runs a background renewal daemon (renew at a fixed fraction of lifetime
elapsed, plus a "CA rotated" push trigger). Talks to step-ca's **HTTP/ACME API
directly** — shelling out to the `step` CLI is rejected for packaging reasons
(**R6**); feasibility is confirmed by the **P5-T2a** spike. Key types:
`Enroller`, `CertStore`, `Renewer`.

### `internal/scan`
The filesystem walk/diff/tombstone pipeline, **ported as logic** from
`backend/filearr/tasks/scan.py` (brief §2.6) — mtime+size first-pass diff,
`quick_hash` (xxh3 first/last 64 KiB) then `content_hash` tiering, move detection
via `(quick_hash, size)` candidate matching with `content_hash` disambiguation,
tombstone-not-delete, sidecar classification, batched-commit-then-emit ordering
(CLAUDE.md invariant 5). Gitignore/preset matching uses the Go library chosen by
**P5-T3a**. `fsnotify` watch mode reuses the T5 debounce-into-full-rescan
decision. Emits change records to `index` + `outbox` in one transaction. Key
types: `Walker`, `DiffResult`, `HashTier`, `MoveDetector`.

### `internal/index`
The local SQLite store and its FTS5 virtual table (§3.3). Mirrors a **narrow**
subset of central `items` (id, rel_path, filename, extension, size, mtime,
quick_hash, content_hash, narrow metadata JSON, `status` mirroring `ItemStatus`,
plus local-only `synced_at`/`local_seq_no`). FTS5 external-content table kept in
sync via triggers. Runs `PRAGMA integrity_check` on startup; on failure the file
is deleted and rebuilt from a fresh walk (disposable-index philosophy,
invariant 1) and the rebuild is flagged upstream for observability. Key types:
`Store`, `Item`, `FTSIndex`, `IntegrityGuard`.

### `internal/outbox`
The transactional outbox (§4.1) and replication client (§4.3). Writes the local
item change and the outbox row **in the same SQLite transaction**; a drain
goroutine reads `WHERE sent_at IS NULL ORDER BY seq_no`, batches on size-OR-age
(≈500 rows / 5 s / 2 MB), POSTs to
`/api/agents/{id}/replication-batch`, and marks `sent_at` only on an ACK naming
the exact seq range. Handles the `409 {expected_seq_no}` gap response by
rewinding the drain (the server-side verdict is
`backend/filearr/agentsync.check_batch`). Exponential backoff reset-on-success;
block-don't-drop when offline. Key types: `Outbox`, `Batch`, `Replicator`,
`Backoff`. Also drives the **full-reconciliation sweep** (§4.4): builds the
manifest whose digest matches `agentsync.manifest_digest` and sends it for the
server-side anti-join on a slow interval / after long offline periods.

### `internal/config`
Policy sync (§6). Poll `GET /api/agents/{id}/policy` with `ETag`/`If-None-Match`
as the reliable background path; opportunistically hold an SSE stream open only
while a local UI/CLI is active for near-instant apply. Applies the received
policy (enabled libraries, `scan_paths`, preset bundles, local-access
enable/disable flag) and reports `policy_version_applied` back. mTLS is the only
integrity layer (**R4**). Key types: `PolicyClient`, `Policy`, `ETagCache`.

### `internal/update`
Self-update (§5). Fetches a **minisign-style signed manifest** (Tauri pattern —
full TUF explicitly rejected as disproportionate), verifies its signature
against a pinned Ed25519 public key, downloads and A/B-swaps the binary per-OS,
and runs a crash-loop boot-counting rollback state machine (revert to the
previous binary within N failed launches). Honors staged rollout via
`agents.rollout_group` (**R5** — a text column now; documented migration path to
phase-6 machine groups, never two parallel authorities). Key types: `Updater`,
`Manifest`, `Verifier`, `RollbackGuard`.

### `internal/localapi`
The local query surface (roadmap §2 / phase-7 doc): a `filearr query ...` CLI and
optional local web UI reading the local FTS5 index **directly**, so search works
with the agent fully disconnected. Enable/disable and auth-required are
**centrally controlled** via the `config` package's policy flag — a
"disable local access" policy must be honored within one poll interval. Detailed
CLI/UI shape is owned by the phase-7 (local query access) doc, not here. Key
types: `QueryServer`, `LocalQuery`, `AccessPolicy`.

### `internal/history`
The P7-T6 local query frecency store: a zoxide-style frequency+recency ranking of
the DSL queries a same-machine user has run, surfaced as suggestions
(`filearr query --history`, socket `GET /v1/history`). It lives in a **separate
SQLite database file** (`history.db`), NOT the index/outbox database (`index.db`),
which is the architectural isolation that keeps search terms local: the outbox /
replication subsystem is only ever handed the index Store's handle, so it is
**incapable** of touching a history row — not merely policy-gated (research §6).
The store contains no networking code. Recording is best-effort and never fails a
query; the socket surface can read history, the web UI is given only the write
side. Score = accumulated rank × zoxide recency-bucket weight; maintenance
(halving decay past a rank-sum ceiling + floor/retention prune) rides the record
write path, so there is no daemon timer. Key types: `Store`, `Entry`.

> **Wiping local state also wipes history.** `history.db` lives in the agent data
> dir alongside `index.db`; deleting the data dir (or that file) erases all local
> search history. Search history is never sent to central, so central holds no
> copy to restore — this is intentional (research §6).
